"""Gmail intake and report delivery.

Intake is deliberately narrow. A label-free subject query only finds candidates; a
deterministic trust gate then accepts a message only if it comes from an allow-listed forwarder,
has the expected subject and (by default) passes DKIM and DMARC with sender-domain alignment, as
recorded by the receiving mail server. Only the ``text/html`` part is parsed. OAuth scopes are
read-only plus send.
"""

from __future__ import annotations

import base64
import logging
import re
from email.utils import getaddresses
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

__all__ = [
    "GmailMessage",
    "EFT_QUERY",
    "EFT_FROM",
    "EFT_SUBJECT",
    "SCOPES",
    "GmailClient",
    "normalize_subject",
    "parse_addr",
    "dkim_domain",
    "dmarc_passed",
    "ar_header_from_domain",
    "extract_html_part",
    "header_value",
    "dkim_passed",
]

log = logging.getLogger(__name__)

SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
)

# The payer's original sender. Informational only: forwarding replaces From.
EFT_FROM = "remittance@example.com"

EFT_SUBJECT = "EFT Remittance Advice"

_SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fw|fwd|tr|aw|wg)\s*:\s*)+", re.I)


@dataclass(frozen=True, slots=True)
class GmailMessage:
    """A message that passed the intake gate. ``intake_trusted`` is set only by the gate itself."""
    message_id: str
    internal_date: datetime
    from_addr: str
    subject: str
    html: str
    dkim_pass: bool
    dmarc_pass: bool = False
    intake_trusted: bool = False


# Candidate narrowing only. Trust is decided in code, never by the search query.
EFT_QUERY = 'subject:"EFT Remittance Advice"'


def normalize_subject(subject: str) -> str:
    """Strip forwarding prefixes such as ``Fwd:`` and ``Re:`` and collapse whitespace."""
    s = _SUBJECT_PREFIX_RE.sub("", subject or "")
    return re.sub(r"\s+", " ", s).strip()


def parse_addr(from_header: str) -> str:
    """Return the single sender address from a ``From`` header, or ``""``.

    Uses the RFC 5322 parser. A regex that grabs the first ``<...>`` can be fooled by a display name
    that contains a trusted address.
    """
    addrs = [a for _n, a in getaddresses([from_header or ""]) if a]
    if len(addrs) != 1:
        return ""
    return addrs[0].strip().lower()


def dkim_domain(payload: dict) -> str:
    """DKIM signing domain as verified by the receiving server, never the raw DKIM-Signature header."""
    ar = re.sub(r"\([^)]*\)", " ", _authoritative_ar(payload))
    m = re.search(r"header\.d\s*=\s*([A-Za-z0-9.\-_]+)", ar)
    return (m.group(1) if m else "").strip().lower().rstrip(".")


def header_value(payload: dict, name: str) -> str:
    """Case-insensitive header lookup in a Gmail API payload."""
    want = name.lower()
    for h in payload.get("headers", ()) or ():
        if (h.get("name") or "").lower() == want:
            return h.get("value") or ""
    return ""


def _authoritative_ar(payload: dict, authserv_id: str = "") -> str:
    """Return only the topmost Authentication-Results header, which the receiving server adds.

    Senders can inject their own Authentication-Results headers further down the message.
    """
    for h in payload.get("headers", ()) or ():
        if (h.get("name") or "").lower() == "authentication-results":
            v = h.get("value") or ""
            if authserv_id and not v.strip().lower().startswith(authserv_id.strip().lower()):
                return ""
            return v
    return ""


def _ar_verdict(ar: str, method: str) -> str:
    """Extract the result for ``method`` from an Authentication-Results value, ignoring comments."""
    stripped = re.sub(r"\([^)]*\)", " ", ar)
    m = re.search(rf"\b{method}\s*=\s*([A-Za-z]+)", stripped)
    return (m.group(1) if m else "").lower()


def ar_header_from_domain(ar: str) -> str:
    """The ``header.from`` domain that DMARC was evaluated against."""
    m = re.search(r"header\.from\s*=\s*([A-Za-z0-9.\-_]+)", re.sub(r"\([^)]*\)", " ", ar))
    return (m.group(1) if m else "").strip().lower().rstrip(".")


def dmarc_passed(payload: dict) -> bool:
    """True only if the authoritative Authentication-Results header records ``dmarc=pass``."""
    return _ar_verdict(_authoritative_ar(payload), "dmarc") == "pass"


def dkim_passed(payload: dict) -> bool:
    """True only if the authoritative Authentication-Results header records ``dkim=pass``."""
    return _ar_verdict(_authoritative_ar(payload), "dkim") == "pass"


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("ascii") + b"=" * (-len(data) % 4))


