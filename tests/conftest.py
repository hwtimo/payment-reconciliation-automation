"""Shared fixtures and builders. Every value in the test suite is synthetic."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from remittance_reconciler.database import Database
from remittance_reconciler.models import EftRow, PortalInvoice


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "eft"


def load_fixture(name: str) -> str:
    filename = name if name.endswith(".html") else f"{name}.html"
    path = FIXTURE_DIR / filename
    if not path.is_file():
        pytest.fail(f"EFT fixture not found: {path}")
    return path.read_text(encoding="utf-8")


VENDOR_NO = "8100001"
PAYMENT_DOCUMENT_NO = "004000000101"
EFT_DATE = date(2037, 5, 15)
DEPOSIT_AMOUNT = Decimal("665.60")
ROW_DATE = date(2037, 5, 8)
PORTAL_INVOICE_DATE = date(2037, 5, 7)

_EFT_ROW_DEFAULTS: dict[str, object] = {
    "invoice_no": "300248-B01",
    "row_date": ROW_DATE,
    "reference_no": "90-05501088",
    "document_no": "055000220066",
    "gross": Decimal("118.40"),
    "prev_paid": Decimal("0.00"),
    "outstanding": Decimal("0.00"),
    "net": Decimal("118.40"),
}

_PORTAL_DEFAULTS: dict[str, object] = {
    "invoice_no": "300248-B01",
    "balance": Decimal("118.40"),
    "payer": "ACME Claims",
    "status": "Unpaid",
    "total": Decimal("118.40"),
    "collected": Decimal("0.00"),
    "location": "Sample Clinic - Location A",
}


def make_eft_row(**over) -> EftRow:
    unknown = set(over) - set(_EFT_ROW_DEFAULTS)
    if unknown:
        raise TypeError(f"make_eft_row() got unexpected field(s): {sorted(unknown)}")
    return EftRow(**{**_EFT_ROW_DEFAULTS, **over})


def make_portal(**over) -> PortalInvoice:
    unknown = set(over) - set(_PORTAL_DEFAULTS)
    if unknown:
        raise TypeError(f"make_portal() got unexpected field(s): {sorted(unknown)}")
    return PortalInvoice(**{**_PORTAL_DEFAULTS, **over})


@pytest.fixture
def tmp_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "eft.db")
    db.migrate()
    return db


def seed_authorized_statement(
    db: Database,
    rows: "Sequence[EftRow]",
    *,
    vendor_no: str = "8100001",
    payment_document_no: str = "004000000101",
    eft_date: date = date(2037, 7, 16),
    fingerprint: str = "fp",
    message_id: str = "msg-authorized-0001",
    internal_date: datetime | None = None,
    intake_trusted: bool = True,
    quarantine_reason: str | None = None,
    deposit: Decimal | None = None,
) -> int:
    from remittance_reconciler.models import EftStatement

    total = deposit if deposit is not None else sum(
        (r.net for r in rows), Decimal("0.00")
    )
    st = EftStatement(
        vendor_no, payment_document_no, eft_date, total, tuple(rows), fingerprint
    )
    sid = db.create_statement(st)
    db.insert_work_rows(sid, st.rows)
    db.insert_email(
        message_id,
        internal_date or datetime(2037, 7, 17, 14, 5, tzinfo=timezone.utc),
        statement_id=sid,
        quarantine_reason=quarantine_reason,
        from_addr="billing@clinic.example.com",
        dkim_pass=True,
        dmarc_pass=True,
        intake_trusted=intake_trusted,
    )
    return sid


def provenance_for(db: Database, invoice_no: str, sid: int):
    from remittance_reconciler.provenance import verify_provenance

    work = next(w for w in db.work_rows(sid) if w.invoice_no == invoice_no)
    prov, why = verify_provenance(db, work)
    assert why is None, f"fixture is not provenanced: {why}"
    return work, prov
