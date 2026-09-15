"""Orchestration and command-line entry point.

One run: start-window check -> exclusive lock -> scratch cleanup -> report outbox ->
Gmail intake -> DB-driven queue -> portal authentication -> for each statement: reconcile ->
detail validation -> guarded writes -> finalize -> report.
"""

from __future__ import annotations

import fcntl
import json
import os
import logging
from logging.handlers import RotatingFileHandler
import time
from dataclasses import dataclass
from decimal import Decimal
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import IO

from .config import Config, load_config
from .database import Database
from .gmail import GmailClient
from .portal import LoginRequired, PortalSession
from .database import (
    RUN_STATUS_FAILED,
    RUN_STATUS_HALTED,
    RUN_STATUS_LOGIN_REQUIRED,
    RUN_STATUS_SUCCESS,
)
from .models import RunStats, StatementRow, StatementState, WorkState
from .report import build_login_required, build_failure, build_heartbeat, build_summary
from .parser import ParseError, content_fingerprint, parse_statement
from .portal import RECORD_PAYMENT, IdentityMismatch, PortalError, MenuError
from .portal_csv import parse_sales_csv
from .models import ClaimOutcome, Decision, EftRow, Verdict, WarnCode
from .provenance import Provenance, verify_provenance
from .writepath import (
    AlreadyPaid, WriteAnomaly, WriteBlocked, execute_one_authorized_work,
)
from .reconcile import (
    check_export_count,
    classify,
    canonical_invoice_key,
    normalize_invoice_no,
    sales_report_window,
    statement_age_days,
    statement_is_complete,
    verdict_to_state,
)

log = logging.getLogger(__name__)

STATEMENT_CONTENT_CONFLICT = "STATEMENT_CONTENT_CONFLICT"

G9_MAX_CONSECUTIVE_FAILURES = 3

DEFAULT_MAX_STATEMENT_ATTEMPTS = 5

# Quarantine reasons retried on later runs (for example after a vendor is added to config).
RECOVERABLE_QUARANTINE = frozenset({"V9", "PARSE_CRASH", "V6"})

__all__ = [
    "acquire_lock",
    "clean_scratch",
    "flush_report_outbox",
    "ingest_messages",
    "build_queue",
    "reconcile_statement",
    "execute_statement",
    "finalize_statement",
    "run_once",
    "main",
]


