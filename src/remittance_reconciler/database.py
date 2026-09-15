"""SQLite persistence: work queue, audit trail and at-most-once write ledger.

Runs in WAL mode with ``synchronous=FULL``. Provenance columns are written once at ingestion and
are excluded from the update whitelist. Write claims are never deleted, and each one is appended
to an fsync'd journal *before* its database row is inserted.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from .provenance import Provenance
from .models import (
    ClaimOutcome,
    ClaimRow,
    EftRow,
    EftStatement,
    OutboxRow,
    StatementRow,
    StatementState,
    Verdict,
    WarnCode,
    WorkRow,
    WorkState,
)

import logging

log = logging.getLogger(__name__)

__all__ = ["SCHEMA_SQL", "Database", "RUN_STATUS_SUCCESS", "RUN_STATUS_FAILED",
           "RUN_STATUS_RUNNING", "RUN_STATUS_LOGIN_REQUIRED",
           "RUN_STATUS_HALTED"]

RUN_STATUS_SUCCESS = "SUCCESS"
RUN_STATUS_FAILED = "FAILED"
RUN_STATUS_RUNNING = "RUNNING"
RUN_STATUS_LOGIN_REQUIRED = "LOGIN_REQUIRED"
RUN_STATUS_HALTED = "HALTED"


SCHEMA_SQL = """
-- Gmail delivery identity
CREATE TABLE IF NOT EXISTS emails (
    message_id          TEXT PRIMARY KEY,
    statement_id        INTEGER,          -- NULL when parsing failed
    gmail_internal_date TEXT NOT NULL,
    quarantine_reason   TEXT,             -- non-NULL = quarantined (V6/V9/PARSE_CRASH are retried)
    created_at          TEXT,
    -- Root of EFT provenance: without these columns we could not later answer
    --   "which forwarded email authorized this click?"
    from_addr           TEXT,
    dkim_pass           INTEGER,
    dmarc_pass          INTEGER,
    -- Trusted-forward gate result. Default 0 = untrusted (fail-closed).
    intake_trusted      INTEGER NOT NULL DEFAULT 0
);

-- Financial identity of an EFT statement
CREATE TABLE IF NOT EXISTS statements (
    id                  INTEGER PRIMARY KEY,
    vendor_no           TEXT NOT NULL,
    payment_document_no TEXT NOT NULL,
    deposit_amount      TEXT NOT NULL,
    eft_date            TEXT,
    content_fingerprint TEXT NOT NULL,    -- G-22
    state               TEXT NOT NULL,    -- PENDING | COMPLETED | TERMINAL_EXCEPTION
    attempt_count       INTEGER NOT NULL DEFAULT 0,   -- G-21 attempt ceiling, checked when finalizing
    last_error          TEXT,
    created_at          TEXT,
    updated_at          TEXT,
    UNIQUE(vendor_no, payment_document_no)
);

-- Report delivery is decoupled from financial processing (G-23)
CREATE TABLE IF NOT EXISTS report_outbox (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER,
    kind          TEXT,                   -- SUMMARY | HEARTBEAT | FAILURE
    payload       TEXT,
    state         TEXT,                   -- PENDING | SENT
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT,
    sent_at       TEXT
);

-- Silent-failure detection (G-20)
CREATE TABLE IF NOT EXISTS run_log (
    id                   INTEGER PRIMARY KEY,
    started_at           TEXT,
    finished_at          TEXT,
    statements_processed INTEGER,
    approved_count       INTEGER,
    approved_total       TEXT,
    manual_review_count  INTEGER,
    status               TEXT,
    error                TEXT
);

