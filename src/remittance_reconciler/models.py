"""Shared value types: enums and frozen dataclasses.

This module imports nothing from the package, so every other module can depend on it
without import cycles. Money is always ``decimal.Decimal``. Invoice identifiers are always
the full string, including the ``-Cnn`` suffix.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

__all__ = [
    "Verdict",
    "WorkState",
    "StatementState",
    "WarnCode",
    "ClaimOutcome",
    "EftRow",
    "EftStatement",
    "PortalInvoice",
    "InvoiceDetail",
    "Decision",
    "StatementRow",
    "WorkRow",
    "ClaimRow",
    "OutboxRow",
    "RunStats",
]


class Verdict(StrEnum):
    """Why a remittance row was classified the way it was. A reason, not a state."""
    APPROVE_OK = "APPROVE_OK"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    PAYER_MISMATCH = "PAYER_MISMATCH"
    ALREADY_PAID = "ALREADY_PAID"
    NOT_FOUND = "NOT_FOUND"
    CREDIT_MEMO = "CREDIT_MEMO"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class WorkState(StrEnum):
    """Lifecycle of one remittance row. CONFIRMED, NO_ACTION and MANUAL_REVIEW are terminal."""
    PARSED = "PARSED"
    RECONCILED = "RECONCILED"
    DETAIL_VALIDATED = "DETAIL_VALIDATED"
    PENDING_WRITE = "PENDING_WRITE"
    CONFIRMED = "CONFIRMED"
    NO_ACTION = "NO_ACTION"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class StatementState(StrEnum):
    """Lifecycle of a statement. The daily work queue is every PENDING statement."""
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    TERMINAL_EXCEPTION = "TERMINAL_EXCEPTION"


class WarnCode(StrEnum):
    """Soft warnings, recorded for humans. Never used to authorize a write."""
    OUTSTANDING_NONZERO = "OUTSTANDING_NONZERO"
    GROSS_VS_TOTAL = "GROSS_VS_TOTAL"
    PREVPAID_VS_COLLECTED = "PREVPAID_VS_COLLECTED"
    LOCATION = "LOCATION"
    SUBMISSION = "SUBMISSION"
    ACCOUNTING_EQ = "ACCOUNTING_EQ"
    EXTRA_COLUMNS = "EXTRA_COLUMNS"
    DATE_WINDOW_DRIFT = "DATE_WINDOW_DRIFT"
    MALFORMED_ROW_IDENTIFIER = "MALFORMED_ROW_IDENTIFIER"


class ClaimOutcome(StrEnum):
    """Final outcome of a write claim (``None`` while unresolved)."""
    CONFIRMED = "CONFIRMED"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


@dataclass(frozen=True, slots=True)
class EftRow:
    """One row of a parsed remittance table."""
    invoice_no: str
    row_date: date
    reference_no: str
    document_no: str
    gross: Decimal
    prev_paid: Decimal
    outstanding: Decimal
    net: Decimal

    identifier_ok: bool = True


@dataclass(frozen=True, slots=True)
class EftStatement:
    """A fully parsed and validated remittance email."""
    vendor_no: str
    payment_document_no: str
    eft_date: date
    deposit_amount: Decimal
    rows: tuple[EftRow, ...]
    content_fingerprint: str
    warnings: tuple[WarnCode, ...] = ()


@dataclass(frozen=True, slots=True)
class PortalInvoice:
    """One row of the portal's sales-report export. Patient-identifying columns are never extracted."""
    invoice_no: str
    balance: Decimal
    payer: str
    status: str
    total: Decimal
    collected: Decimal
    location: str


@dataclass(frozen=True, slots=True)
class InvoiceDetail:
    """Values read from the portal's invoice detail pane immediately before a write."""
    invoice_no: str
    total: Decimal
    payment_status: str
    submission_status: str
    portal_invoice_id: str


@dataclass(frozen=True, slots=True)
class Decision:
    """Result of classifying one remittance row against the portal."""
    verdict: Verdict
    warnings: tuple[WarnCode, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class StatementRow:
    """A ``statements`` table row."""
    id: int
    vendor_no: str
    payment_document_no: str
    deposit_amount: Decimal
    eft_date: date | None
    content_fingerprint: str
    state: StatementState
    attempt_count: int = 0
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WorkRow:
    """An ``invoice_work`` table row, including its immutable provenance columns."""
    id: int
    statement_id: int
    invoice_no: str
    row_date: date | None = None

    eft_gross: Decimal | None = None
    eft_prev_paid: Decimal | None = None
    eft_outstanding: Decimal | None = None
    eft_net: Decimal | None = None

    portal_balance: Decimal | None = None
    portal_payer: str | None = None
    portal_status: str | None = None
    portal_total: Decimal | None = None
    portal_collected: Decimal | None = None
    portal_location: str | None = None

    portal_invoice_id: str | None = None
    portal_href: str | None = None

    raw_eft_invoice_no: str | None = None
    eft_net_provenance: Decimal | None = None

    warnings: tuple[WarnCode, ...] = ()
    verdict: Verdict | None = None
    state: WorkState = WorkState.PARSED
    attribution: str | None = None
    error_code: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    vendor_no: str = ""


@dataclass(frozen=True, slots=True)
class ClaimRow:
    """A ``write_claims`` row. Its existence means the invoice crossed the irreversible write boundary."""
    invoice_no: str
    statement_id: int | None
    portal_invoice_id: str | None
    claimed_amount: Decimal
    claimed_at: datetime
    final_outcome: ClaimOutcome | None = None

    source_message_id: str | None = None
    vendor_no: str | None = None
    payment_document_no: str | None = None
    content_fingerprint: str | None = None
    raw_eft_invoice_no: str | None = None


@dataclass(frozen=True, slots=True)
class OutboxRow:
    """A queued report email."""
    id: int
    run_id: int | None
    kind: str
    payload: str
    state: str
    attempt_count: int = 0
    created_at: datetime | None = None
    sent_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RunStats:
    """Counters for one run, stored in ``run_log`` and used in report bodies."""
    run_id: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    statements_processed: int = 0
    approved_count: int = 0
    approved_total: Decimal = Decimal("0.00")
    manual_review_count: int = 0
    status: str = ""
    error: str | None = None
