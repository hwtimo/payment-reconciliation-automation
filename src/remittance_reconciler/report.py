"""Report bodies: completion summary, idle heartbeat, failure and login-required alerts.

Silence is the worst failure mode of an unattended system, so every run enqueues exactly one
report. Reports contain billing identifiers only, never patient or clinical fields.
"""

from __future__ import annotations

import csv
import html
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from .models import RunStats, WorkRow, WorkState

__all__ = [
    "build_login_required",
    "AUDIT_CSV_COLUMNS",
    "build_summary",
    "build_heartbeat",
    "build_failure",
    "write_audit_csv",
]

AUDIT_CSV_COLUMNS: tuple[str, ...] = (
    "time",
    "vendor",
    "invoice_no",
    "eft_net",
    "portal_balance",
    "verdict",
    "state",
    "warnings",
)


def build_summary(stats: RunStats, exceptions: Sequence[WorkRow],
                  abandoned: Sequence = ()) -> tuple[str, str]:
    """Completion summary: counts, rows needing review (with reason and attribution) and abandoned statements."""
    subject = (
        f"Remittance Reconciler — Completed · {stats.statements_processed} statement(s) · "
        f"{stats.approved_count} approved"
    )
    if stats.manual_review_count:
        subject += f" · {stats.manual_review_count} manual review"
    if abandoned:
        subject += f" · {len(abandoned)} ABANDONED"

    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(w.invoice_no))}</td>"
        f"<td align='right'>{_money(w.eft_net)}</td>"
        f"<td align='right'>{_money(w.portal_balance)}</td>"
        f"<td>{html.escape(str(w.verdict.value if w.verdict else ''))}</td>"
        f"<td>{html.escape(str(w.state.value if w.state else ''))}</td>"
        f"<td>{html.escape(str(w.error_code or ''))}</td>"
        f"<td>{html.escape(str(w.attribution or ''))}</td>"
        "</tr>"
        for w in exceptions
    )
    table = (
        "<table border='1' cellpadding='4' cellspacing='0'>"
        "<thead><tr><th>Invoice</th><th>EFT Net</th><th>Portal Balance</th>"
        "<th>Reason</th><th>State</th><th>Detail</th><th>Attribution</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        if exceptions
        else "<p>No exceptions.</p>"
    )
    abandoned_html = ""
    if abandoned:
        rows_a = "".join(
            "<tr>"
            f"<td>{html.escape(str(a.vendor_no))}</td>"
            f"<td>{html.escape(str(a.payment_document_no))}</td>"
            f"<td align='right'>{_money(a.deposit_amount)}</td>"
            f"<td>{html.escape(str(a.last_error or ''))}</td>"
            "</tr>"
            for a in abandoned
        )
        abandoned_html = (
            "<h4 style='color:#b00'>Abandoned statements — these will NOT be "
            "retried automatically</h4>"
            "<table border='1' cellpadding='4' cellspacing='0'>"
            "<thead><tr><th>Vendor</th><th>Remittance No</th>"
            "<th>Deposit</th><th>Reason</th></tr></thead>"
            f"<tbody>{rows_a}</tbody></table>"
        )

    body = (
        "<h3>Remittance Reconciler — Completed</h3>"
        f"<p>Statements: {stats.statements_processed} &middot; "
        f"Approved: {stats.approved_count} &middot; "
        f"Approved Total: ${stats.approved_total} &middot; "
        f"<b>Manual Review: {stats.manual_review_count}</b></p>"
        f"{table}"
        f"{abandoned_html}"
        "<p style='color:#666;font-size:12px'>Minimal billing audit data — "
        "no patient names or clinical fields.</p>"
    )
    return subject, body


def build_heartbeat(last_run: datetime | None) -> tuple[str, str]:
    """Idle-day heartbeat. If it stops arriving, that absence is the outage signal."""
    when = last_run.isoformat() if last_run else "never"
    return (
        "Remittance Reconciler — Heartbeat (nothing to process)",
        f"<p>Automation ran and found nothing to process. Last successful run: {html.escape(when)}.</p>"
        "<p style='color:#666;font-size:12px'>If this message stops arriving, check the bot — "
        "its absence is the outage signal.</p>",
    )


def build_failure(error: str) -> tuple[str, str]:
    """Immediate alert for a failed or halted run."""
    return (
        "Remittance Reconciler — FAILED",
        f"<h3>Automation Failed</h3><pre>{html.escape(str(error))}</pre>"
        "<p>No invoices were paid by this run unless a completion summary also arrived.</p>",
    )


def build_login_required(marker: str) -> tuple[str, str]:
    """Alert telling staff exactly how to restore the portal session. No payments were attempted."""
    return (
        "Remittance Reconciler — MANUAL PORTAL LOGIN REQUIRED",
        "<h3>The portal requires an interactive login</h3>"
        f"<p>Detected: <code>{html.escape(str(marker))}</code></p>"
        "<p><b>No invoices were paid.</b> The automation made no payment claim and "
        "clicked nothing. It did not attempt to solve or bypass the verification, "
        "and it will not retry.</p>"
        "<h4>What staff needs to do</h4>"
        "<ol>"
        "<li>Run the interactive login helper on the Mac mini:<br>"
        "<code>uv run python tools/portal_login.py</code></li>"
        "<li>Complete the sign-in and any verification in the window that opens.</li>"
        "<li>Leave it signed in and close the window when told to.</li>"
        "</ol>"
        "<p>The authenticated session is preserved in the dedicated browser profile, "
        "so subsequent unattended runs will reuse it without logging in again.</p>",
    )


def write_audit_csv(path: Path, rows: Sequence[WorkRow]) -> None:
    """Write an audit CSV restricted to whitelisted columns, with owner-only permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(AUDIT_CSV_COLUMNS)
        for r in rows:
            w.writerow(
                [
                    (r.updated_at or r.created_at or "").isoformat()
                    if (r.updated_at or r.created_at)
                    else "",
                    r.vendor_no,
                    r.invoice_no,
                    _money(r.eft_net),
                    _money(r.portal_balance),
                    r.verdict.value if r.verdict else "",
                    WorkState(r.state).value if r.state else "",
                    ",".join(x.value for x in (r.warnings or ())),
                ]
            )
    path.chmod(0o600)


def _money(v) -> str:
    return "" if v is None else str(v)