def extract_html_part(payload: dict) -> str:
    """Depth-first search of the MIME tree for the ``text/html`` part. Never falls back to plain text."""
    mime = (payload.get("mimeType") or "").lower()
    body = payload.get("body") or {}
    if mime == "text/html" and body.get("data"):
        return _b64(body["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", ()) or ():
        found = extract_html_part(part)
        if found:
            return found
    return ""


class GmailClient:
    """Thin Gmail API wrapper for fetching remittance candidates and sending reports."""
    def __init__(
        self,
        service: object | None = None,
        sender: str = "me",
        *,
        trusted_forwarders: tuple[str, ...] = (),
        require_dkim_pass: bool = True,
        require_dmarc_pass: bool = True,
        trusted_dkim_domain_suffix: str = "",
        trusted_authserv_id: str = "mx.google.com",
    ) -> None:
        self._service = service
        self.sender = sender
        self.trusted_forwarders = tuple(a.strip().lower() for a in trusted_forwarders)
        self.require_dkim_pass = require_dkim_pass
        self.require_dmarc_pass = require_dmarc_pass
        self.trusted_dkim_domain_suffix = trusted_dkim_domain_suffix
        self.trusted_authserv_id = trusted_authserv_id

    @classmethod
    def from_config(cls, token_path: Path, cfg) -> "GmailClient":
        """Build a client from the stored token and the intake policy in ``cfg``."""
        c = cls.from_token(token_path)
        c.trusted_forwarders = tuple(a.strip().lower() for a in cfg.trusted_forwarders)
        c.require_dkim_pass = cfg.require_dkim_pass
        c.require_dmarc_pass = cfg.require_dmarc_pass
        c.trusted_dkim_domain_suffix = cfg.trusted_dkim_domain_suffix
        c.trusted_authserv_id = cfg.trusted_authserv_id
        return c


    @classmethod
    def from_token(cls, token_path: Path, client_secret_path: Path | None = None) -> "GmailClient":
        """Build a client from a stored OAuth token, refreshing it (0600) when expired."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(str(token_path), list(SCOPES))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            token_path.chmod(0o600)
        return cls(build("gmail", "v1", credentials=creds, cache_discovery=False))

    @classmethod
    def authorize(cls, client_secret_path: Path, token_path: Path) -> "GmailClient":
        """One-time interactive consent. Run by a person, never by the scheduled job."""
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), list(SCOPES))
        creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        token_path.chmod(0o600)
        return cls(build("gmail", "v1", credentials=creds, cache_discovery=False))


    def fetch_eft_messages(self, after: datetime) -> Iterator[GmailMessage]:
        """Yield trusted remittance messages. ``after`` only narrows the candidate search."""
        query = f"{EFT_QUERY} after:{after.astimezone(timezone.utc).strftime('%Y/%m/%d')}"
        svc = self._service.users().messages()  # type: ignore[union-attr]
        req = svc.list(userId="me", q=query, maxResults=100)
        while req is not None:
            resp = req.execute()
            for stub in resp.get("messages", ()) or ():
                msg = svc.get(userId="me", id=stub["id"], format="full").execute()
                parsed = self._to_message(msg)
                if parsed is not None:
                    yield parsed
            req = svc.list_next(req, resp)

    def _to_message(self, msg: dict) -> GmailMessage | None:
        """Apply the intake trust gate. Returns ``None`` unless the message was provably forwarded by a trusted sender."""
        payload = msg.get("payload") or {}
        mid = msg.get("id")
        addr = parse_addr(header_value(payload, "From"))
        subject = header_value(payload, "Subject")

        if addr not in self.trusted_forwarders:
            log.info("skipping %s: sender not a trusted forwarder", mid)
            return None

        if normalize_subject(subject).lower() != EFT_SUBJECT.lower():
            log.info("skipping %s: subject does not normalize to the EFT subject", mid)
            return None

        dkim_ok = dkim_passed(payload)
        if self.trusted_authserv_id and not _authoritative_ar(payload, self.trusted_authserv_id):
            log.warning("rejecting %s: no Authentication-Results from the trusted MTA", mid)
            return None
        if self.require_dkim_pass and not dkim_ok:
            log.warning("rejecting %s: trusted forwarder without dkim=pass", mid)
            return None
        if self.require_dmarc_pass:
            if not dmarc_passed(payload):
                log.warning("rejecting %s: trusted forwarder without dmarc=pass", mid)
                return None
            aligned = ar_header_from_domain(_authoritative_ar(payload, self.trusted_authserv_id))
            want = addr.rsplit("@", 1)[-1]
            if aligned and aligned != want:
                log.warning("rejecting %s: dmarc aligned on a different domain than the sender", mid)
                return None
        if self.trusted_dkim_domain_suffix:
            d = dkim_domain(payload)
            suffix = self.trusted_dkim_domain_suffix.lower().lstrip(".")
            if not (d == suffix or d.endswith("." + suffix)):
                log.warning("rejecting %s: DKIM d= is not the trusted forwarder domain", mid)
                return None

        html = extract_html_part(payload)
        if not html:
            log.info("skipping %s: no text/html part", msg.get("id"))
            return None

        internal = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc)
        return GmailMessage(
            message_id=msg["id"],
            internal_date=internal,
            from_addr=addr,
            subject=subject,
            html=html,
            dkim_pass=dkim_ok,
            dmarc_pass=dmarc_passed(payload),
            intake_trusted=True,
        )

    def send(self, to: str, subject: str, html_body: str) -> None:
        """Send one HTML report email."""
        mime = MIMEText(html_body, "html", "utf-8")
        mime["To"] = to
        mime["Subject"] = subject
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
        self._service.users().messages().send(  # type: ignore[union-attr]
            userId="me", body={"raw": raw}
        ).execute()
