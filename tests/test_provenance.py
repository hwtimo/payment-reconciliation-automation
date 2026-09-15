"""Provenance-based write authorization and tamper detection."""

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
from remittance_reconciler.models import (
    EftRow,
    EftStatement,
    InvoiceDetail,
    StatementState,
    Verdict,
    WorkState,
)
from remittance_reconciler.provenance import verify_provenance

PAC = timezone(timedelta(hours=-7))

ROGUE = "290023-B01"
AUTHORIZED = "290007-B01"
NET = D("131.65")


def cfg(**kw) -> Config:
    base = dict(
        automation_start_at=datetime(2037, 6, 17, tzinfo=PAC),
        dry_run=False,
        max_invoices_per_run=50,
        max_total_amount_per_run=D("10000.00"),
        inter_invoice_delay_seconds=0,
        known_vendors=("8100002",),
        post_click_success_signals=("Paid / Settled",),
    )
    base.update(kw)
    return Config(**base)


class _PaneStub:
    def __init__(self, owner): self._o = owner

    def evaluate(self, _js, arg=None):
        inv = getattr(self._o, "current_invoice", None)
        return f"Invoice {inv}" if inv else None

    class _Kb:
        def press(self, _k): pass
    keyboard = _Kb()


class EagerPortal:
    def __init__(self, invoice_no: str, total: D = NET):
        self.invoice_no, self.total = invoice_no, total
        self.clicked: list[str] = []
        self.current_invoice = invoice_no
        self.page = _PaneStub(self)

    @property
    def click_count(self) -> int:
        return len(self.clicked)

    def open_invoice(self, href, *a, **k):
        return InvoiceDetail(
            invoice_no=self.invoice_no, total=self.total,
            payment_status="Unpaid", submission_status="Submitted",
            portal_invoice_id="4300001",
        )

    def resolve_record_payment(self, detail):
        return PayTarget(detail.invoice_no, locator=object(), menu=object())

    def click_record_payment(self, target):
        self.clicked.append(target.invoice_no)
        return "Paid / Settled"


def _row(invoice_no: str = AUTHORIZED, net: D = NET) -> EftRow:
    return EftRow(invoice_no, date(2037, 6, 30), "90-77000001", "055000330001",
                  net, D("0.00"), D("0.00"), net)


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "d.db")
    db.migrate()
    return db


def _make_writable(db: Database, sid: int, invoice_no: str) -> None:
    w = next(x for x in db.work_rows(sid) if x.invoice_no == invoice_no)
    db.update_work(
        w.id, state=WorkState.DETAIL_VALIDATED.value, verdict=Verdict.APPROVE_OK.value,
        portal_href="#invoices/4300001", portal_invoice_id="4300001",
    )


def claims(db: Database) -> int:
    return db.conn.execute("select count(*) c from write_claims").fetchone()["c"]


def test_portal_only_candidate_is_rejected(tmp_path: Path) -> None:
    db = _db(tmp_path)

    sid = seed_authorized_statement(db, [_row(AUTHORIZED)], vendor_no="8100002",
                                    payment_document_no="004000000301")
    _make_writable(db, sid, AUTHORIZED)

    db.conn.execute(
        "INSERT INTO invoice_work (statement_id, invoice_no, eft_net, state, verdict,"
        " portal_href, portal_invoice_id, portal_payer, portal_status, portal_balance)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, ROGUE, str(NET), WorkState.DETAIL_VALIDATED.value,
         Verdict.APPROVE_OK.value, "#invoices/4300004", "4300004",
         "ACME Direct", "Unpaid", str(NET)),
    )
    db.conn.commit()

    rogue = next(w for w in db.work_rows(sid) if w.invoice_no == ROGUE)
    prov, why = verify_provenance(db, rogue)

    assert prov is None
    assert why is not None and "PROVENANCE" in why

    assert ROGUE not in {w.invoice_no for w in db.authorized_write_candidates()}

    portal = EagerPortal(ROGUE)
    m.execute_statement(db, portal, cfg(), db.pending_statements()[0])
    assert portal.click_count == 0, "an invoice without provenance was clicked"
    assert claims(db) == 0