def acquire_lock(lock_path: Path) -> IO[bytes]:
    """Take a non-blocking exclusive ``flock``; raise if another run holds it (G-12)."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("ab+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise RuntimeError(f"another run already holds {lock_path}") from None
    return fh


def clean_scratch(scratch_dir: Path) -> int:
    """Delete files a crashed run left in ``scratch/``. Only called while the lock is held."""
    scratch_dir = Path(scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    removed = 0
    for child in scratch_dir.iterdir():
        if child.name == ".gitkeep" or child.is_dir():
            continue
        try:
            child.unlink()
            removed += 1
        except OSError:  # pragma: no cover
            log.warning("could not remove scratch leftover: %s", child.name)
    return removed


def flush_report_outbox(db: Database, gmail: GmailClient, cfg: Config) -> int:
    """Send pending reports. Failures stay queued and never affect financial state (G-23)."""
    sent = 0
    for rep in db.pending_reports():
        if rep.attempt_count >= cfg.max_statement_attempts:
            _record_incident(
                Path(db.path).resolve().parent.parent,
                "REPORT_UNDELIVERED",
                f"report id={rep.id} kind={rep.kind} attempts={rep.attempt_count}"
                f" still undelivered",
            )
        subject, body = _split_payload(rep.payload)
        if not cfg.report_recipients:
            log.error("report %s has no recipients configured; keeping it PENDING",
                      rep.id)
            _record_incident(
                Path(db.path).resolve().parent.parent, "NO_REPORT_RECIPIENTS",
                f"report id={rep.id} kind={rep.kind} could not be delivered:"
                " report_recipients is empty")
            continue
        try:
            for to in cfg.report_recipients:
                gmail.send(to, subject, body)
        except Exception as exc:
            db.bump_report_attempt(rep.id)
            log.warning("report %s send failed: %s", rep.id, exc)
            continue
        db.mark_report_sent(rep.id)
        sent += 1
    return sent


def _split_payload(payload: str) -> tuple[str, str]:
    subject, _, body = payload.partition("\n\n")
    return subject, body


def ingest_messages(
    db: Database, gmail: GmailClient, cfg: Config, events: list[str] | None = None,
    *, historical_allow: frozenset[str] = frozenset(),
) -> int:
    """Turn trusted remittance emails into statements and work rows.

    Applies the cutover guard (G-0), quarantines parse failures and stale statements (G-19), detects
    same-key/different-content conflicts (G-22) and writes each statement atomically. Ingestion only
    adds to the queue; processing is driven by statement state.
    """
    added = 0
    scan_from = cfg.automation_start_at.replace(year=2000) if historical_allow \
        else cfg.automation_start_at
    for msg in gmail.fetch_eft_messages(scan_from):
        if (msg.internal_date < cfg.automation_start_at
                and msg.message_id not in historical_allow):
            continue
        if db.email_exists(msg.message_id):
            prior = db.quarantine_reason(msg.message_id)
            if prior is None or prior not in RECOVERABLE_QUARANTINE:
                continue
            log.info("retrying %s previously quarantined as %s", msg.message_id, prior)

        try:
            st = parse_statement(msg.html, cfg.known_vendors)
        except ParseError as exc:
            db.upsert_quarantine(msg.message_id, msg.internal_date, exc.code)
            log.warning("quarantined %s: %s", msg.message_id, exc.code)
            _note(events, f"QUARANTINE {exc.code}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            db.upsert_quarantine(msg.message_id, msg.internal_date, "PARSE_CRASH")
            log.exception("parse crashed for %s", msg.message_id)
            _note(events, f"QUARANTINE PARSE_CRASH ({type(exc).__name__}): statement not ingested")
            continue

        age = statement_age_days(st.eft_date, msg.internal_date.date())
        if age > cfg.max_statement_age_days:
            db.upsert_quarantine(msg.message_id, msg.internal_date, "G19_DATE_SKEW")
            log.warning("quarantined %s: statement is %s days old at receipt",
                        msg.message_id, age)
            _note(events, f"QUARANTINE G19_DATE_SKEW: statement {age} days old at receipt")
            continue

        fp = st.content_fingerprint or content_fingerprint(
            st.vendor_no, st.payment_document_no, st.eft_date, st.deposit_amount, st.rows
        )
        existing = db.find_statement(st.vendor_no, st.payment_document_no)

        if existing is not None and existing.content_fingerprint != fp:
            with db.transaction():
                db.upsert_quarantine(
                    msg.message_id, msg.internal_date, STATEMENT_CONTENT_CONFLICT
                )
                if existing.state == StatementState.PENDING:
                    db.set_statement_state(
                        existing.id,
                        StatementState.TERMINAL_EXCEPTION,
                        STATEMENT_CONTENT_CONFLICT,
                    )
            log.error("G-22 content conflict on (%s,%s)", st.vendor_no, st.payment_document_no)
            _note(events, "G-22 CONTENT CONFLICT: incoming email quarantined"
                          + ("; existing PENDING statement halted" if existing.state ==
                             StatementState.PENDING else "; existing history left unchanged"))
            continue

        with db.transaction():
            if existing is None:
                sid = db.create_statement(st)
                db.insert_work_rows(sid, st.rows)
                added += 1
            else:
                sid = existing.id
            if db.email_exists(msg.message_id):
                db.link_email(
                    msg.message_id, sid,
                    from_addr=msg.from_addr,
                    dkim_pass=msg.dkim_pass,
                    dmarc_pass=msg.dmarc_pass,
                    intake_trusted=msg.intake_trusted,
                )
            else:
                db.insert_email(
                    msg.message_id, msg.internal_date, statement_id=sid,
                    from_addr=msg.from_addr,
                    dkim_pass=msg.dkim_pass,
                    dmarc_pass=msg.dmarc_pass,
                    intake_trusted=msg.intake_trusted,
                )
    return added


def _note(events: list[str] | None, msg: str) -> None:
    if events is not None:
        events.append(msg)


def build_queue(db: Database) -> list[StatementRow]:
    """The work queue is every PENDING statement in the database, not the mailbox."""
    return db.pending_statements()


def reconcile_statement(
    db: Database, portal: PortalSession, cfg: Config, st: StatementRow
) -> None:
    """Read-only: export the sales report, check export integrity, classify every row and persist the verdicts."""
    rows = db.work_rows(st.id)
    if not rows:
        return
    eft_rows = [
        EftRow(r.invoice_no, r.row_date, "", "", r.eft_gross, r.eft_prev_paid,
               r.eft_outstanding, r.eft_net)
        for r in rows
    ]
    lo, hi = sales_report_window(eft_rows, buffer_days=0)
    portal_rows, screen = _export_and_parse(portal, cfg, lo, hi, all_invoice_states=False)

    ok, warns, reason = check_export_count(
        len(portal_rows), len({canonical_invoice_key(j.invoice_no) for j in portal_rows}), screen, screen
    )
    if not ok:
        raise PortalError(f"G-7 integrity failed: {reason}")

    index = {canonical_invoice_key(j.invoice_no): j for j in portal_rows}
    if len(index) != len(portal_rows):
        raise PortalError("G-6: identifier collision while building the portal index")

    decisions: list[tuple] = []
    for work, row in zip(rows, eft_rows, strict=True):
        portal_row = index.get(canonical_invoice_key(work.invoice_no))
        decisions.append((work, row, portal_row, classify(row, portal_row)))

    missing = [d for d in decisions if d[3].verdict is Verdict.NOT_FOUND]
    if missing:
        wide_lo = lo - timedelta(days=cfg.date_buffer_days + 14)
        wide_hi = hi + timedelta(days=cfg.date_buffer_days + 14)
        wide_rows, wide_screen = _export_and_parse(
            portal, cfg, wide_lo, wide_hi, all_invoice_states=True
        )
        ok2, warns2, reason2 = check_export_count(
            len(wide_rows), len({canonical_invoice_key(j.invoice_no) for j in wide_rows}),
            wide_screen, wide_screen,
        )
        if not ok2:
            log.warning("fallback export failed G-7 (%s); NOT_FOUND rows left unresolved", reason2)
        else:
            wide = {canonical_invoice_key(j.invoice_no): j for j in wide_rows}
            for i, (work, row, _old, _d) in enumerate(decisions):
                if _d.verdict is not Verdict.NOT_FOUND:
                    continue
                j = wide.get(canonical_invoice_key(work.invoice_no))
                if j is None:
                    continue
                status = (j.status or "").strip().lower()
                if status == "paid" and j.balance == 0 and j.collected >= row.net:
                    d2 = Decision(Verdict.ALREADY_PAID, (), "paid before automation reached it")
                elif status == "paid":
                    d2 = Decision(Verdict.MANUAL_REVIEW, (),
                                  f"marked paid but collected={j.collected} balance={j.balance}")
                else:
                    d2 = classify(row, j)
                    d2 = Decision(d2.verdict,
                                  tuple(d2.warnings) + (WarnCode.DATE_WINDOW_DRIFT,),
                                  d2.reason)
                decisions[i] = (work, row, j, d2)

    for work, _row, portal_row, decision in decisions:
        _persist_decision(db, work, portal_row, decision, extra_warnings=warns)


def _export_and_parse(portal: PortalSession, cfg: Config, lo, hi, *, all_invoice_states: bool):
    """Export, parse and delete the CSV. The file never outlives the call."""
    portal._open_sales_report(lo, hi, all_invoice_states=all_invoice_states)
    screen = portal.visible_invoice_count()
    path = portal.export_outstanding_csv(
        lo, hi, all_invoice_states=all_invoice_states,
        scratch_dir=Path(cfg.scratch_dir) if cfg.scratch_dir else None,
    )
    try:
        rows = parse_sales_csv(path.read_text(encoding="utf-8-sig"))
    finally:
        path.unlink(missing_ok=True)
    return rows, screen


def _persist_decision(db: Database, work, portal_row, decision, extra_warnings=()) -> None:
    fields = {
        "verdict": decision.verdict.value,
        "state": verdict_to_state(decision.verdict).value,
        "warnings": tuple(decision.warnings) + tuple(extra_warnings),
        "error_code": decision.reason or None,
    }
    if portal_row is not None:
        fields.update(
            portal_balance=portal_row.balance, portal_payer=portal_row.payer,
            portal_status=portal_row.status, portal_total=portal_row.total,
            portal_collected=portal_row.collected, portal_location=portal_row.location,
        )
    db.update_work(work.id, **fields)


def detail_shadow_statement(
    db: Database, portal: PortalSession, cfg: Config, st: StatementRow,
    only_work_id: int | None = None,
) -> dict[str, int]:
    """Read-only detail validation of RECONCILED rows. Rows that pass become DETAIL_VALIDATED."""
    all_rows = [r for r in db.work_rows(st.id) if r.state is WorkState.RECONCILED]
    if only_work_id is not None:
        all_rows = [r for r in all_rows if r.id == only_work_id]
    tally: dict[str, int] = {}
    def bump(k: str) -> None:
        tally[k] = tally.get(k, 0) + 1

    if not all_rows:
        return tally

    eft_rows = [
        EftRow(r.invoice_no, r.row_date, "", "", r.eft_gross, r.eft_prev_paid,
               r.eft_outstanding, r.eft_net)
        for r in all_rows
    ]
    lo, hi = sales_report_window(eft_rows, buffer_days=0)
    drifted = [r for r in all_rows if WarnCode.DATE_WINDOW_DRIFT in (r.warnings or ())]
    narrow = [r for r in all_rows if r not in drifted]

    hrefs: dict[str, str] = {}
    if narrow:
        portal._open_sales_report(lo, hi, all_invoice_states=False)
        hrefs.update(portal.collect_hrefs({r.invoice_no for r in narrow}))

    if drifted:
        by_day: dict[date, list] = {}
        for r in drifted:
            by_day.setdefault(r.row_date, []).append(r)
        for day, group in sorted(by_day.items()):
            d_lo = day - timedelta(days=1 + cfg.date_buffer_days)
            d_hi = day - timedelta(days=1) + timedelta(days=cfg.date_buffer_days)
            portal._open_sales_report(d_lo, d_hi, all_invoice_states=True)
            hrefs.update(portal.collect_hrefs({r.invoice_no for r in group}))

    rows = all_rows
    for r in rows:
        href = hrefs.get(normalize_invoice_no(r.invoice_no)) or hrefs.get(r.invoice_no)
        if not href:
            _fail_detail(db, r, "HREF_MISSING"); bump("href_missing"); continue
        try:
            detail = portal.open_invoice(href, expected_invoice_no=r.invoice_no)
        except IdentityMismatch:
            _fail_detail(db, r, "DETAIL_ID_MISMATCH"); bump("detail_id_mismatch"); continue
        except PortalError:
            _fail_detail(db, r, "DETAIL_READ_FAILED"); bump("detail_read_failed"); continue

        if detail.payment_status.strip().lower() == "paid":
            db.update_work(r.id, verdict=Verdict.ALREADY_PAID.value,
                           state=WorkState.NO_ACTION.value,
                           error_code="already paid at detail-read time")
            bump("detail_already_paid"); continue

        if r.eft_net is None or detail.total != r.eft_net:
            _fail_detail(db, r, "DETAIL_TOTAL_MISMATCH"); bump("detail_total_mismatch"); continue

        trigger = portal.action_trigger()
        if trigger.count() != 1:
            _fail_detail(db, r, "ACTION_TRIGGER_NOT_UNIQUE"); bump("action_trigger_not_unique"); continue
        if (trigger.get_attribute("aria-expanded") or "").lower() != "true":
            trigger.click()
        menu = portal.action_menu()
        try:
            menu.first.wait_for(state="visible", timeout=10000)
        except Exception:
            _fail_detail(db, r, "ACTION_MENU_NOT_OPEN"); bump("action_menu_not_open"); continue

        target = menu.get_by_role("menuitem", name=RECORD_PAYMENT, exact=True)
        n = target.count()
        portal.page.keyboard.press("Escape")
        if n == 0:
            _fail_detail(db, r, "PAYMENT_TARGET_MISSING"); bump("payment_target_missing"); continue
        if n > 1:
            _fail_detail(db, r, "PAYMENT_TARGET_DUPLICATE"); bump("payment_target_duplicate"); continue

        db.update_work(r.id, state=WorkState.DETAIL_VALIDATED.value,
                       portal_invoice_id=detail.portal_invoice_id, portal_href=href,
                       portal_total=detail.total, portal_status=detail.payment_status)
        bump("detail_shadow_pass")
    return tally


def _fail_detail(db: Database, row, code: str) -> None:
    db.update_work(row.id, verdict=Verdict.MANUAL_REVIEW.value,
                   state=WorkState.MANUAL_REVIEW.value, error_code=code)


@dataclass
class RunBudget:
    """Per-run invoice-count and amount caps (G-2b)."""
    max_invoices: int | None
    max_total: Decimal | None
    invoices_used: int = 0
    amount_used: Decimal = Decimal("0.00")
    consecutive_write_failures: int = 0

    def allows(self, amount: Decimal) -> bool:
        if self.max_invoices is not None and self.invoices_used >= self.max_invoices:
            return False
        if (self.max_total is not None
                and self.amount_used + amount > self.max_total):
            return False
        return True

    def consume(self, amount: Decimal) -> None:
        self.invoices_used += 1
        self.amount_used += amount


SMOKE_GATE_REASON = "not the one-shot smoke target"

# Reasons that skip a row without changing its state. Throttling is not a failure.
THROTTLE_REASONS = frozenset({
    "dry_run is enabled",
    "run limits exhausted",
    SMOKE_GATE_REASON,
})


class RunHalted(Exception):
    """Raised after an ambiguous financial outcome; stops the run for human inspection."""


class SmokeAbort(Exception):
    """A supervised run's target set is invalid. Nothing is substituted."""


