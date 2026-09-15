"""One-shot supervised target selection and its integration with the nightly run."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from conftest import provenance_for, seed_authorized_statement
from remittance_reconciler import main as m
from remittance_reconciler.config import Config
from remittance_reconciler.database import Database
from remittance_reconciler.portal import PayTarget
from remittance_reconciler.main import SmokeAbort, resolve_smoke_target
from remittance_reconciler.models import (
    ClaimOutcome,
    EftRow,
    InvoiceDetail,
    StatementState,
    Verdict,
    WorkState,
)

PAC = timezone(timedelta(hours=-7))
NET = D("64.00")

INV_A, INV_B, INV_C = "290016-B01", "290018-B01", "290019-B01"


class _PaneStub:
    def __init__(self, owner): self._o = owner

    def evaluate(self, _js, arg=None):
        inv = getattr(self._o, "current_invoice", None)
        return f"Invoice {inv}" if inv else None

    class _Kb:
        def press(self, _k): pass
    keyboard = _Kb()


class HonestPortal:
    def __init__(self, *, paid: set[str] | None = None, signal: str = "Paid / Settled",
                 open_error: Exception | None = None,
                 recovery_signal: str | None = None):
        self.paid = paid or set()
        self.signal = signal
        self.open_error = open_error
        self.recovery_signal = recovery_signal
        self.clicked: list[str] = []
        self.current_invoice: str | None = None
        self.page = _PaneStub(self)
        self.opened: list[str] = []

    @property
    def click_count(self) -> int:
        return len(self.clicked)

    def open_invoice(self, href, expected_invoice_no=None, **k):
        if self.open_error:
            raise self.open_error
        inv = href.rsplit("/", 1)[-1]
        self.opened.append(inv)
        self.current_invoice = inv
        return InvoiceDetail(
            invoice_no=inv, total=NET,
            payment_status="Paid" if inv in self.paid else "Unpaid",
            submission_status="Settled" if inv in self.paid else "Submitted",
            portal_invoice_id=f"j{inv}",
        )

    def resolve_record_payment(self, detail):
        return PayTarget(detail.invoice_no, locator=object(), menu=object(),
                         expected_amount=detail.total)

    def click_record_payment(self, target):
        self.clicked.append(target.invoice_no)
        return self.signal

    def _observe_success_signal(self, invoice_no, expected_amount=None, **k):
        if self.recovery_signal is not None:
            return self.recovery_signal
        return "Paid / Settled" if invoice_no in self.paid else ''


def cfg(**kw) -> Config:
    base = dict(
        automation_start_at=datetime(2037, 6, 17, tzinfo=PAC),
        dry_run=False,
        max_invoices_per_run=1,
        max_total_amount_per_run=NET,
        inter_invoice_delay_seconds=0,
        known_vendors=("8100001",),
        post_click_success_signals=("Paid / Settled",),
    )
    base.update(kw)
    return Config(**base)


def seed(tmp_path: Path, *, invoices=(INV_A, INV_B, INV_C),
         state=WorkState.DETAIL_VALIDATED) -> tuple[Database, int, dict[str, int]]:
    db = Database(tmp_path / "d.db")
    db.migrate()
    rows = [EftRow(inv, date(2037, 7, 7), "r", "d", NET, D("0.00"), D("0.00"), NET)
            for inv in invoices]
    sid = seed_authorized_statement(db, rows)
    ids = {}
    for w in db.work_rows(sid):
        db.update_work(w.id, state=state.value, verdict=Verdict.APPROVE_OK.value,
                       portal_href=f"#invoices/{w.invoice_no}",
                       portal_invoice_id=f"j{w.invoice_no}")
        ids[w.invoice_no] = w.id
    return db, sid, ids


def claims(db: Database) -> int:
    return db.conn.execute("select count(*) c from write_claims").fetchone()["c"]


def states(db: Database, sid: int) -> dict[str, str]:
    return {w.invoice_no: w.state.value for w in db.work_rows(sid)}


def test_smoke_target_is_the_only_row_that_clicks(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    portal = HonestPortal()
    m.execute_statement(db, portal, cfg(smoke_work_id=ids[INV_B]), db.pending_statements()[0])

    assert portal.clicked == [INV_B], f"a non-target row was clicked: {portal.clicked}"
    assert claims(db) == 1
    assert db.get_claim(INV_B) is not None
    assert db.get_claim(INV_A) is None and db.get_claim(INV_C) is None


def test_non_target_rows_keep_their_state(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    m.execute_statement(db, HonestPortal(), cfg(smoke_work_id=ids[INV_B]),
                        db.pending_statements()[0])

    st = states(db, sid)
    assert st[INV_A] == WorkState.DETAIL_VALIDATED.value
    assert st[INV_C] == WorkState.DETAIL_VALIDATED.value
    assert st[INV_B] == WorkState.CONFIRMED.value


def test_smoke_work_id_a_cannot_cause_work_id_b_to_click(tmp_path: Path) -> None:
    for target, other in ((INV_A, INV_B), (INV_C, INV_A)):
        db, sid, ids = seed(tmp_path / target)
        portal = HonestPortal()
        m.execute_statement(db, portal, cfg(smoke_work_id=ids[target]),
                            db.pending_statements()[0])
        assert portal.clicked == [target]
        assert other not in portal.clicked


def test_without_a_smoke_target_the_sort_first_row_wins(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    portal = HonestPortal()
    m.execute_statement(db, portal, cfg(), db.pending_statements()[0])
    assert portal.clicked == [INV_A], "something other than the first sorted row was clicked"


def test_missing_work_id_aborts_and_never_substitutes(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    with pytest.raises(SmokeAbort, match="does not exist"):
        resolve_smoke_target(db, cfg(smoke_work_id=999999))
    assert claims(db) == 0


def test_unconfigured_work_id_aborts(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    with pytest.raises(SmokeAbort, match="no smoke work ids are configured"):
        resolve_smoke_target(db, cfg(smoke_work_id=None))


def test_target_that_lost_provenance_aborts(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    db.conn.execute("UPDATE invoice_work SET raw_eft_invoice_no=NULL WHERE id=?",
                    (ids[INV_B],))
    db.conn.commit()
    with pytest.raises(SmokeAbort, match="lost provenance"):
        resolve_smoke_target(db, cfg(smoke_work_id=ids[INV_B]))


def test_target_whose_source_email_became_untrusted_aborts(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    db.conn.execute("UPDATE emails SET intake_trusted=0 WHERE statement_id=?", (sid,))
    db.conn.commit()
    with pytest.raises(SmokeAbort, match="trusted-forward gate"):
        resolve_smoke_target(db, cfg(smoke_work_id=ids[INV_B]))


def test_target_on_a_terminal_statement_aborts(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    db.set_statement_state(sid, StatementState.TERMINAL_EXCEPTION)
    with pytest.raises(SmokeAbort, match="TERMINAL_EXCEPTION"):
        resolve_smoke_target(db, cfg(smoke_work_id=ids[INV_B]))


def test_target_that_already_has_a_claim_is_skipped_not_aborted(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    db.insert_claim(*provenance_for(db, INV_B, sid))

    work, _prov = resolve_smoke_target(db, cfg(smoke_work_id=ids[INV_B]))
    assert work.id == ids[INV_B]

    portal = HonestPortal()
    m.execute_statement(db, portal, cfg(smoke_work_id=ids[INV_B]),
                        db.pending_statements()[0])
    assert portal.click_count == 0
    assert claims(db) == 1


def test_target_exceeding_the_temporary_cap_aborts(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    with pytest.raises(SmokeAbort, match="exceeds max_total_amount_per_run"):
        resolve_smoke_target(db, cfg(smoke_work_id=ids[INV_B],
                                     max_total_amount_per_run=D("63.99")))
    work, prov = resolve_smoke_target(
        db, cfg(smoke_work_id=ids[INV_B], max_total_amount_per_run=NET))
    assert work.invoice_no == INV_B and prov.eft_net == NET


def test_target_identity_cross_check_aborts_on_mismatch(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    with pytest.raises(SmokeAbort, match="expected '290026-B01'"):
        resolve_smoke_target(db, cfg(smoke_work_id=ids[INV_B],
                                     smoke_expect_invoice_no="290026-B01"))
    with pytest.raises(SmokeAbort, match="amount is 64.00, expected 99.00"):
        resolve_smoke_target(db, cfg(smoke_work_id=ids[INV_B],
                                     smoke_expect_eft_net=D("99.00")))
    work, _ = resolve_smoke_target(db, cfg(
        smoke_work_id=ids[INV_B], smoke_expect_invoice_no=INV_B,
        smoke_expect_eft_net=NET))
    assert work.id == ids[INV_B]


def test_aborting_never_falls_back_to_another_invoice(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    db.conn.execute("UPDATE invoice_work SET raw_eft_invoice_no=NULL WHERE id=?",
                    (ids[INV_B],))
    db.conn.commit()
    with pytest.raises(SmokeAbort):
        resolve_smoke_target(db, cfg(smoke_work_id=ids[INV_B]))
    assert claims(db) == 0
    assert states(db, sid)[INV_A] == WorkState.DETAIL_VALIDATED.value


def test_target_already_paid_never_clicks(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    portal = HonestPortal(paid={INV_B})
    m.execute_statement(db, portal, cfg(smoke_work_id=ids[INV_B]),
                        db.pending_statements()[0])

    assert portal.click_count == 0, "clicked an already-paid invoice"
    assert claims(db) == 0
    w = next(x for x in db.work_rows(sid) if x.invoice_no == INV_B)
    assert w.state is WorkState.NO_ACTION
    assert w.verdict is Verdict.ALREADY_PAID
    assert w.error_code == "ALREADY_PAID_AT_WRITE_BOUNDARY"
    assert states(db, sid)[INV_A] == WorkState.DETAIL_VALIDATED.value


def test_observed_g5_signal_confirms_the_smoke(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    portal = HonestPortal(signal="Paid / Settled")
    m.execute_statement(db, portal, cfg(smoke_work_id=ids[INV_B]),
                        db.pending_statements()[0])

    w = next(x for x in db.work_rows(sid) if x.invoice_no == INV_B)
    assert w.state is WorkState.CONFIRMED
    claim = db.get_claim(INV_B)
    assert claim is not None and claim.final_outcome is ClaimOutcome.CONFIRMED
    assert claim.source_message_id and claim.payment_document_no


def test_repeated_runs_click_exactly_once(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    c = cfg(smoke_work_id=ids[INV_B])
    portal = HonestPortal()
    for _ in range(3):
        m.execute_statement(db, portal, c, db.pending_statements()[0])
    assert portal.click_count == 1
    assert claims(db) == 1


def test_unknown_outcome_then_restart_never_reclicks(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    c = cfg(smoke_work_id=ids[INV_B])
    from remittance_reconciler.main import RunHalted

    portal = HonestPortal(signal="")
    with pytest.raises(RunHalted):
        m.execute_statement(db, portal, c, db.pending_statements()[0])
    assert portal.click_count == 1
    assert db.get_claim(INV_B).final_outcome is ClaimOutcome.UNKNOWN_OUTCOME

    m.execute_statement(db, portal, c, db.pending_statements()[0])
    assert portal.click_count == 1, "a re-click happened"


def test_crash_after_success_before_finalization_recovers_read_only(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    db.insert_claim(*provenance_for(db, INV_B, sid))
    assert db.get_claim(INV_B).final_outcome is None

    portal = HonestPortal(paid={INV_B})
    m.execute_statement(db, portal, cfg(smoke_work_id=ids[INV_B]),
                        db.pending_statements()[0])

    assert portal.click_count == 0, "recovery re-clicked"
    w = next(x for x in db.work_rows(sid) if x.invoice_no == INV_B)
    assert w.state is WorkState.CONFIRMED
    assert db.get_claim(INV_B).final_outcome is ClaimOutcome.CONFIRMED
    assert w.attribution == "recovered-readonly"
    assert w.error_code == "RECOVERED_AFTER_CRASH"


def test_crash_recovery_when_not_paid_goes_to_manual_review(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    db.insert_claim(*provenance_for(db, INV_B, sid))

    portal = HonestPortal()
    m.execute_statement(db, portal, cfg(smoke_work_id=ids[INV_B]),
                        db.pending_statements()[0])

    assert portal.click_count == 0
    w = next(x for x in db.work_rows(sid) if x.invoice_no == INV_B)
    assert w.state is WorkState.MANUAL_REVIEW
    assert w.error_code == "CLAIM_EXISTS_UNRESOLVED"
    assert w.attribution == "unverified"


def test_crash_recovery_read_failure_leaves_state_retryable(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    db.insert_claim(*provenance_for(db, INV_B, sid))

    portal = HonestPortal(open_error=RuntimeError("network"))
    m.execute_statement(db, portal, cfg(smoke_work_id=ids[INV_B]),
                        db.pending_statements()[0])
    assert portal.click_count == 0
    w = next(x for x in db.work_rows(sid) if x.invoice_no == INV_B)
    assert w.state is WorkState.MANUAL_REVIEW
    assert db.get_claim(INV_B).final_outcome is None


def test_dry_run_blocks_even_the_smoke_target(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    portal = HonestPortal()
    m.execute_statement(db, portal, cfg(smoke_work_id=ids[INV_B], dry_run=True),
                        db.pending_statements()[0])
    assert portal.click_count == 0
    assert claims(db) == 0
    assert states(db, sid)[INV_B] == WorkState.DETAIL_VALIDATED.value


def test_zero_cap_blocks_even_the_smoke_target(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    with pytest.raises(SmokeAbort, match="exceeds max_total_amount_per_run"):
        resolve_smoke_target(db, cfg(smoke_work_id=ids[INV_B],
                                     max_total_amount_per_run=D("0.00")))


class ShadowPortal:
    class _Loc:
        def __init__(self, n=1): self._n = n
        def count(self): return self._n
        def get_attribute(self, _): return "false"
        def click(self): pass
        @property
        def first(self): return self
        def wait_for(self, **_): pass
        def get_by_role(self, _role, name=None, exact=None): return ShadowPortal._Loc(1)

    class _Page:
        class _Kb:
            def press(self, _): pass
        keyboard = _Kb()

    def __init__(self, hrefs: dict[str, str]):
        self.hrefs = hrefs
        self.page = self._Page()
        self.href_queries: list[set] = []

    def _open_sales_report(self, lo, hi, all_invoice_states=False): pass

    def collect_hrefs(self, wanted):
        self.href_queries.append(set(wanted))
        return {k: v for k, v in self.hrefs.items() if k in wanted}

    def open_invoice(self, href, expected_invoice_no=None, **k):
        inv = href.rsplit("/", 1)[-1]
        return InvoiceDetail(invoice_no=inv, total=NET, payment_status="Unpaid",
                             submission_status="Submitted", portal_invoice_id=f"j{inv}")

    def action_trigger(self): return self._Loc(1)
    def action_menu(self): return self._Loc(1)


def test_detail_shadow_only_touches_the_smoke_target(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path, state=WorkState.RECONCILED)
    portal = ShadowPortal({inv: f"#invoices/{inv}" for inv in (INV_A, INV_B, INV_C)})

    m.detail_shadow_statement(db, portal, cfg(), db.pending_statements()[0],
                              only_work_id=ids[INV_B])

    assert portal.href_queries == [{INV_B}], f"queried a non-target row: {portal.href_queries}"
    st = states(db, sid)
    assert st[INV_B] == WorkState.DETAIL_VALIDATED.value
    assert st[INV_A] == WorkState.RECONCILED.value
    assert st[INV_C] == WorkState.RECONCILED.value


def test_run_once_performs_detail_validation_before_writing() -> None:
    import ast
    import inspect

    src = inspect.getsource(m.run_once)
    tree = ast.parse(src.lstrip())
    called = {
        n.func.id for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    for stage in ("reconcile_statement", "detail_shadow_statement",
                  "execute_statement", "finalize_statement"):
        assert stage in called, f"run_once does not call {stage}"

    i_rec = src.index("reconcile_statement(db")
    i_det = src.index("detail_shadow_statement(db")
    i_exe = src.index("execute_statement(db")
    assert i_rec < i_det < i_exe, "stage order was changed"


def test_reconciled_row_alone_is_never_write_eligible(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path, state=WorkState.RECONCILED)
    portal = HonestPortal()
    m.execute_statement(db, portal, cfg(smoke_work_id=ids[INV_B]),
                        db.pending_statements()[0])
    assert portal.click_count == 0
    assert claims(db) == 0


class NullGmail:
    def __init__(self): self.sent: list[tuple[str, str]] = []

    def fetch_eft_messages(self, after): return iter(())
    def send(self, to, subject, html_body): self.sent.append((subject, html_body))


def test_manual_review_row_survives_into_the_run_report(tmp_path: Path) -> None:
    from remittance_reconciler.database import Database as _DB

    db = _DB(tmp_path / "d.db"); db.migrate()
    rows = [EftRow(inv, date(2037, 7, 7), "r", "d", NET, D("0.00"), D("0.00"), NET)
            for inv in (INV_A, INV_B)]
    sid = seed_authorized_statement(db, rows)
    w = db.work_rows(sid)
    db.update_work(w[0].id, state=WorkState.CONFIRMED.value)
    db.update_work(w[1].id, state=WorkState.MANUAL_REVIEW.value,
                   error_code="CLAIM_EXISTS_UNRESOLVED", attribution="unverified")

    monkey = {"reconcile_statement": lambda *a, **k: None,
              "detail_shadow_statement": lambda *a, **k: {},
              "execute_statement": lambda *a, **k: None}
    saved = {k: getattr(m, k) for k in monkey}
    for k, v in monkey.items():
        setattr(m, k, v)
    try:
        class _AuthedPortal:
            def ensure_authenticated(self, *a, **k): return "reused"

        gmail = NullGmail()
        stats = m.run_once(cfg(dry_run=True), db, gmail, portal=_AuthedPortal())
    finally:
        for k, v in saved.items():
            setattr(m, k, v)

    assert db.get_statement(sid).state is StatementState.COMPLETED
    assert stats.manual_review_count == 1, (
        "MANUAL_REVIEW rows vanished from the report; nobody would be notified"
    )
    payload = db.conn.execute(
        "SELECT payload FROM report_outbox ORDER BY id DESC LIMIT 1"
    ).fetchone()["payload"]
    assert INV_B in payload, "the summary omits an invoice a human must review"


def test_run_budget_is_shared_across_statements(tmp_path: Path) -> None:
    import ast
    import inspect

    src = inspect.getsource(m.run_once)
    assert "RunBudget(" in src, "run_once does not create a budget"
    assert "execute_statement(db, portal, cfg, st, budget)" in src, (
        "the run budget is not passed to execute_statement"
    )
    i_budget = src.index("budget = RunBudget(")
    i_loop = src.index("for st in queue:")
    assert i_budget < i_loop, "the budget is created inside the statement loop"


def test_recovery_requires_the_full_g5_contract_not_just_paid(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    db.insert_claim(*provenance_for(db, INV_B, sid))

    portal = HonestPortal(paid={INV_B}, recovery_signal="")
    m.execute_statement(db, portal, cfg(smoke_work_id=ids[INV_B]),
                        db.pending_statements()[0])

    assert portal.click_count == 0, "recovery re-clicked"
    w = next(x for x in db.work_rows(sid) if x.invoice_no == INV_B)
    assert w.state is WorkState.MANUAL_REVIEW, "confirmed without sufficient evidence"
    assert db.get_claim(INV_B).final_outcome is None


def test_stale_detail_validated_row_cannot_click_a_paid_invoice(tmp_path: Path) -> None:
    db, sid, ids = seed(tmp_path)
    w = next(x for x in db.work_rows(sid) if x.invoice_no == INV_B)
    assert w.state is WorkState.DETAIL_VALIDATED
    assert w.verdict is Verdict.APPROVE_OK
    assert db.get_claim(INV_B) is None

    portal = HonestPortal(paid={INV_B})
    m.execute_statement(db, portal, cfg(smoke_work_id=ids[INV_B]),
                        db.pending_statements()[0])

    assert portal.click_count == 0, "clicked an already-paid invoice"
    assert claims(db) == 0, "a claim was created"
    after = next(x for x in db.work_rows(sid) if x.invoice_no == INV_B)
    assert after.state is WorkState.NO_ACTION
    assert after.verdict is Verdict.ALREADY_PAID


def test_both_entry_points_share_one_financial_primitive() -> None:
    from remittance_reconciler import smoke, writepath

    assert smoke.execute_smoke_click is writepath.execute_one_authorized_work
    assert smoke.validate_smoke_target is writepath.validate_sales_and_detail

    import ast
    import inspect

    prod = ast.unparse(ast.parse(inspect.getsource(m.execute_statement).lstrip()))
    assert "execute_one_authorized_work(" in prod
    assert "insert_claim" not in prod, "production has its own claim implementation"
    assert "click_record_payment" not in prod, "production has its own click implementation"
