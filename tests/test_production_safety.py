"""Operational safety: run window, claim journal, backups, LaunchAgent template and run caps."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from conftest import provenance_for, seed_authorized_statement
from remittance_reconciler import main as m
from remittance_reconciler.config import Config
from remittance_reconciler.database import Database
from remittance_reconciler.models import ClaimOutcome, EftRow

PAC = timezone(timedelta(hours=-7))


def cfg(**kw) -> Config:
    base = dict(automation_start_at=datetime(2037, 6, 17, tzinfo=PAC),
                known_vendors=("8100001",))
    base.update(kw)
    return Config(**base)


def test_window_accepts_the_target_hour(monkeypatch) -> None:
    from zoneinfo import ZoneInfo

    c = cfg()
    tz = ZoneInfo(c.run_window_tz)

    class _Now(datetime):
        @classmethod
        def now(cls, tzinfo=None):
            return datetime(2037, 7, 19, 3, 0, tzinfo=tz)

    monkeypatch.setattr(m, "datetime", _Now)
    assert m._outside_run_window(c) is None


@pytest.mark.parametrize("hh,mm,inside", [
    (2, 44, False),
    (2, 45, True),
    (3, 0, True),
    (4, 30, True),
    (4, 31, False),
    (9, 0, False),
    (12, 0, False),
    (17, 30, False),
    (23, 59, False),
])
def test_window_boundaries(monkeypatch, hh, mm, inside) -> None:
    from zoneinfo import ZoneInfo

    c = cfg()
    tz = ZoneInfo(c.run_window_tz)

    class _Now(datetime):
        @classmethod
        def now(cls, tzinfo=None):
            return datetime(2037, 7, 19, hh, mm, tzinfo=tz)

    monkeypatch.setattr(m, "datetime", _Now)
    assert (m._outside_run_window(c) is None) is inside


def test_window_is_evaluated_before_any_side_effect() -> None:
    import ast
    import inspect

    src = ast.unparse(ast.parse(inspect.getsource(m.main).lstrip()))
    i_guard = src.index("_outside_run_window")
    for later in ("acquire_lock", "clean_scratch", "Database(", "GmailClient"):
        assert i_guard < src.index(later), f"{later} runs before the window check"


def test_window_guard_never_aborts_a_running_job() -> None:
    import ast
    import inspect

    for fn in (m.run_once, m.execute_statement):
        body = ast.unparse(ast.parse(inspect.getsource(fn).lstrip()))
        assert "_outside_run_window" not in body, (
            f"{fn.__name__} re-checks the window mid-run (abort risk)")


def test_bad_timezone_refuses_rather_than_guessing() -> None:
    assert m._outside_run_window(cfg(run_window_tz="Mars/Olympus")) is not None


def test_force_now_flag_exists_for_supervised_runs() -> None:
    import ast
    import inspect

    src = ast.unparse(ast.parse(inspect.getsource(m.main).lstrip()))
    assert "force_now" in src


def _seed_claimable(tmp_path: Path):
    db = Database(tmp_path / "d.db"); db.migrate()
    rows = [EftRow("300248-B01", date(2037, 7, 7), "r", "d",
                   D("118.40"), D("0.00"), D("0.00"), D("118.40"))]
    sid = seed_authorized_statement(db, rows)
    return db, sid


def test_claim_is_journalled_before_the_database_row(tmp_path: Path) -> None:
    db, sid = _seed_claimable(tmp_path)
    work, prov = provenance_for(db, "300248-B01", sid)

    import ast
    import inspect

    body = ast.unparse(ast.parse(inspect.getsource(Database.insert_claim).lstrip()))
    i_journal = body.index("_append_claim_journal")
    i_db = body.index("INSERT INTO write_claims")
    assert i_journal < i_db, "the journal is written after the DB INSERT"

    db.insert_claim(work, prov)
    assert db.get_claim("300248-B01") is not None
    assert [r["invoice_no"] for r in db.read_claim_journal()] == ["300248-B01"]


def test_claim_journal_survives_database_loss(tmp_path: Path) -> None:
    db, sid = _seed_claimable(tmp_path)
    work, prov = provenance_for(db, "300248-B01", sid)
    db.insert_claim(work, prov)
    journal = db.claim_journal_path
    db.close()

    (tmp_path / "d.db").unlink()
    assert journal.is_file(), "the journal was lost together with the DB"

    recs = [json.loads(l) for l in journal.read_text().splitlines() if l.strip()]
    assert [r["invoice_no"] for r in recs] == ["300248-B01"]
    assert recs[0]["claimed_amount"] == "118.40"
    assert recs[0]["source_message_id"] and recs[0]["payment_document_no"]
    assert oct(journal.stat().st_mode & 0o777) == "0o600"


def test_backup_preserves_every_write_claim(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import backup_db

    db, sid = _seed_claimable(tmp_path)
    work, prov = provenance_for(db, "300248-B01", sid)
    db.insert_claim(work, prov)
    db.set_claim_outcome("300248-B01", ClaimOutcome.CONFIRMED)
    db.conn.commit()

    dest = backup_db.backup(tmp_path / "d.db", tmp_path / "backups", keep=5)
    assert dest.is_file()
    assert oct(dest.stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "backups").stat().st_mode & 0o777) == "0o700"

    c = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        rows = c.execute("SELECT invoice_no, final_outcome, claimed_amount"
                         " FROM write_claims").fetchall()
    finally:
        c.close()
    assert rows == [("300248-B01", "CONFIRMED", "118.40")]

    assert list((tmp_path / "backups").glob("*.claims.jsonl"))


def test_restore_never_touches_the_production_database(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import backup_db

    db, sid = _seed_claimable(tmp_path)
    work, prov = provenance_for(db, "300248-B01", sid)
    db.insert_claim(work, prov)
    db.conn.commit()

    live = tmp_path / "d.db"
    before = live.read_bytes()
    dest = backup_db.backup(live, tmp_path / "backups", keep=5)
    assert backup_db.verify_restore(dest, live) == 0
    assert live.read_bytes() == before, "the production DB was modified"


def test_backup_retention_prunes_oldest_only(tmp_path: Path) -> None:
    import sys
    import time

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import backup_db

    db, sid = _seed_claimable(tmp_path)
    out = tmp_path / "backups"
    made = []
    for _ in range(4):
        made.append(backup_db.backup(tmp_path / "d.db", out, keep=2))
        time.sleep(1.05)
    kept = sorted(out.glob("eft-*.db"))
    assert len(kept) == 2, kept
    assert kept == sorted(made)[-2:], "something other than the oldest backup was pruned"


def _plist() -> dict:
    import plistlib

    root = Path(__file__).resolve().parent.parent
    return plistlib.loads((root / "com.example.remittance-reconciler.plist").read_bytes())


def _installed_here(d: dict) -> bool:
    return Path(d["WorkingDirectory"]) == Path(__file__).resolve().parent.parent


def test_plist_is_valid_and_uses_the_absolute_interpreter() -> None:
    d = _plist()
    argv = d["ProgramArguments"]
    assert argv[0] == "/Users/automation/remittance-reconciler/.venv/bin/python"
    assert Path(argv[0]).is_absolute()
    if _installed_here(d):
        assert Path(argv[0]).exists(), "the interpreter does not exist"


def test_plist_pins_the_config_path_explicitly() -> None:
    d = _plist()
    argv = d["ProgramArguments"]
    assert "--config" in argv
    cfg_path = Path(argv[argv.index("--config") + 1])
    assert cfg_path.is_absolute()
    if _installed_here(d):
        assert cfg_path.exists()
    assert cfg_path == Path("/Users/automation/remittance-reconciler/config.yaml")


def test_every_plist_path_is_absolute() -> None:
    d = _plist()
    for key in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
        assert Path(d[key]).is_absolute(), f"{key} is a relative path"
    for k, v in d.get("EnvironmentVariables", {}).items():
        assert Path(v).is_absolute(), f"env {k} is a relative path"
    if _installed_here(d):
        assert Path(d["WorkingDirectory"]).is_dir()
        assert Path(d["StandardOutPath"]).parent.is_dir()


def test_plist_never_bypasses_the_run_window() -> None:
    d = _plist()
    assert "--force-now" not in d["ProgramArguments"]


def test_plist_does_not_fire_on_load_or_respawn() -> None:
    d = _plist()
    assert d.get("RunAtLoad") is False
    assert "KeepAlive" not in d
    assert d["StartCalendarInterval"] == {"Hour": 3, "Minute": 0}


def test_derived_paths_never_depend_on_cwd() -> None:
    import ast
    import inspect

    src = ast.unparse(ast.parse(inspect.getsource(m.main).lstrip()))
    i_root = src.index("root = Path(args.config).resolve().parent")
    for later in ("logs", "acquire_lock", "Database("):
        assert i_root < src.index(later), f"{later} comes before the root computation"
    for bad in ("Path('logs')", 'Path("logs")', "Path.cwd()"):
        assert bad not in src, f"main() has a CWD-dependent path: {bad}"


def test_phi_export_directory_is_absolute(tmp_path: Path) -> None:
    from remittance_reconciler.config import load_config

    root = tmp_path.resolve()
    (root / "config.yaml").write_text('automation_start_at: "2037-07-17T00:00:00-07:00"\n', encoding="utf-8")
    c = load_config(root / "config.yaml")
    assert Path(c.scratch_dir).is_absolute()
    assert Path(c.scratch_dir) == root / "scratch"

    import ast
    import inspect

    src = ast.unparse(ast.parse(inspect.getsource(m._export_and_parse).lstrip()))
    assert "scratch_dir" in src, "the export is not given an absolute path"


def test_none_means_unlimited_not_a_huge_number() -> None:
    from remittance_reconciler.main import RunBudget

    b = RunBudget(max_invoices=None, max_total=None)
    for i in range(500):
        assert b.allows(D("999999.99")), f"blocked at iteration {i}"
        b.consume(D("999999.99"))
    assert b.invoices_used == 500


def test_finite_caps_still_bind() -> None:
    from remittance_reconciler.main import RunBudget

    b = RunBudget(max_invoices=2, max_total=D("100.00"))
    assert b.allows(D("60.00")); b.consume(D("60.00"))
    assert not b.allows(D("60.00")), "the amount cap did not bind"
    assert b.allows(D("40.00")); b.consume(D("40.00"))
    assert not b.allows(D("0.01")), "the count cap did not bind"


def test_unlimited_config_round_trips_from_yaml(tmp_path: Path) -> None:
    from remittance_reconciler.config import load_config

    p = tmp_path / "c.yaml"
    p.write_text(
        'automation_start_at: "2037-07-17T00:00:00-07:00"\n'
        "max_invoices_per_run: null\n"
        'max_total_amount_per_run: null\n'
    )
    c = load_config(p)
    assert c.max_invoices_per_run is None
    assert c.max_total_amount_per_run is None

    p2 = tmp_path / "d.yaml"
    p2.write_text('automation_start_at: "2037-07-17T00:00:00-07:00"\n')
    c2 = load_config(p2)
    assert c2.max_invoices_per_run == 50
    assert c2.max_total_amount_per_run == D("10000.00")


def test_unlimited_does_not_disable_the_per_invoice_safety_gates() -> None:
    import ast
    import inspect

    from remittance_reconciler import writepath

    body = ast.unparse(ast.parse(
        inspect.getsource(writepath.execute_one_authorized_work).lstrip()))
    for gate in ("get_claim", "DETAIL_VALIDATED", "APPROVE_OK",
                 "validate_authorized_work", "insert_claim"):
        assert gate in body, f"the {gate} gate disappeared on the unlimited path"
    assert "max_total_amount_per_run is not None" in body


def test_supervised_smoke_refuses_unlimited_caps(tmp_path: Path) -> None:
    from remittance_reconciler.main import SmokeAbort, resolve_smoke_targets

    db, sid = _seed_claimable(tmp_path)
    w = db.work_rows(sid)[0]
    with pytest.raises(SmokeAbort, match="explicit caps"):
        resolve_smoke_targets(db, cfg(smoke_work_ids=(w.id,),
                                      max_invoices_per_run=None,
                                      max_total_amount_per_run=None))


def test_ambiguous_outcome_halts_the_entire_run(tmp_path: Path) -> None:
    from remittance_reconciler.portal import PayTarget
    from remittance_reconciler.main import RunHalted
    from remittance_reconciler.models import InvoiceDetail, Verdict, WorkState

    NET = D("64.00")

    class _Pane:
        def __init__(self, o): self._o = o
        def evaluate(self, _js, arg=None):
            return f"Invoice {self._o.current}" if self._o.current else None
        class _Kb:
            def press(self, _k): pass
        keyboard = _Kb()

    class _Portal:
        def __init__(self, ambiguous: str):
            self.ambiguous = ambiguous
            self.clicked: list[str] = []
            self.current = None
            self.page = _Pane(self)
        def open_invoice(self, href, expected_invoice_no=None, **k):
            inv = href.rsplit("/", 1)[-1]; self.current = inv
            return InvoiceDetail(invoice_no=inv, total=NET, payment_status="Unpaid",
                                 submission_status="Submitted", portal_invoice_id=f"j{inv}")
        def resolve_record_payment(self, d):
            return PayTarget(d.invoice_no, locator=object(), menu=object(),
                             expected_amount=d.total)
        def click_record_payment(self, t):
            self.clicked.append(t.invoice_no)
            return "" if t.invoice_no == self.ambiguous else "Paid / Settled"

    db = Database(tmp_path / "d.db"); db.migrate()
    invoices = [f"3172{60+i}-B01" for i in range(5)]
    rows = [EftRow(i, date(2037, 7, 7), "r", "d", NET, D("0.00"), D("0.00"), NET)
            for i in invoices]
    sid = seed_authorized_statement(db, rows)
    for w in db.work_rows(sid):
        db.update_work(w.id, state=WorkState.DETAIL_VALIDATED.value,
                       verdict=Verdict.APPROVE_OK.value,
                       portal_href=f"#invoices/{w.invoice_no}",
                       portal_invoice_id=f"j{w.invoice_no}")

    c = cfg(dry_run=False, max_invoices_per_run=None,
            max_total_amount_per_run=None,
            post_click_success_signals=("Paid / Settled",),
            inter_invoice_delay_seconds=0)
    portal = _Portal(ambiguous=invoices[1])

    with pytest.raises(RunHalted):
        m.execute_statement(db, portal, c, db.pending_statements()[0])

    assert portal.clicked == invoices[:2], f"clicked after an ambiguous outcome: {portal.clicked}"
    for later in invoices[2:]:
        assert db.get_claim(later) is None, f"a claim was created for {later}"
    assert db.get_claim(invoices[1]).final_outcome is ClaimOutcome.UNKNOWN_OUTCOME


def test_halt_is_reported_distinctly_from_failure() -> None:
    import ast
    import inspect

    from remittance_reconciler.database import RUN_STATUS_FAILED, RUN_STATUS_HALTED

    assert RUN_STATUS_HALTED != RUN_STATUS_FAILED
    src = ast.unparse(ast.parse(inspect.getsource(m.run_once).lstrip()))
    assert "RUN_STATUS_HALTED" in src
    entry = ast.unparse(ast.parse(inspect.getsource(m.main).lstrip()))
    assert "return 5" in entry, "HALTED has no distinct exit code"
