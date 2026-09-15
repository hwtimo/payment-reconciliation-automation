"""Pipeline orchestration: queueing, content conflicts, dry run, write boundary and report delivery."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    DEPOSIT_AMOUNT,
    EFT_DATE,
    PAYMENT_DOCUMENT_NO,
    VENDOR_NO,
    load_fixture,
    seed_authorized_statement,
)

from remittance_reconciler import main
from remittance_reconciler.config import Config
from remittance_reconciler.database import RUN_STATUS_SUCCESS, Database
from remittance_reconciler.gmail import GmailMessage
from remittance_reconciler.portal import PortalError, MenuError, PayTarget
from remittance_reconciler.models import (
    ClaimOutcome,
    EftRow,
    EftStatement,
    InvoiceDetail,
    StatementRow,
    StatementState,
    Verdict,
    WorkRow,
    WorkState,
)


PACIFIC = timezone(timedelta(hours=-7))

SUCCESS_SIGNAL = "Invoice paid and approved"

STATEMENT_ROWS: tuple[EftRow, ...] = tuple(
    EftRow(
        invoice_no=invoice_no,
        row_date=EFT_DATE - timedelta(days=7),
        reference_no=reference_no,
        document_no=PAYMENT_DOCUMENT_NO,
        gross=Decimal("118.40"),
        prev_paid=Decimal("0.00"),
        outstanding=outstanding,
        net=net,
    )
    for invoice_no, reference_no, outstanding, net in (
        ("300248-B01", "AB123456", Decimal("0.00"), Decimal("118.40")),
        ("300248-B02", "AB123456", Decimal("0.00"), Decimal("118.40")),
        ("290001-B01", "AB123457", Decimal("0.00"), Decimal("118.40")),
        ("290002-B01", "AB123458", Decimal("0.00"), Decimal("118.40")),
        ("290003-B01", "AB123459", Decimal("54.40"), Decimal("64.00")),
        ("290004-B01", "AB123460", Decimal("54.40"), Decimal("64.00")),
        ("290005-B01", "AB123461", Decimal("54.40"), Decimal("64.00")),
    )
)

INVOICE_NO = STATEMENT_ROWS[0].invoice_no
OTHER_INVOICE_NO = STATEMENT_ROWS[2].invoice_no

PORTAL_INVOICE_IDS: dict[str, str] = {
    "300248-B01": "4400101",
    "300248-B02": "4400102",
    "290001-B01": "4400103",
    "290002-B01": "4400104",
    "290003-B01": "4400105",
    "290004-B01": "4400106",
    "290005-B01": "4400107",
}


def href_for(invoice_no: str) -> str:
    return f"#/invoices/{PORTAL_INVOICE_IDS[invoice_no]}"


class CallLog:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str]] = []

    def record(self, name: str, detail: str = "") -> None:
        self.entries.append((name, detail))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.entries]

    def count(self, name: str) -> int:
        return self.names.count(name)

    def first_index(self, name: str) -> int:
        names = self.names
        assert name in names, f"{name!r} missing from the call log. Actual log: {names}"
        return names.index(name)

    def details(self, name: str) -> list[str]:
        return [detail for entry_name, detail in self.entries if entry_name == name]


class _PaneStub:
    def __init__(self, owner):
        self._o = owner

    def evaluate(self, _js, arg=None):
        inv = getattr(self._o, "current_invoice", None)
        return f"Invoice {inv}" if inv else None

    class _Kb:
        def press(self, _k): pass
    keyboard = _Kb()

class FakePortalSession:
    def __init__(self, log: CallLog) -> None:
        self.log = log

        self.hrefs: dict[str, str] = {}
        self.details_by_href: dict[str, InvoiceDetail] = {}
        self.csv_path: Path | None = None
        self.open_invoice_error: Exception | None = None
        self.resolve_error: Exception | None = None
        self.click_error: Exception | None = None
        self.click_signal: str = SUCCESS_SIGNAL

        self.clicked: list[str] = []
        self.current_invoice: str | None = None
        self.page = _PaneStub(self)


    @property
    def click_count(self) -> int:
        return len(self.clicked)

    def calls(self, method: str) -> int:
        return self.log.count(f"portal.{method}")

    @property
    def browser_calls(self) -> int:
        return sum(
            self.calls(name)
            for name in (
                "export_outstanding_csv",
                "collect_hrefs",
                "open_invoice",
                "resolve_record_payment",
                "click_record_payment",
            )
        )


    def login(self) -> None:
        self.log.record("portal.login")

    def export_outstanding_csv(
        self, start: Any, end: Any, *, all_invoice_states: bool = False
    ) -> Path:
        self.log.record(
            "portal.export_outstanding_csv", f"{start}..{end} all={all_invoice_states}"
        )
        if self.csv_path is None:
            raise PortalError("fake: export CSV is not ready")
        return self.csv_path

    def collect_hrefs(self, targets: set[str], start: Any, end: Any) -> dict[str, str]:
        self.log.record("portal.collect_hrefs", ",".join(sorted(targets)))
        return {inv: href for inv, href in self.hrefs.items() if inv in targets}

    def open_invoice(self, href: str, expected_invoice_no: str | None = None,
                     **_k) -> InvoiceDetail:
        self.log.record("portal.open_invoice", href)
        if self.open_invoice_error is not None:
            raise self.open_invoice_error
        try:
            d = self.details_by_href[href]
            self.current_invoice = d.invoice_no
            return d
        except KeyError:
            raise PortalError(f"fake: unknown href {href!r}") from None

    def resolve_record_payment(self, detail: InvoiceDetail) -> PayTarget:
        self.log.record("portal.resolve_record_payment", detail.invoice_no)
        if self.resolve_error is not None:
            raise self.resolve_error
        return PayTarget(detail.invoice_no, locator=object(), menu=object())

    def click_record_payment(self, target: PayTarget) -> str:
        self.log.record("portal.click_record_payment", target.invoice_no)
        self.clicked.append(target.invoice_no)
        if self.click_error is not None:
            raise self.click_error
        return self.click_signal


class FakeGmailClient:
    def __init__(self, log: CallLog) -> None:
        self.log = log
        self.messages: list[GmailMessage] = []
        self.send_error: Exception | None = None
        self.sent: list[tuple[str, str, str]] = []
        self.after_args: list[datetime] = []

    def fetch_eft_messages(self, after: datetime):
        self.after_args.append(after)
        self.log.record("gmail.fetch_eft_messages", after.isoformat())
        return iter(tuple(self.messages))

    def send(self, to: str, subject: str, html_body: str) -> None:
        self.log.record("gmail.send", subject)
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((to, subject, html_body))


class RecordingDatabase:
    _RECORDED = frozenset(
        {
            "insert_claim",
            "set_claim_outcome",
            "set_statement_state",
            "bump_attempt",
            "insert_email",
            "create_statement",
            "insert_work_rows",
            "enqueue_report",
            "mark_report_sent",
            "bump_report_attempt",
        }
    )

    def __init__(self, inner: Database, log: CallLog) -> None:
        self.inner = inner
        self.log = log

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(self.inner, name)
        if name not in self._RECORDED or not callable(attr):
            return attr

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            detail = str(args[0]) if args else str(sorted(kwargs))
            self.log.record(f"db.{name}", detail)
            return attr(*args, **kwargs)

        return wrapper


def make_statement(
    rows: tuple[EftRow, ...] = STATEMENT_ROWS,
    *,
    vendor_no: str = VENDOR_NO,
    payment_document_no: str = PAYMENT_DOCUMENT_NO,
    eft_date=EFT_DATE,
) -> EftStatement:
    deposit_amount = sum((row.net for row in rows), Decimal("0.00"))
    return EftStatement(
        vendor_no=vendor_no,
        payment_document_no=payment_document_no,
        eft_date=eft_date,
        deposit_amount=deposit_amount,
        rows=rows,
        content_fingerprint=f"fp-seeded-{payment_document_no}-{len(rows)}",
    )


def make_message(
    html: str,
    *,
    message_id: str = "msg-0001",
    internal_date: datetime | None = None,
    subject: str = "EFT Remittance Advice",
    from_addr: str = "remittance@example.com",
    dkim_pass: bool = True,
) -> GmailMessage:
    if internal_date is None:
        internal_date = datetime(
            EFT_DATE.year, EFT_DATE.month, EFT_DATE.day, 6, 0, tzinfo=PACIFIC
        ) + timedelta(days=1)
    return GmailMessage(
        message_id=message_id,
        internal_date=internal_date,
        from_addr=from_addr,
        subject=subject,
        html=html,
        dkim_pass=dkim_pass,
    )


def _claim_args(db, sid: int, invoice_no: str):
    from remittance_reconciler.provenance import verify_provenance

    work = next(w for w in db.work_rows(sid) if w.invoice_no == invoice_no)
    prov, why = verify_provenance(db, work)
    assert why is None, f"fixture is not provenanced: {why}"
    return work, prov


def seed_reconciled(
    db: RecordingDatabase,
    rows: tuple[EftRow, ...] = STATEMENT_ROWS,
) -> StatementRow:
    statement = make_statement(rows)
    sid = db.create_statement(statement)
    db.insert_work_rows(sid, rows)
    db.insert_email(
        f"msg-{sid:04d}", datetime(2037, 7, 17, 14, 5, tzinfo=timezone.utc),
        statement_id=sid,
        from_addr="billing@clinic.example.com",
        dkim_pass=True, dmarc_pass=True, intake_trusted=True,
    )
    by_invoice = {row.invoice_no: row for row in rows}
    for work in db.work_rows(sid):
        row = by_invoice[work.invoice_no]
        db.update_work(
            work.id,
            portal_href=href_for(work.invoice_no),
            portal_invoice_id=PORTAL_INVOICE_IDS[work.invoice_no],
            portal_balance=row.net,
            portal_payer="ACME Claims",
            portal_status="Unpaid",
            portal_total=row.gross,
            portal_collected=Decimal("0.00"),
            portal_location="Sample Clinic - Location A",
            verdict=Verdict.APPROVE_OK,
            state=WorkState.DETAIL_VALIDATED,
        )
    seeded = db.find_statement(statement.vendor_no, statement.payment_document_no)
    assert seeded is not None, "could not re-read the statement right after seeding"
    return seeded


def arm_happy_path(portal: FakePortalSession, rows: tuple[EftRow, ...] = STATEMENT_ROWS) -> None:
    for row in rows:
        href = href_for(row.invoice_no)
        portal.hrefs[row.invoice_no] = href
        portal.details_by_href[href] = InvoiceDetail(
            invoice_no=row.invoice_no,
            total=row.net,
            payment_status="Unpaid",
            submission_status="Submitted",
            portal_invoice_id=PORTAL_INVOICE_IDS[row.invoice_no],
        )


def work_by_invoice(db: RecordingDatabase, sid: int) -> dict[str, WorkRow]:
    return {work.invoice_no: work for work in db.work_rows(sid)}


def _query_one(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
    con = sqlite3.connect(db_path)
    try:
        return con.execute(sql, params).fetchone()
    finally:
        con.close()


def claim_count(db_path: Path) -> int:
    row = _query_one(db_path, "SELECT COUNT(*) FROM write_claims")
    assert row is not None
    return int(row[0])


def quarantine_reason(db_path: Path, message_id: str) -> str | None:
    row = _query_one(
        db_path,
        "SELECT quarantine_reason FROM emails WHERE message_id = ?",
        (message_id,),
    )
    assert row is not None, f"no emails row for {message_id!r} (permanent-skip record missing)"
    return row[0]


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def log() -> CallLog:
    return CallLog()


@pytest.fixture
def portal(log: CallLog) -> FakePortalSession:
    return FakePortalSession(log)


@pytest.fixture
def gmail(log: CallLog) -> FakeGmailClient:
    return FakeGmailClient(log)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "eft.db"


@pytest.fixture
def db(db_path: Path, log: CallLog) -> RecordingDatabase:
    inner = Database(db_path)
    inner.migrate()
    return RecordingDatabase(inner, log)


@pytest.fixture
def cfg() -> Config:
    return Config(
        automation_start_at=datetime(2037, 4, 17, 0, 0, tzinfo=PACIFIC),
        dry_run=False,
        max_invoices_per_run=50,
        max_total_amount_per_run=Decimal("10000.00"),
        max_statement_age_days=30,
        max_statement_attempts=5,
        heartbeat_days=1,
        inter_invoice_delay_seconds=0,
        date_buffer_days=0,
        known_vendors=(VENDOR_NO,),
        report_recipients=("ops@example.invalid",),
        post_click_success_signals=(SUCCESS_SIGNAL,),
        debug_capture=False,
    )


def test_live_run_clicks_every_approved_row(
    db: RecordingDatabase, db_path: Path, portal: FakePortalSession, cfg: Config
) -> None:
    statement = seed_reconciled(db)
    arm_happy_path(portal)

    main.execute_statement(db, portal, cfg, statement)

    assert portal.click_count == len(STATEMENT_ROWS)
    assert sorted(portal.clicked) == sorted(row.invoice_no for row in STATEMENT_ROWS)
    assert claim_count(db_path) == len(STATEMENT_ROWS)
    states = {work.invoice_no: work.state for work in db.work_rows(statement.id)}
    assert all(state == WorkState.CONFIRMED for state in states.values()), states


MENU_FAILURE_CASES = [
    "dropdown trigger not found",
    "dropdown failed to open",
    "menu container not confirmed",
    'no exact "Record Payment" target',
    "more than one exact target",
]


@pytest.mark.parametrize("case", MENU_FAILURE_CASES)
def test_menu_error_creates_no_claim_and_never_clicks(
    db: RecordingDatabase,
    db_path: Path,
    portal: FakePortalSession,
    cfg: Config,
    case: str,
) -> None:
    statement = seed_reconciled(db, STATEMENT_ROWS[:1])
    arm_happy_path(portal, STATEMENT_ROWS[:1])
    portal.resolve_error = MenuError(case)

    main.execute_statement(db, portal, cfg, statement)

    assert db.get_claim(INVOICE_NO) is None, f"{case}: a claim was created (permanent block)"
    assert claim_count(db_path) == 0, f"{case}: a row remained in write_claims"
    assert portal.click_count == 0, f"{case}: a click happened"
    assert portal.calls("resolve_record_payment") == 1
    assert db.log.count("db.insert_claim") == 0


def test_menu_error_is_retryable_on_the_next_run(
    db: RecordingDatabase, db_path: Path, portal: FakePortalSession, cfg: Config
) -> None:
    statement = seed_reconciled(db, STATEMENT_ROWS[:1])
    arm_happy_path(portal, STATEMENT_ROWS[:1])

    portal.resolve_error = MenuError("dropdown failed to open")
    main.execute_statement(db, portal, cfg, statement)
    assert portal.click_count == 0
    assert claim_count(db_path) == 0

    portal.resolve_error = None
    retried = db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert retried is not None
    main.execute_statement(db, portal, cfg, retried)

    assert portal.click_count == 1, "a menu failure permanently blocked the invoice"
    assert db.get_claim(INVOICE_NO) is not None


def test_claim_is_committed_between_resolve_and_click(
    db: RecordingDatabase, portal: FakePortalSession, cfg: Config
) -> None:
    statement = seed_reconciled(db, STATEMENT_ROWS[:1])
    arm_happy_path(portal, STATEMENT_ROWS[:1])

    main.execute_statement(db, portal, cfg, statement)

    resolved_at = db.log.first_index("portal.resolve_record_payment")
    claimed_at = db.log.first_index("db.insert_claim")
    clicked_at = db.log.first_index("portal.click_record_payment")
    assert resolved_at < claimed_at < clicked_at, db.log.names
    assert portal.click_count == 1


def test_open_invoice_failure_creates_no_claim_and_retries_next_run(
    db: RecordingDatabase, db_path: Path, portal: FakePortalSession, cfg: Config
) -> None:
    statement = seed_reconciled(db, STATEMENT_ROWS[:1])
    arm_happy_path(portal, STATEMENT_ROWS[:1])
    portal.open_invoice_error = PortalError("fake: navigation to the detail pane failed")

    main.execute_statement(db, portal, cfg, statement)

    assert claim_count(db_path) == 0
    assert portal.click_count == 0
    assert portal.calls("resolve_record_payment") == 0, "opened the menu despite a navigation failure"

    portal.open_invoice_error = None
    retried = db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert retried is not None
    main.execute_statement(db, portal, cfg, retried)

    assert portal.click_count == 1
    assert db.get_claim(INVOICE_NO) is not None


@pytest.mark.parametrize(
    ("label", "click_signal", "click_error"),
    [
        ("success signal is an empty string", "", None),
        ("success signal is a different string", "Something else happened", None),
        ("click ended with an exception", SUCCESS_SIGNAL, PortalError("fake: no response after click")),
    ],
)
def test_unknown_outcome_never_reclicks(
    db: RecordingDatabase,
    portal: FakePortalSession,
    cfg: Config,
    label: str,
    click_signal: str,
    click_error: Exception | None,
) -> None:
    statement = seed_reconciled(db, STATEMENT_ROWS[:1])
    arm_happy_path(portal, STATEMENT_ROWS[:1])
    portal.click_signal = click_signal
    portal.click_error = click_error


    with pytest.raises(main.RunHalted):
        main.execute_statement(db, portal, cfg, statement)

    assert portal.click_count == 1, f"{label}: click count is not 1"
    work = work_by_invoice(db, statement.id)[INVOICE_NO]
    assert work.state == WorkState.MANUAL_REVIEW, label
    claim = db.get_claim(INVOICE_NO)
    assert claim is not None, f"{label}: clicked but no claim exists"
    assert claim.final_outcome != ClaimOutcome.CONFIRMED, label

    retried = db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert retried is not None
    main.execute_statement(db, portal, cfg, retried)
    assert portal.click_count == 1, f"{label}: the next run re-clicked (double payment)"


@pytest.mark.parametrize(
    ("label", "outcome"),
    [
        ("outcome CONFIRMED - skipped silently", ClaimOutcome.CONFIRMED),
        ("outcome UNKNOWN_OUTCOME - MANUAL_REVIEW", ClaimOutcome.UNKNOWN_OUTCOME),
        ("outcome NULL - MANUAL_REVIEW", None),
    ],
)
def test_existing_claim_blocks_click(
    db: RecordingDatabase,
    portal: FakePortalSession,
    cfg: Config,
    label: str,
    outcome: ClaimOutcome | None,
) -> None:
    statement = seed_reconciled(db, STATEMENT_ROWS[:1])
    arm_happy_path(portal, STATEMENT_ROWS[:1])
    db.insert_claim(*_claim_args(db, statement.id, INVOICE_NO))
    if outcome is not None:
        db.set_claim_outcome(INVOICE_NO, outcome)

    main.execute_statement(db, portal, cfg, statement)

    assert portal.click_count == 0, label


def test_wrong_href_detail_blocks_click(
    db: RecordingDatabase, db_path: Path, portal: FakePortalSession, cfg: Config
) -> None:
    rows = STATEMENT_ROWS[:1]
    statement = seed_reconciled(db, rows)
    arm_happy_path(portal, rows)
    portal.details_by_href[href_for(INVOICE_NO)] = InvoiceDetail(
        invoice_no=OTHER_INVOICE_NO,
        total=rows[0].net,
        payment_status="Unpaid",
        submission_status="Submitted",
        portal_invoice_id=PORTAL_INVOICE_IDS[OTHER_INVOICE_NO],
    )

    main.execute_statement(db, portal, cfg, statement)

    assert portal.click_count == 0
    assert claim_count(db_path) == 0
    assert db.get_claim(INVOICE_NO) is None
    assert db.get_claim(OTHER_INVOICE_NO) is None, "a claim was created for the wrong invoice"
    assert portal.calls("resolve_record_payment") == 0, "opened the menu despite a validation failure"
    work = work_by_invoice(db, statement.id)[INVOICE_NO]
    assert work.state == WorkState.MANUAL_REVIEW


def test_detail_total_mismatch_blocks_click(
    db: RecordingDatabase, db_path: Path, portal: FakePortalSession, cfg: Config
) -> None:
    rows = STATEMENT_ROWS[:1]
    statement = seed_reconciled(db, rows)
    arm_happy_path(portal, rows)
    portal.details_by_href[href_for(INVOICE_NO)] = InvoiceDetail(
        invoice_no=INVOICE_NO,
        total=Decimal("68.40"),
        payment_status="Partially Paid",
        submission_status="Submitted",
        portal_invoice_id=PORTAL_INVOICE_IDS[INVOICE_NO],
    )

    main.execute_statement(db, portal, cfg, statement)

    assert portal.click_count == 0
    assert claim_count(db_path) == 0
    work = work_by_invoice(db, statement.id)[INVOICE_NO]
    assert work.state == WorkState.MANUAL_REVIEW


def test_already_paid_detail_blocks_click(
    db: RecordingDatabase, db_path: Path, portal: FakePortalSession, cfg: Config
) -> None:
    rows = STATEMENT_ROWS[:1]
    statement = seed_reconciled(db, rows)
    arm_happy_path(portal, rows)
    portal.details_by_href[href_for(INVOICE_NO)] = InvoiceDetail(
        invoice_no=INVOICE_NO,
        total=rows[0].net,
        payment_status="Paid",
        submission_status="Submitted",
        portal_invoice_id=PORTAL_INVOICE_IDS[INVOICE_NO],
    )

    main.execute_statement(db, portal, cfg, statement)

    assert portal.click_count == 0
    assert claim_count(db_path) == 0
    work = work_by_invoice(db, statement.id)[INVOICE_NO]
    assert work.state == WorkState.NO_ACTION


def test_same_key_same_fingerprint_is_plain_duplicate(
    db: RecordingDatabase,
    db_path: Path,
    gmail: FakeGmailClient,
    cfg: Config,
) -> None:
    html = load_fixture("normal_7rows")
    gmail.messages = [make_message(html, message_id="msg-1")]
    main.ingest_messages(db, gmail, cfg)
    before = db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert before is not None
    row_count_before = len(db.work_rows(before.id))

    gmail.messages = [make_message(html, message_id="msg-2")]
    main.ingest_messages(db, gmail, cfg)

    after = db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert after is not None
    assert after.id == before.id, "two statements were created for the same financial key"
    assert after.content_fingerprint == before.content_fingerprint
    assert after.state == StatementState.PENDING
    assert len(db.work_rows(after.id)) == row_count_before, "work rows were inserted twice"
    assert quarantine_reason(db_path, "msg-2") is None, "quarantined a legitimate duplicate"


def test_conflict_with_completed_statement_leaves_history_untouched(
    db: RecordingDatabase,
    db_path: Path,
    gmail: FakeGmailClient,
    cfg: Config,
) -> None:
    gmail.messages = [make_message(load_fixture("normal_7rows"), message_id="msg-1")]
    main.ingest_messages(db, gmail, cfg)
    existing = db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert existing is not None
    db.set_statement_state(existing.id, StatementState.COMPLETED)
    before = db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert before is not None

    gmail.messages = [make_message(load_fixture("statement_v2_8rows"), message_id="msg-2")]
    main.ingest_messages(db, gmail, cfg)

    after = db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert after is not None
    assert after.state == StatementState.COMPLETED, "completed history was modified"
    assert after.content_fingerprint == before.content_fingerprint
    assert after.deposit_amount == before.deposit_amount == DEPOSIT_AMOUNT
    assert len(db.work_rows(after.id)) == len(STATEMENT_ROWS), "rows from the new version were mixed in"
    assert quarantine_reason(db_path, "msg-2") == "STATEMENT_CONTENT_CONFLICT"


def test_conflict_with_pending_statement_stops_automation(
    db: RecordingDatabase,
    db_path: Path,
    gmail: FakeGmailClient,
    portal: FakePortalSession,
    cfg: Config,
) -> None:
    gmail.messages = [make_message(load_fixture("normal_7rows"), message_id="msg-1")]
    main.ingest_messages(db, gmail, cfg)
    existing = db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert existing is not None
    assert existing.state == StatementState.PENDING

    gmail.messages = [make_message(load_fixture("statement_v2_8rows"), message_id="msg-2")]
    main.ingest_messages(db, gmail, cfg)

    stopped = db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert stopped is not None
    assert stopped.state == StatementState.TERMINAL_EXCEPTION, "the old version is still queued"
    assert stopped.last_error == "STATEMENT_CONTENT_CONFLICT"
    assert quarantine_reason(db_path, "msg-2") == "STATEMENT_CONTENT_CONFLICT"

    arm_happy_path(portal)
    queue = main.build_queue(db)
    assert [row.id for row in queue] == [], "the stopped statement is still queued"
    assert portal.click_count == 0
    assert claim_count(db_path) == 0


def test_messages_before_automation_start_at_are_not_processed(
    db: RecordingDatabase, gmail: FakeGmailClient, cfg: Config
) -> None:
    strict_cfg = replace(
        cfg, automation_start_at=datetime(2037, 5, 17, 0, 0, tzinfo=PACIFIC)
    )
    gmail.messages = [
        make_message(
            load_fixture("normal_7rows"),
            message_id="msg-old",
            internal_date=datetime(2037, 5, 16, 6, 0, tzinfo=PACIFIC),
        )
    ]

    queued = main.ingest_messages(db, gmail, strict_cfg)

    assert queued == 0
    assert db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO) is None
    assert main.build_queue(db) == []


def test_eft_date_skew_beyond_max_age_is_quarantined(
    db: RecordingDatabase, db_path: Path, gmail: FakeGmailClient, cfg: Config
) -> None:
    gmail.messages = [
        make_message(
            load_fixture("normal_7rows"),
            message_id="msg-migrated",
            internal_date=datetime(2037, 7, 1, 6, 0, tzinfo=PACIFIC),
        )
    ]

    queued = main.ingest_messages(db, gmail, cfg)

    assert queued == 0
    assert db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO) is None
    assert quarantine_reason(db_path, "msg-migrated") is not None
    assert main.build_queue(db) == []


def test_g19_uses_eft_date_not_row_date(
    db: RecordingDatabase, db_path: Path, gmail: FakeGmailClient, cfg: Config
) -> None:
    gmail.messages = [
        make_message(
            load_fixture("normal_7rows"),
            message_id="msg-normal",
            internal_date=datetime(2037, 6, 10, 6, 0, tzinfo=PACIFIC),
        )
    ]

    queued = main.ingest_messages(db, gmail, cfg)

    assert queued == 1, "a legitimate email was quarantined: G-19 was computed from row_date"
    statement = db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert statement is not None
    assert statement.state == StatementState.PENDING
    assert statement.eft_date == EFT_DATE
    assert quarantine_reason(db_path, "msg-normal") is None


def test_queue_is_driven_by_db_not_by_gmail(
    db: RecordingDatabase, gmail: FakeGmailClient, cfg: Config
) -> None:
    statement = seed_reconciled(db)

    gmail.messages = []
    assert main.ingest_messages(db, gmail, cfg) == 0

    queue = main.build_queue(db)
    assert [row.id for row in queue] == [statement.id]


def test_run_cap_keeps_statement_pending_without_bumping_attempts(
    db: RecordingDatabase, portal: FakePortalSession, cfg: Config
) -> None:
    throttled = replace(cfg, max_invoices_per_run=2)
    statement = seed_reconciled(db)
    arm_happy_path(portal)

    main.execute_statement(db, portal, throttled, statement)
    state = main.finalize_statement(db, statement)

    assert portal.click_count == 2, "the run cap was not enforced"
    assert state == StatementState.PENDING
    after = db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert after is not None
    assert after.state == StatementState.PENDING
    assert after.attempt_count == 0, "counted a throttle as a failure"
    assert db.log.count("db.bump_attempt") == 0, "called bump_attempt on a throttle"


def test_all_terminal_rows_complete_the_statement(
    db: RecordingDatabase, portal: FakePortalSession, cfg: Config
) -> None:
    statement = seed_reconciled(db)
    arm_happy_path(portal)
    portal.details_by_href[href_for(INVOICE_NO)] = InvoiceDetail(
        invoice_no=INVOICE_NO,
        total=STATEMENT_ROWS[0].net,
        payment_status="Paid",
        submission_status="Submitted",
        portal_invoice_id=PORTAL_INVOICE_IDS[INVOICE_NO],
    )

    main.execute_statement(db, portal, cfg, statement)
    state = main.finalize_statement(db, statement)

    assert portal.click_count == len(STATEMENT_ROWS) - 1
    assert state == StatementState.COMPLETED
    after = db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert after is not None
    assert after.state == StatementState.COMPLETED
    assert after.attempt_count == 0
    assert main.build_queue(db) == [], "a completed statement is reprocessed on the next run"


def test_report_send_failure_does_not_reopen_financial_work(
    db: RecordingDatabase,
    portal: FakePortalSession,
    gmail: FakeGmailClient,
    cfg: Config,
) -> None:
    statement = seed_reconciled(db)
    arm_happy_path(portal)
    main.execute_statement(db, portal, cfg, statement)
    assert main.finalize_statement(db, statement) == StatementState.COMPLETED
    clicks_after_payment = portal.click_count

    run_id = db.start_run()
    db.enqueue_report(run_id, "SUMMARY", "<p>Remittance Reconciler — Completed</p>")

    gmail.send_error = RuntimeError("fake: SMTP failure")
    browser_calls_before = portal.browser_calls
    main.flush_report_outbox(db, gmail, cfg)

    assert gmail.sent == []
    pending = db.pending_reports()
    assert len(pending) == 1, "a report that failed to send left the queue"
    assert pending[0].state == "PENDING"
    completed = db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert completed is not None
    assert completed.state == StatementState.COMPLETED, "a mail failure rolled back financial processing"
    assert portal.browser_calls == browser_calls_before, "report resend entered the portal"

    gmail.send_error = None
    browser_calls_before = portal.browser_calls
    assert main.build_queue(db) == [], "a COMPLETED statement was re-queued"
    main.flush_report_outbox(db, gmail, cfg)

    assert len(gmail.sent) == 1
    assert db.pending_reports() == []
    assert portal.browser_calls == browser_calls_before, "the portal was re-entered"
    assert portal.click_count == clicks_after_payment, "a click happened during the resend run"


def test_dry_run_never_clicks_and_never_claims(
    db: RecordingDatabase, db_path: Path, portal: FakePortalSession, cfg: Config
) -> None:
    dry_cfg = replace(cfg, dry_run=True)
    statement = seed_reconciled(db)
    arm_happy_path(portal)

    main.execute_statement(db, portal, dry_cfg, statement)

    assert portal.click_count == 0
    assert claim_count(db_path) == 0
    assert db.log.count("db.insert_claim") == 0
    states = {work.invoice_no: work.state for work in db.work_rows(statement.id)}
    assert all(state != WorkState.CONFIRMED for state in states.values()), states


@pytest.mark.parametrize(
    "scenario",
    ["happy path", "already Paid", "bad href", "MenuError"],
)
def test_dry_run_clicks_zero_on_every_path(
    db: RecordingDatabase,
    db_path: Path,
    portal: FakePortalSession,
    cfg: Config,
    scenario: str,
) -> None:
    dry_cfg = replace(cfg, dry_run=True)
    rows = STATEMENT_ROWS[:1]
    statement = seed_reconciled(db, rows)
    arm_happy_path(portal, rows)

    if scenario == "already Paid":
        portal.details_by_href[href_for(INVOICE_NO)] = InvoiceDetail(
            invoice_no=INVOICE_NO,
            total=rows[0].net,
            payment_status="Paid",
            submission_status="Submitted",
            portal_invoice_id=PORTAL_INVOICE_IDS[INVOICE_NO],
        )
    elif scenario == "bad href":
        portal.details_by_href[href_for(INVOICE_NO)] = InvoiceDetail(
            invoice_no=OTHER_INVOICE_NO,
            total=rows[0].net,
            payment_status="Unpaid",
            submission_status="Submitted",
            portal_invoice_id=PORTAL_INVOICE_IDS[OTHER_INVOICE_NO],
        )
    elif scenario == "MenuError":
        portal.resolve_error = MenuError("dropdown trigger not found")

    main.execute_statement(db, portal, dry_cfg, statement)

    assert portal.click_count == 0, scenario
    assert claim_count(db_path) == 0, scenario


EXPORT_HEADER = (
    "Location,Service Date,Invoice Date,Patient ID,Patient,Service,Provider,"
    "Payer,Invoice #,Category,Notes,Status,Subtotal,Total,Collected,Balance,Tax"
)


class _MenuStub:
    @property
    def first(self) -> "_MenuStub":
        return self

    def count(self) -> int:
        return 1

    def get_attribute(self, _name: str) -> str:
        return "true"

    def wait_for(self, **_k: Any) -> None:
        pass

    def get_by_role(self, _role: str, name: str | None = None, exact: bool = False) -> "_MenuStub":
        return self


class PipelinePortal:
    """Serves both exports, the detail pane and the action menu so every stage runs unmodified."""

    def __init__(self, export_dir: Path,
                 invoices: dict[str, tuple[str, Decimal, Decimal, Decimal]]) -> None:
        self.export_dir = export_dir
        self.invoices = invoices
        self.all_invoice_states = False
        self.exports = 0
        self.clicked: list[str] = []
        self.current_invoice: str | None = None
        self.page = _PaneStub(self)

    def ensure_authenticated(self) -> str:
        return "reused"

    def _listed(self) -> dict[str, tuple[str, Decimal, Decimal, Decimal]]:
        return {inv: v for inv, v in self.invoices.items()
                if self.all_invoice_states or v[0] != "Paid"}

    def _open_sales_report(self, start: Any, end: Any, all_invoice_states: bool = False) -> None:
        self.all_invoice_states = all_invoice_states

    def visible_invoice_count(self) -> int:
        return len(self._listed())

    def export_outstanding_csv(self, start: Any, end: Any, *, all_invoice_states: bool = False,
                               scratch_dir: Path | None = None) -> Path:
        self.exports += 1
        path = self.export_dir / f"export-{self.exports}.csv"
        lines = [EXPORT_HEADER] + [
            f"Sample Clinic - Location A,,,,,,,ACME Claims,{inv},,,{status},"
            f"{total},{total},{collected},{balance},0.00"
            for inv, (status, total, collected, balance) in self._listed().items()
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def collect_hrefs(self, targets: set[str]) -> dict[str, str]:
        return {inv: f"#/invoices/{inv}" for inv in targets if inv in self.invoices}

    def open_invoice(self, href: str, expected_invoice_no: str | None = None,
                     **_k: Any) -> InvoiceDetail:
        invoice_no = href.rsplit("/", 1)[-1]
        status, total, _collected, _balance = self.invoices[invoice_no]
        self.current_invoice = invoice_no
        return InvoiceDetail(invoice_no=invoice_no, total=total, payment_status=status,
                             submission_status="Submitted",
                             portal_invoice_id=f"44{invoice_no[:6]}")

    def action_trigger(self) -> _MenuStub:
        return _MenuStub()

    def action_menu(self) -> _MenuStub:
        return _MenuStub()

    def resolve_record_payment(self, detail: InvoiceDetail) -> PayTarget:
        return PayTarget(detail.invoice_no, locator=object(), menu=object())

    def click_record_payment(self, target: PayTarget) -> str:
        self.clicked.append(target.invoice_no)
        return SUCCESS_SIGNAL


def test_nightly_run_keeps_earlier_outcomes_and_reports_only_real_exceptions(
    db: RecordingDatabase, tmp_path: Path, gmail: FakeGmailClient, cfg: Config
) -> None:
    paid_earlier, writable, short_paid = "300301-B01", "300302-B01", "300303-B01"
    malformed_paid = "300304-S01"
    row_date = EFT_DATE - timedelta(days=7)
    rows = [
        EftRow(invoice_no, row_date, reference_no, PAYMENT_DOCUMENT_NO,
               net, Decimal("0.00"), Decimal("0.00"), net,
               identifier_ok=invoice_no != malformed_paid)
        for invoice_no, reference_no, net in (
            (paid_earlier, "AB123470", Decimal("118.40")),
            (writable, "AB123471", Decimal("64.00")),
            (short_paid, "AB123472", Decimal("118.40")),
            (malformed_paid, "AB123473", Decimal("46.00")),
        )
    ]
    sid = seed_authorized_statement(db, rows)
    portal = PipelinePortal(tmp_path, {
        paid_earlier: ("Paid", Decimal("118.40"), Decimal("118.40"), Decimal("0.00")),
        writable: ("Unpaid", Decimal("64.00"), Decimal("0.00"), Decimal("64.00")),
        short_paid: ("Unpaid", Decimal("131.65"), Decimal("0.00"), Decimal("131.65")),
        malformed_paid: ("Paid", Decimal("46.00"), Decimal("46.00"), Decimal("0.00")),
    })

    stats = main.run_once(cfg, db, gmail, portal)

    assert stats.status == RUN_STATUS_SUCCESS, stats.error
    assert portal.clicked == [writable]
    after = work_by_invoice(db, sid)
    assert after[writable].state == WorkState.CONFIRMED
    assert (after[paid_earlier].state, after[paid_earlier].verdict,
            after[paid_earlier].error_code) == (
        WorkState.NO_ACTION, Verdict.ALREADY_PAID, "paid before automation reached it")
    assert (after[short_paid].state, after[short_paid].verdict,
            after[short_paid].error_code) == (
        WorkState.MANUAL_REVIEW, Verdict.AMOUNT_MISMATCH, "net=118.40 balance=131.65")
    assert after[malformed_paid].state == WorkState.MANUAL_REVIEW, (
        "a paid match for a malformed identifier was kept out of review")
    assert "does not match the required format" in (after[malformed_paid].error_code or "")
    assert db.get_statement(sid).state == StatementState.COMPLETED

    assert (stats.approved_count, stats.manual_review_count) == (1, 2)
    [(_to, subject, body)] = gmail.sent
    assert "2 manual review" in subject
    assert short_paid in body and "AMOUNT_MISMATCH" in body
    assert malformed_paid in body
    assert paid_earlier not in body, "an already-paid invoice was listed for manual review"