def smoke_allow_list(cfg: Config) -> tuple[int, ...]:
    """Work-row ids allowed to cross the write boundary in a supervised run."""
    if cfg.smoke_work_ids:
        return tuple(cfg.smoke_work_ids)
    if cfg.smoke_work_id is not None:
        return (cfg.smoke_work_id,)
    return ()


def resolve_smoke_targets(db: Database, cfg: Config) -> list[tuple]:
    """Validate every allow-listed target and the batch total before any portal access."""
    ids = smoke_allow_list(cfg)
    if not ids:
        raise SmokeAbort("no smoke work ids are configured")
    if len(set(ids)) != len(ids):
        raise SmokeAbort(f"smoke allow-list has duplicate ids: {ids}")
    if cfg.max_invoices_per_run is None or cfg.max_total_amount_per_run is None:
        raise SmokeAbort(
            "supervised smoke requires explicit caps; max_invoices_per_run and "
            "max_total_amount_per_run must not be unlimited")
    if len(ids) > cfg.max_invoices_per_run:
        raise SmokeAbort(
            f"allow-list has {len(ids)} targets but max_invoices_per_run"
            f" is {cfg.max_invoices_per_run}"
        )

    out: list[tuple] = []
    total = Decimal("0.00")
    for wid in ids:
        work, prov = _resolve_one_smoke_target(db, cfg, wid)
        total += work.eft_net
        out.append((work, prov))

    if total > cfg.max_total_amount_per_run:
        raise SmokeAbort(
            f"batch total {total} exceeds max_total_amount_per_run"
            f" {cfg.max_total_amount_per_run}"
        )
    return out


