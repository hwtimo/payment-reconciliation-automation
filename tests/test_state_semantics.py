"""Row-state semantics: rows that were only parsed are never treated as reconciled."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from remittance_reconciler.database import Database
from remittance_reconciler.models import EftRow, EftStatement, WorkState
from remittance_reconciler.reconcile import (
    PORTAL_RECONCILED_STATES,
    TERMINAL_STATES,
    is_portal_reconciled,
    is_terminal,
    statement_is_complete,
)

PAC = timezone(timedelta(hours=-7))


def _statement() -> EftStatement:
    rows = tuple(
        EftRow(f"30000{i}-b01", date(2037, 7, 8), f"90-0000000{i}", f"0550002200{i}",
               Decimal("100.00"), Decimal("0.00"), Decimal("0.00"), Decimal("100.00"))
        for i in range(3)
    )
    return EftStatement("8100001", "004000000101", date(2037, 7, 16),
                        Decimal("300.00"), rows, "fp")


def test_ingest_persists_rows_as_parsed_not_reconciled(tmp_path: Path):
    db = Database(tmp_path / "d.db"); db.migrate()
    st = _statement()
    sid = db.create_statement(st)
    db.insert_work_rows(sid, st.rows)

    rows = db.work_rows(sid)
    assert rows, "no rows persisted"
    assert all(r.state is WorkState.PARSED for r in rows), \
        f"parsed-only rows must be PARSED, got {sorted({r.state for r in rows})}"
    assert not any(r.state is WorkState.RECONCILED for r in rows)
    for r in rows:
        assert r.verdict is None
        assert r.portal_balance is None and r.portal_payer is None and r.portal_invoice_id is None


def test_parsed_is_not_interpreted_as_portal_reconciled():
    assert not is_portal_reconciled(WorkState.PARSED)
    assert WorkState.PARSED not in PORTAL_RECONCILED_STATES
    for s in (WorkState.RECONCILED, WorkState.PENDING_WRITE, WorkState.CONFIRMED,
              WorkState.NO_ACTION, WorkState.MANUAL_REVIEW):
        assert is_portal_reconciled(s)


def test_parsed_is_not_terminal():
    assert not is_terminal(WorkState.PARSED)
    assert WorkState.PARSED not in TERMINAL_STATES


def test_statement_of_parsed_rows_is_never_complete():
    assert statement_is_complete([WorkState.PARSED] * 12) is False
    assert statement_is_complete([WorkState.PARSED, WorkState.CONFIRMED]) is False


def test_workrow_default_state_is_parsed():
    from remittance_reconciler.models import WorkRow
    assert WorkRow(id=1, statement_id=1, invoice_no="300001-b01").state is WorkState.PARSED


def test_null_state_in_db_reads_back_as_parsed(tmp_path: Path):
    db = Database(tmp_path / "d.db"); db.migrate()
    st = _statement(); sid = db.create_statement(st); db.insert_work_rows(sid, st.rows)
    db.conn.execute("UPDATE invoice_work SET state=NULL")
    assert all(r.state is WorkState.PARSED for r in db.work_rows(sid))


def test_no_code_path_marks_rows_reconciled_without_portal(tmp_path: Path):
    import base64
    from remittance_reconciler import main as m
    from remittance_reconciler.config import Config
    from remittance_reconciler.gmail import GmailClient

    html = (Path(__file__).parent / "fixtures" / "eft" / "forwarded_b.html").read_text()
    enc = base64.urlsafe_b64encode(html.encode()).decode().rstrip("=")
    msg = {"id": "s1", "internalDate": str(int(datetime(2037, 7, 17, 9, tzinfo=PAC).timestamp() * 1000)),
           "payload": {"headers": [
               {"name": "From", "value": "B <billing@example-clinic.test>"},
               {"name": "Subject", "value": "Fwd: EFT Remittance Advice"},
               {"name": "Authentication-Results",
                "value": "mx.google.com; dkim=pass; spf=pass; dmarc=pass header.from=example-clinic.test"}],
               "parts": [{"mimeType": "text/html", "body": {"data": enc}}]}}

    class S:
        def users(self): return self
        def messages(self): return self
        def list(self, **k): return self
        def list_next(self, *a): return None
        def execute(self): return {"messages": [{"id": "s1"}]}
        def get(self, **k): return type("E", (), {"execute": staticmethod(lambda: msg)})()

    db = Database(tmp_path / "d.db"); db.migrate()
    cfg = Config(automation_start_at=datetime(2037, 7, 17, tzinfo=PAC), dry_run=True,
                 known_vendors=("8100001",), trusted_forwarders=("billing@example-clinic.test",))
    g = GmailClient(S(), trusted_forwarders=("billing@example-clinic.test",))
    assert m.ingest_messages(db, g, cfg) == 1

    states = {r.state for s in db.pending_statements() for r in db.work_rows(s.id)}
    assert states == {WorkState.PARSED}, f"pipeline produced non-PARSED states: {states}"


def test_execute_eligibility_cannot_come_from_a_parsed_row(tmp_path: Path):
    db = Database(tmp_path / "d.db"); db.migrate()
    st = _statement(); sid = db.create_statement(st); db.insert_work_rows(sid, st.rows)

    rows = db.work_rows(sid)
    assert not any(is_portal_reconciled(r.state) for r in rows)
    assert all(db.get_claim(r.invoice_no) is None for r in rows)
    assert db.conn.execute("select count(*) c from write_claims").fetchone()["c"] == 0


def test_parsed_row_is_never_write_eligible(tmp_path):
    from decimal import Decimal as _D

    from conftest import seed_authorized_statement
    from remittance_reconciler.config import Config as _C
    from remittance_reconciler.database import Database
    from remittance_reconciler.main import RunBudget, write_eligibility
    from remittance_reconciler.models import Verdict as _V

    db = Database(tmp_path / "d.db"); db.migrate()
    st = _statement(); sid = seed_authorized_statement(db, st.rows)
    w = db.work_rows(sid)[0]
    db.update_work(w.id, verdict=_V.APPROVE_OK.value)
    w = db.work_rows(sid)[0]
    assert w.state is WorkState.PARSED

    cfg = _C(automation_start_at=datetime(2037, 7, 17, tzinfo=timezone.utc), dry_run=False,
             max_invoices_per_run=50, max_total_amount_per_run=_D("10000.00"))
    _prov, reason = write_eligibility(
        db, w, cfg, RunBudget(50, _D("10000.00")), None, _D("10000.00"))
    assert reason is not None and "PARSED" in reason
