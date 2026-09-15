"""Parsing and intake of forwarded remittance emails, using synthetic fixtures."""

from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from remittance_reconciler.config import Config
from remittance_reconciler.database import Database
from remittance_reconciler.gmail import EFT_SUBJECT, GmailClient, normalize_subject
from remittance_reconciler.models import WarnCode, StatementState
from remittance_reconciler.parser import INVOICE_RE, ParseError, content_fingerprint, parse_statement
from remittance_reconciler import main as m

FIX = Path(__file__).parent / "fixtures" / "eft"
PAC = timezone(timedelta(hours=-7))
VENDORS = ("8100001",)
FORWARDER = "billing@example-clinic.test"


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name,rows", [("forwarded_b.html", 47), ("forwarded_mixedcase.html", 29)])
def test_parses_forwarded_statement(name: str, rows: int):
    st = parse_statement(fixture(name), VENDORS)
    assert len(st.rows) == rows
    assert st.deposit_amount == sum(r.net for r in st.rows), "V4 triple-sum must hold"
    assert st.vendor_no in VENDORS
    assert st.payment_document_no.isdigit()
    assert st.content_fingerprint


def test_forwarded_mail_has_no_thead():
    html = fixture("forwarded_b.html")
    assert "<thead" not in html
    assert html.count("<th") == 8
    parse_statement(html, VENDORS)


def test_forwarded_mail_is_wrapped_in_gmail_quote():
    html = fixture("forwarded_b.html")
    assert "gmail_quote" in html
    parse_statement(html, VENDORS)


def test_forwarding_attribution_date_does_not_become_eft_date():
    html = fixture("forwarded_b.html")
    base = parse_statement(html, VENDORS).eft_date

    mutated = html.replace("Jul 17, 2037", "Jan 2, 2099").replace("Fri, Jul 17", "Sat, Jan 2")
    assert mutated != html, "fixture no longer contains the attribution date"
    assert parse_statement(mutated, VENDORS).eft_date == base, (
        "eft_date moved when only the forwarding attribution date changed"
    )


def test_comma_formatted_money_parses():
    html = fixture("forwarded_b.html")
    assert re.search(r"USD \$\d{1,3},\d{3}\.\d{2}", re.sub(r"\s+", " ", html))
    st = parse_statement(html, VENDORS)
    assert st.deposit_amount > Decimal("1000.00")


def test_invoice_suffix_and_case_are_preserved_verbatim():
    st = parse_statement(fixture("forwarded_mixedcase.html"), VENDORS)
    ids = [r.invoice_no for r in st.rows]
    assert any(re.search(r"-b\d{2}$", i) for i in ids), "lowercase suffix lost"
    assert any(re.search(r"-B\d{2}$", i) for i in ids), "uppercase suffix lost"
    assert all(re.fullmatch(r"\d+-[Bb]\d{2}", i) for i in ids)


def test_v6_regex_accepts_both_cases():
    assert re.match(INVOICE_RE, "290009-b01")
    assert re.match(INVOICE_RE, "290009-B01")
    assert not re.match(INVOICE_RE, "290009B01")


def test_malformed_suffix_is_isolated_never_repaired():
    st = parse_statement(fixture("forwarded_a.html"), VENDORS)

    bad = [r for r in st.rows if not r.identifier_ok]
    assert len(bad) == 1
    assert bad[0].invoice_no.endswith("-b0")
    assert WarnCode.MALFORMED_ROW_IDENTIFIER in st.warnings

    assert sum((r.net for r in st.rows), Decimal("0.00")) == st.deposit_amount

    assert all(r.identifier_ok for r in st.rows if r is not bad[0])


def test_all_identifiers_malformed_still_rejects_the_statement():
    import re as _re
    html = fixture("forwarded_b.html")
    broken = _re.sub(r"(\d{6})-([Bb])(\d{2})", r"\1X\2\3", html)
    with pytest.raises(ParseError) as exc:
        parse_statement(broken, VENDORS)
    assert exc.value.code == "V6"


