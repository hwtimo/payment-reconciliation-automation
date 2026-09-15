"""Supervised allow-list batches: cumulative caps, halts and isolation."""

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
from remittance_reconciler.portal import PayTarget
from remittance_reconciler.main import SmokeAbort, resolve_smoke_targets, smoke_allow_list
from remittance_reconciler.models import ClaimOutcome, EftRow, InvoiceDetail, PortalInvoice, Verdict, WorkState

PAC = timezone(timedelta(hours=-7))
NET = D("64.00")
ROW_DATE = date(2037, 7, 7)

INVOICES = ["290015-B01", "290020-B01", "290025-B01", "290027-B01"]
DECOY = "290015-B01"
AUTHORIZED = ["290020-B01", "290025-B01", "290027-B01"]
TOTAL = D("192.00")


def cfg(**kw) -> Config:
    base = dict(
        automation_start_at=datetime(2037, 6, 17, tzinfo=PAC),
        dry_run=False,
        max_invoices_per_run=3,
        max_total_amount_per_run=TOTAL,
        inter_invoice_delay_seconds=0,
        known_vendors=("8100001",),
        post_click_success_signals=("Paid / Settled",),
        date_buffer_days=3,
    )
    base.update(kw)
    return Config(**base)


class _Loc:
    def is_visible(self): return True
    def is_enabled(self): return True


class _Page:
    class _Kb:
        def press(self, k): pass
    def __init__(self, owner): self.keyboard = self._Kb(); self._o = owner
    def evaluate(self, _js, arg=None):
        return f"Invoice {self._o.current}" if self._o.current else None


class BatchPortal:
    def __init__(self, *, paid=(), signal_for=None, raise_for=None):
        self.paid = set(paid)
        self.signal_for = signal_for or {}
        self.raise_for = raise_for or {}
        self.clicked: list[str] = []
        self.opened: list[str] = []
        self.current: str | None = None
        self.page = _Page(self)

    @property
    def click_count(self) -> int: return len(self.clicked)

    def collect_hrefs(self, targets, **k):
        return {t: f"#invoices/{t}" for t in targets if t in INVOICES}

    def open_invoice(self, href, expected_invoice_no=None, **k):
        inv = href.rsplit("/", 1)[-1]
        self.opened.append(inv)
        self.current = inv
        return InvoiceDetail(
            invoice_no=inv, total=NET,
            payment_status="Paid" if inv in self.paid else "Unpaid",
            submission_status="Settled" if inv in self.paid else "Submitted",
            portal_invoice_id=f"j{inv}")

    def resolve_record_payment(self, detail):
        return PayTarget(detail.invoice_no, locator=_Loc(), menu=object(),
                         expected_amount=detail.total)

    def click_record_payment(self, target):
        inv = target.invoice_no
        self.clicked.append(inv)
        if inv in self.raise_for:
            raise self.raise_for[inv]
        return self.signal_for.get(inv, "Paid / Settled")


def seed(tmp_path: Path):
    db = Database(tmp_path / "d.db"); db.migrate()
    rows = [EftRow(i, ROW_DATE, "r", "d", NET, D("0.00"), D("0.00"), NET) for i in INVOICES]
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


def auth_ids(ids): return [ids[i] for i in AUTHORIZED]


def patch_sales(monkeypatch):
    rows = [PortalInvoice(invoice_no=i, balance=NET, payer="ACME Plan B", status="Unpaid",
                        total=NET, collected=D("0.00"), location="L") for i in INVOICES]
    monkeypatch.setattr(m, "_export_and_parse", lambda *a, **k: (rows, len(rows)))


def snapshot(db):
    return {r["id"]: tuple(r) for r in db.conn.execute("SELECT * FROM invoice_work ORDER BY id")}


def run_batch(db, portal, c, ids):
    targets = resolve_smoke_targets(db, c)
    return sm.execute_smoke_batch(db, portal, c, targets)


def test_only_the_three_authorized_ids_can_click(tmp_path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path); patch_sales(monkeypatch)
    c = cfg(smoke_work_ids=tuple(auth_ids(ids)))
    portal = BatchPortal()
    run_batch(db, portal, c, ids)

    assert sorted(portal.clicked) == sorted(AUTHORIZED)
    assert DECOY not in portal.clicked
    assert DECOY not in portal.opened, "an unauthorized invoice must not even be opened"


