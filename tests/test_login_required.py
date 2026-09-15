"""Portal authentication: session reuse, challenge detection, LOGIN_REQUIRED handling and reporting."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from conftest import seed_authorized_statement
from remittance_reconciler import main as m
from remittance_reconciler.config import Config
from remittance_reconciler.database import (
    RUN_STATUS_FAILED,
    RUN_STATUS_LOGIN_REQUIRED,
    RUN_STATUS_SUCCESS,
    Database,
)
from remittance_reconciler.portal import (
    CHALLENGE_CONTAINER_SELECTORS,
    CHALLENGE_IFRAME_SELECTORS,
    CHALLENGE_INPUT_SELECTORS,
    INTERACTIVE_CHALLENGE_TEXT,
    LOGIN_FORM_SELECTOR,
    LOGIN_SUCCESS_NAV,
    PortalSession,
    LoginError,
    LoginRequired,
)
from remittance_reconciler.models import EftRow, Verdict, WorkState

PAC = timezone(timedelta(hours=-7))
NET = D("64.00")


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


class FakeElement:
    def __init__(self, *selectors: str, visible: bool = True,
                 w: int = 300, h: int = 78, appears_at_ms: int = 0):
        self.selectors = set(selectors)
        self.visible = visible
        self.w = w
        self.h = h
        self.appears_at_ms = appears_at_ms

    def box(self) -> dict[str, int]:
        return {"visible": True, "w": self.w, "h": self.h}


def recaptcha_badge() -> FakeElement:
    return FakeElement('iframe[src*="recaptcha"]', visible=True, w=256, h=60)


def invisible_sitekey_widget() -> FakeElement:
    return FakeElement(".g-recaptcha", "[data-sitekey]", visible=False, w=705, h=0)


def hidden_challenge_container() -> FakeElement:
    return FakeElement(".login-challenge", "[data-sitekey]", visible=False, w=0, h=0)


def normal_login_page_elements() -> list[FakeElement]:
    return [recaptcha_badge(), invisible_sitekey_widget(), hidden_challenge_container()]


class FakePage:
    def __init__(self, *, authed: bool = False, challenge_selector: str | None = None,
                 body_text: str = "Sign in",
                 elements: Sequence[FakeElement] = (),
                 login_form: bool | None = None,
                 authed_at_ms: int = 0):
        self.body_text = body_text
        self.gotos: list[str] = []
        self.now_ms = 0
        self.elements: list[FakeElement] = list(elements)
        self._nav = FakeElement(LOGIN_SUCCESS_NAV, appears_at_ms=authed_at_ms)
        self._form = FakeElement(LOGIN_FORM_SELECTOR)
        self.authed = authed
        self.login_form = (not authed) if login_form is None else login_form
        if challenge_selector is not None:
            self.elements.append(FakeElement(challenge_selector))

    @property
    def authed(self) -> bool:
        return self._nav in self.elements

    @authed.setter
    def authed(self, value: bool) -> None:
        if value and self._nav not in self.elements:
            self.elements.append(self._nav)
        elif not value and self._nav in self.elements:
            self.elements.remove(self._nav)

    @property
    def login_form(self) -> bool:
        return self._form in self.elements

    @login_form.setter
    def login_form(self, value: bool) -> None:
        if value and self._form not in self.elements:
            self.elements.append(self._form)
        elif not value and self._form in self.elements:
            self.elements.remove(self._form)

    def goto(self, url, **k):
        self.gotos.append(url)

    def wait_for_timeout(self, ms):
        self.now_ms += int(ms)

    def wait_for_selector(self, sel, timeout=None):
        budget = 5000 if timeout is None else int(timeout)
        deadline = self.now_ms + budget
        while True:
            if self._matches(sel):
                return object()
            if self.now_ms >= deadline:
                raise TimeoutError(f"no {sel}")
            self.wait_for_timeout(50)

    def locator(self, sel):
        return _Locator(self, sel)

    def inner_text(self, _sel):
        return self.body_text

    def _live(self, sel: str) -> list[FakeElement]:
        return [e for e in self.elements
                if sel in e.selectors and self.now_ms >= e.appears_at_ms]

    def _matches(self, sel: str) -> bool:
        return any(e.visible for e in self._live(sel))


class _Locator:
    def __init__(self, page: FakePage, sel: str):
        self._page = page
        self._sel = sel

    def count(self) -> int:
        return len(self._page._live(self._sel))

    def evaluate_all(self, _js):
        return [e.box() for e in self._page._live(self._sel) if e.visible]


class RecordingPortal(PortalSession):
    def __init__(self, page, *, login_effect=None, becomes_authed: bool = True,
                 challenge_after_login: str | None = None):
        super().__init__(page)
        self.login_calls = 0
        self._effect = login_effect
        self._becomes_authed = becomes_authed
        self._challenge_after = challenge_after_login

    def login(self, service=None, timeout_ms=30000):
        self.login_calls += 1
        if self._effect:
            raise self._effect
        if self._challenge_after:
            self.page.elements.append(FakeElement(self._challenge_after))
        self.page.authed = self._becomes_authed
        self.page.login_form = not self._becomes_authed


def test_live_session_is_reused_without_logging_in() -> None:
    portal = RecordingPortal(FakePage(authed=True))
    assert portal.ensure_authenticated() == "reused"
    assert portal.login_calls == 0, "logged in despite a live session"


def test_expired_session_triggers_exactly_one_login() -> None:
    portal = RecordingPortal(FakePage(authed=False))
    assert portal.ensure_authenticated() == "logged-in"
    assert portal.login_calls == 1


def test_login_is_never_retried_on_failure() -> None:
    portal = RecordingPortal(FakePage(authed=False), login_effect=LoginError("rejected"))
    with pytest.raises(LoginError):
        portal.ensure_authenticated()
    assert portal.login_calls == 1


@pytest.mark.parametrize("selector", [
    *CHALLENGE_IFRAME_SELECTORS,
    *CHALLENGE_CONTAINER_SELECTORS,
    *CHALLENGE_INPUT_SELECTORS,
])
def test_every_challenge_selector_stops_before_credentials(selector: str) -> None:
    portal = RecordingPortal(FakePage(authed=False, elements=normal_login_page_elements(),
                                  challenge_selector=selector))
    with pytest.raises(LoginRequired) as ei:
        portal.ensure_authenticated()
    assert selector in ei.value.marker
    assert portal.login_calls == 0, "attempted login while a challenge was showing"


@pytest.mark.parametrize("phrase", INTERACTIVE_CHALLENGE_TEXT)
def test_every_challenge_phrase_stops_before_credentials(phrase: str) -> None:
    portal = RecordingPortal(FakePage(authed=False, body_text=f"Please {phrase} to continue"))
    with pytest.raises(LoginRequired):
        portal.ensure_authenticated()
    assert portal.login_calls == 0


def test_challenge_appearing_after_submit_is_login_required_not_error() -> None:
    page = FakePage(authed=False)

    def _fail_then_challenge(*a, **k):
        page.elements.append(FakeElement('iframe[src*="recaptcha"][src*="bframe"]'))
        raise LoginError("login rejected: still on the sign-in form")

    portal = RecordingPortal(page)
    portal.login = lambda *a, **k: _fail_then_challenge()  # type: ignore[assignment]
    with pytest.raises(LoginRequired):
        portal.ensure_authenticated()


def test_challenge_after_a_successful_login_still_blocks() -> None:
    portal = RecordingPortal(FakePage(authed=False), challenge_after_login=".cf-turnstile")
    with pytest.raises(LoginRequired):
        portal.ensure_authenticated()
    assert portal.login_calls == 1


def test_detection_never_interacts_with_the_challenge() -> None:
    import ast
    import inspect

    src = inspect.getsource(PortalSession.detect_interactive_challenge)
    tree = ast.parse(src.lstrip())
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for banned in ("click", "fill", "type", "check", "press", "solve", "dispatch_event"):
        assert banned not in called, f"challenge detection calls {banned}"


class TrackingPortal:
    def __init__(self, marker: str = "selector .g-recaptcha"):
        self.marker = marker
        self.calls: list[str] = []

    def ensure_authenticated(self, *a, **k):
        self.calls.append("ensure_authenticated")
        raise LoginRequired(self.marker)

    def _open_sales_report(self, *a, **k): self.calls.append("_open_sales_report")
    def visible_invoice_count(self, *a, **k): self.calls.append("visible_invoice_count"); return 0
    def export_outstanding_csv(self, *a, **k): self.calls.append("export_outstanding_csv")
    def collect_hrefs(self, *a, **k): self.calls.append("collect_hrefs"); return {}
    def open_invoice(self, *a, **k): self.calls.append("open_invoice")
    def action_trigger(self, *a, **k): self.calls.append("action_trigger")
    def action_menu(self, *a, **k): self.calls.append("action_menu")
    def resolve_record_payment(self, *a, **k): self.calls.append("resolve_record_payment")
    def click_record_payment(self, *a, **k): self.calls.append("click_record_payment")


class NullGmail:
    def __init__(self): self.sent: list[tuple[str, str]] = []
    def fetch_eft_messages(self, after): return iter(())
    def send(self, to, subject, html_body): self.sent.append((subject, html_body))


def _seed(tmp_path: Path):
    db = Database(tmp_path / "d.db"); db.migrate()
    rows = [EftRow("290018-B01", date(2037, 7, 7), "r", "d", NET, D("0.00"), D("0.00"), NET)]
    sid = seed_authorized_statement(db, rows)
    for w in db.work_rows(sid):
        db.update_work(w.id, state=WorkState.DETAIL_VALIDATED.value,
                       verdict=Verdict.APPROVE_OK.value,
                       portal_href="#invoices/4300003", portal_invoice_id="4300003")
    return db, sid


def test_login_required_never_reaches_any_portal_stage(tmp_path: Path) -> None:
    db, sid = _seed(tmp_path)
    portal = TrackingPortal()
    stats = m.run_once(cfg(), db, NullGmail(), portal=portal)

    assert portal.calls == ["ensure_authenticated"], (
        f"portal stages ran after an authentication failure: {portal.calls}"
    )
    assert stats.status == RUN_STATUS_LOGIN_REQUIRED
    assert stats.status != RUN_STATUS_SUCCESS


def test_login_required_creates_no_write_claim(tmp_path: Path) -> None:
    db, sid = _seed(tmp_path)
    m.run_once(cfg(), db, NullGmail(), portal=TrackingPortal())
    assert db.conn.execute("select count(*) c from write_claims").fetchone()["c"] == 0


def test_login_required_leaves_every_work_row_untouched(tmp_path: Path) -> None:
    db, sid = _seed(tmp_path)
    before = {r["id"]: tuple(r) for r in
              db.conn.execute("SELECT * FROM invoice_work ORDER BY id")}
    m.run_once(cfg(), db, NullGmail(), portal=TrackingPortal())
    after = {r["id"]: tuple(r) for r in
             db.conn.execute("SELECT * FROM invoice_work ORDER BY id")}
    assert after == before


def test_login_required_leaves_the_statement_pending(tmp_path: Path) -> None:
    db, sid = _seed(tmp_path)
    m.run_once(cfg(), db, NullGmail(), portal=TrackingPortal())
    assert db.get_statement(sid).state.value == "PENDING"
    assert [s.id for s in db.pending_statements()] == [sid]


def test_login_required_does_not_bump_statement_attempts(tmp_path: Path) -> None:
    db, sid = _seed(tmp_path)
    for _ in range(10):
        m.run_once(cfg(), db, NullGmail(), portal=TrackingPortal())
    st = db.get_statement(sid)
    assert st.attempt_count == 0, f"attempt_count rose to {st.attempt_count}"
    assert st.state.value == "PENDING"


def test_login_required_is_not_reported_as_a_failure(tmp_path: Path) -> None:
    db, sid = _seed(tmp_path)
    stats = m.run_once(cfg(), db, NullGmail(), portal=TrackingPortal())
    assert stats.status == RUN_STATUS_LOGIN_REQUIRED
    assert stats.status != RUN_STATUS_FAILED


def test_login_required_notifies_staff_with_actionable_instructions(tmp_path: Path) -> None:
    db, sid = _seed(tmp_path)
    gmail = NullGmail()
    m.run_once(cfg(), db, gmail, portal=TrackingPortal())

    payload = db.conn.execute(
        "SELECT payload, kind FROM report_outbox ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert payload["kind"] == "LOGIN_REQUIRED"
    body = payload["payload"]
    assert "MANUAL PORTAL LOGIN REQUIRED" in body
    assert "No invoices were paid" in body
    assert "tools/portal_login.py" in body
    assert "g-recaptcha" in body.lower()


def test_login_required_run_is_recorded_in_run_log(tmp_path: Path) -> None:
    db, sid = _seed(tmp_path)
    m.run_once(cfg(), db, NullGmail(), portal=TrackingPortal())
    row = db.conn.execute(
        "SELECT status, error FROM run_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == RUN_STATUS_LOGIN_REQUIRED
    assert "LOGIN_REQUIRED" in (row["error"] or "")


def test_authentication_precedes_every_portal_stage_in_source() -> None:
    import ast
    import inspect

    src = inspect.getsource(m.run_once)
    body = ast.unparse(ast.parse(src.lstrip()))
    i_auth = body.index("ensure_authenticated")
    for stage in ("reconcile_statement(", "detail_shadow_statement(",
                  "execute_statement("):
        assert i_auth < body.index(stage), f"{stage} comes before authentication"


def test_login_helper_performs_no_financial_action() -> None:
    import ast

    root = Path(__file__).resolve().parent.parent
    src = ast.unparse(ast.parse((root / "tools" / "portal_login.py").read_text()))
    for banned in ("insert_claim", "click_record_payment", "resolve_record_payment",
                   "run_once", "execute_smoke_click", "collect_hrefs",
                   "export_outstanding_csv"):
        assert banned not in src, f"the login helper uses {banned}"


def test_persistent_profile_is_created_private() -> None:
    import ast
    import inspect

    from remittance_reconciler.portal import launch_persistent_session

    src = ast.unparse(ast.parse(inspect.getsource(launch_persistent_session).lstrip()))
    assert "chmod(448)" in src or "chmod(0o700)" in src or "0o700" in src, (
        "the profile directory is not set to 0700"
    )
    assert "launch_persistent_context" in src, "does not use a persistent context"


def test_profile_directory_is_gitignored() -> None:
    import subprocess

    root = Path(__file__).resolve().parent.parent
    inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root, capture_output=True)
    if inside.returncode != 0:
        pytest.skip("requires a git work tree; run `git init` first")
    probe = root / "secrets" / "portal-profile" / "Cookies"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.touch()
    try:
        r = subprocess.run(["git", "check-ignore", "-q", str(probe)],
                           cwd=root, capture_output=True)
        assert r.returncode == 0, "the profile is not covered by .gitignore"
    finally:
        probe.unlink(missing_ok=True)


def test_launch_agent_runs_with_portal() -> None:
    import plistlib

    root = Path(__file__).resolve().parent.parent
    d = plistlib.loads((root / "com.example.remittance-reconciler.plist").read_bytes())
    assert "--with-portal" in d["ProgramArguments"]
    assert d["StartCalendarInterval"]["Hour"] == 3


def test_entrypoint_exit_code_distinguishes_login_required() -> None:
    import ast
    import inspect

    src = ast.unparse(ast.parse(inspect.getsource(m.main).lstrip()))
    assert "RUN_STATUS_LOGIN_REQUIRED" in src
    assert "return 2" in src


def test_headless_launch_strips_the_headless_ua_token() -> None:
    from remittance_reconciler.portal import _headless_safe_user_agent

    class _FakeProbePage:
        def evaluate(self, _js):
            return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "HeadlessChrome/151.0.7922.34 Safari/537.36")

    class _FakeProbeBrowser:
        def new_page(self): return _FakeProbePage()
        def close(self): pass

    class _FakeChromium:
        def launch(self, **k): return _FakeProbeBrowser()

    class _FakePw:
        chromium = _FakeChromium()

    ua = _headless_safe_user_agent(_FakePw())
    assert ua is not None
    assert "HeadlessChrome" not in ua
    assert "Chrome/151.0.7922.34" in ua
    assert "Macintosh; Intel Mac OS X 10_15_7" in ua


def test_non_headless_ua_is_left_alone() -> None:
    from remittance_reconciler.portal import _headless_safe_user_agent

    class _P:
        def evaluate(self, _js):
            return "Mozilla/5.0 ... Chrome/151.0.0.0 Safari/537.36"
    class _B:
        def new_page(self): return _P()
        def close(self): pass
    class _C:
        def launch(self, **k): return _B()
    class _Pw:
        chromium = _C()

    assert _headless_safe_user_agent(_Pw()) is None


def test_ua_probe_failure_is_not_fatal() -> None:
    from remittance_reconciler.portal import _headless_safe_user_agent

    class _C:
        def launch(self, **k): raise RuntimeError("no browser")
    class _Pw:
        chromium = _C()

    assert _headless_safe_user_agent(_Pw()) is None


def test_smoke_runner_defaults_to_production_headless() -> None:
    import ast

    root = Path(__file__).resolve().parent.parent
    src = ast.unparse(ast.parse((root / "tools" / "run_smoke.py").read_text()))
    assert "cfg.portal_headless and" in src and "args.headed" in src


def test_summary_reports_what_was_actually_paid(tmp_path: Path) -> None:
    from conftest import provenance_for
    from remittance_reconciler.models import ClaimOutcome

    db = Database(tmp_path / "d.db"); db.migrate()
    rows = [EftRow(f"3161{70+i}-B01", date(2037, 7, 7), "r", "d",
                   NET, D("0.00"), D("0.00"), NET) for i in range(2)]
    sid = seed_authorized_statement(db, rows)
    w = db.work_rows(sid)
    db.insert_claim(*provenance_for(db, w[0].invoice_no, sid))
    db.set_claim_outcome(w[0].invoice_no, ClaimOutcome.CONFIRMED)
    db.update_work(w[0].id, state=WorkState.CONFIRMED.value)
    db.update_work(w[1].id, state=WorkState.MANUAL_REVIEW.value,
                   verdict=Verdict.MANUAL_REVIEW.value,
                   error_code="NO_POSITIVE_SUCCESS_SIGNAL",
                   attribution="unverified")

    class _J:
        def ensure_authenticated(self, *a, **k): return "reused"

    saved = {k: getattr(m, k) for k in
             ("reconcile_statement", "detail_shadow_statement", "execute_statement")}
    for k in saved:
        setattr(m, k, lambda *a, **k2: None)
    try:
        stats = m.run_once(cfg(dry_run=True), db, NullGmail(), portal=_J())
    finally:
        for k, v in saved.items():
            setattr(m, k, v)

    assert stats.approved_count == 1, "the confirmed payment count is not reported"
    assert stats.approved_total == NET, "the paid amount is not reported"
    assert stats.manual_review_count == 1

    payload = db.conn.execute(
        "SELECT payload FROM report_outbox ORDER BY id DESC LIMIT 1").fetchone()["payload"]
    assert "Approved: 1" in payload
    assert "$64.00" in payload


def test_exception_table_distinguishes_unverified_clicks(tmp_path: Path) -> None:
    from remittance_reconciler.models import RunStats, WorkRow
    from remittance_reconciler.report import build_summary

    unverified = WorkRow(id=1, statement_id=1, invoice_no="290016-B01", eft_net=NET,
                         verdict=Verdict.MANUAL_REVIEW, state=WorkState.MANUAL_REVIEW,
                         error_code="NO_POSITIVE_SUCCESS_SIGNAL",
                         attribution="unverified")
    benign = WorkRow(id=2, statement_id=1, invoice_no="290017-B01", eft_net=NET,
                     verdict=Verdict.MANUAL_REVIEW, state=WorkState.MANUAL_REVIEW,
                     error_code="AMOUNT_MISMATCH")
    stats = RunStats(run_id=1, statements_processed=1, approved_count=0,
                     approved_total=D("0.00"), manual_review_count=2,
                     status="SUCCESS", error=None)
    _subj, body = build_summary(stats, [unverified, benign])

    assert "NO_POSITIVE_SUCCESS_SIGNAL" in body
    assert "AMOUNT_MISMATCH" in body
    assert "unverified" in body, "attribution is not reported, so rows where money may have moved cannot be identified"


def test_abandoned_statement_is_named_in_the_report(tmp_path: Path) -> None:
    from remittance_reconciler.models import StatementState

    db, sid = _seed(tmp_path)
    db.set_statement_state(sid, StatementState.TERMINAL_EXCEPTION, "G21_MAX_ATTEMPTS")

    class _J:
        def ensure_authenticated(self, *a, **k): return "reused"

    gmail = NullGmail()
    saved = {k: getattr(m, k) for k in
             ("reconcile_statement", "detail_shadow_statement", "execute_statement")}
    for k in saved:
        setattr(m, k, lambda *a, **k2: None)
    try:
        m.run_once(cfg(dry_run=True), db, gmail, portal=_J())
    finally:
        for k, v in saved.items():
            setattr(m, k, v)

    payload = db.conn.execute(
        "SELECT payload, kind FROM report_outbox ORDER BY id DESC LIMIT 1").fetchone()
    body = payload["payload"]
    assert "Abandoned" in body, "the abandoned statement is missing from the report"
    assert "004000000401" in body or "8100001" in body, "cannot tell which statement it is"
    assert "G21_MAX_ATTEMPTS" in body, "no reason given"
    assert "nothing to process" not in body.lower(), "reported all-clear over a loss"


def test_undelivered_report_is_never_dropped(tmp_path: Path) -> None:
    db, sid = _seed(tmp_path)
    c = replace_recipients(cfg(dry_run=True), ("a@example.com",))
    rid = db.enqueue_report(1, "SUMMARY", "s\n\nb")
    for _ in range(c.max_statement_attempts + 2):
        db.bump_report_attempt(rid)

    class _Boom:
        def send(self, *a, **k): raise RuntimeError("gmail down")

    m.flush_report_outbox(db, _Boom(), c)
    still = [r.id for r in db.pending_reports()]
    assert rid in still, "the report disappeared"

    class _Ok:
        def __init__(self): self.sent = []
        def send(self, to, s, b): self.sent.append(to)

    ok = _Ok()
    m.flush_report_outbox(db, ok, c)
    assert ok.sent == ["a@example.com"], "still not sent after recovery"
    assert rid not in [r.id for r in db.pending_reports()]


def replace_recipients(c, recips):
    from dataclasses import replace as _r
    return _r(c, report_recipients=recips)


def test_report_with_no_recipients_is_not_marked_sent(tmp_path: Path) -> None:
    db, sid = _seed(tmp_path)
    c = replace_recipients(cfg(dry_run=True), ())
    rid = db.enqueue_report(1, "SUMMARY", "s\n\nb")

    class _Never:
        def send(self, *a, **k):  # pragma: no cover
            raise AssertionError("attempted to send with no recipients")

    assert m.flush_report_outbox(db, _Never(), c) == 0
    assert rid in [r.id for r in db.pending_reports()], "the report was falsely marked SENT"


def test_normal_portal_login_page_is_not_a_challenge() -> None:
    page = FakePage(authed=False, elements=normal_login_page_elements())
    portal = RecordingPortal(page)

    assert portal.detect_interactive_challenge() is None, (
        "misjudged a normal login page as a challenge (known regression)")
    assert portal.ensure_authenticated() == "logged-in"
    assert portal.login_calls == 1, "the recovery path (a single Keychain login) did not run"


def test_old_existence_only_detector_would_have_misfired() -> None:
    page = FakePage(authed=False, elements=normal_login_page_elements())
    legacy_selectors = ('iframe[src*="recaptcha"]', ".g-recaptcha", "[data-sitekey]")
    fired = [sel for sel in legacy_selectors if page.locator(sel).count()]
    assert fired, "the fake no longer reproduces the regression screen"

    assert RecordingPortal(page).detect_interactive_challenge() is None


@pytest.mark.parametrize("element,expected", [
    (FakeElement('iframe[src*="recaptcha"][src*="bframe"]', w=400, h=580),
     "challenge iframe"),
    (FakeElement(".login-challenge", w=705, h=120), "challenge container"),
    (FakeElement('input[autocomplete="one-time-code"]', w=220, h=40),
     "verification input"),
])
def test_positive_evidence_of_a_live_challenge_still_stops(element, expected) -> None:
    page = FakePage(authed=False, elements=[*normal_login_page_elements(), element])
    portal = RecordingPortal(page)

    marker = portal.detect_interactive_challenge()
    assert marker is not None and expected in marker

    with pytest.raises(LoginRequired):
        portal.ensure_authenticated()
    assert portal.login_calls == 0, "entered credentials while a challenge was showing"


def test_authenticated_page_is_never_derailed_by_challenge_markers() -> None:
    page = FakePage(
        authed=True,
        elements=[recaptcha_badge(), invisible_sitekey_widget()],
        body_text=("Security checklist: enable two-factor authentication. "
                   "Staff may reset an authentication code from this screen."),
    )
    portal = RecordingPortal(page)

    assert portal.detect_interactive_challenge() is None
    assert portal.ensure_authenticated() == "reused"
    assert portal.login_calls == 0, "discarded a live session and logged in"


def test_slow_authenticated_spa_is_not_mistaken_for_a_logged_out_session() -> None:
    page = FakePage(authed=True, login_form=False, authed_at_ms=12000)
    portal = RecordingPortal(page)

    assert page.locator(LOGIN_SUCCESS_NAV).count() == 0, "must not have appeared yet"
    assert portal.ensure_authenticated() == "reused"
    assert portal.login_calls == 0
    assert page.now_ms >= 12000, "if it passed without waiting, the fake is wrong"

    stale = FakePage(authed=True, login_form=False, authed_at_ms=12000)
    with pytest.raises(TimeoutError):
        stale.wait_for_selector(LOGIN_SUCCESS_NAV, timeout=5000)


def test_indeterminate_page_is_fail_closed_and_never_uses_credentials() -> None:
    page = FakePage(authed=False, login_form=False)
    portal = RecordingPortal(page)

    assert portal.observe_login_state(timeout_ms=1000) == "unknown"
    with pytest.raises(LoginRequired) as ei:
        portal.ensure_authenticated(state_timeout_ms=1000)
    assert "could not positively determine" in str(ei.value.marker)
    assert portal.login_calls == 0, "entered credentials without knowing the state"
    assert portal.observe_login_state(timeout_ms=0) == "unknown", "there was no upper bound"


def test_login_that_never_becomes_authenticated_is_login_required_once() -> None:
    page = FakePage(authed=False, elements=normal_login_page_elements())
    portal = RecordingPortal(page, becomes_authed=False)

    with pytest.raises(LoginRequired) as ei:
        portal.ensure_authenticated(state_timeout_ms=1000)
    assert "never became visible" in str(ei.value.marker)
    assert portal.login_calls == 1, "retried login"


def test_visibility_is_the_signal_not_existence() -> None:
    sel = 'iframe[src*="recaptcha"][src*="bframe"]'

    hidden = FakePage(authed=False, elements=[FakeElement(sel, visible=False, w=0, h=0)])
    shown = FakePage(authed=False, elements=[FakeElement(sel, visible=True, w=400, h=580)])

    assert hidden.locator(sel).count() == shown.locator(sel).count() == 1
    assert RecordingPortal(hidden).detect_interactive_challenge() is None
    assert RecordingPortal(shown).detect_interactive_challenge() is not None