def test_portal_search_result_alone_can_never_become_a_candidate(tmp_path: Path) -> None:
    db = _db(tmp_path)
    assert db.authorized_write_candidates() == []

    sid = seed_authorized_statement(db, [_row()])
    _make_writable(db, sid, AUTHORIZED)
    got = {w.invoice_no for w in db.authorized_write_candidates()}
    assert got == {AUTHORIZED}, "candidates must come only from EFT-derived rows"


@pytest.mark.parametrize("sid", [0, None])
def test_missing_statement_id_is_rejected(tmp_path: Path, sid) -> None:
    from remittance_reconciler.models import WorkRow

    db = _db(tmp_path)
    row = WorkRow(id=1, statement_id=sid, invoice_no=AUTHORIZED, eft_net=NET,
                  raw_eft_invoice_no=AUTHORIZED, eft_net_provenance=NET,
                  state=WorkState.DETAIL_VALIDATED, verdict=Verdict.APPROVE_OK)
    prov, why = verify_provenance(db, row)
    assert prov is None and "no statement_id" in why


def test_nonexistent_statement_is_rejected(tmp_path: Path) -> None:
    from remittance_reconciler.models import WorkRow

    db = _db(tmp_path)
    row = WorkRow(id=1, statement_id=999, invoice_no=AUTHORIZED, eft_net=NET,
                  raw_eft_invoice_no=AUTHORIZED, eft_net_provenance=NET,
                  state=WorkState.DETAIL_VALIDATED, verdict=Verdict.APPROVE_OK)
    prov, why = verify_provenance(db, row)
    assert prov is None and "does not exist" in why


def test_statement_without_a_source_email_is_rejected(tmp_path: Path) -> None:
    db = _db(tmp_path)
    rows = (_row(),)
    st = EftStatement("8100002", "004000000301", date(2037, 7, 9), NET, rows, "fp")
    sid = db.create_statement(st)
    db.insert_work_rows(sid, st.rows)
    _make_writable(db, sid, AUTHORIZED)

    prov, why = verify_provenance(db, db.work_rows(sid)[0])
    assert prov is None and "no source email" in why
    assert db.authorized_write_candidates() == []