def test_unknown_vendor_is_quarantined():
    with pytest.raises(ParseError) as exc:
        parse_statement(fixture("forwarded_b.html"), ("9999999",))
    assert exc.value.code == "V9"


def test_v4_triple_sum_mismatch_is_rejected():
    html = fixture("forwarded_b.html")
    st = parse_statement(html, VENDORS)
    bad = html.replace(f"{st.deposit_amount:,}", f"{st.deposit_amount + Decimal('0.01'):,}", 1)
    if bad == html:
        pytest.skip("deposit literal not found in the expected format")
    with pytest.raises(ParseError) as exc:
        parse_statement(bad, VENDORS)
    assert exc.value.code == "V4"


def test_same_statement_same_fingerprint():
    a = parse_statement(fixture("forwarded_b.html"), VENDORS)
    b = parse_statement(fixture("forwarded_b.html"), VENDORS)
    assert a.content_fingerprint == b.content_fingerprint


def test_row_order_does_not_change_fingerprint():
    st = parse_statement(fixture("forwarded_b.html"), VENDORS)
    shuffled = list(reversed(st.rows))
    assert content_fingerprint(
        st.vendor_no, st.payment_document_no, st.eft_date, st.deposit_amount, shuffled
    ) == st.content_fingerprint


def test_meaningful_change_changes_fingerprint():
    st = parse_statement(fixture("forwarded_b.html"), VENDORS)
    rows = list(st.rows)
    rows[0] = type(rows[0])(**{**{f: getattr(rows[0], f) for f in rows[0].__slots__},
                               "net": rows[0].net + Decimal("1.00")})
    assert content_fingerprint(
        st.vendor_no, st.payment_document_no, st.eft_date, st.deposit_amount, rows
    ) != st.content_fingerprint


def enc(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def gmsg(mid: str, html: str, *, sender: str = f"Billing <{FORWARDER}>",
         subject: str = "Fwd: EFT Remittance Advice",
         auth: str | None = "dkim=pass; spf=pass; dmarc=pass",
         when: datetime | None = None) -> dict:
    when = when or datetime(2037, 7, 17, 9, 12, tzinfo=PAC)
    hdrs = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]
    if auth is not None:
        hdrs.append({"name": "Authentication-Results", "value": f"mx.google.com; {auth}"})
    return {"id": mid, "internalDate": str(int(when.timestamp() * 1000)),
            "payload": {"mimeType": "multipart/alternative", "headers": hdrs,
                        "parts": [{"mimeType": "text/html", "body": {"data": enc(html)}}]}}


class _Svc:
    def __init__(self, msgs): self.msgs = msgs
    def users(self): return self
    def messages(self): return self
    def list(self, **kw): return self
    def list_next(self, *a): return None
    def execute(self): return {"messages": [{"id": m["id"]} for m in self.msgs]}
    def get(self, userId=None, id=None, **kw):
        msg = next(m for m in self.msgs if m["id"] == id)
        return type("E", (), {"execute": staticmethod(lambda: msg)})()


def client(msgs) -> GmailClient:
    return GmailClient(_Svc(msgs), trusted_forwarders=(FORWARDER,),
                       require_dkim_pass=True, require_dmarc_pass=True)


def fetch(msgs):
    return list(client(msgs).fetch_eft_messages(datetime(2037, 7, 17, tzinfo=PAC)))


def test_accept_trusted_clinic_forward():
    assert len(fetch([gmsg("m1", fixture("forwarded_b.html"))])) == 1


def test_reject_untrusted_sender():
    assert fetch([gmsg("m1", fixture("forwarded_b.html"),
                       sender="Someone <attacker@example.com>")]) == []


def test_reject_spoofed_quoted_payer_from():
    html = fixture("forwarded_b.html")
    assert "remittance@example.com" in html, "fixture should retain the quoted ACME address"
    assert fetch([gmsg("m1", html, sender="Attacker <attacker@evil.test>")]) == []


def test_reject_trusted_subject_from_wrong_sender():
    assert fetch([gmsg("m1", fixture("forwarded_b.html"),
                       sender="Fake <no-reply@payer.example.com>")]) == []


