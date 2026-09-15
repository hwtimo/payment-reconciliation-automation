"""Portal contract: export-count integrity (G-7), DOM scoping, config loading and pagination."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from remittance_reconciler import portal
from remittance_reconciler.config import load_config
from remittance_reconciler.models import Verdict, WarnCode, WorkState
from remittance_reconciler.reconcile import check_export_count, verdict_to_state


def test_g7_quiescent_counts_require_exact_equality():
    ok, warns, _ = check_export_count(163, 163, 163, 163)
    assert ok and warns == ()

    ok, _, reason = check_export_count(48, 48, 46, 46)
    assert not ok and "settled screen count" in reason


def test_g7_accepts_band_only_when_drift_actually_observed():
    ok, warns, reason = check_export_count(48, 48, 46, 49)
    assert ok
    assert WarnCode.DATE_WINDOW_DRIFT in warns
    assert "drift" in reason

    ok, _, reason = check_export_count(99, 99, 46, 49)
    assert not ok and "outside drift band" in reason


def test_g7_duplicate_identifiers_always_abort():
    ok, _, reason = check_export_count(48, 46, 48, 48)
    assert not ok and "duplicate identifiers" in reason


def test_g7_missing_screen_count_warns_but_does_not_abort():
    ok, warns, _ = check_export_count(163, 163, None, None)
    assert ok and WarnCode.DATE_WINDOW_DRIFT in warns


@pytest.mark.parametrize(
    "verdict,expected",
    [
        (Verdict.APPROVE_OK, WorkState.RECONCILED),
        (Verdict.ALREADY_PAID, WorkState.NO_ACTION),
        (Verdict.AMOUNT_MISMATCH, WorkState.MANUAL_REVIEW),
        (Verdict.NOT_FOUND, WorkState.MANUAL_REVIEW),
        (Verdict.PAYER_MISMATCH, WorkState.MANUAL_REVIEW),
        (Verdict.CREDIT_MEMO, WorkState.MANUAL_REVIEW),
        (Verdict.MANUAL_REVIEW, WorkState.MANUAL_REVIEW),
    ],
)
def test_verdict_maps_to_state(verdict: Verdict, expected: WorkState):
    assert verdict_to_state(verdict) is expected


def test_detail_pane_is_scoped():
    assert portal.DETAIL_PANE == "#invoice-detail-pane"


def test_action_trigger_is_content_anchored_not_class_anchored():
    assert "Record Payment" in portal.ACTION_TRIGGER_XPATH
    assert "preceding-sibling::button" in portal.ACTION_TRIGGER_XPATH
    assert "dropdown-toggle" not in portal.ACTION_TRIGGER_XPATH


def test_invoice_href_pattern_excludes_patient_links():
    assert portal.INVOICE_HREF_RE.search("#invoices/4300002")
    assert not portal.INVOICE_HREF_RE.search("#patients/10001/billing")


def test_identity_mismatch_is_a_distinct_domain_error():
    assert issubclass(portal.IdentityMismatch, portal.PortalError)
    assert not issubclass(portal.IdentityMismatch, portal.MenuError)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_config_rejects_replace_me_placeholder(tmp_path: Path):
    with pytest.raises(ValueError, match="automation_start_at"):
        load_config(_write(tmp_path, 'automation_start_at: "REPLACE_ME"\n'))


def test_config_rejects_naive_datetime(tmp_path: Path):
    with pytest.raises(ValueError, match="tz-aware"):
        load_config(_write(tmp_path, 'automation_start_at: "2037-07-01T00:00:00"\n'))


def test_config_money_is_decimal_not_float(tmp_path: Path):
    cfg = load_config(
        _write(
            tmp_path,
            'automation_start_at: "2037-07-01T00:00:00-07:00"\n'
            'max_total_amount_per_run: "10000.00"\n',
        )
    )
    assert isinstance(cfg.max_total_amount_per_run, Decimal)
    assert cfg.max_total_amount_per_run == Decimal("10000.00")
    assert cfg.automation_start_at.utcoffset() == timedelta(hours=-7)


def test_config_dry_run_defaults_true(tmp_path: Path):
    cfg = load_config(_write(tmp_path, 'automation_start_at: "2037-07-01T00:00:00-07:00"\n'))
    assert cfg.dry_run is True
    assert cfg.post_click_success_signals == ()


def test_ingestion_transaction_leaves_no_orphan_email(tmp_path: Path):
    from datetime import date as _date

    from remittance_reconciler.database import Database
    from remittance_reconciler.models import EftRow, EftStatement

    db = Database(tmp_path / "t.db")
    db.migrate()
    st = EftStatement(
        vendor_no="8100001",
        payment_document_no="004000000101",
        eft_date=_date(2037, 6, 29),
        deposit_amount=Decimal("118.40"),
        rows=(
            EftRow("300248-B01", _date(2037, 6, 22), "r", "d",
                   Decimal("118.40"), Decimal("0.00"), Decimal("0.00"), Decimal("118.40")),
        ),
        content_fingerprint="fp",
    )
    now = datetime.now(timezone.utc)

    with pytest.raises(RuntimeError):
        with db.transaction():
            sid = db.create_statement(st)
            db.insert_work_rows(sid, st.rows)
            db.insert_email("msg-1", now, statement_id=sid)
            raise RuntimeError("crash mid-ingestion")

    count = lambda t: db.conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
    assert count("statements") == 0
    assert count("emails") == 0, "orphan email row would permanently skip this message"
    assert count("invoice_work") == 0
    assert db.email_exists("msg-1") is False, "message must remain re-ingestable"


def test_claim_commits_independently_of_any_open_transaction(tmp_path: Path):
    import sqlite3

    from remittance_reconciler.database import Database

    from datetime import date as _date

    from conftest import provenance_for, seed_authorized_statement
    from remittance_reconciler.models import EftRow

    path = tmp_path / "t.db"
    db = Database(path)
    db.migrate()
    row = EftRow("300248-B01", _date(2037, 8, 22), "r", "d",
                 Decimal("118.40"), Decimal("0.00"), Decimal("0.00"), Decimal("118.40"))
    sid = seed_authorized_statement(db, [row])
    args = provenance_for(db, "300248-B01", sid)
    db.insert_claim(*args)

    other = sqlite3.connect(str(path))
    assert other.execute("SELECT COUNT(*) FROM write_claims").fetchone()[0] == 1

    with pytest.raises(sqlite3.IntegrityError):
        db.insert_claim(*args)


def test_g19_age_is_received_minus_eft_date():
    from datetime import date as _d

    from remittance_reconciler.reconcile import statement_age_days

    assert statement_age_days(_d(2037, 6, 29), _d(2037, 8, 15)) == 47
    assert statement_age_days(_d(2037, 6, 29), _d(2037, 7, 25)) == 26
    assert statement_age_days(_d(2037, 6, 22), _d(2037, 7, 25)) == 33


def test_g19_old_statement_exceeds_threshold_and_future_dated_does_not():
    from datetime import date as _d

    from remittance_reconciler.reconcile import statement_age_days

    limit = 30
    assert statement_age_days(_d(2037, 6, 29), _d(2037, 8, 15)) > limit, (
        "passing a 47-day-old statement would re-pay invoices already paid manually"
    )
    assert not statement_age_days(_d(2037, 10, 1), _d(2037, 8, 15)) > limit, (
        "a guard that fires only on future dates is dead code"
    )


class _FakeAnchor:
    def __init__(self, text: str, href: str) -> None:
        self._t, self._h = text, href

    def get_attribute(self, _n: str) -> str:
        return self._h

    def inner_text(self) -> str:
        return self._t


class _FakeLocator:
    def __init__(self, page: "_FakePage") -> None:
        self.page = page

    def all(self):
        return self.page.visible_anchors()

    def count(self) -> int:
        return len(self.page.visible_anchors())


class _FakeButton:
    def __init__(self, page: "_FakePage") -> None:
        self.page = page

    def count(self) -> int:
        return 1 if self.page.pages_loaded < self.page.total_pages else 0

    @property
    def first(self):
        return self

    def is_enabled(self) -> bool:
        return True

    def click(self) -> None:
        self.page.pages_loaded += 1


class _FakePage:
    PAGE = 25

    def __init__(self, anchors, total_pages: int) -> None:
        self._anchors, self.total_pages, self.pages_loaded = anchors, total_pages, 1

    def visible_anchors(self):
        return self._anchors[: self.PAGE * self.pages_loaded]

    def locator(self, _sel: str):
        return _FakeLocator(self)

    def get_by_role(self, _role: str, name=None):
        return _FakeButton(self)

    def wait_for_function(self, *_a, **_k):
        return None

    def evaluate(self, _script, *_a, **_k):
        return [[a.inner_text(), a.get_attribute("href")] for a in self.visible_anchors()]


def test_collect_hrefs_traverses_pagination():
    anchors = [_FakeAnchor(f"#3000{i:02d}-B01", f"#invoices/{4400000+i}") for i in range(60)]
    anchors.insert(10, _FakeAnchor("Some Patient", "#patients/10001/billing"))

    page = _FakePage(anchors, total_pages=3)
    session = portal.PortalSession(page)

    got = session.collect_hrefs({"300001-B01", "300030-B01", "300055-B01"})

    assert set(got) == {"300001-B01", "300030-B01", "300055-B01"}, (
        f"targets beyond page 1 were dropped: got {sorted(got)}"
    )
    assert got["300030-B01"] == "#invoices/4400030"


def test_collect_hrefs_ignores_patient_anchors():
    anchors = [
        _FakeAnchor("Some Patient", "#patients/10001/billing"),
        _FakeAnchor("#300001-B01", "#invoices/4400001"),
    ]
    session = portal.PortalSession(_FakePage(anchors, total_pages=1))
    got = session.collect_hrefs({"300001-B01"})
    assert got == {"300001-B01": "#invoices/4400001"}


def test_collect_hrefs_early_exits_without_loading_more():
    anchors = [_FakeAnchor(f"#3000{i:02d}-B01", f"#invoices/{4400000+i}") for i in range(60)]
    page = _FakePage(anchors, total_pages=3)
    portal.PortalSession(page).collect_hrefs({"300002-B01"})
    assert page.pages_loaded == 1, "found on page 1 but loaded more pages"
