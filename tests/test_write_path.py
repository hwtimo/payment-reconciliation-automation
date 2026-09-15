"""The single write primitive: eligibility, dry run, caps and outcome handling."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from remittance_reconciler import main as m
from remittance_reconciler.config import Config
from remittance_reconciler.database import Database
from remittance_reconciler.portal import PortalError, MenuError, PayTarget
from conftest import provenance_for, seed_authorized_statement
from remittance_reconciler.main import RunBudget, write_eligibility
from remittance_reconciler.models import (
    ClaimOutcome,
    EftRow,
    EftStatement,
    InvoiceDetail,
    StatementState,
    Verdict,
    WorkState,
)

PAC = timezone(timedelta(hours=-7))
INV = "300248-B01"
NET = D("118.40")
SIGNAL = "Payment recorded"


def cfg(**kw) -> Config:
    base = dict(automation_start_at=datetime(2037, 6, 17, tzinfo=PAC), dry_run=False,
                max_invoices_per_run=50, max_total_amount_per_run=D("10000.00"),
                inter_invoice_delay_seconds=0, known_vendors=("8100001",),
                post_click_success_signals=(SIGNAL,))
    base.update(kw)
    return Config(**base)


class _PaneStub:
    def __init__(self, owner):
        self._o = owner

    def evaluate(self, _js, arg=None):
        inv = getattr(self._o, "current_invoice", None)
        return f"Invoice {inv}" if inv else None

    class _Kb:
        def press(self, _k): pass
    keyboard = _Kb()

class FakePortal:
    def __init__(self, *, open_error=None, resolve_error=None, click_error=None,
                 signal=SIGNAL, paid=False, total=NET, invoice_no=INV):
        self.open_error, self.resolve_error, self.click_error = open_error, resolve_error, click_error
        self.signal, self.paid, self.total, self.invoice_no = signal, paid, total, invoice_no
        self.clicked: list[str] = []
        self.current_invoice = invoice_no
        self.page = _PaneStub(self)

    @property
    def click_count(self) -> int: return len(self.clicked)

    def open_invoice(self, href, *a, **k):
        if self.open_error: raise self.open_error
        self.current_invoice = self.invoice_no
        return InvoiceDetail(invoice_no=self.invoice_no, total=self.total,
                             payment_status="Paid" if self.paid else "Unpaid",
                             submission_status="Submitted", portal_invoice_id="7001")

    def resolve_record_payment(self, detail):
        if self.resolve_error: raise self.resolve_error
        return PayTarget(detail.invoice_no, locator=object(), menu=object())

    def click_record_payment(self, target):
        self.clicked.append(target.invoice_no)
        if self.click_error: raise self.click_error
        return self.signal


INV2 = "300249-B01"
NET2 = D("881.60")


def seed(tmp_path: Path, *, net=NET, state=WorkState.DETAIL_VALIDATED,
         verdict=Verdict.APPROVE_OK, second_row: bool = True):
    db = Database(tmp_path / "d.db"); db.migrate()
    rows = [EftRow(INV, date(2037, 7, 8), "r", "d", net, D("0.00"), D("0.00"), net)]
    if second_row:
        rows.append(EftRow(INV2, date(2037, 7, 8), "r2", "d2", NET2,
                           D("0.00"), D("0.00"), NET2))
    sid = seed_authorized_statement(db, rows)
    w = next(x for x in db.work_rows(sid) if x.invoice_no == INV)
    db.update_work(w.id, state=state.value, verdict=verdict.value,
                   portal_href="#invoices/7001", portal_invoice_id="7001")
    for other in db.work_rows(sid):
        if other.invoice_no != INV:
            db.update_work(other.id, state=WorkState.PARSED.value)
    return db, db.pending_statements()[0]


def claim_args(db, st, invoice_no=INV):
    return provenance_for(db, invoice_no, st.id)


def _why(*a, **k) -> str:
    return write_eligibility(*a, **k)[1] or ""


def _run_expect_halt(db, portal, c, st):
    import pytest as _p

    from remittance_reconciler.main import RunHalted

    with _p.raises(RunHalted):
        m.execute_statement(db, portal, c, st)

def claims(db) -> int:
    return db.conn.execute("select count(*) c from write_claims").fetchone()["c"]


@pytest.mark.parametrize("state,ok", [
    (WorkState.PARSED, False),
    (WorkState.RECONCILED, False),
    (WorkState.DETAIL_VALIDATED, True), (WorkState.PENDING_WRITE, False),
    (WorkState.CONFIRMED, False), (WorkState.NO_ACTION, False),
    (WorkState.MANUAL_REVIEW, False),
])
def test_eligibility_by_state(tmp_path: Path, state, ok):
    db, st = seed(tmp_path, state=state)
    w = db.work_rows(st.id)[0]
    prov, reason = write_eligibility(
        db, w, cfg(), RunBudget(50, D("10000.00")), None, D("1000.00"))
    assert (reason is None) is ok, reason
    assert (prov is not None) is ok


def test_dry_run_rejected(tmp_path: Path):
    db, st = seed(tmp_path)
    w = db.work_rows(st.id)[0]
    assert _why(db, w, cfg(dry_run=True), RunBudget(50, D("1e4")), None, D("1000")) \
        == "dry_run is enabled"


def test_max_invoices_exhausted(tmp_path: Path):
    db, st = seed(tmp_path)
    w = db.work_rows(st.id)[0]
    b = RunBudget(1, D("10000.00")); b.consume(D("1.00"))
    assert "run limits" in _why(db, w, cfg(), b, None, D("1000"))


def test_max_total_amount_exceeded(tmp_path: Path):
    db, st = seed(tmp_path)
    w = db.work_rows(st.id)[0]
    assert "run limits" in _why(db, w, cfg(), RunBudget(50, D("10.00")), None, D("1000"))


def test_statement_amount_guard(tmp_path: Path):
    db, st = seed(tmp_path)
    w = db.work_rows(st.id)[0]
    assert "statement amount guard" in _why(db, w, cfg(), RunBudget(50, D("1e4")), None, D("1.00"))


def test_non_positive_net_rejected(tmp_path: Path):
    db, st = seed(tmp_path, net=D("0.00"))
    w = db.work_rows(st.id)[0]
    assert "not positive" in _why(db, w, cfg(), RunBudget(50, D("1e4")), None, D("1000"))


def test_existing_claim_rejected(tmp_path: Path):
    db, st = seed(tmp_path)
    w = db.work_rows(st.id)[0]
    db.insert_claim(*claim_args(db, st))
    assert "already exists" in _why(db, w, cfg(), RunBudget(50, D("1e4")), db.get_claim(INV), D("1000"))


def test_happy_path_clicks_once_and_confirms(tmp_path: Path):
    db, st = seed(tmp_path); j = FakePortal()
    m.execute_statement(db, j, cfg(), st)
    assert j.click_count == 1
    assert claims(db) == 1
    assert db.get_claim(INV).final_outcome is ClaimOutcome.CONFIRMED
    assert db.work_rows(st.id)[0].state is WorkState.CONFIRMED


def test_dry_run_creates_no_claim_and_no_click(tmp_path: Path):
    db, st = seed(tmp_path); j = FakePortal()
    m.execute_statement(db, j, cfg(dry_run=True), st)
    assert j.click_count == 0 and claims(db) == 0


@pytest.mark.parametrize("err", [
    MenuError("trigger missing"), MenuError("menu did not open"),
    MenuError("target not present"), MenuError("resolved 2 targets"),
    MenuError("target is disabled"),
])
def test_menu_failures_create_no_claim_and_never_click(tmp_path: Path, err):
    db, st = seed(tmp_path); j = FakePortal(resolve_error=err)
    m.execute_statement(db, j, cfg(), st)
    assert claims(db) == 0, "a claim here permanently blocks a healthy invoice"
    assert j.click_count == 0


def test_reconciled_alone_never_reaches_the_write_boundary(tmp_path: Path):
    db, st = seed(tmp_path, state=WorkState.RECONCILED)
    j = FakePortal()
    m.execute_statement(db, j, cfg(), st)
    assert j.click_count == 0 and claims(db) == 0


PAID_BEFORE_RUN = "paid before automation reached it"


@pytest.mark.parametrize("dry_run", [False, True], ids=["live", "dry-run"])
@pytest.mark.parametrize("net,state,verdict,reason", [
    pytest.param(NET, WorkState.NO_ACTION, Verdict.ALREADY_PAID, PAID_BEFORE_RUN,
                 id="paid-at-reconcile"),
    pytest.param(NET, WorkState.NO_ACTION, Verdict.ALREADY_PAID,
                 "already paid at detail-read time", id="paid-at-detail"),
    pytest.param(NET, WorkState.MANUAL_REVIEW, Verdict.AMOUNT_MISMATCH,
                 "net=118.40 balance=131.65", id="amount-mismatch"),
    pytest.param(NET, WorkState.MANUAL_REVIEW, Verdict.MANUAL_REVIEW,
                 "DETAIL_TOTAL_MISMATCH", id="detail-mismatch"),
    pytest.param(D("-20.00"), WorkState.MANUAL_REVIEW, Verdict.CREDIT_MEMO,
                 "net=-20.00 gross=-20.00", id="credit-memo"),
])
def test_write_stage_keeps_outcomes_recorded_by_earlier_stages(
        tmp_path: Path, net, state, verdict, reason, dry_run):
    db, st = seed(tmp_path, net=net, state=state, verdict=verdict, second_row=False)
    db.update_work(db.work_rows(st.id)[0].id, error_code=reason)
    j = FakePortal()
    m.execute_statement(db, j, cfg(dry_run=dry_run), st)
    w = db.work_rows(st.id)[0]
    assert (w.state, w.verdict, w.error_code) == (state, verdict, reason)
    assert j.click_count == 0 and claims(db) == 0


@pytest.mark.parametrize("state", [
    pytest.param(WorkState.PARSED, id="parsed"),
    pytest.param(WorkState.RECONCILED, id="reconciled"),
    pytest.param(WorkState.PENDING_WRITE, id="pending-write-without-claim"),
    pytest.param(WorkState.CONFIRMED, id="confirmed-without-claim"),
])
def test_unresolved_row_that_cannot_be_written_goes_to_manual_review(tmp_path: Path, state):
    db, st = seed(tmp_path, state=state, second_row=False)
    j = FakePortal()
    m.execute_statement(db, j, cfg(), st)
    w = db.work_rows(st.id)[0]
    assert (w.state, w.verdict) == (WorkState.MANUAL_REVIEW, Verdict.MANUAL_REVIEW)
    assert w.error_code == f"state {state.value} is not write-eligible"
    assert j.click_count == 0 and claims(db) == 0


@pytest.mark.parametrize("invoice_no,identifier_ok,intake_trusted,reason", [
    pytest.param("300251-S01", False, True,
                 "PROVENANCE: EFT row identifier '300251-S01' does not match the required"
                 " format; it cannot be reconciled against the portal",
                 id="malformed-identifier-matched-a-paid-invoice"),
    pytest.param("300251-B01", True, False,
                 "PROVENANCE: source email did not pass the trusted-forward gate",
                 id="untrusted-source"),
])
def test_already_paid_row_that_fails_provenance_goes_to_manual_review(
        tmp_path: Path, invoice_no, identifier_ok, intake_trusted, reason):
    db = Database(tmp_path / "d.db"); db.migrate()
    row = EftRow(invoice_no, date(2037, 7, 8), "r", "d", NET, D("0.00"), D("0.00"), NET,
                 identifier_ok=identifier_ok)
    sid = seed_authorized_statement(db, [row], intake_trusted=intake_trusted)
    db.update_work(db.work_rows(sid)[0].id, state=WorkState.NO_ACTION.value,
                   verdict=Verdict.ALREADY_PAID.value, error_code=PAID_BEFORE_RUN)
    j = FakePortal(invoice_no=invoice_no)
    m.execute_statement(db, j, cfg(), db.get_statement(sid))
    w = db.work_rows(sid)[0]
    assert (w.state, w.verdict, w.error_code) == (
        WorkState.MANUAL_REVIEW, Verdict.MANUAL_REVIEW, reason)
    assert j.click_count == 0 and claims(db) == 0


def test_validated_row_that_loses_provenance_goes_to_manual_review(tmp_path: Path):
    db, st = seed(tmp_path, second_row=False)
    db.conn.execute("UPDATE emails SET intake_trusted = 0"); db.conn.commit()
    j = FakePortal()
    m.execute_statement(db, j, cfg(), st)
    w = db.work_rows(st.id)[0]
    assert w.state is WorkState.MANUAL_REVIEW
    assert w.error_code == "PROVENANCE: source email did not pass the trusted-forward gate"
    assert j.click_count == 0 and claims(db) == 0


def test_eligible_row_is_still_written_once_next_to_a_resolved_row(tmp_path: Path):
    db, st = seed(tmp_path)
    paid = next(w for w in db.work_rows(st.id) if w.invoice_no == INV2)
    db.update_work(paid.id, state=WorkState.NO_ACTION.value,
                   verdict=Verdict.ALREADY_PAID.value, error_code=PAID_BEFORE_RUN)
    j = FakePortal()
    m.execute_statement(db, j, cfg(), st)
    assert j.clicked == [INV]
    assert db.get_claim(INV).final_outcome is ClaimOutcome.CONFIRMED
    assert db.get_claim(INV2) is None
    after = {w.invoice_no: w for w in db.work_rows(st.id)}
    assert after[INV].state is WorkState.CONFIRMED
    assert (after[INV2].state, after[INV2].verdict, after[INV2].error_code) == (
        WorkState.NO_ACTION, Verdict.ALREADY_PAID, PAID_BEFORE_RUN)
    assert m.finalize_statement(db, st) is StatementState.COMPLETED


@pytest.mark.parametrize("state", [WorkState.NO_ACTION, WorkState.MANUAL_REVIEW])
def test_row_left_unchanged_never_becomes_write_eligible(tmp_path: Path, state):
    db, st = seed(tmp_path, state=state, verdict=Verdict.APPROVE_OK, second_row=False)
    j = FakePortal()
    for _ in range(3):
        m.execute_statement(db, j, cfg(), st)
    w = db.work_rows(st.id)[0]
    assert w.state is state
    assert j.click_count == 0 and claims(db) == 0
    prov, reason = write_eligibility(
        db, w, cfg(), RunBudget(50, D("10000.00")), None, D("1000.00"))
    assert prov is None and reason == f"state {state.value} is not write-eligible"


class _PaidWithoutSignal(FakePortal):
    def _observe_success_signal(self, invoice_no, expected_amount=None, **_k):
        return ""


def test_ambiguous_click_is_escalated_even_after_the_invoice_looks_paid(tmp_path: Path):
    db, st = seed(tmp_path, second_row=False)
    db.insert_claim(*claim_args(db, st))
    db.set_claim_outcome(INV, ClaimOutcome.UNKNOWN_OUTCOME)
    db.update_work(db.work_rows(st.id)[0].id, state=WorkState.NO_ACTION.value,
                   verdict=Verdict.ALREADY_PAID.value, error_code=PAID_BEFORE_RUN)
    j = _PaidWithoutSignal(paid=True)
    m.execute_statement(db, j, cfg(), st)
    m.execute_statement(db, j, cfg(), st)
    w = db.work_rows(st.id)[0]
    assert j.click_count == 0
    assert (w.state, w.verdict, w.error_code) == (
        WorkState.MANUAL_REVIEW, Verdict.MANUAL_REVIEW, "CLAIM_EXISTS_UNRESOLVED")
    assert db.get_claim(INV).final_outcome is ClaimOutcome.UNKNOWN_OUTCOME


def test_navigation_failure_creates_no_claim_and_stays_retryable(tmp_path: Path):
    db, st = seed(tmp_path); j = FakePortal(open_error=PortalError("nav failed"))
    m.execute_statement(db, j, cfg(), st)
    assert claims(db) == 0 and j.click_count == 0
    assert db.work_rows(st.id)[0].state is WorkState.DETAIL_VALIDATED, "must stay retryable"


def test_claim_is_committed_before_the_click(tmp_path: Path):
    db, st = seed(tmp_path)
    seen: list[int] = []

    class J(FakePortal):
        def click_record_payment(self, target):
            import sqlite3
            other = sqlite3.connect(str(db.path))
            seen.append(other.execute("select count(*) from write_claims").fetchone()[0])
            return super().click_record_payment(target)

    m.execute_statement(db, J(), cfg(), st)
    assert seen == [1], "the claim was not durably committed before the click"


def test_detail_total_mismatch_blocks_before_any_claim(tmp_path: Path):
    db, st = seed(tmp_path); j = FakePortal(total=D("99.00"))
    m.execute_statement(db, j, cfg(), st)
    assert claims(db) == 0 and j.click_count == 0


def test_detail_identifier_mismatch_blocks_before_any_claim(tmp_path: Path):
    db, st = seed(tmp_path); j = FakePortal(invoice_no="309999-B01")
    m.execute_statement(db, j, cfg(), st)
    assert claims(db) == 0 and j.click_count == 0


def test_already_paid_at_write_boundary_blocks(tmp_path: Path):
    db, st = seed(tmp_path); j = FakePortal(paid=True)
    m.execute_statement(db, j, cfg(), st)
    assert claims(db) == 0 and j.click_count == 0


def test_no_positive_signal_is_unknown_outcome_not_failure(tmp_path: Path):
    db, st = seed(tmp_path); j = FakePortal(signal="")
    _run_expect_halt(db, j, cfg(), st)
    assert j.click_count == 1
    assert db.get_claim(INV).final_outcome is ClaimOutcome.UNKNOWN_OUTCOME
    assert db.work_rows(st.id)[0].state is WorkState.MANUAL_REVIEW


def test_wrong_signal_text_is_unknown_outcome(tmp_path: Path):
    db, st = seed(tmp_path); j = FakePortal(signal="Something else entirely")
    _run_expect_halt(db, j, cfg(), st)
    assert db.get_claim(INV).final_outcome is ClaimOutcome.UNKNOWN_OUTCOME


def test_click_raising_still_records_unknown_outcome_and_never_reclicks(tmp_path: Path):
    db, st = seed(tmp_path); j = FakePortal(click_error=TimeoutError("no response"))
    _run_expect_halt(db, j, cfg(), st)
    assert j.click_count == 1
    assert db.get_claim(INV).final_outcome is ClaimOutcome.UNKNOWN_OUTCOME
    m.execute_statement(db, j, cfg(), st)
    assert j.click_count == 1, "a second click was issued after an ambiguous outcome"


def test_confirmed_claim_is_silently_skipped_on_restart(tmp_path: Path):
    db, st = seed(tmp_path); j = FakePortal()
    m.execute_statement(db, j, cfg(), st)
    m.execute_statement(db, j, cfg(), st)
    assert j.click_count == 1 and claims(db) == 1


def test_unknown_outcome_claim_routes_to_manual_review_without_clicking(tmp_path: Path):
    db, st = seed(tmp_path)
    db.insert_claim(*claim_args(db, st))
    db.set_claim_outcome(INV, ClaimOutcome.UNKNOWN_OUTCOME)
    j = FakePortal()
    m.execute_statement(db, j, cfg(), st)
    assert j.click_count == 0
    assert db.work_rows(st.id)[0].state is WorkState.MANUAL_REVIEW


def test_null_outcome_claim_is_crash_boundary_recovery_and_never_clicks(tmp_path: Path):
    db, st = seed(tmp_path)
    db.insert_claim(*claim_args(db, st))
    j = FakePortal()
    m.execute_statement(db, j, cfg(), st)
    assert j.click_count == 0
    w = db.work_rows(st.id)[0]
    assert w.state is WorkState.MANUAL_REVIEW and w.attribution == "unverified"


def test_claim_survives_a_process_restart(tmp_path: Path):
    db, st = seed(tmp_path); j = FakePortal()
    m.execute_statement(db, j, cfg(), st)
    db.close()
    db2 = Database(tmp_path / "d.db"); db2.migrate()
    assert claims(db2) == 1
    m.execute_statement(db2, j, cfg(), db2.pending_statements()[0] if db2.pending_statements()
                        else st)
    assert j.click_count == 1


def test_at_most_once_across_repeated_runs_and_restarts(tmp_path: Path):
    db, st = seed(tmp_path)
    j = FakePortal(signal="")
    for attempt in range(5):
        if attempt == 0:
            _run_expect_halt(db, j, cfg(), st)
        else:
            m.execute_statement(db, j, cfg(), st)
        db.close()
        db = Database(tmp_path / "d.db"); db.migrate()
        pend = db.pending_statements()
        st = pend[0] if pend else st
    assert j.click_count == 1, f"invoice was clicked {j.click_count} times"
    assert claims(db) == 1


def test_click_primitive_contains_no_retry_loop():
    import inspect

    from remittance_reconciler.portal import PortalSession
    src = inspect.getsource(PortalSession.click_record_payment)
    body = src.split('"""')[-1]
    assert "for " not in body and "while " not in body, "a retry loop appeared in the click primitive"
    assert body.count(".click(") == 1, "more than one click call in the primitive"