def resolve_smoke_target(db: Database, cfg: Config):
    targets = resolve_smoke_targets(db, cfg)
    if len(targets) != 1:
        raise SmokeAbort(f"expected exactly one smoke target, got {len(targets)}")
    return targets[0]


def _resolve_one_smoke_target(db: Database, cfg: Config, wid: int):
    work = db.work_row_by_id(wid)
    if work is None:
        raise SmokeAbort(f"smoke target work_id={wid} does not exist")

    if cfg.smoke_expect_invoice_no and work.invoice_no != cfg.smoke_expect_invoice_no:
        raise SmokeAbort(
            f"smoke target work_id={wid} is {work.invoice_no!r},"
            f" expected {cfg.smoke_expect_invoice_no!r}"
        )
    if cfg.smoke_expect_eft_net is not None and work.eft_net != cfg.smoke_expect_eft_net:
        raise SmokeAbort(
            f"smoke target work_id={wid} amount is {work.eft_net},"
            f" expected {cfg.smoke_expect_eft_net}"
        )

    prov, why = verify_provenance(db, work)
    if why is not None:
        raise SmokeAbort(f"smoke target work_id={wid} lost provenance: {why}")


    if work.eft_net is None or work.eft_net <= 0:
        raise SmokeAbort(f"smoke target work_id={wid} has no positive EFT net")

    if work.row_date is None:
        raise SmokeAbort(f"smoke target work_id={wid} has no row_date")

    if work.eft_net > cfg.max_total_amount_per_run:
        raise SmokeAbort(
            f"smoke target work_id={wid} amount {work.eft_net} exceeds"
            f" max_total_amount_per_run {cfg.max_total_amount_per_run}"
        )

    return work, prov


