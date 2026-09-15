"""Typed configuration loaded from ``config.yaml``.

Money values are read as strings and converted to ``Decimal``. The loader rejects a placeholder
or timezone-naive ``automation_start_at`` so the cutover guard can never be silently wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

__all__ = ["Config", "load_config"]


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable runtime configuration. ``None`` caps mean unlimited; per-invoice safety gates still apply."""
    automation_start_at: datetime

    dry_run: bool = True

    max_invoices_per_run: int | None = 50
    max_total_amount_per_run: Decimal | None = Decimal("10000.00")

    max_statement_age_days: int = 30

    max_statement_attempts: int = 5

    heartbeat_days: int = 1

    inter_invoice_delay_seconds: int = 3

    date_buffer_days: int = 3

    known_vendors: tuple[str, ...] = ()

    trusted_forwarders: tuple[str, ...] = ()

    require_dkim_pass: bool = True

    require_dmarc_pass: bool = True

    trusted_dkim_domain_suffix: str = ""

    trusted_authserv_id: str = "mx.google.com"

    report_recipients: tuple[str, ...] = ()

    post_click_success_signals: tuple[str, ...] = ()

    portal_profile_dir: str = "secrets/portal-profile"

    portal_headless: bool = True

    smoke_work_id: int | None = None

    smoke_work_ids: tuple[int, ...] = ()

    smoke_expect_invoice_no: str = ""
    smoke_expect_eft_net: Decimal | None = None

    run_window_start: str = "02:45"
    run_window_end: str = "04:30"
    run_window_tz: str = "America/Los_Angeles"

    scratch_dir: str = ""

    debug_capture: bool = False


def load_config(path: Path) -> Config:
    """Parse ``path`` into a :class:`Config`, failing fast on unsafe or incomplete values."""
    import yaml

    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    start_raw = raw.get("automation_start_at")
    if start_raw in (None, "", "REPLACE_ME"):
        raise ValueError(
            "automation_start_at is required and must be a tz-aware ISO-8601 timestamp "
            "(config.yaml still holds the REPLACE_ME placeholder)"
        )
    if isinstance(start_raw, datetime):
        start = start_raw
    else:
        start = datetime.fromisoformat(str(start_raw))
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("automation_start_at must be tz-aware (e.g. 2037-07-01T00:00:00-07:00)")

    def money(key: str, default: str) -> Decimal:
        return Decimal(str(raw.get(key, default)))

    def tup(key: str) -> tuple[str, ...]:
        v = raw.get(key) or ()
        if isinstance(v, str):
            v = [v]
        return tuple(str(x) for x in v)

    return Config(
        automation_start_at=start,
        dry_run=bool(raw.get("dry_run", True)),
        max_invoices_per_run=(
            None if ("max_invoices_per_run" in raw
                     and raw["max_invoices_per_run"] is None)
            else int(raw.get("max_invoices_per_run", 50))),
        max_total_amount_per_run=(
            None if ("max_total_amount_per_run" in raw
                     and raw["max_total_amount_per_run"] is None)
            else money("max_total_amount_per_run", "10000.00")),
        max_statement_age_days=int(raw.get("max_statement_age_days", 30)),
        max_statement_attempts=int(raw.get("max_statement_attempts", 5)),
        heartbeat_days=int(raw.get("heartbeat_days", 1)),
        inter_invoice_delay_seconds=int(raw.get("inter_invoice_delay_seconds", 3)),
        date_buffer_days=int(raw.get("date_buffer_days", 3)),
        known_vendors=tup("known_vendors"),
        trusted_forwarders=tuple(
            s for s in (x.strip().lower() for x in tup("trusted_forwarders")) if s
        ),
        require_dkim_pass=bool(raw.get("require_dkim_pass", True)),
        require_dmarc_pass=bool(raw.get("require_dmarc_pass", True)),
        trusted_dkim_domain_suffix=str(raw.get("trusted_dkim_domain_suffix", "") or ""),
        trusted_authserv_id=str(raw.get("trusted_authserv_id", "mx.google.com") or ""),
        report_recipients=tup("report_recipients"),
        post_click_success_signals=tup("post_click_success_signals"),
        portal_profile_dir=str(raw.get("portal_profile_dir", "secrets/portal-profile")),
        portal_headless=bool(raw.get("portal_headless", True)),
        run_window_start=str(raw.get("run_window_start", "02:45")),
        run_window_end=str(raw.get("run_window_end", "04:30")),
        run_window_tz=str(raw.get("run_window_tz", "America/Los_Angeles")),
        scratch_dir=str(Path(raw.get("scratch_dir") or (path.resolve().parent / "scratch"))),
        smoke_work_id=(None if raw.get("smoke_work_id") in (None, "")
                       else int(raw["smoke_work_id"])),
        smoke_work_ids=tuple(
            int(x) for x in (raw.get("smoke_work_ids") or ()) if str(x).strip()
        ),
        smoke_expect_invoice_no=str(raw.get("smoke_expect_invoice_no", "") or ""),
        smoke_expect_eft_net=(None if raw.get("smoke_expect_eft_net") in (None, "")
                              else Decimal(str(raw["smoke_expect_eft_net"]))),
        debug_capture=bool(raw.get("debug_capture", False)),
    )
