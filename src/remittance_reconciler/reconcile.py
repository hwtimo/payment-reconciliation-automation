"""Pure reconciliation rules: classification, terminal states, date windows and export checks.

Every function is deterministic and side-effect free, so the decision table can be tested
exhaustively.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from decimal import Decimal

from .models import Decision, EftRow, PortalInvoice, Verdict, WarnCode, WorkState

__all__ = [
    "PAYER_PREFIX",
    "TERMINAL_STATES",
    "PORTAL_RECONCILED_STATES",
    "WRITE_CANDIDATE_STATES",
    "is_write_candidate",
    "is_portal_reconciled",
    "normalize_invoice_no",
    "canonical_invoice_key",
    "classify",
    "verdict_to_state",
    "is_terminal",
    "statement_is_complete",
    "sales_report_window",
    "statement_amount_guard",
    "check_export_count",
    "statement_age_days",
]

# Hard gate: the portal's payer field must start with this value (synthetic placeholder).
PAYER_PREFIX = "ACME"

TERMINAL_STATES: frozenset[WorkState] = frozenset(
    {WorkState.CONFIRMED, WorkState.NO_ACTION, WorkState.MANUAL_REVIEW}
)

PORTAL_RECONCILED_STATES: frozenset[WorkState] = frozenset(
    {WorkState.RECONCILED, WorkState.DETAIL_VALIDATED, WorkState.PENDING_WRITE,
     WorkState.CONFIRMED, WorkState.NO_ACTION, WorkState.MANUAL_REVIEW}
)

WRITE_CANDIDATE_STATES: frozenset[WorkState] = frozenset({WorkState.DETAIL_VALIDATED})


def is_write_candidate(state: WorkState) -> bool:
    """True only for rows that passed detail validation. Necessary, not sufficient, for a write."""
    return WorkState(state) in WRITE_CANDIDATE_STATES


def is_portal_reconciled(state: WorkState) -> bool:
    """True once a row has been reconciled against the portal. PARSED rows have not."""
    return WorkState(state) in PORTAL_RECONCILED_STATES


def normalize_invoice_no(raw: str) -> str:
    """Strip whitespace and one leading ``#``. Never uppercases, pads or removes the suffix."""
    return str(raw).strip().lstrip("#").strip()


def canonical_invoice_key(raw: str) -> str:
    """Case-folded comparison key. Suffix digits are untouched, so ``-B01`` and ``-B02`` stay distinct."""
    return normalize_invoice_no(raw).upper()


def classify(row: EftRow, portal: PortalInvoice | None) -> Decision:
    """Classify one remittance row against the matching portal sales row.

    ``APPROVE_OK`` requires every hard gate: identifier match, ``net == balance``, the expected payer
    and a positive net. Credits, missing rows, payer or amount mismatches and partially collected
    invoices are routed to a human instead of being guessed at.
    """
    if row.net <= 0 or row.gross < 0:
        return Decision(Verdict.CREDIT_MEMO, (), f"net={row.net} gross={row.gross}")

    if portal is None:
        return Decision(Verdict.NOT_FOUND, (), "no portal row for identifier")

    if canonical_invoice_key(row.invoice_no) != canonical_invoice_key(portal.invoice_no):
        return Decision(
            Verdict.MANUAL_REVIEW,
            (),
            f"identifier mismatch {row.invoice_no!r} vs {portal.invoice_no!r}",
        )

    if not portal.payer.strip().upper().startswith(PAYER_PREFIX):
        return Decision(Verdict.PAYER_MISMATCH, (), f"payer={portal.payer!r}")

    warnings: list[WarnCode] = []
    if row.outstanding != 0:
        warnings.append(WarnCode.OUTSTANDING_NONZERO)
    if row.gross != portal.total:
        warnings.append(WarnCode.GROSS_VS_TOTAL)
    if row.prev_paid != portal.collected:
        warnings.append(WarnCode.PREVPAID_VS_COLLECTED)

    if row.net != portal.balance:
        return Decision(
            Verdict.AMOUNT_MISMATCH,
            tuple(warnings),
            f"net={row.net} balance={portal.balance}",
        )

    if portal.collected > 0 and portal.balance > 0:
        return Decision(
            Verdict.MANUAL_REVIEW,
            tuple(warnings),
            f"partially paid: collected={portal.collected} balance={portal.balance}",
        )

    return Decision(Verdict.APPROVE_OK, tuple(warnings), "")


def verdict_to_state(verdict: Verdict) -> WorkState:
    """Map a verdict to a row state. Everything except APPROVE_OK and ALREADY_PAID becomes MANUAL_REVIEW."""
    if verdict is Verdict.APPROVE_OK:
        return WorkState.RECONCILED
    if verdict is Verdict.ALREADY_PAID:
        return WorkState.NO_ACTION
    return WorkState.MANUAL_REVIEW


def is_terminal(state: WorkState) -> bool:
    """CONFIRMED, NO_ACTION and MANUAL_REVIEW are terminal from the automation's point of view."""
    return WorkState(state) in TERMINAL_STATES


def statement_is_complete(states: Iterable[WorkState]) -> bool:
    """True when every row is terminal. A statement with no rows is never complete."""
    states = list(states)
    if not states:
        return False
    return all(is_terminal(s) for s in states)


def sales_report_window(rows: Sequence[EftRow], buffer_days: int = 0) -> tuple[date, date]:
    """Date window for the first, deliberately narrow sales-report export."""
    if not rows:
        raise ValueError("sales_report_window requires at least one row")
    if buffer_days < 0:
        raise ValueError("buffer_days must be >= 0")
    dates = [r.row_date for r in rows]
    lo = min(dates) - timedelta(days=1 + buffer_days)
    hi = max(dates) - timedelta(days=1) + timedelta(days=buffer_days)
    return lo, hi


def statement_amount_guard(approved: Sequence[Decimal], deposit_amount: Decimal) -> bool:
    """G-2a: amounts approved for one statement may never exceed its deposit."""
    return sum(approved, Decimal("0.00")) <= deposit_amount


def statement_age_days(eft_date: date, gmail_internal_date: date) -> int:
    """G-19: days between the statement date and receipt (received minus statement date)."""
    return (gmail_internal_date - eft_date).days


def check_export_count(
    parsed_rows: int,
    distinct_invoice_nos: int,
    count_before: int | None,
    count_after: int | None,
) -> tuple[bool, tuple[WarnCode, ...], str]:
    """G-7: check that an export is complete, tolerating live drift only when drift was observed."""
    if parsed_rows != distinct_invoice_nos:
        return (
            False,
            (),
            f"duplicate identifiers in export: rows={parsed_rows} "
            f"distinct={distinct_invoice_nos}",
        )

    if count_before is None or count_after is None:
        return True, (WarnCode.DATE_WINDOW_DRIFT,), "on-screen count unavailable"

    lo, hi = min(count_before, count_after), max(count_before, count_after)

    if lo == hi:
        if parsed_rows != lo:
            return False, (), f"export rows {parsed_rows} != settled screen count {lo}"
        return True, (), ""

    if lo <= parsed_rows <= hi:
        return (
            True,
            (WarnCode.DATE_WINDOW_DRIFT,),
            f"live drift during export: screen {count_before}->{count_after}, rows={parsed_rows}",
        )
    return False, (), f"export rows {parsed_rows} outside drift band [{lo},{hi}]"