def write_eligibility(
    db: Database, work, cfg: Config, budget: RunBudget, claim,
    statement_remaining: Decimal,
) -> tuple[Provenance | None, str | None]:
    """Return ``(provenance, None)`` if a row may be written now, otherwise ``(None, reason)``."""
    _allow = smoke_allow_list(cfg)
    if _allow and work.id not in _allow:
        return None, SMOKE_GATE_REASON
    prov, why = verify_provenance(db, work)
    if why is not None:
        return None, why
    if work.state is not WorkState.DETAIL_VALIDATED:
        return None, f"state {work.state.value} is not write-eligible"
    if work.verdict is not Verdict.APPROVE_OK:
        return None, f"verdict {work.verdict.value if work.verdict else 'None'} is not APPROVE_OK"
    if cfg.dry_run:
        return None, "dry_run is enabled"
    if work.eft_net is None or work.eft_net <= 0:
        return None, "EFT net is missing or not positive"
    if claim is not None:
        return None, "a write_claim already exists for this invoice"
    if not budget.allows(work.eft_net):
        return None, "run limits exhausted"
    if work.eft_net > statement_remaining:
        return None, "statement amount guard would be exceeded"
    return prov, None


def execute_statement(
    db: Database, portal: PortalSession, cfg: Config, st: StatementRow,
    budget: "RunBudget | None" = None,
) -> None:
    """Select eligible rows, call the single write primitive, and halt on ambiguous outcomes."""
    if budget is None:
        budget = RunBudget(cfg.max_invoices_per_run, cfg.max_total_amount_per_run)

    rows = sorted(db.work_rows(st.id), key=lambda r: r.invoice_no)
    approved_so_far = sum(
        (r.eft_net or Decimal("0.00")) for r in rows
        if r.state in (WorkState.CONFIRMED, WorkState.PENDING_WRITE)
    )

    for work in rows:
        claim = db.get_claim(work.invoice_no)

        if claim is not None:
            if claim.final_outcome is ClaimOutcome.CONFIRMED:
                if work.state is not WorkState.CONFIRMED:
                    db.update_work(work.id, state=WorkState.CONFIRMED.value)
                continue
            if _recover_unresolved_claim(db, portal, cfg, work, claim):
                continue
            db.update_work(
                work.id, state=WorkState.MANUAL_REVIEW.value,
                verdict=Verdict.MANUAL_REVIEW.value,
                error_code="CLAIM_EXISTS_UNRESOLVED",
                attribution="unverified",
            )
            continue

        statement_remaining = st.deposit_amount - approved_so_far
        prov, reason = write_eligibility(
            db, work, cfg, budget, claim, statement_remaining
        )
        if reason is not None:
            if reason in THROTTLE_REASONS:
                log.info("skipping %s: %s", work.invoice_no, reason)
            elif _keeps_earlier_outcome(db, work):
                log.info("leaving %s as %s: %s", work.invoice_no, work.state.value, reason)
            else:
                db.update_work(work.id, state=WorkState.MANUAL_REVIEW.value,
                               verdict=Verdict.MANUAL_REVIEW.value, error_code=reason)
            continue

        try:
            result = execute_one_authorized_work(db, portal, cfg, work, prov)
        except (AlreadyPaid, WriteAnomaly):
            continue
        except WriteBlocked as exc:
            log.info("write blocked for %s: %s", work.invoice_no, exc)
            db.update_work(work.id, error_code=f"BLOCKED: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            db.update_work(work.id, error_code=f"WRITE_FAILED: {type(exc).__name__}")
            budget.consecutive_write_failures += 1
            if budget.consecutive_write_failures >= G9_MAX_CONSECUTIVE_FAILURES:
                raise PortalError("G-9: consecutive write failures; aborting run") from exc
            continue

        if result.get("clicked"):
            budget.consume(work.eft_net)
            approved_so_far += work.eft_net
        if result.get("outcome") == "CONFIRMED":
            budget.consecutive_write_failures = 0
        elif result.get("outcome") == "UNKNOWN_OUTCOME":
            raise RunHalted(
                f"ambiguous financial outcome on {work.invoice_no};"
                " halting the run for human inspection")

        if cfg.inter_invoice_delay_seconds:
            time.sleep(cfg.inter_invoice_delay_seconds)


def _keeps_earlier_outcome(db: Database, work) -> bool:
    """True if a row the write stage rejected should keep the outcome an earlier stage recorded.

    A MANUAL_REVIEW row is already in front of a person. A NO_ACTION row stays out of review only
    while its provenance verifies: "already paid" concluded from a row that fails verification,
    such as a malformed identifier matched to a paid invoice, still needs a person.
    """
    if work.state is WorkState.MANUAL_REVIEW:
        return True
    return work.state is WorkState.NO_ACTION and verify_provenance(db, work)[1] is None


def _recover_unresolved_claim(db: Database, portal, cfg: Config, work, claim) -> bool:
    """Read-only recovery: confirm an unresolved claim only if the full success contract holds. Never clicks."""
    if portal is None or not work.portal_href:
        return False
    try:
        detail = portal.open_invoice(work.portal_href, expected_invoice_no=work.invoice_no)
    except Exception as exc:  # noqa: BLE001
        log.warning("claim recovery could not read %s: %s", work.invoice_no, exc)
        return False

    if (detail.payment_status or "").strip().lower() != "paid":
        log.warning("claim exists for %s but the portal still shows %r",
                    work.invoice_no, detail.payment_status)
        return False

    signal = portal._observe_success_signal(
        work.invoice_no, expected_amount=work.eft_net
    )
    if not signal:
        log.warning("claim recovery: %s looks Paid but does not satisfy the"
                    " measured G-5 contract; leaving it for a human",
                    work.invoice_no)
        return False

    log.info("claim recovery: %s satisfies G-5 (%s); confirming without re-clicking",
             work.invoice_no, signal)
    if claim.final_outcome is not ClaimOutcome.CONFIRMED:
        db.set_claim_outcome(work.invoice_no, ClaimOutcome.CONFIRMED)
    db.update_work(
        work.id, state=WorkState.CONFIRMED.value,
        error_code="RECOVERED_AFTER_CRASH",
        attribution="recovered-readonly",
    )
    return True


def _g18_reject_reason(work, detail) -> str | None:
    if canonical_invoice_key(detail.invoice_no) != canonical_invoice_key(work.invoice_no):
        return "DETAIL_ID_MISMATCH"
    if (detail.payment_status or "").strip().lower() == "paid":
        return "ALREADY_PAID"
    if work.eft_net is None or detail.total != work.eft_net:
        return "DETAIL_TOTAL_MISMATCH"
    return None


def finalize_statement(
    db: Database, st: StatementRow, cfg: Config | None = None
) -> StatementState:
    """Mark a statement COMPLETED when every row is terminal, or TERMINAL_EXCEPTION past the attempt ceiling."""
    rows = db.work_rows(st.id)
    if statement_is_complete([r.state for r in rows]):
        db.set_statement_state(st.id, StatementState.COMPLETED)
        return StatementState.COMPLETED

    current = db.get_statement(st.id) or st
    limit = cfg.max_statement_attempts if cfg is not None else DEFAULT_MAX_STATEMENT_ATTEMPTS
    if current.attempt_count > limit:
        db.set_statement_state(
            st.id, StatementState.TERMINAL_EXCEPTION, "G21_MAX_ATTEMPTS"
        )
        return StatementState.TERMINAL_EXCEPTION

    return StatementState.PENDING


def run_once(
    cfg: Config, db: Database, gmail: GmailClient, portal: PortalSession | None = None
) -> RunStats:
    """Execute one complete run and always enqueue exactly one report (G-20)."""
    run_id = db.start_run()
    stats_error: str | None = None
    ingested = 0
    queued = 0
    events: list[str] = []
    touched: list = []
    login_required_marker: str | None = None
    try:
        flush_report_outbox(db, gmail, cfg)

        events: list[str] = []
        ingested = ingest_messages(db, gmail, cfg, events)
        queue = build_queue(db)

        if cfg.smoke_work_id is not None:
            target, target_prov = resolve_smoke_target(db, cfg)
            log.info(
                "one-shot smoke armed: work_id=%s invoice=%s net=%s authorized by %s",
                target.id, target.invoice_no, target.eft_net, target_prov.audit_ref(),
            )
            _note(events, f"ONE-SHOT SMOKE: work_id={target.id} only")
            queue = [st for st in queue if st.id == target.statement_id]
            if not queue:
                raise SmokeAbort(
                    f"smoke target statement {target.statement_id} is not in the"
                    " pending queue"
                )

        queued = len(queue)

        if portal is not None:
            login_mode = portal.ensure_authenticated()
            log.info("portal session: %s", login_mode)

        budget = RunBudget(cfg.max_invoices_per_run, cfg.max_total_amount_per_run)

        for st in queue:
            if portal is None:
                log.info("statement %s left PENDING (portal stage unavailable)", st.id)
                continue
            reconcile_statement(db, portal, cfg, st)
            detail_shadow_statement(db, portal, cfg, st, only_work_id=cfg.smoke_work_id)
            execute_statement(db, portal, cfg, st, budget)
            touched.extend(db.work_rows(st.id))
            finalize_statement(db, st, cfg)

        status = RUN_STATUS_SUCCESS
    except RunHalted as exc:
        status = RUN_STATUS_HALTED
        stats_error = f"HALTED: {exc}"
        log.error("run halted: %s", exc)
    except LoginRequired as exc:
        status = RUN_STATUS_LOGIN_REQUIRED
        login_required_marker = exc.marker
        stats_error = f"LOGIN_REQUIRED: {exc.marker}"
        log.error("portal requires an interactive login (%s); no financial action taken",
                  exc.marker)
    except Exception as exc:  # noqa: BLE001
        status = RUN_STATUS_FAILED
        stats_error = f"{type(exc).__name__}: {exc}"
        log.exception("run failed")

    seen_ids: set[int] = set()
    work = []
    for w in [*touched, *(x for s in db.pending_statements() for x in db.work_rows(s.id))]:
        if w.id not in seen_ids:
            seen_ids.add(w.id)
            work.append(w)
    manual = sum(1 for w in work if w.state == WorkState.MANUAL_REVIEW)
    confirmed = [w for w in work if w.state is WorkState.CONFIRMED]
    approved_count = sum(
        1 for w in confirmed if (c := db.get_claim(w.invoice_no)) is not None
        and c.final_outcome is ClaimOutcome.CONFIRMED
    )
    approved_total = sum(
        ((w.eft_net or Decimal("0.00")) for w in confirmed
         if (c := db.get_claim(w.invoice_no)) is not None
         and c.final_outcome is ClaimOutcome.CONFIRMED),
        Decimal("0.00"),
    )
    stats = RunStats(
        run_id=run_id,
        statements_processed=queued,
        approved_count=approved_count,
        approved_total=approved_total,
        manual_review_count=manual,
        status=status,
        error=stats_error,
    )
    db.finish_run(
        run_id,
        statements_processed=stats.statements_processed,
        approved_count=stats.approved_count,
        approved_total=stats.approved_total,
        manual_review_count=stats.manual_review_count,
        status=status,
        error=stats_error,
    )

    if status == RUN_STATUS_HALTED:
        subject, body = build_failure(stats_error or "halted")
        subject = subject.replace("FAILED", "HALTED — ambiguous financial outcome")
        kind = "FAILURE"
    elif status == RUN_STATUS_LOGIN_REQUIRED:
        subject, body = build_login_required(login_required_marker or "unknown")
        kind = "LOGIN_REQUIRED"
    elif status == RUN_STATUS_FAILED:
        subject, body = build_failure(stats_error or "unknown error")
        kind = "FAILURE"
    elif queued or ingested or events:
        abandoned = db.terminal_statements()
        subject, body = build_summary(
            stats, [w for w in work if w.state == WorkState.MANUAL_REVIEW], abandoned)
        if events:
            body += "<h4>Intake exceptions requiring attention</h4><ul>" + \
                    "".join(f"<li>{e}</li>" for e in events) + "</ul>"
            subject += f" · {len(events)} intake exception(s)"
        kind = "SUMMARY"
    elif db.terminal_statements():
        abandoned = db.terminal_statements()
        subject, body = build_summary(stats, [], abandoned)
        kind = "SUMMARY"
    else:
        subject, body = build_heartbeat(db.last_successful_run())
        kind = "HEARTBEAT"
    db.enqueue_report(run_id, kind, f"{subject}\n\n{body}")
    flush_report_outbox(db, gmail, cfg)
    return stats


def _outside_run_window(cfg: Config) -> str | None:
    """Return a reason if the current local time is outside the configured start window."""
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(cfg.run_window_tz)
    except Exception as exc:  # noqa: BLE001
        return f"run_window_tz {cfg.run_window_tz!r} is not a valid timezone: {exc}"
    try:
        lo = _parse_hhmm(cfg.run_window_start)
        hi = _parse_hhmm(cfg.run_window_end)
    except ValueError as exc:
        return f"run window is misconfigured: {exc}"

    now = datetime.now(tz)
    cur = (now.hour, now.minute)
    if lo <= cur <= hi:
        return None
    return (f"local time {now:%H:%M} ({cfg.run_window_tz}) is outside the allowed "
            f"start window {cfg.run_window_start}-{cfg.run_window_end}")


def _parse_hhmm(raw: str) -> tuple[int, int]:
    try:
        h, m = str(raw).split(":")
        h, m = int(h), int(m)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{raw!r} is not HH:MM") from exc
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"{raw!r} is not a valid time of day")
    return h, m


