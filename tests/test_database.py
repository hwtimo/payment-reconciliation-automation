"""Persistence: schema and durability settings, DB-driven queue, immutable provenance, claim ledger."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import (
    DEPOSIT_AMOUNT,
    EFT_DATE,
    PAYMENT_DOCUMENT_NO,
    VENDOR_NO,
    make_eft_row,
)
from remittance_reconciler.database import Database
from remittance_reconciler.models import (
    ClaimOutcome,
    EftStatement,
    StatementState,
    Verdict,
    WarnCode,
    WorkState,
)


RUN_STATUS_SUCCESS = "SUCCESS"
RUN_STATUS_FAILED = "FAILED"

KIND_SUMMARY = "SUMMARY"
KIND_HEARTBEAT = "HEARTBEAT"
KIND_FAILURE = "FAILURE"

OUTBOX_PENDING = "PENDING"

FINGERPRINT_V1 = "a" * 64
FINGERPRINT_V2 = "b" * 64

INVOICE_B01 = "300248-B01"
INVOICE_B02 = "300248-B02"

GMAIL_INTERNAL_DATE = datetime(2037, 5, 15, 16, 20, 0, tzinfo=UTC)


_STATEMENT_DEFAULTS: dict[str, object] = {
    "vendor_no": VENDOR_NO,
    "payment_document_no": PAYMENT_DOCUMENT_NO,
    "eft_date": EFT_DATE,
    "deposit_amount": DEPOSIT_AMOUNT,
    "rows": (make_eft_row(),),
    "content_fingerprint": FINGERPRINT_V1,
    "warnings": (),
}


def make_statement(**over) -> EftStatement:
    unknown = set(over) - set(_STATEMENT_DEFAULTS)
    if unknown:
        raise TypeError(f"make_statement() got unexpected field(s): {sorted(unknown)}")
    return EftStatement(**{**_STATEMENT_DEFAULTS, **over})


def _claimable(db: Database, invoice_no: str = INVOICE_B01, *, sid: int | None = None,
               **statement_over):
    from remittance_reconciler.provenance import verify_provenance

    rows = (
        make_eft_row(invoice_no=INVOICE_B01, net=Decimal("118.40")),
        make_eft_row(invoice_no=INVOICE_B02, net=Decimal("64.00")),
    )
    if sid is None:
        st = make_statement(
            rows=rows,
            deposit_amount=sum((r.net for r in rows), Decimal("0.00")),
            **statement_over,
        )
        sid = db.create_statement(st)
        db.insert_work_rows(sid, st.rows)
        db.insert_email(
            f"msg-{sid:04d}", datetime(2037, 7, 17, 14, 5, tzinfo=timezone.utc),
            statement_id=sid, from_addr="billing@clinic.example.com",
            dkim_pass=True, dmarc_pass=True, intake_trusted=True,
        )
    portal_ids = {INVOICE_B01: "4400101", INVOICE_B02: "4400102"}
    for w in db.work_rows(sid):
        if w.portal_invoice_id is None and w.invoice_no in portal_ids:
            db.update_work(w.id, portal_invoice_id=portal_ids[w.invoice_no])
    work = next(w for w in db.work_rows(sid) if w.invoice_no == invoice_no)
    prov, why = verify_provenance(db, work)
    assert why is None, f"fixture is not provenanced: {why}"
    return work, prov


def _pragma(db: Database, name: str) -> object:
    conn = getattr(db, "conn", None)
    if conn is None:
        pytest.fail("Database must expose a live sqlite3.Connection as `conn`")
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def _only_work_id(db: Database, sid: int, invoice_no: str) -> int:
    matches = [w for w in db.work_rows(sid) if w.invoice_no == invoice_no]
    assert len(matches) == 1, f"{invoice_no} has {len(matches)} work rows (expected 1)"
    return matches[0].id


def test_migrate_sets_journal_mode_wal(tmp_db: Database) -> None:
    assert str(_pragma(tmp_db, "journal_mode")).lower() == "wal"


def test_migrate_sets_synchronous_full(tmp_db: Database) -> None:
    assert _pragma(tmp_db, "synchronous") == 2


def test_journal_mode_wal_persists_in_db_file(tmp_db: Database, tmp_path: Path) -> None:
    external = sqlite3.connect(tmp_path / "eft.db")
    try:
        mode = external.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        external.close()
    assert str(mode).lower() == "wal"


def test_migrate_is_idempotent(tmp_db: Database) -> None:
    tmp_db.migrate()
    tmp_db.migrate()
    assert tmp_db.pending_statements() == []


def test_get_claim_absent_returns_none(tmp_db: Database) -> None:
    assert tmp_db.get_claim(INVOICE_B01) is None


def test_insert_claim_then_get_claim_returns_row(tmp_db: Database) -> None:
    work, prov = _claimable(tmp_db)
    sid = work.statement_id
    tmp_db.insert_claim(work, prov)

    claim = tmp_db.get_claim(INVOICE_B01)
    assert claim is not None
    assert claim.invoice_no == INVOICE_B01
    assert claim.statement_id == sid
    assert claim.portal_invoice_id == "4400101"
    assert claim.final_outcome is None


def test_claimed_amount_roundtrips_as_decimal(tmp_db: Database) -> None:
    work, prov = _claimable(tmp_db)
    sid = work.statement_id
    tmp_db.insert_claim(work, prov)

    claim = tmp_db.get_claim(INVOICE_B01)
    assert claim is not None
    assert isinstance(claim.claimed_amount, Decimal)
    assert claim.claimed_amount == Decimal("118.40")
    assert str(claim.claimed_amount) == "118.40"


def test_claimed_at_is_timezone_aware(tmp_db: Database) -> None:
    work, prov = _claimable(tmp_db)
    sid = work.statement_id
    tmp_db.insert_claim(work, prov)

    claim = tmp_db.get_claim(INVOICE_B01)
    assert claim is not None
    assert isinstance(claim.claimed_at, datetime)
    assert claim.claimed_at.tzinfo is not None
    assert claim.claimed_at.utcoffset() is not None


def test_claim_survives_invoice_work_state_change_to_manual_review(
    tmp_db: Database,
) -> None:
    work, prov = _claimable(tmp_db)
    sid = work.statement_id
    work_id = _only_work_id(tmp_db, sid, INVOICE_B01)

    tmp_db.insert_claim(work, prov)
    assert tmp_db.get_claim(INVOICE_B01) is not None

    tmp_db.update_work(work_id, state=WorkState.MANUAL_REVIEW)

    claim = tmp_db.get_claim(INVOICE_B01)
    assert claim is not None, (
        "changing invoice_work.state neutralized the claim (double-payment regression)"
    )
    assert claim.invoice_no == INVOICE_B01


def test_claim_survives_every_work_state(tmp_db: Database) -> None:
    work, prov = _claimable(tmp_db)
    work_id = _only_work_id(tmp_db, work.statement_id, INVOICE_B01)
    tmp_db.insert_claim(work, prov)

    for state in WorkState:
        tmp_db.update_work(work_id, state=state)
        assert tmp_db.get_claim(INVOICE_B01) is not None, f"the claim disappeared at state={state}"


def test_claim_survives_statement_terminal_exception(tmp_db: Database) -> None:
    work, prov = _claimable(tmp_db)
    sid = work.statement_id
    tmp_db.insert_claim(work, prov)

    tmp_db.set_statement_state(
        sid, StatementState.TERMINAL_EXCEPTION, "STATEMENT_CONTENT_CONFLICT"
    )

    assert tmp_db.get_claim(INVOICE_B01) is not None


@pytest.mark.parametrize("outcome", [ClaimOutcome.CONFIRMED, ClaimOutcome.UNKNOWN_OUTCOME])
def test_set_claim_outcome_never_deletes_claim(
    tmp_db: Database, outcome: ClaimOutcome
) -> None:
    work, prov = _claimable(tmp_db)
    sid = work.statement_id
    tmp_db.insert_claim(work, prov)

    tmp_db.set_claim_outcome(INVOICE_B01, outcome)

    claim = tmp_db.get_claim(INVOICE_B01)
    assert claim is not None, f"recording final_outcome={outcome} erased the claim"
    assert claim.final_outcome == outcome
    assert isinstance(claim.final_outcome, ClaimOutcome)


def test_confirmed_claim_still_blocks_and_carries_amount(tmp_db: Database) -> None:
    work, prov = _claimable(tmp_db)
    sid = work.statement_id
    tmp_db.insert_claim(work, prov)
    tmp_db.set_claim_outcome(INVOICE_B01, ClaimOutcome.CONFIRMED)

    claim = tmp_db.get_claim(INVOICE_B01)
    assert claim is not None
    assert claim.final_outcome == ClaimOutcome.CONFIRMED
    assert claim.claimed_amount == Decimal("118.40")


def test_duplicate_claim_same_invoice_raises_integrity_error(tmp_db: Database) -> None:
    work, prov = _claimable(tmp_db)
    sid = work.statement_id
    tmp_db.insert_claim(work, prov)

    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.insert_claim(work, prov)


def test_duplicate_claim_from_other_statement_raises_integrity_error(
    tmp_db: Database,
) -> None:
    a = _claimable(tmp_db, INVOICE_B01)
    b = _claimable(tmp_db, INVOICE_B01,
                   payment_document_no="004000000204",
                   content_fingerprint=FINGERPRINT_V2)
    tmp_db.insert_claim(*a)

    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.insert_claim(*b)


def test_get_claim_matches_full_identifier_only(tmp_db: Database) -> None:
    work, prov = _claimable(tmp_db)
    sid = work.statement_id
    tmp_db.insert_claim(work, prov)

    assert tmp_db.get_claim(INVOICE_B02) is None
    assert tmp_db.get_claim("300248") is None
    assert tmp_db.get_claim(INVOICE_B01) is not None


def test_split_suffix_invoices_hold_independent_claims(tmp_db: Database) -> None:
    work, prov = _claimable(tmp_db)
    sid = work.statement_id
    tmp_db.insert_claim(work, prov)
    tmp_db.insert_claim(*_claimable(tmp_db, INVOICE_B02, sid=sid))

    b01 = tmp_db.get_claim(INVOICE_B01)
    b02 = tmp_db.get_claim(INVOICE_B02)
    assert b01 is not None and b02 is not None
    assert b01.portal_invoice_id == "4400101"
    assert b02.portal_invoice_id == "4400102"
    assert b01.claimed_amount == Decimal("118.40")
    assert b02.claimed_amount == Decimal("64.00")


def test_email_exists_false_for_unknown_message(tmp_db: Database) -> None:
    assert tmp_db.email_exists("msg-does-not-exist") is False


def test_insert_email_then_email_exists(tmp_db: Database) -> None:
    sid = tmp_db.create_statement(make_statement())
    tmp_db.insert_email("msg-1", GMAIL_INTERNAL_DATE, statement_id=sid)

    assert tmp_db.email_exists("msg-1") is True


def test_statement_resend_new_message_id_same_financial_key(tmp_db: Database) -> None:
    st = make_statement()
    sid = tmp_db.create_statement(st)
    tmp_db.insert_email("msg-original", GMAIL_INTERNAL_DATE, statement_id=sid)

    tmp_db.insert_email(
        "msg-resend", GMAIL_INTERNAL_DATE + timedelta(days=1), statement_id=sid
    )

    assert tmp_db.email_exists("msg-original") is True
    assert tmp_db.email_exists("msg-resend") is True

    found = tmp_db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert found is not None
    assert found.id == sid


def test_duplicate_message_id_raises_integrity_error(tmp_db: Database) -> None:
    tmp_db.insert_email("msg-1", GMAIL_INTERNAL_DATE)

    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.insert_email("msg-1", GMAIL_INTERNAL_DATE)


def test_quarantined_email_has_no_statement_and_is_permanently_skipped(
    tmp_db: Database,
) -> None:
    tmp_db.insert_email(
        "msg-unparseable",
        GMAIL_INTERNAL_DATE,
        statement_id=None,
        quarantine_reason="V1",
    )

    assert tmp_db.email_exists("msg-unparseable") is True
    assert tmp_db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO) is None
    assert tmp_db.pending_statements() == []


def test_quarantined_email_does_not_create_work(tmp_db: Database) -> None:
    tmp_db.insert_email(
        "msg-unknown-vendor", GMAIL_INTERNAL_DATE, quarantine_reason="V9"
    )
    assert tmp_db.pending_statements() == []


def test_find_statement_absent_returns_none(tmp_db: Database) -> None:
    assert tmp_db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO) is None


def test_find_statement_returns_existing_row(tmp_db: Database) -> None:
    sid = tmp_db.create_statement(make_statement())

    found = tmp_db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert found is not None
    assert found.id == sid
    assert found.vendor_no == VENDOR_NO
    assert found.payment_document_no == PAYMENT_DOCUMENT_NO
    assert found.content_fingerprint == FINGERPRINT_V1


def test_find_statement_row_types(tmp_db: Database) -> None:
    tmp_db.create_statement(make_statement())

    found = tmp_db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert found is not None
    assert isinstance(found.deposit_amount, Decimal)
    assert found.deposit_amount == Decimal("665.60")
    assert found.eft_date == EFT_DATE
    assert isinstance(found.state, StatementState)


def test_find_statement_is_keyed_on_both_columns(tmp_db: Database) -> None:
    tmp_db.create_statement(make_statement())

    assert tmp_db.find_statement(VENDOR_NO, "004000000204") is None
    assert tmp_db.find_statement("9999999", PAYMENT_DOCUMENT_NO) is None


def test_create_statement_starts_pending_with_zero_attempts(tmp_db: Database) -> None:
    sid = tmp_db.create_statement(make_statement())

    found = tmp_db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert found is not None
    assert found.id == sid
    assert found.state == StatementState.PENDING
    assert found.attempt_count == 0
    assert found.last_error is None


def test_create_statement_duplicate_financial_key_raises_integrity_error(
    tmp_db: Database,
) -> None:
    tmp_db.create_statement(make_statement())

    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.create_statement(make_statement(content_fingerprint=FINGERPRINT_V2))


def test_pending_statement_is_returned_with_no_emails_at_all(tmp_db: Database) -> None:
    sid = tmp_db.create_statement(make_statement())

    queue = tmp_db.pending_statements()

    assert [s.id for s in queue] == [sid], (
        "with zero new Gmail messages the PENDING statement left the queue; "
        "the queue is being driven by Gmail messages (regression)"
    )


def test_pending_statement_survives_already_ingested_email(tmp_db: Database) -> None:
    sid = tmp_db.create_statement(make_statement())
    tmp_db.insert_email("msg-yesterday", GMAIL_INTERNAL_DATE, statement_id=sid)

    assert tmp_db.email_exists("msg-yesterday") is True
    assert [s.id for s in tmp_db.pending_statements()] == [sid]


def test_completed_statement_leaves_the_queue(tmp_db: Database) -> None:
    sid = tmp_db.create_statement(make_statement())
    tmp_db.set_statement_state(sid, StatementState.COMPLETED)

    assert tmp_db.pending_statements() == []


def test_terminal_exception_statement_leaves_the_queue(tmp_db: Database) -> None:
    sid = tmp_db.create_statement(make_statement())
    tmp_db.set_statement_state(
        sid, StatementState.TERMINAL_EXCEPTION, "STATEMENT_CONTENT_CONFLICT"
    )

    assert tmp_db.pending_statements() == []


def test_pending_statements_returns_only_pending_among_many(tmp_db: Database) -> None:
    pending_sid = tmp_db.create_statement(make_statement())
    completed_sid = tmp_db.create_statement(
        make_statement(payment_document_no="004000000204", content_fingerprint=FINGERPRINT_V2)
    )
    failed_sid = tmp_db.create_statement(
        make_statement(payment_document_no="004000000205", content_fingerprint="c" * 64)
    )
    tmp_db.set_statement_state(completed_sid, StatementState.COMPLETED)
    tmp_db.set_statement_state(failed_sid, StatementState.TERMINAL_EXCEPTION, "G-21")

    assert {s.id for s in tmp_db.pending_statements()} == {pending_sid}


def test_pending_statements_rows_carry_decimal_deposit_and_state(
    tmp_db: Database,
) -> None:
    tmp_db.create_statement(make_statement())

    (row,) = tmp_db.pending_statements()
    assert isinstance(row.deposit_amount, Decimal)
    assert row.deposit_amount == Decimal("665.60")
    assert row.state == StatementState.PENDING
    assert row.eft_date == EFT_DATE


def test_set_statement_state_records_last_error(tmp_db: Database) -> None:
    sid = tmp_db.create_statement(make_statement())
    tmp_db.set_statement_state(sid, StatementState.TERMINAL_EXCEPTION, "G-21 attempts exceeded")

    found = tmp_db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert found is not None
    assert found.state == StatementState.TERMINAL_EXCEPTION
    assert found.last_error == "G-21 attempts exceeded"


def test_statement_can_return_to_pending_without_losing_attempts(
    tmp_db: Database,
) -> None:
    sid = tmp_db.create_statement(make_statement())
    tmp_db.bump_attempt(sid)
    tmp_db.bump_attempt(sid)
    tmp_db.set_statement_state(sid, StatementState.PENDING)

    (row,) = tmp_db.pending_statements()
    assert row.attempt_count == 2


def test_bump_attempt_returns_incremented_value(tmp_db: Database) -> None:
    sid = tmp_db.create_statement(make_statement())

    assert tmp_db.bump_attempt(sid) == 1
    assert tmp_db.bump_attempt(sid) == 2
    assert tmp_db.bump_attempt(sid) == 3


def test_attempt_count_is_readable_for_cap_comparison(tmp_db: Database) -> None:
    sid = tmp_db.create_statement(make_statement())
    for _ in range(6):
        tmp_db.bump_attempt(sid)

    found = tmp_db.find_statement(VENDOR_NO, PAYMENT_DOCUMENT_NO)
    assert found is not None
    assert found.attempt_count == 6

    (queued,) = tmp_db.pending_statements()
    assert queued.attempt_count == 6
    assert queued.attempt_count > 5


def test_bump_attempt_is_scoped_to_one_statement(tmp_db: Database) -> None:
    sid1 = tmp_db.create_statement(make_statement())
    sid2 = tmp_db.create_statement(
        make_statement(payment_document_no="004000000204", content_fingerprint=FINGERPRINT_V2)
    )

    tmp_db.bump_attempt(sid1)
    tmp_db.bump_attempt(sid1)

    by_id = {s.id: s for s in tmp_db.pending_statements()}
    assert by_id[sid1].attempt_count == 2
    assert by_id[sid2].attempt_count == 0


def test_set_statement_state_does_not_bump_attempt(tmp_db: Database) -> None:
    sid = tmp_db.create_statement(make_statement())
    tmp_db.set_statement_state(sid, StatementState.PENDING)
    tmp_db.set_statement_state(sid, StatementState.PENDING)

    (row,) = tmp_db.pending_statements()
    assert row.attempt_count == 0


def test_insert_work_rows_does_not_bump_attempt(tmp_db: Database) -> None:
    st = make_statement()
    sid = tmp_db.create_statement(st)
    tmp_db.insert_work_rows(sid, st.rows)

    (row,) = tmp_db.pending_statements()
    assert row.attempt_count == 0


def test_create_statement_does_not_insert_work_rows(tmp_db: Database) -> None:
    sid = tmp_db.create_statement(make_statement())
    assert tmp_db.work_rows(sid) == []


def test_insert_work_rows_roundtrip(tmp_db: Database) -> None:
    st = make_statement()
    sid = tmp_db.create_statement(st)
    tmp_db.insert_work_rows(sid, st.rows)

    (row,) = tmp_db.work_rows(sid)
    assert row.statement_id == sid
    assert row.invoice_no == INVOICE_B01
    assert row.row_date == make_eft_row().row_date
    assert isinstance(row.eft_net, Decimal)
    assert row.eft_net == Decimal("118.40")
    assert row.eft_gross == Decimal("118.40")
    assert row.eft_prev_paid == Decimal("0.00")
    assert row.eft_outstanding == Decimal("0.00")
    assert row.state == WorkState.PARSED
    assert isinstance(row.state, WorkState)


def test_work_rows_keeps_split_suffixes_separate(tmp_db: Database) -> None:
    rows = (
        make_eft_row(invoice_no=INVOICE_B01, net=Decimal("118.40")),
        make_eft_row(invoice_no=INVOICE_B02, net=Decimal("64.00")),
    )
    sid = tmp_db.create_statement(make_statement(rows=rows))
    tmp_db.insert_work_rows(sid, rows)

    stored = {w.invoice_no: w for w in tmp_db.work_rows(sid)}
    assert set(stored) == {INVOICE_B01, INVOICE_B02}
    assert stored[INVOICE_B01].eft_net == Decimal("118.40")
    assert stored[INVOICE_B02].eft_net == Decimal("64.00")
    assert stored[INVOICE_B01].id != stored[INVOICE_B02].id


def test_work_rows_are_scoped_to_their_statement(tmp_db: Database) -> None:
    st1 = make_statement()
    sid1 = tmp_db.create_statement(st1)
    tmp_db.insert_work_rows(sid1, st1.rows)

    rows2 = (make_eft_row(invoice_no="290006-B01"),)
    sid2 = tmp_db.create_statement(
        make_statement(
            payment_document_no="004000000204",
            content_fingerprint=FINGERPRINT_V2,
            rows=rows2,
        )
    )
    tmp_db.insert_work_rows(sid2, rows2)

    assert [w.invoice_no for w in tmp_db.work_rows(sid1)] == [INVOICE_B01]
    assert [w.invoice_no for w in tmp_db.work_rows(sid2)] == ["290006-B01"]


def test_update_work_persists_verdict_and_state(tmp_db: Database) -> None:
    st = make_statement()
    sid = tmp_db.create_statement(st)
    tmp_db.insert_work_rows(sid, st.rows)
    work_id = _only_work_id(tmp_db, sid, INVOICE_B01)

    tmp_db.update_work(
        work_id,
        verdict=Verdict.AMOUNT_MISMATCH,
        state=WorkState.MANUAL_REVIEW,
        portal_balance=Decimal("75.00"),
    )

    (row,) = tmp_db.work_rows(sid)
    assert row.verdict == Verdict.AMOUNT_MISMATCH
    assert isinstance(row.verdict, Verdict)
    assert row.state == WorkState.MANUAL_REVIEW
    assert isinstance(row.portal_balance, Decimal)
    assert row.portal_balance == Decimal("75.00")


def test_update_work_warnings_roundtrip_as_warncode_tuple(tmp_db: Database) -> None:
    st = make_statement()
    sid = tmp_db.create_statement(st)
    tmp_db.insert_work_rows(sid, st.rows)
    work_id = _only_work_id(tmp_db, sid, INVOICE_B01)

    tmp_db.update_work(
        work_id, warnings=(WarnCode.OUTSTANDING_NONZERO, WarnCode.ACCOUNTING_EQ)
    )

    (row,) = tmp_db.work_rows(sid)
    assert row.warnings == (WarnCode.OUTSTANDING_NONZERO, WarnCode.ACCOUNTING_EQ)


def test_update_work_touches_only_the_named_row(tmp_db: Database) -> None:
    rows = (
        make_eft_row(invoice_no=INVOICE_B01),
        make_eft_row(invoice_no=INVOICE_B02),
    )
    sid = tmp_db.create_statement(make_statement(rows=rows))
    tmp_db.insert_work_rows(sid, rows)
    work_id = _only_work_id(tmp_db, sid, INVOICE_B01)

    tmp_db.update_work(work_id, state=WorkState.CONFIRMED)

    stored = {w.invoice_no: w for w in tmp_db.work_rows(sid)}
    assert stored[INVOICE_B01].state == WorkState.CONFIRMED
    assert stored[INVOICE_B02].state == WorkState.PARSED


def test_update_work_rejects_unknown_column(tmp_db: Database) -> None:
    st = make_statement()
    sid = tmp_db.create_statement(st)
    tmp_db.insert_work_rows(sid, st.rows)
    work_id = _only_work_id(tmp_db, sid, INVOICE_B01)

    with pytest.raises(ValueError):
        tmp_db.update_work(work_id, patient_name="value that must never be stored")


def test_update_work_rejects_injection_shaped_column(tmp_db: Database) -> None:
    st = make_statement()
    sid = tmp_db.create_statement(st)
    tmp_db.insert_work_rows(sid, st.rows)
    work_id = _only_work_id(tmp_db, sid, INVOICE_B01)

    with pytest.raises(ValueError):
        tmp_db.update_work(**{"work_id": work_id, "state='X' --": "y"})


def test_enqueue_report_appears_in_pending_reports(tmp_db: Database) -> None:
    run_id = tmp_db.start_run()
    outbox_id = tmp_db.enqueue_report(run_id, KIND_SUMMARY, "<p>Completed</p>")

    (row,) = tmp_db.pending_reports()
    assert row.id == outbox_id
    assert row.run_id == run_id
    assert row.kind == KIND_SUMMARY
    assert row.payload == "<p>Completed</p>"
    assert row.state == OUTBOX_PENDING
    assert row.attempt_count == 0


def test_mark_report_sent_removes_only_that_report(tmp_db: Database) -> None:
    run_id = tmp_db.start_run()
    summary_id = tmp_db.enqueue_report(run_id, KIND_SUMMARY, "summary")
    heartbeat_id = tmp_db.enqueue_report(run_id, KIND_HEARTBEAT, "heartbeat")

    tmp_db.mark_report_sent(summary_id)

    assert [r.id for r in tmp_db.pending_reports()] == [heartbeat_id]


def test_bump_report_attempt_returns_incremented_value(tmp_db: Database) -> None:
    run_id = tmp_db.start_run()
    outbox_id = tmp_db.enqueue_report(run_id, KIND_FAILURE, "login failed")

    assert tmp_db.bump_report_attempt(outbox_id) == 1
    assert tmp_db.bump_report_attempt(outbox_id) == 2

    (row,) = tmp_db.pending_reports()
    assert row.attempt_count == 2


def test_bump_report_attempt_does_not_mark_sent(tmp_db: Database) -> None:
    run_id = tmp_db.start_run()
    outbox_id = tmp_db.enqueue_report(run_id, KIND_SUMMARY, "summary")
    for _ in range(10):
        tmp_db.bump_report_attempt(outbox_id)

    (row,) = tmp_db.pending_reports()
    assert row.id == outbox_id
    assert row.state == OUTBOX_PENDING


def test_report_queue_is_independent_of_statement_state(tmp_db: Database) -> None:
    sid = tmp_db.create_statement(make_statement())
    tmp_db.set_statement_state(sid, StatementState.COMPLETED)

    run_id = tmp_db.start_run()
    outbox_id = tmp_db.enqueue_report(run_id, KIND_SUMMARY, "24 approved")
    tmp_db.bump_report_attempt(outbox_id)

    assert tmp_db.pending_statements() == []
    assert len(tmp_db.pending_reports()) == 1


def test_pending_reports_empty_on_fresh_db(tmp_db: Database) -> None:
    assert tmp_db.pending_reports() == []


def test_last_successful_run_is_none_on_fresh_db(tmp_db: Database) -> None:
    assert tmp_db.last_successful_run() is None


def test_unfinished_run_is_not_successful(tmp_db: Database) -> None:
    tmp_db.start_run()

    assert tmp_db.last_successful_run() is None


def test_start_finish_last_successful_run_roundtrip(tmp_db: Database) -> None:
    before = datetime.now(UTC) - timedelta(seconds=1)
    run_id = tmp_db.start_run()
    tmp_db.finish_run(
        run_id,
        statements_processed=3,
        approved_count=24,
        approved_total=Decimal("2241.60"),
        manual_review_count=2,
        status=RUN_STATUS_SUCCESS,
        error=None,
    )
    after = datetime.now(UTC) + timedelta(seconds=1)

    last = tmp_db.last_successful_run()
    assert last is not None
    assert isinstance(last, datetime)
    assert before <= last <= after


def test_last_successful_run_is_timezone_aware(tmp_db: Database) -> None:
    run_id = tmp_db.start_run()
    tmp_db.finish_run(run_id, status=RUN_STATUS_SUCCESS)

    last = tmp_db.last_successful_run()
    assert last is not None
    assert last.tzinfo is not None
    assert last.utcoffset() is not None


def test_last_successful_run_ignores_failed_run(tmp_db: Database) -> None:
    run_id = tmp_db.start_run()
    tmp_db.finish_run(run_id, status=RUN_STATUS_FAILED, error="portal login failed")

    assert tmp_db.last_successful_run() is None


def test_last_successful_run_survives_later_failure(tmp_db: Database) -> None:
    ok_run = tmp_db.start_run()
    tmp_db.finish_run(ok_run, status=RUN_STATUS_SUCCESS)
    first = tmp_db.last_successful_run()

    bad_run = tmp_db.start_run()
    tmp_db.finish_run(bad_run, status=RUN_STATUS_FAILED, error="CSV export failed")

    assert first is not None
    still = tmp_db.last_successful_run()
    assert still is not None
    assert still == first


def test_start_run_returns_distinct_ids(tmp_db: Database) -> None:
    assert tmp_db.start_run() != tmp_db.start_run()


def test_approved_total_is_stored_as_decimal_text(tmp_db: Database) -> None:
    run_id = tmp_db.start_run()
    tmp_db.finish_run(run_id, status=RUN_STATUS_SUCCESS, approved_total=Decimal("2241.60"))

    conn = getattr(tmp_db, "conn", None)
    if conn is None:
        pytest.fail("Database must expose a live sqlite3.Connection as `conn`")
    stored = conn.execute(
        "SELECT approved_total, typeof(approved_total) FROM run_log WHERE id = ?", (run_id,)
    ).fetchone()
    assert stored[1] == "text"
    assert Decimal(stored[0]) == Decimal("2241.60")