def test_fourth_eligible_invoice_cannot_substitute(tmp_path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path); patch_sales(monkeypatch)
    portal = BatchPortal(paid={AUTHORIZED[1]})
    c = cfg(smoke_work_ids=tuple(auth_ids(ids)))
    results = run_batch(db, portal, c, ids)

    assert DECOY not in portal.clicked
    assert db.get_claim(DECOY) is None
    by_id = {r["work_id"]: r for r in results}
    assert by_id[ids[AUTHORIZED[1]]]["outcome"] == "ALREADY_PAID"
    assert portal.clicked == [AUTHORIZED[0], AUTHORIZED[2]]


def test_batch_click_count_never_exceeds_three(tmp_path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path); patch_sales(monkeypatch)
    portal = BatchPortal()
    c = cfg(smoke_work_ids=tuple(auth_ids(ids)))
    for _ in range(3):
        run_batch(db, portal, c, ids)
    assert portal.click_count == 3
    assert db.conn.execute("select count(*) c from write_claims").fetchone()["c"] == 3


def test_confirmed_target_is_never_clicked_again(tmp_path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path); patch_sales(monkeypatch)
    c = cfg(smoke_work_ids=tuple(auth_ids(ids)))
    portal = BatchPortal()
    run_batch(db, portal, c, ids)
    assert portal.click_count == 3

    portal2 = BatchPortal()
    results = run_batch(db, portal2, c, ids)
    assert portal2.click_count == 0, "re-clicked a confirmed target"
    assert all(r["outcome"] == "ABORTED_BEFORE_CLICK" for r in results)


def test_exact_aggregate_cap_is_enforced(tmp_path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path); patch_sales(monkeypatch)
    with pytest.raises(SmokeAbort, match="exceeds max_total_amount_per_run"):
        resolve_smoke_targets(db, cfg(smoke_work_ids=tuple(auth_ids(ids)),
                                      max_total_amount_per_run=D("191.99")))
    got = resolve_smoke_targets(db, cfg(smoke_work_ids=tuple(auth_ids(ids)),
                                        max_total_amount_per_run=TOTAL))
    assert [w.id for w, _ in got] == auth_ids(ids)


def test_cap_below_the_total_aborts_before_touching_portal(tmp_path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path); patch_sales(monkeypatch)
    with pytest.raises(SmokeAbort, match="batch total 192.00 exceeds"):
        resolve_smoke_targets(db, cfg(smoke_work_ids=tuple(auth_ids(ids)),
                                      max_total_amount_per_run=D("128.00")))
    assert db.conn.execute("select count(*) c from write_claims").fetchone()["c"] == 0


def test_executor_also_enforces_the_cumulative_cap(tmp_path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path); patch_sales(monkeypatch)
    targets = resolve_smoke_targets(db, cfg(smoke_work_ids=tuple(auth_ids(ids))))
    tight = cfg(smoke_work_ids=tuple(auth_ids(ids)),
                max_total_amount_per_run=D("128.00"))
    portal = BatchPortal()
    results = sm.execute_smoke_batch(db, portal, tight, targets)

    assert portal.click_count == 2, "clicked past the cumulative cap"
    assert results[-1]["outcome"] == "SKIPPED"
    assert "batch cap" in results[-1]["reason"]


def test_executor_enforces_the_invoice_count_cap(tmp_path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path); patch_sales(monkeypatch)
    targets = resolve_smoke_targets(db, cfg(smoke_work_ids=tuple(auth_ids(ids))))
    portal = BatchPortal()
    results = sm.execute_smoke_batch(
        db, portal, cfg(smoke_work_ids=tuple(auth_ids(ids)), max_invoices_per_run=1),
        targets)
    assert portal.click_count == 1
    assert results[-1]["outcome"] == "SKIPPED"
    assert "invoice cap" in results[-1]["reason"]


def test_allow_list_longer_than_invoice_cap_aborts(tmp_path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path)
    with pytest.raises(SmokeAbort, match="max_invoices_per_run"):
        resolve_smoke_targets(db, cfg(smoke_work_ids=tuple(auth_ids(ids)),
                                      max_invoices_per_run=2))


def test_duplicate_ids_in_the_allow_list_abort(tmp_path) -> None:
    db, sid, ids = seed(tmp_path)
    a = ids[AUTHORIZED[0]]
    with pytest.raises(SmokeAbort, match="duplicate"):
        resolve_smoke_targets(db, cfg(smoke_work_ids=(a, a)))


