"""The single implementation of the irreversible financial write.

The nightly run and supervised runs both call :func:`execute_one_authorized_work`, so the path
that was verified under supervision is the path that runs unattended. The order is fixed:
fresh read-only validation -> resolve the exact menu target -> commit a write claim ->
click exactly once -> verify a persisted success signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from .portal import PortalError, MenuError
from .models import ClaimOutcome, Decision, EftRow, InvoiceDetail, PortalInvoice, Verdict, WorkState
from .provenance import Provenance
from .reconcile import (
    canonical_invoice_key,
    check_export_count,
    classify,
)

log = logging.getLogger(__name__)

__all__ = [
    "AlreadyPaid",
    "WriteAnomaly",
    "WriteBlocked",
    "WriteReadout",
    "execute_one_authorized_work",
    "validate_authorized_work",
    "validate_sales_and_detail",
    "SmokeReadout",
    "SmokeValidationError",
    "execute_smoke_batch",
    "execute_smoke_click",
    "validate_smoke_target",
]


class WriteBlocked(Exception):
    """The row cannot be written right now. Nothing irreversible happened, and no substitute is chosen."""


class AlreadyPaid(WriteBlocked):
    """The portal already shows the invoice as paid; the row becomes NO_ACTION."""


class WriteAnomaly(WriteBlocked):
    """Stored values disagree with the live portal; the row is escalated to MANUAL_REVIEW."""


@dataclass(frozen=True, slots=True)
class WriteReadout:
    """Fresh, unpersisted read of one target invoice."""
    href: str
    portal_row: "PortalInvoice | None"
    detail: InvoiceDetail
    decision: "Decision | None"
    pay_target_count: int
    target_visible: bool
    target_enabled: bool

    def summary(self) -> str:
        sales = ("" if self.portal_row is None else
                 f" balance={self.portal_row.balance} payer={self.portal_row.payer}"
                 f" status={self.portal_row.status}")
        verdict = ("" if self.decision is None else
                   f" verdict={self.decision.verdict.value}")
        return (
            f"href={self.href}{sales}"
            f" detail_total={self.detail.total} payment={self.detail.payment_status}"
            f" submission={self.detail.submission_status}{verdict}"
            f" pay_target_count={self.pay_target_count}"
        )


_PANE_INVOICE_JS = r"""() => {
  const pane = document.querySelector('#invoice-detail-pane');
  if (!pane) return null;
  const h1 = pane.querySelector('h1');
  return h1 ? (h1.innerText || '').replace(/\s+/g, ' ').trim() : null;
}"""


def _assert_pane_still_shows(portal, invoice_no: str) -> None:
    """Immediately before the claim, re-check that the detail pane still shows the target invoice."""
    try:
        seen = portal.page.evaluate(_PANE_INVOICE_JS)
    except Exception as exc:  # noqa: BLE001
        raise WriteBlocked(
            f"could not confirm the detail pane before clicking: {exc}"
        ) from exc
    want = f"Invoice {invoice_no}"
    if not seen or canonical_invoice_key(seen) != canonical_invoice_key(want):
        raise WriteBlocked(
            f"detail pane shows {seen!r} at click time, expected {want!r}"
        )


def _eft_row(work) -> EftRow:
    """Rebuild the reconciliation input from a stored work row."""
    return EftRow(
        work.invoice_no, work.row_date, "", "",
        work.eft_gross, work.eft_prev_paid, work.eft_outstanding, work.eft_net,
    )


def validate_sales_and_detail(portal, cfg, work, prov: Provenance) -> WriteReadout:
    """Read-only supervised pre-check for one row: fresh sales export plus detail validation."""
    from .main import _export_and_parse

    row = _eft_row(work)
    buf = max(int(getattr(cfg, "date_buffer_days", 3) or 0), 1)
    lo = work.row_date - timedelta(days=1 + buf)
    hi = work.row_date + timedelta(days=buf)

    portal_rows, screen_before = _export_and_parse(
        portal, cfg, lo, hi, all_invoice_states=False
    )
    try:
        screen_after = portal.visible_invoice_count()
    except Exception:  # noqa: BLE001
        screen_after = screen_before
    ok, warns, reason = check_export_count(
        len(portal_rows),
        len({canonical_invoice_key(j.invoice_no) for j in portal_rows}),
        screen_before, screen_after,
    )
    if warns:
        log.warning("G-7 degraded on the smoke window: %s",
                    ",".join(w.value for w in warns))
    if not ok:
        raise WriteBlocked(f"G-7 export integrity failed: {reason}")

    key = canonical_invoice_key(work.invoice_no)
    matches = [j for j in portal_rows if canonical_invoice_key(j.invoice_no) == key]
    if len(matches) != 1:
        raise WriteBlocked(
            f"{work.invoice_no} appears {len(matches)} time(s) in the Sales report"
            f" window {lo}..{hi}; expected exactly 1"
        )
    portal_row = matches[0]

    decision = classify(row, portal_row)
    if decision.verdict is not Verdict.APPROVE_OK:
        raise WriteBlocked(
            f"fresh Sales reconciliation is {decision.verdict.value}: {decision.reason}"
        )

    variants = {work.invoice_no, work.invoice_no.upper(), work.invoice_no.lower()}
    hrefs = portal.collect_hrefs(variants)
    want = canonical_invoice_key(work.invoice_no)
    href = hrefs.get(work.invoice_no) or next(
        (v for k, v in sorted(hrefs.items()) if canonical_invoice_key(k) == want), None
    )
    if not href:
        raise WriteBlocked(
            f"no detail href for {work.invoice_no} in window {lo}..{hi}"
        )

    detail = portal.open_invoice(href, expected_invoice_no=work.invoice_no)

    if canonical_invoice_key(detail.invoice_no) != canonical_invoice_key(work.invoice_no):
        raise WriteBlocked(
            f"detail identifier {detail.invoice_no!r} != {work.invoice_no!r}"
        )
    if detail.total != work.eft_net:
        raise WriteBlocked(
            f"detail total {detail.total} != EFT net {work.eft_net}"
        )
    if (detail.payment_status or "").strip().lower() == "paid":
        raise AlreadyPaid(
            f"{work.invoice_no} is already Paid in the portal"
            f" (submission={detail.submission_status!r})"
        )
    if work.portal_invoice_id and detail.portal_invoice_id != work.portal_invoice_id:  # noqa: E501
        raise WriteBlocked(
            f"portal invoice id {detail.portal_invoice_id!r} != stored"
            f" {work.portal_invoice_id!r}"
        )

    try:
        target = portal.resolve_record_payment(detail)
    except MenuError as exc:
        raise WriteBlocked(f"menu resolution failed: {exc}") from exc

    loc = target.locator
    visible = bool(loc.is_visible()) if hasattr(loc, "is_visible") else True
    enabled = bool(loc.is_enabled()) if hasattr(loc, "is_enabled") else True
    if not (visible and enabled):
        raise WriteBlocked(
            f"Record Payment target is not actionable (visible={visible} enabled={enabled})"
        )

    return WriteReadout(
        href=href, portal_row=portal_row, detail=detail, decision=decision,
        pay_target_count=1, target_visible=visible, target_enabled=enabled,
    )


def validate_authorized_work(portal, cfg, work, prov: Provenance) -> WriteReadout:
    """Read-only detail validation just before a write: identifier, total, unpaid status, stored id, exact target."""
    href = work.portal_href
    if not href:
        raise WriteBlocked(f"{work.invoice_no} has no portal href")

    detail = portal.open_invoice(href, expected_invoice_no=work.invoice_no)
    _assert_detail_matches(work, detail)

    try:
        target = portal.resolve_record_payment(detail)
    except MenuError as exc:
        raise WriteBlocked(f"menu resolution failed: {exc}") from exc

    loc = target.locator
    visible = bool(loc.is_visible()) if hasattr(loc, "is_visible") else True
    enabled = bool(loc.is_enabled()) if hasattr(loc, "is_enabled") else True
    if not (visible and enabled):
        raise WriteBlocked(
            f"Record Payment target is not actionable"
            f" (visible={visible} enabled={enabled})")

    return WriteReadout(
        href=href, portal_row=None, detail=detail, decision=None,
        pay_target_count=1, target_visible=visible, target_enabled=enabled,
    )


def _assert_detail_matches(work, detail) -> None:
    """G-18: compare the stored row against the live detail pane."""
    if canonical_invoice_key(detail.invoice_no) != canonical_invoice_key(work.invoice_no):
        raise WriteAnomaly(
            f"DETAIL_ID_MISMATCH: {detail.invoice_no!r} != {work.invoice_no!r}")
    if detail.total != work.eft_net:
        raise WriteAnomaly(
            f"DETAIL_TOTAL_MISMATCH: {detail.total} != EFT net {work.eft_net}")
    if (detail.payment_status or "").strip().lower() == "paid":
        raise AlreadyPaid(
            f"{work.invoice_no} is already Paid in the portal"
            f" (submission={detail.submission_status!r})")
    if work.portal_invoice_id and detail.portal_invoice_id != work.portal_invoice_id:
        raise WriteAnomaly(
            f"PORTAL_ID_MISMATCH: {detail.portal_invoice_id!r} != stored"
            f" {work.portal_invoice_id!r}")


def execute_one_authorized_work(db, portal, cfg, work, prov: Provenance) -> dict:
    """Cross the irreversible boundary for exactly one authorized row.

    Never loops over rows and never substitutes another invoice. A click whose outcome cannot be
    positively confirmed is recorded as UNKNOWN_OUTCOME and handed to a person; it is never retried.
    """
    if cfg.dry_run:
        raise WriteBlocked("dry_run is enabled; refusing to arm")
    if db.get_claim(work.invoice_no) is not None:
        raise WriteBlocked("a write_claim already exists for this invoice")
    if work.state is not WorkState.DETAIL_VALIDATED:
        raise WriteBlocked(f"state {work.state.value} is not write-eligible")
    if work.verdict is not Verdict.APPROVE_OK:
        raise WriteBlocked("verdict is not APPROVE_OK")
    if work.eft_net is None or work.eft_net <= 0:
        raise WriteBlocked("EFT net is missing or not positive")
    if (cfg.max_total_amount_per_run is not None
            and work.eft_net > cfg.max_total_amount_per_run):
        raise WriteBlocked(
            f"amount {work.eft_net} exceeds cap {cfg.max_total_amount_per_run}"
        )

    try:
        readout = validate_authorized_work(portal, cfg, work, prov)
    except WriteAnomaly as exc:
        log.error("write anomaly on %s: %s", work.invoice_no, exc)
        db.update_work(
            work.id, state=WorkState.MANUAL_REVIEW.value,
            verdict=Verdict.MANUAL_REVIEW.value, error_code=str(exc),
        )
        return {"clicked": False, "outcome": "MANUAL_REVIEW", "reason": str(exc)}
    except AlreadyPaid as exc:
        log.info("already paid, no action: %s", exc)
        db.update_work(
            work.id, state=WorkState.NO_ACTION.value,
            verdict=Verdict.ALREADY_PAID.value,
            error_code="ALREADY_PAID_AT_WRITE_BOUNDARY",
        )
        return {"clicked": False, "outcome": "ALREADY_PAID", "reason": str(exc)}
    target = portal.resolve_record_payment(readout.detail)

    if canonical_invoice_key(target.invoice_no) != canonical_invoice_key(work.invoice_no):
        raise WriteBlocked(
            f"resolved target is {target.invoice_no!r}, expected {work.invoice_no!r}"
        )
    _assert_pane_still_shows(portal, work.invoice_no)

    # Irreversible boundary (G-11). The claim is durable before the click is sent, so a
    # crash can leave an extra claim but never an unrecorded click.
    db.insert_claim(work, prov)
    try:
        signal = portal.click_record_payment(target)
    except Exception as exc:  # noqa: BLE001
        db.set_claim_outcome(work.invoice_no, ClaimOutcome.UNKNOWN_OUTCOME)
        db.update_work(
            work.id, state=WorkState.MANUAL_REVIEW.value,
            verdict=Verdict.MANUAL_REVIEW.value, attribution="unverified",
            error_code=f"CLICK_OUTCOME_UNKNOWN: {type(exc).__name__}",
        )
        return {"clicked": True, "outcome": "UNKNOWN_OUTCOME",
                "error": f"{type(exc).__name__}: {exc}"}

    db.update_work(work.id, state=WorkState.PENDING_WRITE.value)

    if signal and signal in cfg.post_click_success_signals:
        db.set_claim_outcome(work.invoice_no, ClaimOutcome.CONFIRMED)
        db.update_work(work.id, state=WorkState.CONFIRMED.value)
        return {"clicked": True, "outcome": "CONFIRMED", "signal": signal}

    db.set_claim_outcome(work.invoice_no, ClaimOutcome.UNKNOWN_OUTCOME)
    db.update_work(
        work.id, state=WorkState.MANUAL_REVIEW.value,
        verdict=Verdict.MANUAL_REVIEW.value, attribution="unverified",
        error_code="NO_POSITIVE_SUCCESS_SIGNAL",
    )
    return {"clicked": True, "outcome": "UNKNOWN_OUTCOME",
            "signal": signal or "", "error": "no positive success signal"}


_AMBIGUOUS = "UNKNOWN_OUTCOME"


def execute_smoke_batch(db, portal, cfg, targets: "list[tuple]") -> list[dict]:
    """Supervised allow-list batch. Halts everything after the first ambiguous financial outcome."""
    results: list[dict] = []
    spent = Decimal("0.00")
    clicks = 0

    for work, prov in targets:
        if (cfg.max_invoices_per_run is not None
                and clicks >= cfg.max_invoices_per_run):
            results.append({"work_id": work.id, "invoice_no": work.invoice_no,
                            "outcome": "SKIPPED", "reason": "run invoice cap reached"})
            continue
        if (cfg.max_total_amount_per_run is not None
                and spent + (work.eft_net or Decimal("0.00"))
                > cfg.max_total_amount_per_run):
            results.append({"work_id": work.id, "invoice_no": work.invoice_no,
                            "outcome": "SKIPPED",
                            "reason": f"would exceed batch cap "
                                      f"({spent} + {work.eft_net} > "
                                      f"{cfg.max_total_amount_per_run})"})
            continue

        try:
            outcome = execute_one_authorized_work(db, portal, cfg, work, prov)
        except WriteBlocked as exc:
            log.warning("smoke target work_id=%s rejected before the boundary: %s",
                        work.id, exc)
            results.append({"work_id": work.id, "invoice_no": work.invoice_no,
                            "outcome": "ABORTED_BEFORE_CLICK", "reason": str(exc)})
            continue
        except BaseException as exc:  # noqa: BLE001
            log.error("smoke target work_id=%s failed ambiguously: %s", work.id, exc)
            results.append({"work_id": work.id, "invoice_no": work.invoice_no,
                            "outcome": "FAILED", "reason": f"{type(exc).__name__}: {exc}"})
            results.extend(_not_attempted(targets, after=work.id))
            break

        outcome = {"work_id": work.id, "invoice_no": work.invoice_no, **outcome}
        results.append(outcome)
        if outcome.get("clicked"):
            clicks += 1
            spent += work.eft_net

        if outcome.get("outcome") == _AMBIGUOUS:
            log.error("smoke batch halted after an ambiguous outcome on work_id=%s",
                      work.id)
            results.extend(_not_attempted(targets, after=work.id))
            break

    return results


def _not_attempted(targets: "list[tuple]", *, after: int) -> list[dict]:
    """Mark every target after ``after`` as not attempted."""
    seen = False
    out: list[dict] = []
    for work, _prov in targets:
        if work.id == after:
            seen = True
            continue
        if seen:
            out.append({"work_id": work.id, "invoice_no": work.invoice_no,
                        "outcome": "NOT_ATTEMPTED",
                        "reason": "batch halted by an earlier ambiguous outcome"})
    return out


SmokeValidationError = WriteBlocked
SmokeReadout = WriteReadout
validate_smoke_target = validate_sales_and_detail
execute_smoke_click = execute_one_authorized_work
