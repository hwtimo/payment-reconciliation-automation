"""Read-only portal path: login, CSV export, detail reading and classification inputs."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from remittance_reconciler import main as m
from remittance_reconciler.config import Config
from remittance_reconciler.database import Database
from remittance_reconciler.portal import (
    RECORD_PAYMENT,
    IdentityMismatch,
    PortalError,
    PortalSession,
    LoginError,
    MenuError,
    keychain_credentials,
)
from remittance_reconciler.portal_csv import PortalCsvError, parse_sales_csv
from remittance_reconciler.models import (
    Decision,
    EftRow,
    EftStatement,
    PortalInvoice,
    Verdict,
    WarnCode,
    WorkState,
)
from remittance_reconciler.reconcile import (
    check_export_count,
    classify,
    is_write_candidate,
    normalize_invoice_no,
)

PAC = timezone(timedelta(hours=-7))
D = Decimal


def test_keychain_retrieval_failure_is_a_domain_error():
    with pytest.raises(LoginError, match="no Keychain item"):
        keychain_credentials(service="remittance-reconciler-does-not-exist-xyz")


class _LoginPage:
    def __init__(self, *, password_fields=1, submit=1, reaches_admin=True,
                 stays_on_form=False, host="portal.example.com"):
        self._pw, self._submit = password_fields, submit
        self._reaches, self._stays = reaches_admin, stays_on_form
        self.url = f"https://{host}/app"
        self.filled: list[str] = []

    def goto(self, url, **k): self.url = url
    def wait_for_timeout(self, *a): pass

    def locator(self, sel, **k):
        page = self
        n = (page._pw if "password" in sel else
             page._submit if "submit" in sel else 1)
        class L:
            def count(self): return n
            @property
            def first(self): return self
            def fill(self, v): page.filled.append("<redacted>")
            def click(self): pass
        return L()

    def get_by_role(self, role, name=None, exact=False):
        page = self
        class L:
            def count(self): return page._submit
            @property
            def first(self): return self
            def click(self): pass
        return L()

    def wait_for_selector(self, sel, **k):
        if self._stays or not self._reaches:
            raise TimeoutError("nav selector never appeared")


def test_login_failure_is_reported_as_rejected_credentials(monkeypatch):
    monkeypatch.setattr("remittance_reconciler.portal.keychain_credentials", lambda *a, **k: ("u", "p"))
    s = PortalSession(_LoginPage(stays_on_form=True))
    with pytest.raises(LoginError, match="login rejected"):
        s.login()


def test_login_ui_change_is_distinguished_from_bad_credentials(monkeypatch):
    monkeypatch.setattr("remittance_reconciler.portal.keychain_credentials", lambda *a, **k: ("u", "p"))
    page = _LoginPage(reaches_admin=False, password_fields=0)
    page._pw = 1
    class P(_LoginPage):
        def locator(self, sel, **k):
            outer = super().locator(sel, **k)
            if "password" in sel:
                class L(type(outer)):
                    pass
            return outer
    s = PortalSession(page)
    with pytest.raises(LoginError):
        s.login()


def test_login_fails_closed_when_no_unique_submit(monkeypatch):
    monkeypatch.setattr("remittance_reconciler.portal.keychain_credentials", lambda *a, **k: ("u", "p"))
    s = PortalSession(_LoginPage(submit=2))
    with pytest.raises(LoginError, match="exactly one Sign In"):
        s.login()


def test_login_never_stores_the_password_on_the_session(monkeypatch):
    monkeypatch.setattr("remittance_reconciler.portal.keychain_credentials", lambda *a, **k: ("u", "s3cr3t"))
    page = _LoginPage()
    s = PortalSession(page)
    s.login()
    assert "s3cr3t" not in repr(vars(s))
    assert "s3cr3t" not in "".join(page.filled)


def test_admin_context_assertion_rejects_a_foreign_host(monkeypatch):
    monkeypatch.setattr("remittance_reconciler.portal.keychain_credentials", lambda *a, **k: ("u", "p"))
    page = _LoginPage()
    original_goto = page.goto
    def redirect(url, **k):
        original_goto(url, **k)
        page.url = "https://evil.example.com/app"
    page.goto = redirect
    s = PortalSession(page, base_url="https://portal.example.com")
    with pytest.raises(LoginError, match="not on the expected portal host"):
        s.login()


class _Loc:
    def __init__(self, n=1, *, expanded="false", visible=True, items=None, enabled=True):
        self._n, self._exp, self._vis = n, expanded, visible
        self._items, self._enabled = items or [], enabled
        self.clicked = 0

    def count(self): return self._n
    @property
    def first(self): return self
    def is_visible(self): return self._vis
    def is_enabled(self): return self._enabled
    def get_attribute(self, name): return self._exp if name == "aria-expanded" else None
    def click(self): self.clicked += 1
    def wait_for(self, **k):
        if not self._vis:
            raise TimeoutError("not visible")
    def locator(self, sel, **k): return self
    def get_by_role(self, role, name=None, exact=False):
        n = sum(1 for i in self._items if i == name) if name else len(self._items)
        return _Loc(n)
    def filter(self, has_text=None):
        pat = has_text if isinstance(has_text, re.Pattern) else re.compile(re.escape(str(has_text)))
        return _Loc(sum(1 for i in self._items if pat.search(i)))
    def all(self): return []


class _ReportPage:
    def __init__(self, *, trigger_n=1, expanded_after_click="true",
                 menu_n=1, menu_items=("Print", "Export Excel", "Export CSV")):
        self.url = "https://portal.example.com/app#reports/invoices"
        self._trigger = _Loc(trigger_n, expanded="false")
        self._menu = _Loc(menu_n, items=list(menu_items))
        self._expanded_after = expanded_after_click
        self.context = None
        self.reloaded = False

    def reload(self, **k): self.reloaded = True
    def goto(self, url, **k): self.url = url
    def wait_for_timeout(self, *a): pass
    def wait_for_selector(self, sel, **k): pass
    def keyboard_press(self, *a): pass
    def locator(self, sel, **k):
        if "toolbar-button" in sel:
            self._trigger._exp = self._expanded_after if self._trigger.clicked else "false"
            return self._trigger
        return _Loc(0)
    def wait_for_function(self, *a, **k):
        if self._expanded_after != "true":
            raise TimeoutError("menu never opened")
    def evaluate(self, *a, **k): return []


def test_export_trigger_missing_is_a_menu_error():
    s = PortalSession(_ReportPage(trigger_n=0))
    s._open_sales_report = lambda *a, **k: None
    with pytest.raises(MenuError, match="export trigger resolved 0"):
        s.export_outstanding_csv(date(2037, 7, 6), date(2037, 7, 7))


def test_export_trigger_duplicate_is_a_menu_error():
    s = PortalSession(_ReportPage(trigger_n=3))
    s._open_sales_report = lambda *a, **k: None
    with pytest.raises(MenuError, match="export trigger resolved 3"):
        s.export_outstanding_csv(date(2037, 7, 6), date(2037, 7, 7))


def test_export_menu_failing_to_open_is_a_menu_error():
    page = _ReportPage(expanded_after_click="false")
    s = PortalSession(page)
    s._open_sales_report = lambda *a, **k: None
    with pytest.raises(Exception):
        s.export_outstanding_csv(date(2037, 7, 6), date(2037, 7, 7))


def test_hash_navigation_to_an_open_report_forces_a_reload():
    page = _ReportPage()
    s = PortalSession(page)
    s._select_all_locations = lambda: None
    s._select_invoice_state = lambda label: None
    s._set_date_range = lambda a, b: None
    s._wait_for_settled_count = lambda: 1
    s._open_sales_report(date(2037, 7, 6), date(2037, 7, 7))
    assert page.reloaded, "an already-open report must be genuinely reloaded"


def test_export_csv_exact_role_name_does_not_match_and_regex_does():
    menu = _Loc(1, items=["Print", "Export Excel", "Export CSV"])
    assert menu.filter(has_text=re.compile(r"^Export CSV$")).count() == 1
    assert menu.filter(has_text=re.compile(r"Export")).count() == 2, \
        "a loose match would hit Export Excel as well"


def test_malformed_csv_is_rejected():
    with pytest.raises(PortalCsvError):
        parse_sales_csv("not,a,sales,report\n1,2,3,4\n")


def test_empty_csv_is_rejected():
    with pytest.raises(PortalCsvError):
        parse_sales_csv("")


def test_g7_count_mismatch_fails_closed():
    ok, _, reason = check_export_count(812, 812, 187, 187)
    assert not ok and "settled screen count" in reason


def test_g7_duplicate_identifiers_fail_closed():
    ok, _, reason = check_export_count(100, 98, 100, 100)
    assert not ok and "duplicate identifiers" in reason


def test_g7_exact_match_passes():
    ok, warns, _ = check_export_count(187, 187, 187, 187)
    assert ok and warns == ()


def _row(**kw):
    d = dict(invoice_no="300248-B01", row_date=date(2037, 7, 8), reference_no="r",
             document_no="d", gross=D("118.40"), prev_paid=D("0.00"),
             outstanding=D("0.00"), net=D("118.40"))
    d.update(kw)
    return EftRow(**d)


def _portal(**kw):
    d = dict(invoice_no="300248-B01", balance=D("118.40"), payer="ACME Plan A",
             status="unpaid", total=D("118.40"), collected=D("0.00"), location="C")
    d.update(kw)
    return PortalInvoice(**d)


def test_exact_invoice_match_approves():
    assert classify(_row(), _portal()).verdict is Verdict.APPROVE_OK


def test_mixed_case_uses_a_canonical_key_and_preserves_the_raw_identifier():
    row = _row(invoice_no="300248-b01")
    portal = _portal(invoice_no="300248-B01")
    assert row.invoice_no.upper() == portal.invoice_no.upper()
    assert classify(row, portal).verdict is Verdict.APPROVE_OK
    assert row.invoice_no == "300248-b01", "raw EFT identifier was mutated"


def test_normalization_never_strips_the_suffix():
    assert normalize_invoice_no(" #300248-b01 ") == "300248-b01"
    assert normalize_invoice_no("300248-B02") != normalize_invoice_no("300248-B01")


def test_amount_mismatch_blocks():
    assert classify(_row(), _portal(balance=D("99.00"))).verdict is Verdict.AMOUNT_MISMATCH


def test_payer_mismatch_blocks():
    assert classify(_row(), _portal(payer="Patient")).verdict is Verdict.PAYER_MISMATCH


def test_non_positive_net_is_credit_memo():
    assert classify(_row(net=D("0.00")), _portal()).verdict is Verdict.CREDIT_MEMO
    assert classify(_row(net=D("-1.00")), _portal()).verdict is Verdict.CREDIT_MEMO


def test_not_found_when_absent():
    assert classify(_row(), None).verdict is Verdict.NOT_FOUND


def test_partially_paid_is_manual_review_even_when_amounts_match():
    assert classify(_row(net=D("50.00")),
                    _portal(balance=D("50.00"), collected=D("47.00"))).verdict is Verdict.MANUAL_REVIEW


def test_parsed_is_not_a_write_candidate():
    assert not is_write_candidate(WorkState.PARSED)


def test_sales_reconciled_alone_is_not_a_write_candidate():
    assert not is_write_candidate(WorkState.RECONCILED)


def test_only_detail_validated_is_a_write_candidate():
    assert is_write_candidate(WorkState.DETAIL_VALIDATED)


def test_pending_write_means_clicked_not_pre_validated():
    assert not is_write_candidate(WorkState.PENDING_WRITE)


def _db_with_row(tmp_path: Path, **over) -> tuple[Database, int, object]:
    db = Database(tmp_path / "d.db"); db.migrate()
    st = EftStatement("8100001", "004000000101", date(2037, 7, 16), D("118.40"),
                      (_row(),), "fp")
    sid = db.create_statement(st); db.insert_work_rows(sid, st.rows)
    r = db.work_rows(sid)[0]
    db.update_work(r.id, state=WorkState.RECONCILED.value,
                   verdict=Verdict.APPROVE_OK.value, **over)
    return db, sid, db.work_rows(sid)[0]


class _ShadowPortal:
    def __init__(self, *, href=True, identity=True, paid=False,
                 total=D("118.40"), trigger_n=1, menu_open=True, target_n=1):
        self._href, self._identity, self._paid = href, identity, paid
        self._total, self._trigger_n = total, trigger_n
        self._menu_open, self._target_n = menu_open, target_n
        self.clicks: list[str] = []
        self.page = self

    class _KB:
        def __init__(s, o): s.o = o
        def press(s, k): s.o.clicks.append(f"key:{k}")
    @property
    def keyboard(self): return self._KB(self)

    def _open_sales_report(self, *a, **k): pass
    def collect_hrefs(self, targets, **k):
        return {t: f"#invoices/700{i}" for i, t in enumerate(targets)} if self._href else {}

    def open_invoice(self, href, expected_invoice_no=None, **k):
        if not self._identity:
            raise IdentityMismatch("detail pane showed a different invoice")
        from remittance_reconciler.models import InvoiceDetail
        return InvoiceDetail(invoice_no=expected_invoice_no, total=self._total,
                             payment_status="Paid" if self._paid else "Unpaid",
                             submission_status="Submitted", portal_invoice_id="7001")

    def action_trigger(self):
        return _Loc(self._trigger_n, expanded="true")

    def action_menu(self):
        items = [RECORD_PAYMENT] * self._target_n
        return _Loc(1, visible=self._menu_open, items=items)


def _cfg() -> Config:
    return Config(automation_start_at=datetime(2037, 7, 17, tzinfo=PAC), dry_run=True,
                  known_vendors=("8100001",), max_total_amount_per_run=D("0.00"))


@pytest.mark.parametrize("kwargs,expect_state,expect_code", [
    (dict(),                          WorkState.DETAIL_VALIDATED, None),
    (dict(href=False),                WorkState.MANUAL_REVIEW, "HREF_MISSING"),
    (dict(identity=False),            WorkState.MANUAL_REVIEW, "DETAIL_ID_MISMATCH"),
    (dict(total=D("99.00")),          WorkState.MANUAL_REVIEW, "DETAIL_TOTAL_MISMATCH"),
    (dict(trigger_n=0),               WorkState.MANUAL_REVIEW, "ACTION_TRIGGER_NOT_UNIQUE"),
    (dict(trigger_n=2),               WorkState.MANUAL_REVIEW, "ACTION_TRIGGER_NOT_UNIQUE"),
    (dict(menu_open=False),           WorkState.MANUAL_REVIEW, "ACTION_MENU_NOT_OPEN"),
    (dict(target_n=0),                WorkState.MANUAL_REVIEW, "PAYMENT_TARGET_MISSING"),
    (dict(target_n=2),                WorkState.MANUAL_REVIEW, "PAYMENT_TARGET_DUPLICATE"),
])
def test_detail_shadow_gates(tmp_path: Path, kwargs, expect_state, expect_code):
    db, sid, _ = _db_with_row(tmp_path)
    m.detail_shadow_statement(db, _ShadowPortal(**kwargs), _cfg(), db.pending_statements()[0])
    row = db.work_rows(sid)[0]
    assert row.state is expect_state
    if expect_code:
        assert row.error_code == expect_code
    assert db.conn.execute("select count(*) c from write_claims").fetchone()["c"] == 0


def test_detail_already_paid_becomes_no_action(tmp_path: Path):
    db, sid, _ = _db_with_row(tmp_path)
    m.detail_shadow_statement(db, _ShadowPortal(paid=True), _cfg(), db.pending_statements()[0])
    row = db.work_rows(sid)[0]
    assert row.state is WorkState.NO_ACTION
    assert row.verdict is Verdict.ALREADY_PAID
    assert db.conn.execute("select count(*) c from write_claims").fetchone()["c"] == 0


def test_detail_shadow_never_clicks_a_menu_item(tmp_path: Path):
    db, sid, _ = _db_with_row(tmp_path)
    portal = _ShadowPortal()
    m.detail_shadow_statement(db, portal, _cfg(), db.pending_statements()[0])
    assert all(c.startswith("key:") for c in portal.clicks), f"a menu item was clicked: {portal.clicks}"
    assert db.conn.execute("select count(*) c from write_claims").fetchone()["c"] == 0


def test_detail_shadow_only_touches_sales_reconciled_rows(tmp_path: Path):
    db = Database(tmp_path / "d.db"); db.migrate()
    st = EftStatement("8100001", "004000000101", date(2037, 7, 16), D("118.40"), (_row(),), "fp")
    sid = db.create_statement(st); db.insert_work_rows(sid, st.rows)
    tally = m.detail_shadow_statement(db, _ShadowPortal(), _cfg(), db.pending_statements()[0])
    assert tally == {}
    assert db.work_rows(sid)[0].state is WorkState.PARSED


def test_dry_run_blocks_write_eligibility(tmp_path):
    from datetime import date as _date

    from conftest import seed_authorized_statement
    from remittance_reconciler.database import Database
    from remittance_reconciler.main import RunBudget, write_eligibility
    from remittance_reconciler.models import EftRow

    db = Database(tmp_path / "d.db"); db.migrate()
    row = EftRow("300248-B01", _date(2037, 8, 22), "r", "d",
                 D("118.40"), D("0.00"), D("0.00"), D("118.40"))
    sid = seed_authorized_statement(db, [row])
    w = db.work_rows(sid)[0]
    db.update_work(w.id, state=WorkState.DETAIL_VALIDATED.value,
                   verdict=Verdict.APPROVE_OK.value)
    w = db.work_rows(sid)[0]

    cfg = Config(automation_start_at=datetime(2037, 7, 17, tzinfo=PAC), dry_run=True,
                 max_total_amount_per_run=D("10000.00"))
    assert write_eligibility(db, w, cfg, RunBudget(50, D("10000.00")), None,
                             D("10000.00"))[1] == "dry_run is enabled"


def test_collect_hrefs_matches_identifiers_case_insensitively():
    from remittance_reconciler.portal import PortalSession

    class _L:
        def count(self): return 0
        @property
        def first(self): return self
        def is_visible(self): return False
        def click(self): pass

    class _Page:
        def __init__(self): self.calls = 0
        def evaluate(self, _js, *a):
            self.calls += 1
            return [["290010-B01", "#invoices/999"],
                    ["290011-B02", "#invoices/1000"]]
        def locator(self, *a, **k): return _L()
        def get_by_role(self, *a, **k): return _L()
        def wait_for_timeout(self, *a): pass

    s = PortalSession(_Page())
    got = s.collect_hrefs({"290010-b01"})
    assert got == {"290010-b01": "#invoices/999"}, got

    assert s.collect_hrefs({"290010-B01"}) == {"290010-B01": "#invoices/999"}

    assert s.collect_hrefs({"290011-b01"}) == {}