def test_unknown_outcome_stops_the_rest_of_the_batch(tmp_path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path); patch_sales(monkeypatch)
    portal = BatchPortal(signal_for={AUTHORIZED[1]: ""})
    c = cfg(smoke_work_ids=tuple(auth_ids(ids)))
    results = run_batch(db, portal, c, ids)

    assert portal.clicked == [AUTHORIZED[0], AUTHORIZED[1]], (
        f"kept clicking after an ambiguous outcome: {portal.clicked}")
    assert AUTHORIZED[2] not in portal.clicked
    by_id = {r["work_id"]: r for r in results}
    assert by_id[ids[AUTHORIZED[1]]]["outcome"] == "UNKNOWN_OUTCOME"
    assert by_id[ids[AUTHORIZED[2]]]["outcome"] == "NOT_ATTEMPTED"
    assert db.get_claim(AUTHORIZED[2]) is None


def test_click_exception_stops_the_rest_of_the_batch(tmp_path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path); patch_sales(monkeypatch)
    portal = BatchPortal(raise_for={AUTHORIZED[0]: RuntimeError("browser died")})
    c = cfg(smoke_work_ids=tuple(auth_ids(ids)))
    results = run_batch(db, portal, c, ids)

    assert portal.clicked == [AUTHORIZED[0]]
    assert db.get_claim(AUTHORIZED[0]).final_outcome is ClaimOutcome.UNKNOWN_OUTCOME
    assert [r["outcome"] for r in results[1:]] == ["NOT_ATTEMPTED", "NOT_ATTEMPTED"]
    assert db.get_claim(AUTHORIZED[1]) is None
    assert db.get_claim(AUTHORIZED[2]) is None


def test_halted_batch_never_retries_on_a_later_run(tmp_path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path); patch_sales(monkeypatch)
    c = cfg(smoke_work_ids=tuple(auth_ids(ids)))
    run_batch(db, BatchPortal(signal_for={AUTHORIZED[0]: ""}), c, ids)

    portal2 = BatchPortal()
    run_batch(db, portal2, c, ids)
    assert AUTHORIZED[0] not in portal2.clicked, "re-clicked the ambiguous target"


def test_each_claim_commits_before_its_own_click(tmp_path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path); patch_sales(monkeypatch)
    order: list[str] = []

    real_insert = Database.insert_claim
    def spy_insert(self, work, prov):
        order.append(f"claim:{work.invoice_no}")
        return real_insert(self, work, prov)
    monkeypatch.setattr(Database, "insert_claim", spy_insert)

    class Tracing(BatchPortal):
        def click_record_payment(self, target):
            order.append(f"click:{target.invoice_no}")
            return super().click_record_payment(target)

    run_batch(db, Tracing(), cfg(smoke_work_ids=tuple(auth_ids(ids))), ids)

    assert order == [f"{a}:{i}" for i in AUTHORIZED for a in ("claim", "click")], order


def test_non_target_rows_are_untouched_by_the_batch(tmp_path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path); patch_sales(monkeypatch)
    before = snapshot(db)
    run_batch(db, BatchPortal(), cfg(smoke_work_ids=tuple(auth_ids(ids))), ids)
    after = snapshot(db)

    changed = {i for i in set(before) | set(after) if before.get(i) != after.get(i)}
    assert changed == set(auth_ids(ids)), f"non-target rows changed: {changed - set(auth_ids(ids))}"
    assert after[ids[DECOY]] == before[ids[DECOY]]


def test_allow_list_helper_prefers_the_plural_field() -> None:
    assert smoke_allow_list(cfg(smoke_work_ids=(6, 13, 26))) == (6, 13, 26)
    assert smoke_allow_list(cfg(smoke_work_id=5)) == (5,)
    assert smoke_allow_list(cfg()) == ()


def test_empty_allow_list_means_the_gate_is_off(tmp_path, monkeypatch) -> None:
    db, sid, ids = seed(tmp_path)
    with pytest.raises(SmokeAbort, match="no smoke work ids"):
        resolve_smoke_targets(db, cfg())


def test_missing_id_in_the_allow_list_aborts_the_whole_batch(tmp_path) -> None:
    db, sid, ids = seed(tmp_path)
    with pytest.raises(SmokeAbort, match="does not exist"):
        resolve_smoke_targets(db, cfg(smoke_work_ids=(ids[AUTHORIZED[0]], 999999,
                                                     ids[AUTHORIZED[1]])))


def test_lost_provenance_in_the_allow_list_aborts_the_whole_batch(tmp_path) -> None:
    db, sid, ids = seed(tmp_path)
    db.conn.execute("UPDATE invoice_work SET raw_eft_invoice_no=NULL WHERE id=?",
                    (ids[AUTHORIZED[1]],))
    db.conn.commit()
    with pytest.raises(SmokeAbort, match="lost provenance"):
        resolve_smoke_targets(db, cfg(smoke_work_ids=tuple(auth_ids(ids))))
