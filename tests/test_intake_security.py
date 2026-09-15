"""Intake trust gate: sender parsing, DKIM/DMARC alignment, header injection and quarantine recovery."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from remittance_reconciler import main as m
from remittance_reconciler.config import Config
from remittance_reconciler.database import Database
from remittance_reconciler.gmail import GmailClient, dkim_domain, dkim_passed, dmarc_passed, parse_addr

PAC = timezone(timedelta(hours=-7))
TRUSTED = "billing@example-clinic.test"
HTML = (Path(__file__).parent / "fixtures" / "eft" / "forwarded_b.html").read_text()
GOOD_AR = ("mx.google.com; dkim=pass header.d=example-clinic-test.20000101.gappssmtp.com;"
           " spf=pass; dmarc=pass header.from=example-clinic.test")


def enc(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def msg(frm: str, ar: str | list[str] | None = GOOD_AR, html: str = HTML,
        subject: str = "Fwd: EFT Remittance Advice") -> dict:
    hdrs = [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}]
    if ar is not None:
        for v in ([ar] if isinstance(ar, str) else ar):
            hdrs.append({"name": "Authentication-Results", "value": v})
    return {"id": "x", "internalDate": str(int(datetime(2037, 7, 17, 9, tzinfo=PAC).timestamp() * 1000)),
            "payload": {"mimeType": "multipart/alternative", "headers": hdrs,
                        "parts": [{"mimeType": "text/html", "body": {"data": enc(html)}}]}}


class _Svc:
    def __init__(self, mm): self.m = mm
    def users(self): return self
    def messages(self): return self
    def list(self, **k): return self
    def list_next(self, *a): return None
    def execute(self): return {"messages": [{"id": "x"}]}
    def get(self, **k): return type("E", (), {"execute": staticmethod(lambda: self.m)})()


def accepted(mm: dict, **kw) -> bool:
    c = GmailClient(_Svc(mm), trusted_forwarders=kw.pop("trusted", (TRUSTED,)), **kw)
    return bool(list(c.fetch_eft_messages(datetime(2037, 7, 17, tzinfo=PAC))))


def test_display_name_angle_bracket_injection_is_rejected():
    evil = f'"Clinic Billing <{TRUSTED}>" <mallory@attacker.test>'
    assert parse_addr(evil) == "mallory@attacker.test", "must use an RFC-5322 parser"
    assert not accepted(msg(evil, ar="mx.google.com; dkim=pass; spf=pass;"
                                     " dmarc=pass header.from=attacker.test"))


def test_multiple_addresses_in_from_are_rejected():
    assert parse_addr(f"B <{TRUSTED}>, M <m@attacker.test>") == ""
    assert not accepted(msg(f"B <{TRUSTED}>, M <m@attacker.test>"))


def test_legitimate_forward_is_still_accepted():
    assert accepted(msg(f"Clinic Billing <{TRUSTED}>"))


def test_sender_supplied_ar_header_cannot_override_the_real_verdict():
    real_fail = ("mx.google.com; dkim=neutral; spf=softfail;"
                 " dmarc=fail (p=REJECT) header.from=example-clinic.test")
    injected = "relay.attacker.test; dkim=pass; spf=pass; dmarc=pass"
    p = msg(f"B <{TRUSTED}>", ar=[real_fail, injected])["payload"]
    assert dkim_passed(p) is False
    assert dmarc_passed(p) is False
    assert not accepted(msg(f"B <{TRUSTED}>", ar=[real_fail, injected]))


def test_pass_substring_inside_a_failing_verdict_comment_is_not_a_pass():
    ar = 'mx.google.com; dkim=fail reason="expected dkim=pass but key missing"; dmarc=fail'
    assert dkim_passed(msg(f"B <{TRUSTED}>", ar=ar)["payload"]) is False


def test_missing_authoritative_ar_is_rejected():
    assert not accepted(msg(f"B <{TRUSTED}>", ar="relay.attacker.test; dkim=pass; dmarc=pass"))


def test_dmarc_must_align_with_the_accepted_sender_domain():
    misaligned = "mx.google.com; dkim=pass; spf=pass; dmarc=pass header.from=attacker.test"
    assert not accepted(msg(f"B <{TRUSTED}>", ar=misaligned))


def test_dkim_domain_comes_from_the_verified_ar_not_the_raw_header():
    p = msg(f"B <{TRUSTED}>")["payload"]
    p["headers"].insert(0, {"name": "DKIM-Signature", "value": "v=1; d=attacker.test; s=x"})
    assert dkim_domain(p) == "example-clinic-test.20000101.gappssmtp.com"


def test_dkim_suffix_match_requires_a_label_boundary():
    ar = ("mx.google.com; dkim=pass header.d=notexample-clinic.test; spf=pass;"
          " dmarc=pass header.from=example-clinic.test")
    assert not accepted(msg(f"B <{TRUSTED}>", ar=ar),
                        trusted_dkim_domain_suffix="example-clinic.test")


def test_blank_allow_list_entry_does_not_open_the_gate(tmp_path: Path):
    from remittance_reconciler.config import load_config
    (tmp_path / "c.yaml").write_text(
        'automation_start_at: "2037-07-17T00:00:00-07:00"\n'
        'trusted_forwarders:\n  - ""\n  - "   "\n', encoding="utf-8")
    assert load_config(tmp_path / "c.yaml").trusted_forwarders == ()
    assert not accepted(msg("", ar=GOOD_AR), trusted=("",))


def cfg(**kw) -> Config:
    base = dict(automation_start_at=datetime(2037, 7, 17, 0, 0, tzinfo=PAC), dry_run=True,
                known_vendors=("8100001",), trusted_forwarders=(TRUSTED,))
    base.update(kw)
    return Config(**base)


def client(mm) -> GmailClient:
    return GmailClient(_Svc(mm), trusted_forwarders=(TRUSTED,))


def test_v9_quarantine_is_recoverable_after_config_fix(tmp_path: Path):
    db = Database(tmp_path / "d.db"); db.migrate()
    g = client(msg(f"B <{TRUSTED}>"))
    assert m.ingest_messages(db, g, cfg(known_vendors=("9999999",))) == 0
    assert db.quarantine_reason("x") == "V9"

    assert m.ingest_messages(db, g, cfg(known_vendors=("8100001",))) == 1
    assert len(m.build_queue(db)) == 1


def test_structural_quarantine_is_not_retried(tmp_path: Path):
    db = Database(tmp_path / "d.db"); db.migrate()
    import re as _re
    bad = _re.sub(
        r"(\d{6})-([Bb])(\d{2})", r"\1X\2\3",
        (Path(__file__).parent / "fixtures" / "eft" / "forwarded_b.html").read_text())
    g = client(msg(f"B <{TRUSTED}>", html=bad))
    c = cfg()
    assert m.ingest_messages(db, g, c) == 0
    assert db.quarantine_reason("x") == "V6"
    assert m.ingest_messages(db, g, c) == 0
    assert m.build_queue(db) == []


def test_malformed_html_does_not_head_of_line_block_the_queue(tmp_path: Path):
    db = Database(tmp_path / "d.db"); db.migrate()
    events: list[str] = []
    assert m.ingest_messages(db, client(msg(f"B <{TRUSTED}>", html="   ")), cfg(), events) == 0
    assert db.quarantine_reason("x") == "PARSE_CRASH"
    assert any("PARSE_CRASH" in e for e in events)


def test_quarantines_are_surfaced_to_humans(tmp_path: Path):
    db = Database(tmp_path / "d.db"); db.migrate()
    events: list[str] = []
    m.ingest_messages(db, client(msg(f"B <{TRUSTED}>")), cfg(known_vendors=("9999999",)), events)
    assert events and "V9" in events[0]


def _aged(mid: str, days_before: int, html: str) -> dict:
    d = msg(f"B <{TRUSTED}>", html=html)
    d["id"] = mid
    when = datetime(2037, 7, 17, 0, 0, tzinfo=PAC) - timedelta(days=days_before)
    d["internalDate"] = str(int(when.timestamp() * 1000))
    return d


def test_pre_start_at_mail_never_enters_by_default(tmp_path: Path):
    import inspect

    assert inspect.signature(m.ingest_messages).parameters[
        "historical_allow"].default == frozenset()

    db = Database(tmp_path / "d.db"); db.migrate()
    html = (Path(__file__).parent / "fixtures" / "eft" / "forwarded_b.html").read_text()
    assert m.ingest_messages(db, client(_aged("old", 30, html)), cfg()) == 0
    assert db.conn.execute("SELECT COUNT(*) c FROM statements").fetchone()["c"] == 0


def test_named_historical_message_enters_but_others_still_do_not(tmp_path: Path):
    db = Database(tmp_path / "d.db"); db.migrate()
    html = (Path(__file__).parent / "fixtures" / "eft" / "forwarded_b.html").read_text()

    assert m.ingest_messages(db, client(_aged("wanted", 30, html)), cfg()) == 0

    added = m.ingest_messages(db, client(_aged("wanted", 30, html)), cfg(),
                              historical_allow=frozenset({"wanted"}))
    assert added == 1
    assert [r["message_id"] for r in
            db.conn.execute("SELECT message_id FROM emails")] == ["wanted"]

    assert m.ingest_messages(db, client(_aged("other", 30, html)), cfg(),
                             historical_allow=frozenset({"wanted"})) == 0
    assert db.conn.execute("SELECT COUNT(*) c FROM emails").fetchone()["c"] == 1