def test_untrusted_source_email_is_rejected(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sid = seed_authorized_statement(db, [_row()], intake_trusted=False)
    _make_writable(db, sid, AUTHORIZED)

    prov, why = verify_provenance(db, db.work_rows(sid)[0])
    assert prov is None and "trusted-forward gate" in why
    assert db.authorized_write_candidates() == []


def test_quarantined_source_email_is_rejected(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sid = seed_authorized_statement(db, [_row()], quarantine_reason="V6")
    _make_writable(db, sid, AUTHORIZED)

    prov, why = verify_provenance(db, db.work_rows(sid)[0])
    assert prov is None and "quarantined" in why
    assert db.authorized_write_candidates() == []


def test_terminal_exception_statement_is_rejected(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sid = seed_authorized_statement(db, [_row()])
    _make_writable(db, sid, AUTHORIZED)
    db.set_statement_state(sid, StatementState.TERMINAL_EXCEPTION)

    prov, why = verify_provenance(db, db.work_rows(sid)[0])
    assert prov is None and "TERMINAL_EXCEPTION" in why
    assert db.authorized_write_candidates() == []


def test_invoice_not_present_in_the_linked_statement_is_rejected(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sid = seed_authorized_statement(db, [_row()])
    db.conn.execute(
        "INSERT INTO invoice_work (statement_id, invoice_no, eft_net, state, verdict)"
        " VALUES (?,?,?,?,?)",
        (sid, "999999-B01", str(NET), WorkState.DETAIL_VALIDATED.value,
         Verdict.APPROVE_OK.value),
    )
    db.conn.commit()

    intruder = next(w for w in db.work_rows(sid) if w.invoice_no == "999999-B01")
    prov, why = verify_provenance(db, intruder)
    assert prov is None and "no raw EFT invoice number" in why


def test_identifier_tampering_is_rejected(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sid = seed_authorized_statement(db, [_row()])
    w = db.work_rows(sid)[0]
    db.update_work(w.id, invoice_no="290007-B02")

    prov, why = verify_provenance(db, db.work_rows(sid)[0])
    assert prov is None and "does not match the parsed" in why


def test_provenance_amount_mismatch_is_rejected(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sid = seed_authorized_statement(db, [_row()])
    w = db.work_rows(sid)[0]
    db.update_work(w.id, eft_net=D("9999.00"))

    prov, why = verify_provenance(db, db.work_rows(sid)[0])
    assert prov is None and "does not match the parsed EFT row amount" in why


def test_injected_row_breaks_the_rowset_and_blocks_the_whole_statement(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    sid = seed_authorized_statement(db, [_row()])
    _make_writable(db, sid, AUTHORIZED)
    assert verify_provenance(db, db.work_rows(sid)[0])[1] is None

    db.conn.execute(
        "INSERT INTO invoice_work (statement_id, invoice_no, eft_net,"
        " raw_eft_invoice_no, eft_net_provenance, state, verdict)"
        " VALUES (?,?,?,?,?,?,?)",
        (sid, ROGUE, str(NET), ROGUE, str(NET),
         WorkState.DETAIL_VALIDATED.value, Verdict.APPROVE_OK.value),
    )
    db.conn.commit()

    for w in db.work_rows(sid):
        prov, why = verify_provenance(db, w)
        assert prov is None, f"{w.invoice_no} passed from a tampered statement"
        assert "row-set is not intact" in why


def test_authorized_eft_row_with_all_gates_passing_is_accepted(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sid = seed_authorized_statement(
        db, [_row()], vendor_no="8100002", payment_document_no="004000000301",
        message_id="msg-synthetic-0001", fingerprint="f" * 64,
    )
    _make_writable(db, sid, AUTHORIZED)

    work = db.work_rows(sid)[0]
    prov, why = verify_provenance(db, work)
    assert why is None and prov is not None
    assert prov.source_message_id == "msg-synthetic-0001"
    assert prov.vendor_no == "8100002"
    assert prov.payment_document_no == "004000000301"
    assert prov.raw_eft_invoice_no == AUTHORIZED
    assert prov.eft_net == NET

    assert [w.invoice_no for w in db.authorized_write_candidates()] == [AUTHORIZED]

    portal = EagerPortal(AUTHORIZED)
    m.execute_statement(db, portal, cfg(), db.pending_statements()[0])
    assert portal.click_count == 1
    assert claims(db) == 1


def test_insert_claim_requires_matching_provenance(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sid = seed_authorized_statement(
        db, [_row(AUTHORIZED), _row("290008-B01", D("108.00"))])
    good_work, good_prov = provenance_for(db, AUTHORIZED, sid)
    other_work, _ = provenance_for(db, "290008-B01", sid)

    with pytest.raises(ValueError, match="provenance is for"):
        db.insert_claim(other_work, good_prov)
    assert claims(db) == 0


def test_claim_records_the_authorizing_statement(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sid = seed_authorized_statement(
        db, [_row()], vendor_no="8100002", payment_document_no="004000000301",
        message_id="msg-synthetic-0001", fingerprint="f" * 64,
    )
    _make_writable(db, sid, AUTHORIZED)
    db.insert_claim(*provenance_for(db, AUTHORIZED, sid))

    claim = db.get_claim(AUTHORIZED)
    assert claim is not None
    assert claim.source_message_id == "msg-synthetic-0001"
    assert claim.vendor_no == "8100002"
    assert claim.payment_document_no == "004000000301"
    assert claim.content_fingerprint == "f" * 64
    assert claim.raw_eft_invoice_no == AUTHORIZED


def test_claim_cannot_be_built_from_an_invoice_number_alone() -> None:
    import inspect

    sig = inspect.signature(Database.insert_claim)
    assert list(sig.parameters) == ["self", "work", "prov"]


def test_provenance_columns_cannot_be_updated(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sid = seed_authorized_statement(db, [_row()])
    w = db.work_rows(sid)[0]
    for col in ("raw_eft_invoice_no", "eft_net_provenance"):
        with pytest.raises(ValueError, match="unknown invoice_work column"):
            db.update_work(w.id, **{col: "tampered"})


@pytest.mark.parametrize(
    "state,verdict,expected",
    [
        (WorkState.DETAIL_VALIDATED, Verdict.APPROVE_OK, True),
        (WorkState.RECONCILED, Verdict.APPROVE_OK, False),
        (WorkState.PARSED, Verdict.APPROVE_OK, False),
        (WorkState.PENDING_WRITE, Verdict.APPROVE_OK, False),
        (WorkState.CONFIRMED, Verdict.APPROVE_OK, False),
        (WorkState.DETAIL_VALIDATED, Verdict.MANUAL_REVIEW, False),
        (WorkState.DETAIL_VALIDATED, Verdict.ALREADY_PAID, False),
    ],
)
def test_candidate_query_gates(tmp_path: Path, state, verdict, expected) -> None:
    db = _db(tmp_path)
    sid = seed_authorized_statement(db, [_row()])
    w = db.work_rows(sid)[0]
    db.update_work(w.id, state=state.value, verdict=verdict.value,
                   portal_href="#x", portal_invoice_id="1")
    got = [c.invoice_no for c in db.authorized_write_candidates()]
    assert (got == [AUTHORIZED]) is expected


def test_existing_claim_removes_the_row_from_candidates(tmp_path: Path) -> None:
    db = _db(tmp_path)
    sid = seed_authorized_statement(db, [_row()])
    _make_writable(db, sid, AUTHORIZED)
    assert db.authorized_write_candidates()

    db.insert_claim(*provenance_for(db, AUTHORIZED, sid))
    assert db.authorized_write_candidates() == []


def test_malformed_row_is_terminal_from_the_moment_it_is_persisted(tmp_path: Path) -> None:
    db = _db(tmp_path)
    good = _row("290007-B01", D("100.00"))
    bad = EftRow("290029-b0", date(2037, 7, 9), "r", "d",
                 D("131.65"), D("0.00"), D("0.00"), D("131.65"),
                 identifier_ok=False)
    sid = seed_authorized_statement(db, [good, bad], deposit=D("231.65"))

    rows = {w.invoice_no: w for w in db.work_rows(sid)}
    assert rows["290007-B01"].state is WorkState.PARSED
    assert rows["290029-b0"].state is WorkState.MANUAL_REVIEW
    assert rows["290029-b0"].verdict is Verdict.MANUAL_REVIEW
    assert rows["290029-b0"].error_code == "V6_ROW_IDENTIFIER"

    total, n, missing = db.statement_provenance_sum(sid)
    assert total == D("231.65") and n == 2 and missing == 0


def test_malformed_row_can_never_become_write_eligible(tmp_path: Path) -> None:
    db = _db(tmp_path)
    good = _row("290007-B01", D("100.00"))
    bad = EftRow("290029-b0", date(2037, 7, 9), "r", "d",
                 D("131.65"), D("0.00"), D("0.00"), D("131.65"),
                 identifier_ok=False)
    sid = seed_authorized_statement(db, [good, bad], deposit=D("231.65"))
    w = next(x for x in db.work_rows(sid) if x.invoice_no == "290029-b0")

    db.update_work(w.id, state=WorkState.DETAIL_VALIDATED.value,
                   verdict=Verdict.APPROVE_OK.value, portal_href="#x",
                   portal_invoice_id="1")
    w = next(x for x in db.work_rows(sid) if x.invoice_no == "290029-b0")

    prov, why = verify_provenance(db, w)
    assert prov is None
    assert "does not match the required format" in why
    assert w.invoice_no not in {c.invoice_no for c in db.authorized_write_candidates()}


def test_good_rows_in_the_same_statement_remain_authorized(tmp_path: Path) -> None:
    db = _db(tmp_path)
    good = _row("290007-B01", D("100.00"))
    bad = EftRow("290029-b0", date(2037, 7, 9), "r", "d",
                 D("131.65"), D("0.00"), D("0.00"), D("131.65"),
                 identifier_ok=False)
    sid = seed_authorized_statement(db, [good, bad], deposit=D("231.65"))
    _make_writable(db, sid, "290007-B01")

    prov, why = verify_provenance(
        db, next(w for w in db.work_rows(sid) if w.invoice_no == "290007-B01"))
    assert why is None and prov is not None
    assert [c.invoice_no for c in db.authorized_write_candidates()] == ["290007-B01"]