def test_reject_trusted_sender_with_dkim_failure():
    assert fetch([gmsg("m1", fixture("forwarded_b.html"),
                       auth="dkim=fail; spf=pass; dmarc=fail")]) == []


def test_reject_trusted_sender_with_missing_auth_header():
    assert fetch([gmsg("m1", fixture("forwarded_b.html"), auth=None)]) == []


def test_reject_dkim_pass_without_dmarc_alignment():
    assert fetch([gmsg("m1", fixture("forwarded_b.html"), auth="dkim=pass; dmarc=fail")]) == []


def test_reject_wrong_subject():
    assert fetch([gmsg("m1", fixture("forwarded_b.html"), subject="Fwd: Payroll")]) == []


def test_subject_normalization_allows_fwd_prefixes():
    assert normalize_subject("Fwd: Re: EFT Remittance Advice") == EFT_SUBJECT


def test_empty_allow_list_is_fail_closed():
    c = GmailClient(_Svc([gmsg("m1", fixture("forwarded_b.html"))]), trusted_forwarders=())
    assert list(c.fetch_eft_messages(datetime(2037, 7, 17, tzinfo=PAC))) == []


def test_arbitrary_html_with_payer_wording_fails_structurally():
    junk = "<html><body><p>Payee ID: 8100001 EFT Remittance Advice</p></body></html>"
    got = fetch([gmsg("m1", junk)])
    assert len(got) == 1, "intake gate passes it; the parser is the structural gate"
    with pytest.raises(ParseError):
        parse_statement(got[0].html, VENDORS)


def cfg(tmp_path: Path, **kw) -> Config:
    base = dict(
        automation_start_at=datetime(2037, 7, 17, 0, 0, tzinfo=PAC),
        dry_run=True, known_vendors=VENDORS, trusted_forwarders=(FORWARDER,),
        max_total_amount_per_run=Decimal("0.00"), max_invoices_per_run=1,
    )
    base.update(kw)
    return Config(**base)


def fresh_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "eft.db"); db.migrate(); return db


def test_aug31_message_is_ingested_and_left_pending(tmp_path: Path):
    db = fresh_db(tmp_path)
    g = client([gmsg("m-new", fixture("forwarded_b.html"))])
    assert m.ingest_messages(db, g, cfg(tmp_path)) == 1
    q = m.build_queue(db)
    assert len(q) == 1 and q[0].state == StatementState.PENDING
    assert len(db.work_rows(q[0].id)) == 47


def test_historical_message_before_start_at_is_excluded(tmp_path: Path):
    db = fresh_db(tmp_path)
    old = gmsg("m-old", fixture("forwarded_mixedcase.html"),
               when=datetime(2037, 7, 13, 2, 5, tzinfo=PAC))
    assert m.ingest_messages(db, client([old]), cfg(tmp_path)) == 0
    assert m.build_queue(db) == []


def test_duplicate_gmail_message_does_not_duplicate_work(tmp_path: Path):
    db = fresh_db(tmp_path)
    msg = gmsg("m-1", fixture("forwarded_b.html"))
    c = cfg(tmp_path)
    m.ingest_messages(db, client([msg]), c)
    before = len(db.work_rows(m.build_queue(db)[0].id))
    m.ingest_messages(db, client([msg]), c)
    q = m.build_queue(db)
    assert len(q) == 1 and len(db.work_rows(q[0].id)) == before


def test_same_statement_new_message_id_does_not_duplicate_work(tmp_path: Path):
    db = fresh_db(tmp_path); c = cfg(tmp_path)
    html = fixture("forwarded_b.html")
    m.ingest_messages(db, client([gmsg("m-1", html)]), c)
    sid = m.build_queue(db)[0].id
    before = len(db.work_rows(sid))
    m.ingest_messages(db, client([gmsg("m-2", html)]), c)
    q = m.build_queue(db)
    assert len(q) == 1 and q[0].id == sid
    assert len(db.work_rows(sid)) == before


def _all_ids_broken(name: str = "forwarded_b.html") -> str:
    import re as _re
    return _re.sub(r"(\d{6})-([Bb])(\d{2})", r"\1X\2\3", fixture(name))


