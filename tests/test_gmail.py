"""Gmail query, MIME extraction and sender gating."""

from __future__ import annotations

import base64
from datetime import datetime, timezone

from remittance_reconciler.gmail import (
    EFT_FROM,
    EFT_QUERY,
    SCOPES,
    GmailClient,
    dkim_passed,
    extract_html_part,
    header_value,
)


def enc(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def payload(html: str | None = "<html><table></table></html>", *, sender: str = EFT_FROM,
            subject: str = "EFT Remittance Advice", dkim: str | None = "dkim=pass",
            nested: bool = True) -> dict:
    headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]
    if dkim is not None:
        headers.append({"name": "Authentication-Results", "value": f"mx.google.com; {dkim}"})
    body: dict = {"mimeType": "multipart/related", "headers": headers}
    parts: list[dict] = [{"mimeType": "text/plain", "body": {"data": enc("plain")}}]
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": enc(html)}})
    body["parts"] = [{"mimeType": "multipart/alternative", "parts": parts}] if nested else parts
    return body


def test_html_part_found_through_nested_multipart():
    assert "<table>" in extract_html_part(payload())


def test_plain_text_is_never_substituted_for_html():
    assert extract_html_part(payload(html=None)) == ""


def test_header_lookup_is_case_insensitive():
    assert header_value(payload(), "from") == EFT_FROM
    assert header_value(payload(), "SUBJECT").startswith("EFT Remittance")


def test_dkim_pass_detected():
    assert dkim_passed(payload()) is True


def test_dkim_absent_is_not_a_pass():
    assert dkim_passed(payload(dkim=None)) is False


def test_dkim_fail_is_not_a_pass():
    assert dkim_passed(payload(dkim="dkim=fail")) is False


def test_dkim_neutral_is_not_a_pass():
    assert dkim_passed(payload(dkim="dkim=neutral")) is False


class _FakeMessages:
    def __init__(self, msgs: list[dict]) -> None:
        self._msgs = msgs

    def list(self, **kw):
        self.q = kw.get("q", "")
        return self

    def list_next(self, *_a):
        return None

    def execute(self):
        return {"messages": [{"id": m["id"]} for m in self._msgs]}

    def get(self, userId=None, id=None, format=None, **kw):
        msg = next(m for m in self._msgs if m["id"] == id)
        return _Exec(msg)


class _Exec:
    def __init__(self, v): self._v = v
    def execute(self): return self._v


class _FakeService:
    def __init__(self, msgs): self._m = _FakeMessages(msgs)
    def users(self): return self
    def messages(self): return self._m


def _msg(mid: str, **kw) -> dict:
    return {"id": mid, "internalDate": "2130267200000", "payload": payload(**kw)}


FORWARDER = "billing@example-clinic.test"


def _fetch(msgs, **kw):
    client = GmailClient(
        _FakeService(msgs),
        trusted_forwarders=kw.pop("trusted_forwarders", (FORWARDER,)),
        require_dkim_pass=kw.pop("require_dkim_pass", True),
        require_dmarc_pass=kw.pop("require_dmarc_pass", False),
    )
    return list(client.fetch_eft_messages(datetime(2037, 7, 1, tzinfo=timezone.utc)))


def test_valid_message_is_yielded():
    out = _fetch([_msg("m1", sender=FORWARDER)])
    assert len(out) == 1
    assert out[0].message_id == "m1"
    assert out[0].dkim_pass is True
    assert out[0].internal_date.tzinfo is not None, "naive datetime breaks the G-0 comparison"


def test_wrong_sender_is_skipped():
    assert _fetch([_msg("m1", sender="attacker@example.com")]) == []


def test_direct_payer_sender_is_no_longer_trusted():
    assert _fetch([_msg("m1", sender=EFT_FROM)]) == []


def test_display_name_wrapped_address_still_matches():
    assert len(_fetch([_msg("m1", sender=f"Clinic Billing <{FORWARDER}>")])) == 1


def test_right_sender_wrong_subject_is_skipped_quietly():
    assert _fetch([_msg("m1", sender=FORWARDER, subject="Your ACME statement is ready")]) == []


def test_message_without_html_part_is_skipped():
    assert _fetch([_msg("m1", sender=FORWARDER, html=None)]) == []


def test_query_is_label_free_and_sender_free_and_carries_after():
    assert "label:" not in EFT_QUERY
    assert "from:" not in EFT_QUERY, "a from: clause makes forwarded mail invisible"
    assert EFT_FROM not in EFT_QUERY
    svc = _FakeService([_msg("m1", sender=FORWARDER)])
    list(GmailClient(svc, trusted_forwarders=(FORWARDER,), require_dmarc_pass=False)
         .fetch_eft_messages(datetime(2037, 7, 1, tzinfo=timezone.utc)))
    assert "after:2037/07/01" in svc._m.q
    assert "label:" not in svc._m.q


def test_scopes_are_least_privilege():
    assert set(SCOPES) == {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    }