CREATE TABLE IF NOT EXISTS invoice_work (
    id              INTEGER PRIMARY KEY,
    statement_id    INTEGER NOT NULL,
    invoice_no      TEXT NOT NULL,        -- full identifier, suffix included
    row_date        TEXT,
    eft_gross       TEXT,
    eft_prev_paid   TEXT,
    eft_outstanding TEXT,
    eft_net         TEXT,
    portal_balance    TEXT,
    portal_payer      TEXT,
    portal_status     TEXT,
    portal_total      TEXT,
    portal_collected  TEXT,
    portal_location   TEXT,
    portal_invoice_id TEXT,
    portal_href       TEXT,
    -- Immutable provenance: filled only by insert_work_rows from a parsed EftRow
    --   and absent from the update_work whitelist, so no later path can change it.
    --   NULL means "not derived from a parsed EFT row" and is grounds to refuse writes.
    raw_eft_invoice_no  TEXT,
    eft_net_provenance  TEXT,
    warnings        TEXT,                 -- warning codes raised
    verdict         TEXT,
    state           TEXT,                 -- PARSED | RECONCILED | DETAIL_VALIDATED
                                          -- | PENDING_WRITE | CONFIRMED | NO_ACTION
                                          -- | MANUAL_REVIEW
    attribution     TEXT,
    error_code      TEXT,
    created_at      TEXT,
    updated_at      TEXT
);

-- Invariant: a row's existence means "this invoice crossed the irreversible write boundary".
-- Rows are never deleted.
CREATE TABLE IF NOT EXISTS write_claims (
    invoice_no      TEXT PRIMARY KEY,
    statement_id    INTEGER,
    portal_invoice_id TEXT,
    claimed_amount  TEXT,
    claimed_at      TEXT,
    final_outcome   TEXT,                 -- NULL | CONFIRMED | UNKNOWN_OUTCOME
    -- Audit trail: "which forwarded EFT statement authorized this click?" must be
    --   answerable from the database alone. Never store patient information.
    source_message_id   TEXT,
    vendor_no           TEXT,
    payment_document_no TEXT,
    content_fingerprint TEXT,
    raw_eft_invoice_no  TEXT
);

CREATE INDEX IF NOT EXISTS idx_statements_state    ON statements(state);
CREATE INDEX IF NOT EXISTS idx_work_statement      ON invoice_work(statement_id);
CREATE INDEX IF NOT EXISTS idx_outbox_state        ON report_outbox(state);
CREATE INDEX IF NOT EXISTS idx_emails_statement    ON emails(statement_id);

-- The at-most-once ledger must be case-insensitive.
--   The payer mixes ``-b01`` and ``-B01`` for the same invoice. A case-sensitive
--   key would admit a second claim for the same invoice and silently break the
--   invariant "an existing claim forbids any re-click".
CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_invoice_nocase
    ON write_claims(invoice_no COLLATE NOCASE);
