"""Supervised runs read and change only their target rows."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from conftest import provenance_for, seed_authorized_statement
from remittance_reconciler import main as m
from remittance_reconciler import smoke as sm
from remittance_reconciler.config import Config
from remittance_reconciler.database import Database
from remittance_reconciler.portal import MenuError, PayTarget
from remittance_reconciler.models import (
    ClaimOutcome,
    EftRow,
    InvoiceDetail,
    PortalInvoice,
    Verdict,
    WorkState,
)

PAC = timezone(timedelta(hours=-7))
NET = D("64.00")
ROW_DATE = date(2037, 7, 7)

INVOICES = ["290016-B01", "290018-B01", "290019-B01", "290021-B01", "290022-B01"]
TARGET = "290018-B01"


def cfg(**kw) -> Config:
    base = dict(
        automation_start_at=datetime(2037, 6, 17, tzinfo=PAC),
        dry_run=True,
        max_invoices_per_run=1,
        max_total_amount_per_run=NET,
        inter_invoice_delay_seconds=0,
        known_vendors=("8100001",),
        post_click_success_signals=("Paid / Settled",),
        date_buffer_days=3,
    )
    base.update(kw)
    return Config(**base)


class _Loc:
    def __init__(self, visible=True, enabled=True):
        self._v, self._e = visible, enabled
    def is_visible(self): return self._v
    def is_enabled(self): return self._e


class _Page:
    class _Kb:
        def __init__(self): self.presses: list[str] = []
        def press(self, k): self.presses.append(k)

    def __init__(self, pane_invoice: str | None = None):
        self.keyboard = self._Kb()
        self.pane_invoice = pane_invoice

    def evaluate(self, _js, arg=None):
        return None if self.pane_invoice is None else f"Invoice {self.pane_invoice}"


class FakePortal:
    def __init__(self, *, paid: set[str] = frozenset(), signal: str = "Paid / Settled",
                 menu_error: Exception | None = None, missing_href: bool = False,
                 detail_total: D | None = None, pane_invoice: str | None = None,
                 screen_after: int | None = None):
        self.paid = set(paid)
        self.signal = signal
        self.menu_error = menu_error
        self.missing_href = missing_href
        self.detail_total = detail_total
        self.page = _Page(pane_invoice if pane_invoice is not None else TARGET)
        self._screen_after = screen_after
        self.clicked: list[str] = []
        self.href_queries: list[set] = []
        self.opened: list[str] = []

    @property
    def click_count(self) -> int: return len(self.clicked)

    dom_invoices: set[str] = set(INVOICES)

    def collect_hrefs(self, targets, **k):
        self.href_queries.append(set(targets))
        if self.missing_href:
            return {}
        hit = {t for t in targets if t in self.dom_invoices}
        return {t: f"#invoices/{t}" for t in hit}

    def open_invoice(self, href, expected_invoice_no=None, **k):
        inv = href.rsplit("/", 1)[-1]
        self.opened.append(inv)
        return InvoiceDetail(
            invoice_no=inv,
            total=self.detail_total if self.detail_total is not None else NET,
            payment_status="Paid" if inv in self.paid else "Unpaid",
            submission_status="Settled" if inv in self.paid else "Submitted",
            portal_invoice_id=f"j{inv}",
        )

    def resolve_record_payment(self, detail):
        if self.menu_error:
            raise self.menu_error
        return PayTarget(detail.invoice_no, locator=_Loc(), menu=object(),
                         expected_amount=detail.total)

    def click_record_payment(self, target):
        self.clicked.append(target.invoice_no)
        return self.signal


def seed(tmp_path: Path):
    db = Database(tmp_path / "d.db"); db.migrate()
    rows = [EftRow(inv, ROW_DATE, "r", "d", NET, D("0.00"), D("0.00"), NET)
            for inv in INVOICES]
    sid = seed_authorized_statement(db, rows)
    ids = {}
    for w in db.work_rows(sid):
        db.update_work(w.id, state=WorkState.DETAIL_VALIDATED.value,
                       verdict=Verdict.APPROVE_OK.value,
                       portal_href=f"#invoices/{w.invoice_no}",
                       portal_invoice_id=f"j{w.invoice_no}",
                       portal_balance=NET, portal_payer="ACME Plan B",
                       portal_status="Unpaid", portal_total=NET, portal_collected=D("0.00"))
        ids[w.invoice_no] = w.id
    return db, sid, ids


def snapshot(db) -> dict[int, tuple]:
    return {r["id"]: tuple(r) for r in db.conn.execute("SELECT * FROM invoice_work ORDER BY id")}


def changed_ids(before: dict, after: dict, *, exclude: int | None = None) -> set[int]:
    ids = (set(before) | set(after)) - ({exclude} if exclude is not None else set())
    return {i for i in ids if before.get(i) != after.get(i)}


def patch_sales(monkeypatch, portal_rows, screen=None):
    n = screen if screen is not None else len(portal_rows)
    monkeypatch.setattr(m, "_export_and_parse", lambda *a, **k: (portal_rows, n))


def sales_rows(invoices=INVOICES, *, balance=NET, payer="ACME Plan B", status="Unpaid",
               collected=D("0.00")):
    return [PortalInvoice(invoice_no=i, balance=balance, payer=payer, status=status,
                        total=balance, collected=collected, location="Clinic A")
            for i in invoices]


def test_validation_writes_nothing_at_all(tmp_path: Path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path)
    patch_sales(monkeypatch, sales_rows())
    work, prov = provenance_for(db, TARGET, sid)

    before = snapshot(db)
    portal = FakePortal()
    readout = sm.validate_smoke_target(portal, cfg(), work, prov)
    after = snapshot(db)

    assert after == before, "the rehearsal modified the DB"
    assert changed_ids(before, after) == set()
    assert portal.click_count == 0
    assert db.conn.execute("select count(*) c from write_claims").fetchone()["c"] == 0
    assert readout.pay_target_count == 1
    assert readout.detail.invoice_no == TARGET


def test_validation_reads_only_the_target(tmp_path: Path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path)
    patch_sales(monkeypatch, sales_rows())
    work, prov = provenance_for(db, TARGET, sid)

    portal = FakePortal()
    sm.validate_smoke_target(portal, cfg(), work, prov)

    assert len(portal.href_queries) == 1
    queried = portal.href_queries[0]
    assert {q.upper() for q in queried} == {TARGET.upper()}, \
        f"queried a non-target invoice: {queried}"
    assert portal.opened == [TARGET], f"opened a non-target detail: {portal.opened}"


def test_target_row_stays_detail_validated_after_rehearsal(tmp_path: Path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path)
    patch_sales(monkeypatch, sales_rows())
    work, prov = provenance_for(db, TARGET, sid)

    sm.validate_smoke_target(FakePortal(), cfg(), work, prov)
    assert db.work_row_by_id(ids[TARGET]).state is WorkState.DETAIL_VALIDATED


def test_armed_run_changes_only_the_target_row(tmp_path: Path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path)
    patch_sales(monkeypatch, sales_rows())
    work, prov = provenance_for(db, TARGET, sid)

    before = snapshot(db)
    portal = FakePortal()
    result = sm.execute_smoke_click(db, portal, cfg(dry_run=False), work, prov)
    after = snapshot(db)

    assert portal.clicked == [TARGET], f"a non-target was clicked: {portal.clicked}"
    assert result["outcome"] == "CONFIRMED"
    assert changed_ids(before, after, exclude=ids[TARGET]) == set(), \
        "a non-target row was modified"
    assert db.work_row_by_id(ids[TARGET]).state is WorkState.CONFIRMED
    assert db.get_claim(TARGET).final_outcome is ClaimOutcome.CONFIRMED
    assert db.conn.execute("select count(*) c from write_claims").fetchone()["c"] == 1


@pytest.mark.parametrize("signal", ["", "Something else"])
def test_armed_unknown_outcome_still_only_touches_the_target(
    tmp_path: Path, monkeypatch, signal: str
) -> None:
    db, sid, ids = seed(tmp_path)
    patch_sales(monkeypatch, sales_rows())
    work, prov = provenance_for(db, TARGET, sid)

    before = snapshot(db)
    portal = FakePortal(signal=signal)
    result = sm.execute_smoke_click(db, portal, cfg(dry_run=False), work, prov)
    after = snapshot(db)

    assert result["outcome"] == "UNKNOWN_OUTCOME"
    assert portal.click_count == 1
    assert changed_ids(before, after, exclude=ids[TARGET]) == set()
    assert db.work_row_by_id(ids[TARGET]).state is WorkState.MANUAL_REVIEW


@pytest.mark.parametrize("kw,expect", [
    (dict(paid={TARGET}),                        "already Paid"),
    (dict(missing_href=True),                    "no detail href"),
    (dict(menu_error=MenuError("no target")),    "menu resolution failed"),
    (dict(detail_total=D("99.00")),              "detail total"),
])
def test_target_failure_aborts_and_touches_nothing(
    tmp_path: Path, monkeypatch, kw, expect: str
) -> None:
    db, sid, ids = seed(tmp_path)
    patch_sales(monkeypatch, sales_rows())
    work, prov = provenance_for(db, TARGET, sid)

    before = snapshot(db)
    portal = FakePortal(**kw)
    with pytest.raises(sm.SmokeValidationError, match=expect):
        sm.validate_smoke_target(portal, cfg(), work, prov)
    after = snapshot(db)

    assert after == before, "the failure path modified the DB"
    assert portal.click_count == 0
    assert db.conn.execute("select count(*) c from write_claims").fetchone()["c"] == 0


def test_no_fallback_candidate_is_ever_selected(tmp_path: Path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path)
    patch_sales(monkeypatch, sales_rows())
    work, prov = provenance_for(db, TARGET, sid)

    before = snapshot(db)
    portal = FakePortal(paid={TARGET})
    result = sm.execute_smoke_click(db, portal, cfg(dry_run=False), work, prov)
    after = snapshot(db)

    assert result["outcome"] == "ALREADY_PAID"
    assert portal.clicked == []
    assert db.conn.execute("select count(*) c from write_claims").fetchone()["c"] == 0
    assert changed_ids(before, after, exclude=work.id) == set(), "a substitute candidate was processed"


def test_sales_verdict_not_approve_ok_aborts(tmp_path: Path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path)
    patch_sales(monkeypatch, sales_rows(collected=D("10.00")))
    work, prov = provenance_for(db, TARGET, sid)

    before = snapshot(db)
    with pytest.raises(sm.SmokeValidationError, match="fresh Sales reconciliation"):
        sm.validate_smoke_target(FakePortal(), cfg(), work, prov)
    assert snapshot(db) == before


def test_missing_from_sales_window_never_widens_the_search(
    tmp_path: Path, monkeypatch
) -> None:
    db, sid, ids = seed(tmp_path)
    patch_sales(monkeypatch, sales_rows([i for i in INVOICES if i != TARGET]))
    work, prov = provenance_for(db, TARGET, sid)

    with pytest.raises(sm.SmokeValidationError, match="appears 0 time"):
        sm.validate_smoke_target(FakePortal(), cfg(), work, prov)


def test_dry_run_refuses_to_arm(tmp_path: Path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path)
    patch_sales(monkeypatch, sales_rows())
    work, prov = provenance_for(db, TARGET, sid)

    before = snapshot(db)
    portal = FakePortal()
    with pytest.raises(sm.SmokeValidationError, match="dry_run"):
        sm.execute_smoke_click(db, portal, cfg(dry_run=True), work, prov)
    assert portal.click_count == 0
    assert snapshot(db) == before


def test_existing_claim_refuses_to_arm(tmp_path: Path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path)
    patch_sales(monkeypatch, sales_rows())
    work, prov = provenance_for(db, TARGET, sid)
    db.insert_claim(work, prov)

    portal = FakePortal()
    with pytest.raises(sm.SmokeValidationError, match="already exists"):
        sm.execute_smoke_click(db, portal, cfg(dry_run=False), work, prov)
    assert portal.click_count == 0


def test_amount_over_cap_refuses_to_arm(tmp_path: Path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path)
    patch_sales(monkeypatch, sales_rows())
    work, prov = provenance_for(db, TARGET, sid)

    portal = FakePortal()
    with pytest.raises(sm.SmokeValidationError, match="exceeds cap"):
        sm.execute_smoke_click(db, portal, cfg(dry_run=False,
                                             max_total_amount_per_run=D("63.99")),
                               work, prov)
    assert portal.click_count == 0


def test_smoke_runner_never_calls_statement_wide_stages() -> None:
    import ast

    root = Path(__file__).resolve().parent.parent
    src = (root / "tools" / "run_smoke.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    called = {
        n.func.id for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    } | {
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    for banned in ("run_once", "reconcile_statement", "detail_shadow_statement",
                   "execute_statement", "finalize_statement"):
        assert banned not in called, f"the smoke runner calls {banned}"

    imported = {
        a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names
    }
    assert "run_once" not in imported
    assert {"validate_smoke_target", "execute_smoke_batch"} <= imported


def test_smoke_module_has_no_row_loop() -> None:
    import ast
    import inspect

    for fn in (sm.validate_smoke_target, sm.execute_smoke_click):
        tree = ast.parse(inspect.getsource(fn).lstrip())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr != "work_rows", (
                    f"{fn.__name__} reads the statement's row list"
                )


def test_target_window_covers_both_observed_offsets(tmp_path: Path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path)
    seen: list[tuple] = []

    def spy(portal, cfg_, lo, hi, *, all_invoice_states):
        seen.append((lo, hi, all_invoice_states))
        return sales_rows(), len(sales_rows())

    monkeypatch.setattr(m, "_export_and_parse", spy)
    work, prov = provenance_for(db, TARGET, sid)
    sm.validate_smoke_target(FakePortal(), cfg(), work, prov)

    assert len(seen) == 1, "the lookup window must be opened exactly once"
    lo, hi, all_states = seen[0]
    assert lo <= ROW_DATE - timedelta(days=1), f"the window misses row_date-1: {lo}..{hi}"
    assert hi >= ROW_DATE, f"the window misses row_date itself: {lo}..{hi}"
    assert all_states is False


def test_window_never_widens_after_a_miss(tmp_path: Path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path)
    calls = []

    def spy(portal, cfg_, lo, hi, *, all_invoice_states):
        calls.append((lo, hi))
        return [], 0

    monkeypatch.setattr(m, "_export_and_parse", spy)
    work, prov = provenance_for(db, TARGET, sid)
    with pytest.raises(sm.SmokeValidationError):
        sm.validate_smoke_target(FakePortal(), cfg(), work, prov)
    assert len(calls) == 1, f"queried {len(calls)} times; the window was widened"


def test_lowercase_eft_identifier_still_finds_its_href(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "d.db"); db.migrate()
    lower = "290018-b01"
    rows = [EftRow(lower, ROW_DATE, "r", "d", NET, D("0.00"), D("0.00"), NET)]
    sid = seed_authorized_statement(db, rows)
    w = db.work_rows(sid)[0]
    db.update_work(w.id, state=WorkState.DETAIL_VALIDATED.value,
                   verdict=Verdict.APPROVE_OK.value, portal_invoice_id="j290018-B01")
    work, prov = provenance_for(db, lower, sid)

    patch_sales(monkeypatch, [PortalInvoice(invoice_no="290018-B01", balance=NET,
                                          payer="ACME Plan B", status="Unpaid",
                                          total=NET, collected=D("0.00"),
                                          location="Clinic A")])

    portal = FakePortal()
    portal.dom_invoices = {"290018-B01"}
    readout = sm.validate_smoke_target(portal, cfg(), work, prov)
    assert readout.href == "#invoices/290018-B01"
    assert db.work_row_by_id(w.id).invoice_no == lower


def test_pane_switched_to_another_invoice_aborts_before_the_click(
    tmp_path: Path, monkeypatch
) -> None:
    db, sid, ids = seed(tmp_path)
    patch_sales(monkeypatch, sales_rows())
    work, prov = provenance_for(db, TARGET, sid)

    before = snapshot(db)
    portal = FakePortal(pane_invoice="290022-B01")
    with pytest.raises(sm.SmokeValidationError, match="detail pane shows"):
        sm.execute_smoke_click(db, portal, cfg(dry_run=False), work, prov)

    assert portal.click_count == 0, "clicked while a different invoice was open"
    assert snapshot(db) == before
    assert db.conn.execute("select count(*) c from write_claims").fetchone()["c"] == 0


def test_claim_ledger_is_case_insensitive(tmp_path: Path) -> None:
    import sqlite3

    db, sid, ids = seed(tmp_path)
    work, prov = provenance_for(db, TARGET, sid)
    db.insert_claim(work, prov)

    assert db.get_claim(TARGET.lower()) is not None
    assert db.get_claim(TARGET.upper()) is not None

    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO write_claims (invoice_no, statement_id, claimed_amount,"
            " claimed_at) VALUES (?,?,?,?)",
            (TARGET.lower(), sid, "64.00", "2037-07-18T00:00:00+00:00"),
        )
        db.conn.commit()