def test_parser_failure_quarantines_and_persists_nothing(tmp_path: Path):
    db = fresh_db(tmp_path)
    assert m.ingest_messages(db, client([gmsg("m-bad", _all_ids_broken())]),
                             cfg(tmp_path)) == 0
    assert m.build_queue(db) == []
    row = db.conn.execute("SELECT quarantine_reason, statement_id FROM emails "
                          "WHERE message_id='m-bad'").fetchone()
    assert row["quarantine_reason"] == "V6"
    assert row["statement_id"] is None, "no partial statement may survive a parse failure"
    assert db.conn.execute("SELECT COUNT(*) c FROM statements").fetchone()["c"] == 0


def test_quarantined_message_is_not_retried(tmp_path: Path):
    db = fresh_db(tmp_path); c = cfg(tmp_path)
    bad = gmsg("m-bad", fixture("forwarded_a.html"))
    m.ingest_messages(db, client([bad]), c)
    m.ingest_messages(db, client([bad]), c)
    n = db.conn.execute("SELECT COUNT(*) c FROM emails WHERE message_id='m-bad'").fetchone()["c"]
    assert n == 1


def test_pending_queue_survives_restart_with_zero_new_mail(tmp_path: Path):
    db = fresh_db(tmp_path); c = cfg(tmp_path)
    m.ingest_messages(db, client([gmsg("m-1", fixture("forwarded_b.html"))]), c)
    sid = m.build_queue(db)[0].id
    db.close()

    db2 = Database(tmp_path / "eft.db"); db2.migrate()
    assert m.ingest_messages(db2, client([]), c) == 0
    q = m.build_queue(db2)
    assert len(q) == 1 and q[0].id == sid and q[0].state == StatementState.PENDING


def _same_key_different_content(html: str) -> str:
    st = parse_statement(html, VENDORS)
    existing = {r.invoice_no.upper() for r in st.rows}
    for r in reversed(st.rows):
        mo = re.fullmatch(r"(\d+)-([Bb])(\d{2})", r.invoice_no)
        if not mo:
            continue
        alt = f"{mo.group(1)}-{mo.group(2)}{'02' if mo.group(3) != '02' else '03'}"
        if alt.upper() in existing:
            continue
        out = html.replace(f">{r.invoice_no}<", f">{alt}<", 1)
        if out != html:
            return out
    return html


def test_g22_conflict_is_atomic_and_halts_pending(tmp_path: Path):
    db = fresh_db(tmp_path); c = cfg(tmp_path)
    html = fixture("forwarded_b.html")
    m.ingest_messages(db, client([gmsg("m-1", html)]), c)
    sid = m.build_queue(db)[0].id

    conflict = _same_key_different_content(html)
    assert conflict != html, "could not construct a same-key/different-content variant"
    assert parse_statement(conflict, VENDORS).content_fingerprint != \
        parse_statement(html, VENDORS).content_fingerprint

    m.ingest_messages(db, client([gmsg("m-2", conflict)]), c)
    after = db.get_statement(sid)
    assert after.state == StatementState.TERMINAL_EXCEPTION
    assert after.last_error == m.STATEMENT_CONTENT_CONFLICT
    q = db.conn.execute("SELECT quarantine_reason FROM emails WHERE message_id='m-2'").fetchone()
    assert q["quarantine_reason"] == m.STATEMENT_CONTENT_CONFLICT
    assert m.build_queue(db) == [], "a halted statement must leave the live queue"


def test_completed_statement_is_not_mutated_by_a_conflicting_resend(tmp_path: Path):
    db = fresh_db(tmp_path); c = cfg(tmp_path)
    html = fixture("forwarded_b.html")
    m.ingest_messages(db, client([gmsg("m-1", html)]), c)
    sid = m.build_queue(db)[0].id
    db.set_statement_state(sid, StatementState.COMPLETED)

    conflict = _same_key_different_content(html)
    m.ingest_messages(db, client([gmsg("m-2", conflict)]), c)
    assert db.get_statement(sid).state == StatementState.COMPLETED, "history was rewritten"