def _record_incident(root: Path, kind: str, detail: str) -> None:
    """Append an fsync'd JSON record to ``logs/incidents.jsonl`` (owner-only)."""
    try:
        path = root / "logs" / "incidents.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind,
               "detail": str(detail)[:2000]}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001
        log.exception("could not record the incident")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Exit codes: 0 success, 1 failure, 2 login required, 3 outside window, 4 lock held, 5 halted."""
    import argparse
    import os

    ap = argparse.ArgumentParser(prog="remittance-reconciler")
    ap.add_argument("--config", default=os.environ.get("RECONCILER_CONFIG", "config.yaml"))
    ap.add_argument("--no-send", action="store_true",
                    help="do not actually send report emails (they are still queued in the outbox)")
    ap.add_argument("--force-now", action="store_true",
                    help="ignore the run window (supervised manual runs only).")
    ap.add_argument("--with-portal", action="store_true",
                    help="open the portal session with the dedicated persistent profile (production default). "
                         "Without it, only Gmail intake and reporting run; the portal is not touched.")
    args = ap.parse_args(argv)

    root = Path(args.config).resolve().parent
    (root / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[RotatingFileHandler(root / "logs" / "automation.log",
                                      maxBytes=5_000_000, backupCount=7,
                                      encoding="utf-8"),
                  logging.StreamHandler()],
    )

    cfg = load_config(Path(args.config))

    if not args.force_now:
        why = _outside_run_window(cfg)
        if why is not None:
            log.warning("refusing to start: %s", why)
            print(f"OUT OF RUN WINDOW: {why}")
            print("  A run already in progress is never interrupted; this check only looks at the start time.")
            print("  Supervised runs can bypass it with --force-now.")
            _record_incident(root, "OUT_OF_WINDOW", why)
            return 3

    try:
        # G-12: take the lock before cleaning scratch/, so a second process can never
        # delete the export file of a run that is still in progress.
        lock = acquire_lock(root / "data" / ".run.lock")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not acquire the run lock: %s", exc)
        _record_incident(root, "LOCK_UNAVAILABLE", f"{type(exc).__name__}: {exc}")
        return 4
    try:
        removed = clean_scratch(Path(cfg.scratch_dir) if cfg.scratch_dir
                                else root / "scratch")
        log.info("scratch leftovers removed: %d", removed)

        db = Database(root / "data" / "eft.db")
        db.migrate()
        gmail = GmailClient.from_config(root / "secrets" / "token.json", cfg)
        if args.no_send:
            gmail.send = lambda *a, **k: None  # type: ignore[assignment]

        if not args.with_portal:
            stats = run_once(cfg, db, gmail, portal=None)
        else:
            from playwright.sync_api import sync_playwright

            from .portal import launch_persistent_session

            with sync_playwright() as pw:
                context, portal = launch_persistent_session(
                    pw, root / cfg.portal_profile_dir,
                    headless=cfg.portal_headless,
                    success_signals=cfg.post_click_success_signals,
                )
                try:
                    stats = run_once(cfg, db, gmail, portal=portal)
                finally:
                    context.close()

        log.info("run %s finished: status=%s statements=%d",
                 stats.run_id, stats.status, stats.statements_processed)
        db.close()
        if stats.status == RUN_STATUS_SUCCESS:
            return 0
        if stats.status == RUN_STATUS_LOGIN_REQUIRED:
            return 2
        return 5 if stats.status == RUN_STATUS_HALTED else 1
    except Exception as exc:  # noqa: BLE001
        log.exception("bootstrap failed")
        _record_incident(root, "BOOTSTRAP_FAILED", f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
