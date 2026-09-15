"""Write authorization by provenance.

The portal never authorizes a payment; it only confirms one. A row may be written only if it
descends, unbroken, from a trusted forwarded email -> parsed statement -> immutable parsed row,
and the statement's immutable row amounts still sum to its deposit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .models import StatementState, WorkRow
from .parser import INVOICE_RE_C


@dataclass(frozen=True, slots=True)
class Provenance:
    """Proof of origin for a write. Stored on the write claim for audit."""
    statement_id: int
    vendor_no: str
    payment_document_no: str
    content_fingerprint: str
    source_message_id: str
    raw_eft_invoice_no: str
    eft_net: Decimal
    eft_date: date | None = None

    def audit_ref(self) -> str:
        """One-line audit reference naming the email and statement row that authorized a write."""
        return (
            f"msg={self.source_message_id} vendor={self.vendor_no}"
            f" docno={self.payment_document_no} fp={self.content_fingerprint[:16]}"
            f" row={self.raw_eft_invoice_no} net={self.eft_net}"
        )


def verify_provenance(db, work: WorkRow) -> tuple[Provenance | None, str | None]:
    """Return ``(Provenance, None)`` if ``work`` may be written, otherwise ``(None, reason)``. Fails closed."""
    sid = work.statement_id
    if not sid or sid <= 0:
        return None, "PROVENANCE: no statement_id"

    st = db.get_statement(sid)
    if st is None:
        return None, f"PROVENANCE: statement {sid} does not exist"

    if st.state is StatementState.TERMINAL_EXCEPTION:
        return None, f"PROVENANCE: statement {sid} is TERMINAL_EXCEPTION"

    email = db.source_email(sid)
    if email is None:
        return None, f"PROVENANCE: statement {sid} has no source email"
    if email["quarantine_reason"]:
        return None, (
            f"PROVENANCE: source email is quarantined ({email['quarantine_reason']})"
        )

    if not email["intake_trusted"]:
        return None, "PROVENANCE: source email did not pass the trusted-forward gate"

    if work.raw_eft_invoice_no is None:
        return None, "PROVENANCE: no raw EFT invoice number on this row"
    if work.raw_eft_invoice_no != work.invoice_no:
        return None, (
            f"PROVENANCE: invoice_no {work.invoice_no!r} does not match the parsed"
            f" EFT row {work.raw_eft_invoice_no!r}"
        )
    if work.eft_net_provenance is None:
        return None, "PROVENANCE: no provenance amount on this row"
    if work.eft_net is None or work.eft_net != work.eft_net_provenance:
        return None, (
            f"PROVENANCE: EFT net {work.eft_net} does not match the parsed"
            f" EFT row amount {work.eft_net_provenance}"
        )
    if work.eft_net <= 0:
        return None, "PROVENANCE: EFT net is not positive"

    if not INVOICE_RE_C.match(work.raw_eft_invoice_no):
        return None, (
            f"PROVENANCE: EFT row identifier {work.raw_eft_invoice_no!r} does not"
            " match the required format; it cannot be reconciled against the portal"
        )

    total, n_rows, n_missing = db.statement_provenance_sum(sid)
    if n_rows == 0:
        return None, f"PROVENANCE: statement {sid} has no persisted rows"
    if n_missing:
        return None, (
            f"PROVENANCE: statement {sid} has {n_missing} row(s) without provenance"
        )
    if total != st.deposit_amount:
        return None, (
            f"PROVENANCE: statement {sid} row-set is not intact"
            f" (sum {total} != deposit {st.deposit_amount})"
        )

    return (
        Provenance(
            statement_id=sid,
            vendor_no=st.vendor_no,
            payment_document_no=st.payment_document_no,
            content_fingerprint=st.content_fingerprint,
            source_message_id=email["message_id"],
            raw_eft_invoice_no=work.raw_eft_invoice_no,
            eft_net=work.eft_net,
            eft_date=st.eft_date,
        ),
        None,
    )