"""


class Database:
    """SQLite connection wrapper with explicit transactions and typed row mapping."""
    _WORK_COLUMNS = frozenset(
        """statement_id invoice_no row_date eft_gross eft_prev_paid eft_outstanding
        eft_net portal_balance portal_payer portal_status portal_total portal_collected
        portal_location portal_invoice_id portal_href warnings verdict state attribution
        error_code""".split()
    )

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        try:
            self.path.chmod(0o600)
        except OSError:  # pragma: no cover
            pass
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")


    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    _ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("emails", "from_addr", "TEXT"),
        ("emails", "dkim_pass", "INTEGER"),
        ("emails", "dmarc_pass", "INTEGER"),
        ("emails", "intake_trusted", "INTEGER NOT NULL DEFAULT 0"),
        ("invoice_work", "raw_eft_invoice_no", "TEXT"),
        ("invoice_work", "eft_net_provenance", "TEXT"),
        ("write_claims", "source_message_id", "TEXT"),
        ("write_claims", "vendor_no", "TEXT"),
        ("write_claims", "payment_document_no", "TEXT"),
        ("write_claims", "content_fingerprint", "TEXT"),
        ("write_claims", "raw_eft_invoice_no", "TEXT"),
    )

    def migrate(self) -> None:
        """Create the schema and add any columns introduced after the first deployment."""
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA_SQL)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        for table, col, decl in self._ADDED_COLUMNS:
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self.conn.commit()

    @contextmanager
    def transaction(self) -> Iterator["Database"]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT`` or ``ROLLBACK``."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")


    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _dec(v: object) -> Decimal | None:
        return None if v is None or v == "" else Decimal(str(v))

    @staticmethod
    def _txt(v: object) -> str | None:
        return None if v is None else str(v)

    @staticmethod
    def _date(v: object) -> date | None:
        return None if v in (None, "") else date.fromisoformat(str(v))

    @staticmethod
    def _dt(v: object) -> datetime | None:
        return None if v in (None, "") else datetime.fromisoformat(str(v))

    @staticmethod
    def _warns(v: object) -> tuple[WarnCode, ...]:
        if not v:
            return ()
        return tuple(WarnCode(x) for x in str(v).split(",") if x)


    def email_exists(self, message_id: str) -> bool:
        """True if this Gmail message id has been seen before (parsed or quarantined)."""
        r = self.conn.execute(
            "SELECT 1 FROM emails WHERE message_id=?", (message_id,)
        ).fetchone()
        return r is not None

    def insert_email(
        self,
        message_id: str,
        gmail_internal_date: datetime,
        statement_id: int | None = None,
        quarantine_reason: str | None = None,
        *,
        from_addr: str | None = None,
        dkim_pass: bool | None = None,
        dmarc_pass: bool | None = None,
        intake_trusted: bool = False,
    ) -> None:
        """Record a message and its intake-gate results."""
        self.conn.execute(
            "INSERT INTO emails"
            " (message_id, statement_id, gmail_internal_date, quarantine_reason, created_at,"
            "  from_addr, dkim_pass, dmarc_pass, intake_trusted)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                message_id,
                statement_id,
                gmail_internal_date.isoformat(),
                quarantine_reason,
                self._now(),
                from_addr,
                None if dkim_pass is None else int(dkim_pass),
                None if dmarc_pass is None else int(dmarc_pass),
                int(bool(intake_trusted)),
            ),
        )

    def quarantine_reason(self, message_id: str) -> str | None:
        r = self.conn.execute(
            "SELECT quarantine_reason FROM emails WHERE message_id=?", (message_id,)
        ).fetchone()
        return r["quarantine_reason"] if r else None

    def upsert_quarantine(
        self, message_id: str, gmail_internal_date: datetime, reason: str
    ) -> None:
        """Record or update the quarantine reason for a message."""
        self.conn.execute(
            "INSERT INTO emails (message_id, statement_id, gmail_internal_date,"
            " quarantine_reason, created_at) VALUES (?,NULL,?,?,?)"
            " ON CONFLICT(message_id) DO UPDATE SET quarantine_reason=excluded.quarantine_reason",
            (message_id, gmail_internal_date.isoformat(), reason, self._now()),
        )

    def link_email(
        self, message_id: str, statement_id: int, *,
        from_addr: str | None = None,
        dkim_pass: bool | None = None,
        dmarc_pass: bool | None = None,
        intake_trusted: bool = False,
    ) -> None:
        """Attach a message to a statement and clear any earlier quarantine."""
        self.conn.execute(
            "UPDATE emails SET statement_id=?, quarantine_reason=NULL,"
            " from_addr=?, dkim_pass=?, dmarc_pass=?, intake_trusted=?"
            " WHERE message_id=?",
            (
                statement_id,
                from_addr,
                None if dkim_pass is None else int(dkim_pass),
                None if dmarc_pass is None else int(dmarc_pass),
                int(bool(intake_trusted)),
                message_id,
            ),
        )

    def source_email(self, statement_id: int):
        """The first message that produced a statement (the root of its provenance)."""
        return self.conn.execute(
            "SELECT message_id, quarantine_reason, intake_trusted, from_addr,"
            " dkim_pass, dmarc_pass FROM emails WHERE statement_id=?"
            " ORDER BY created_at, message_id LIMIT 1",
            (statement_id,),
        ).fetchone()

    def statement_provenance_sum(self, statement_id: int) -> tuple[Decimal, int, int]:
        """Sum of immutable parsed amounts for a statement, plus row and missing-provenance counts."""
        r = self.conn.execute(
            "SELECT COUNT(*) AS n,"
            " SUM(CASE WHEN eft_net_provenance IS NULL THEN 1 ELSE 0 END) AS missing"
            " FROM invoice_work WHERE statement_id=?",
            (statement_id,),
        ).fetchone()
        total = Decimal("0.00")
        for row in self.conn.execute(
            "SELECT eft_net_provenance FROM invoice_work"
            " WHERE statement_id=? AND eft_net_provenance IS NOT NULL",
            (statement_id,),
        ):
            total += Decimal(row["eft_net_provenance"])
        return total, r["n"], r["missing"] or 0

    def authorized_write_candidates(self) -> list[WorkRow]:
        """Rows that are write-eligible by state and provenance. Never consults the portal."""
        rows = self.conn.execute(
            "SELECT w.statement_id AS sid FROM invoice_work w"
            " JOIN statements s ON s.id = w.statement_id"
            " JOIN emails e ON e.statement_id = s.id"
            " WHERE w.state = ?"
            "   AND w.verdict = ?"
            "   AND w.raw_eft_invoice_no IS NOT NULL"
            "   AND w.eft_net_provenance IS NOT NULL"
            "   AND s.state != ?"
            "   AND e.intake_trusted = 1"
            "   AND e.quarantine_reason IS NULL"
            "   AND NOT EXISTS (SELECT 1 FROM write_claims c WHERE c.invoice_no = w.invoice_no)"
            " GROUP BY w.statement_id",
            (
                WorkState.DETAIL_VALIDATED.value,
                Verdict.APPROVE_OK.value,
                StatementState.TERMINAL_EXCEPTION.value,
            ),
        ).fetchall()
        out: list[WorkRow] = []
        for r in rows:
            for w in self.work_rows(r["sid"]):
                if not (
                    w.state is WorkState.DETAIL_VALIDATED
                    and w.verdict is Verdict.APPROVE_OK
                    and self.get_claim(w.invoice_no) is None
                ):
                    continue
                from .provenance import verify_provenance

                if verify_provenance(self, w)[1] is None:
                    out.append(w)
        return sorted(out, key=lambda w: (w.statement_id, w.invoice_no))


    def _statement_row(self, r: sqlite3.Row) -> StatementRow:
        return StatementRow(
            id=r["id"],
            vendor_no=r["vendor_no"],
            payment_document_no=r["payment_document_no"],
            deposit_amount=Decimal(r["deposit_amount"]),
            eft_date=self._date(r["eft_date"]),
            content_fingerprint=r["content_fingerprint"],
            state=StatementState(r["state"]),
            attempt_count=r["attempt_count"],
            last_error=r["last_error"],
            created_at=self._dt(r["created_at"]),
            updated_at=self._dt(r["updated_at"]),
        )

    def find_statement(self, vendor_no: str, payment_document_no: str) -> StatementRow | None:
        r = self.conn.execute(
            "SELECT * FROM statements WHERE vendor_no=? AND payment_document_no=?",
            (vendor_no, payment_document_no),
        ).fetchone()
        return self._statement_row(r) if r else None

    def get_statement(self, sid: int) -> StatementRow | None:
        r = self.conn.execute("SELECT * FROM statements WHERE id=?", (sid,)).fetchone()
        return self._statement_row(r) if r else None

    def create_statement(self, st: EftStatement) -> int:
        """Insert a new PENDING statement."""
        now = self._now()
        cur = self.conn.execute(
            "INSERT INTO statements"
            " (vendor_no, payment_document_no, deposit_amount, eft_date,"
            "  content_fingerprint, state, attempt_count, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,0,?,?)",
            (
                st.vendor_no,
                st.payment_document_no,
                str(st.deposit_amount),
                st.eft_date.isoformat(),
                st.content_fingerprint,
                StatementState.PENDING.value,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)

    def set_statement_state(
        self, sid: int, state: StatementState, last_error: str | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE statements SET state=?, last_error=?, updated_at=? WHERE id=?",
            (StatementState(state).value, last_error, self._now(), sid),
        )

    def bump_attempt(self, sid: int) -> int:
        self.conn.execute(
            "UPDATE statements SET attempt_count=attempt_count+1, updated_at=? WHERE id=?",
            (self._now(), sid),
        )
        r = self.conn.execute(
            "SELECT attempt_count FROM statements WHERE id=?", (sid,)
        ).fetchone()
        return int(r["attempt_count"]) if r else 0

    def terminal_statements(self) -> list[StatementRow]:
        """Statements abandoned as TERMINAL_EXCEPTION; named in every report."""
        rows = self.conn.execute(
            "SELECT * FROM statements WHERE state=? ORDER BY id",
            (StatementState.TERMINAL_EXCEPTION.value,),
        ).fetchall()
        return [self._statement_row(r) for r in rows]

    def pending_statements(self) -> list[StatementRow]:
        """The daily work queue: every PENDING statement, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM statements WHERE state=? ORDER BY id",
            (StatementState.PENDING.value,),
        ).fetchall()
        return [self._statement_row(r) for r in rows]


    def insert_work_rows(self, sid: int, rows: Sequence[EftRow]) -> None:
        """Persist parsed rows with immutable provenance. Rows with malformed identifiers start as MANUAL_REVIEW."""
        now = self._now()
        self.conn.executemany(
            "INSERT INTO invoice_work"
            " (statement_id, invoice_no, row_date, eft_gross, eft_prev_paid,"
            "  eft_outstanding, eft_net, raw_eft_invoice_no, eft_net_provenance,"
            "  state, verdict, error_code, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    sid,
                    r.invoice_no,
                    r.row_date.isoformat(),
                    str(r.gross),
                    str(r.prev_paid),
                    str(r.outstanding),
                    str(r.net),
                    r.invoice_no,
                    str(r.net),
                    (WorkState.PARSED.value if r.identifier_ok
                     else WorkState.MANUAL_REVIEW.value),
                    None if r.identifier_ok else Verdict.MANUAL_REVIEW.value,
                    None if r.identifier_ok else "V6_ROW_IDENTIFIER",
                    now,
                    now,
                )
                for r in rows
            ],
        )

    def work_rows(self, sid: int) -> list[WorkRow]:
        rows = self.conn.execute(
            "SELECT w.*, s.vendor_no AS _vendor_no FROM invoice_work w"
            " LEFT JOIN statements s ON s.id = w.statement_id"
            " WHERE w.statement_id=? ORDER BY w.id",
            (sid,),
        ).fetchall()
        return [
            WorkRow(
                id=r["id"],
                statement_id=r["statement_id"],
                invoice_no=r["invoice_no"],
                row_date=self._date(r["row_date"]),
                eft_gross=self._dec(r["eft_gross"]),
                eft_prev_paid=self._dec(r["eft_prev_paid"]),
                eft_outstanding=self._dec(r["eft_outstanding"]),
                eft_net=self._dec(r["eft_net"]),
                portal_balance=self._dec(r["portal_balance"]),
                portal_payer=r["portal_payer"],
                portal_status=r["portal_status"],
                portal_total=self._dec(r["portal_total"]),
                portal_collected=self._dec(r["portal_collected"]),
                portal_location=r["portal_location"],
                portal_invoice_id=r["portal_invoice_id"],
                portal_href=r["portal_href"],
                raw_eft_invoice_no=r["raw_eft_invoice_no"],
                eft_net_provenance=self._dec(r["eft_net_provenance"]),
                warnings=self._warns(r["warnings"]),
                verdict=Verdict(r["verdict"]) if r["verdict"] else None,
                state=WorkState(r["state"]) if r["state"] else WorkState.PARSED,
                attribution=r["attribution"],
                error_code=r["error_code"],
                created_at=self._dt(r["created_at"]),
                updated_at=self._dt(r["updated_at"]),
                vendor_no=r["_vendor_no"] or "",
            )
            for r in rows
        ]

    def work_row_by_id(self, work_id: int) -> WorkRow | None:
        r = self.conn.execute(
            "SELECT statement_id FROM invoice_work WHERE id=?", (work_id,)
        ).fetchone()
        if r is None:
            return None
        for w in self.work_rows(r["statement_id"]):
            if w.id == work_id:
                return w
        return None

    def update_work(self, work_id: int, **fields) -> None:
        """Update whitelisted columns only. Provenance columns cannot be changed."""
        if not fields:
            return
        bad = set(fields) - self._WORK_COLUMNS
        if bad:
            raise ValueError(f"unknown invoice_work column(s): {sorted(bad)}")

        def enc(k: str, v: object) -> object:
            if v is None:
                return None
            if k == "warnings":
                if isinstance(v, (list, tuple)):
                    return ",".join(str(WarnCode(x).value) for x in v)
                return str(v)
            if isinstance(v, Decimal):
                return str(v)
            if isinstance(v, (date, datetime)):
                return v.isoformat()
            if isinstance(v, StrEnum):
                return v.value
            return v

        cols = ", ".join(f"{k}=?" for k in fields)
        vals = [enc(k, v) for k, v in fields.items()]
        self.conn.execute(
            f"UPDATE invoice_work SET {cols}, updated_at=? WHERE id=?",
            (*vals, self._now(), work_id),
        )


    def get_claim(self, invoice_no: str) -> ClaimRow | None:
        """Case-insensitive write-claim lookup."""
        r = self.conn.execute(
            "SELECT * FROM write_claims WHERE invoice_no=? COLLATE NOCASE",
            (invoice_no,),
        ).fetchone()
        if not r:
            return None
        return ClaimRow(
            invoice_no=r["invoice_no"],
            statement_id=r["statement_id"],
            portal_invoice_id=r["portal_invoice_id"],
            claimed_amount=Decimal(r["claimed_amount"]),
            claimed_at=self._dt(r["claimed_at"]),
            final_outcome=ClaimOutcome(r["final_outcome"]) if r["final_outcome"] else None,
            source_message_id=r["source_message_id"],
            vendor_no=r["vendor_no"],
            payment_document_no=r["payment_document_no"],
            content_fingerprint=r["content_fingerprint"],
            raw_eft_invoice_no=r["raw_eft_invoice_no"],
        )

    def insert_claim(self, work: WorkRow, prov: Provenance) -> None:
        """Record a write claim: journal line first, then the database row. Requires matching provenance."""
        if prov.raw_eft_invoice_no != work.invoice_no:
            raise ValueError(
                f"provenance is for {prov.raw_eft_invoice_no!r},"
                f" not {work.invoice_no!r}"
            )
        if prov.statement_id != work.statement_id:
            raise ValueError("provenance statement_id does not match the work row")
        if prov.eft_net != work.eft_net:
            raise ValueError("provenance amount does not match the work row")

        self._append_claim_journal({
            "invoice_no": work.invoice_no,
            "statement_id": prov.statement_id,
            "portal_invoice_id": work.portal_invoice_id or "",
            "claimed_amount": str(prov.eft_net),
            "claimed_at": self._now(),
            "source_message_id": prov.source_message_id,
            "vendor_no": prov.vendor_no,
            "payment_document_no": prov.payment_document_no,
            "content_fingerprint": prov.content_fingerprint,
            "raw_eft_invoice_no": prov.raw_eft_invoice_no,
        })
        self.conn.execute(
            "INSERT INTO write_claims"
            " (invoice_no, statement_id, portal_invoice_id, claimed_amount, claimed_at,"
            "  final_outcome, source_message_id, vendor_no, payment_document_no,"
            "  content_fingerprint, raw_eft_invoice_no)"
            " VALUES (?,?,?,?,?,NULL,?,?,?,?,?)",
            (
                work.invoice_no,
                prov.statement_id,
                work.portal_invoice_id or "",
                str(prov.eft_net),
                self._now(),
                prov.source_message_id,
                prov.vendor_no,
                prov.payment_document_no,
                prov.content_fingerprint,
                prov.raw_eft_invoice_no,
            ),
        )
        self.conn.commit()

    @property
    def claim_journal_path(self) -> Path:
        return self.path.with_name(self.path.name + ".claims.jsonl")

    def _append_claim_journal(self, record: dict) -> None:
        """Append one fsync'd JSON line to the owner-only claim journal."""
        line = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
        path = self.claim_journal_path
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover
            pass

    def read_claim_journal(self) -> list[dict]:
        """Read the claim journal, skipping malformed lines."""
        path = self.claim_journal_path
        if not path.is_file():
            return []
        out: list[dict] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except ValueError:
                log.warning("claim journal has a malformed line; skipping it")
        return out

    def set_claim_outcome(self, invoice_no: str, outcome: ClaimOutcome) -> None:
        """Record CONFIRMED or UNKNOWN_OUTCOME on a claim."""
        self.conn.execute(
            "UPDATE write_claims SET final_outcome=? WHERE invoice_no=? COLLATE NOCASE",
            (ClaimOutcome(outcome).value, invoice_no),
        )
        self.conn.commit()


    def enqueue_report(self, run_id: int, kind: str, payload: str) -> int:
        """Queue a report for delivery, independently of financial processing."""
        cur = self.conn.execute(
            "INSERT INTO report_outbox (run_id, kind, payload, state, attempt_count, created_at)"
            " VALUES (?,?,?,'PENDING',0,?)",
            (run_id, kind, payload, self._now()),
        )
        return int(cur.lastrowid)

    def pending_reports(self) -> list[OutboxRow]:
        """Reports that have not been delivered yet."""
        rows = self.conn.execute(
            "SELECT * FROM report_outbox WHERE state='PENDING' ORDER BY id"
        ).fetchall()
        return [
            OutboxRow(
                id=r["id"],
                run_id=r["run_id"],
                kind=r["kind"],
                payload=r["payload"],
                state=r["state"],
                attempt_count=r["attempt_count"],
                created_at=self._dt(r["created_at"]),
                sent_at=self._dt(r["sent_at"]),
            )
            for r in rows
        ]

    def mark_report_sent(self, outbox_id: int) -> None:
        self.conn.execute(
            "UPDATE report_outbox SET state='SENT', sent_at=? WHERE id=?",
            (self._now(), outbox_id),
        )

    def bump_report_attempt(self, outbox_id: int) -> int:
        self.conn.execute(
            "UPDATE report_outbox SET attempt_count=attempt_count+1 WHERE id=?",
            (outbox_id,),
        )
        r = self.conn.execute(
            "SELECT attempt_count FROM report_outbox WHERE id=?", (outbox_id,)
        ).fetchone()
        return int(r["attempt_count"]) if r else 0

    def start_run(self) -> int:
        """Open a ``run_log`` entry."""
        cur = self.conn.execute(
            "INSERT INTO run_log (started_at, status) VALUES (?, ?)",
            (self._now(), RUN_STATUS_RUNNING),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, **stats) -> None:
        """Close a ``run_log`` entry with its final counters and status."""
        allowed = {
            "statements_processed",
            "approved_count",
            "approved_total",
            "manual_review_count",
            "status",
            "error",
        }
        bad = set(stats) - allowed
        if bad:
            raise ValueError(f"unknown run_log column(s): {sorted(bad)}")
        vals = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in stats.items()}
        cols = ", ".join(f"{k}=?" for k in vals)
        sql = f"UPDATE run_log SET finished_at=?{', ' + cols if cols else ''} WHERE id=?"
        self.conn.execute(sql, (self._now(), *vals.values(), run_id))

    def last_successful_run(self) -> datetime | None:
        """Finish time of the most recent successful run, if any."""
        r = self.conn.execute(
            "SELECT finished_at FROM run_log"
            " WHERE status = ? AND finished_at IS NOT NULL"
            " ORDER BY datetime(finished_at) DESC LIMIT 1",
            (RUN_STATUS_SUCCESS,),
        ).fetchone()
        return self._dt(r["finished_at"]) if r else None
