#!/usr/bin/env python3
"""Pre-flight checks for one supervised target: provenance, state and live portal detail.

Performs no financial action: it opens the action menu to count exact targets, then closes it.
"""


from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from remittance_reconciler.config import load_config  # noqa: E402
from remittance_reconciler.database import Database  # noqa: E402
from remittance_reconciler.portal import (  # noqa: E402
    RECORD_PAYMENT, PortalSession, launch_persistent_session,
)
from remittance_reconciler.main import SmokeAbort, resolve_smoke_target  # noqa: E402
from remittance_reconciler.models import WorkState  # noqa: E402
from remittance_reconciler.provenance import verify_provenance  # noqa: E402

OK, BAD = "PASS", "FAIL"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work-id", type=int, required=True)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--db", default="data/eft.db")
    ap.add_argument("--cdp", default=None,
                    help="attach to an already-authenticated browser; without it the "
                         "dedicated persistent profile is used, as in production (recommended).")
    ap.add_argument("--no-portal", action="store_true", help="run database checks only")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    db = Database(Path(args.db))
    db.migrate()

    checks: list[tuple[str, str, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, OK if ok else BAD, detail))

    work = db.work_row_by_id(args.work_id)
    if work is None:
        print(f"work_id={args.work_id} does not exist. Aborting.")
        return 1

    prov, why = verify_provenance(db, work)
    check("provenance chain valid", why is None, why or prov.audit_ref())
    st = db.get_statement(work.statement_id)
    check("statement eligible", st is not None and st.state.value != "TERMINAL_EXCEPTION",
          f"statement {work.statement_id} state={st.state.value if st else '?'}")
    check("no write_claim", db.get_claim(work.invoice_no) is None)
    check("state == DETAIL_VALIDATED", work.state is WorkState.DETAIL_VALIDATED,
          work.state.value)
    check("verdict == APPROVE_OK", (work.verdict.value if work.verdict else "") == "APPROVE_OK",
          work.verdict.value if work.verdict else "None")
    check("no warnings", not work.warnings,
          ",".join(w.value for w in work.warnings) or "(none)")
    check("positive EFT net", (work.eft_net or Decimal("0")) > 0, str(work.eft_net))
    check("payer is ACME", (work.portal_payer or "").upper().startswith("ACME"),
          work.portal_payer or "?")

    probe_cfg = type(cfg)(**{**{f: getattr(cfg, f) for f in cfg.__slots__},
                             "smoke_work_id": args.work_id,
                             "smoke_expect_invoice_no": work.invoice_no,
                             "smoke_expect_eft_net": work.eft_net,
                             "max_total_amount_per_run": work.eft_net})
    try:
        resolve_smoke_target(db, probe_cfg)
        check("resolve_smoke_target accepts (cap == net)", True)
    except SmokeAbort as exc:
        check("resolve_smoke_target accepts (cap == net)", False, str(exc))

    if not args.no_portal:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            if args.cdp:
                b = p.chromium.connect_over_cdp(args.cdp)
                page = b.contexts[0].pages[0]
                portal = PortalSession(page, success_signals=cfg.post_click_success_signals)
            else:
                b, portal = launch_persistent_session(
                    p, Path(args.config).resolve().parent / cfg.portal_profile_dir,
                    headless=cfg.portal_headless,
                    success_signals=cfg.post_click_success_signals,
                )
                mode = portal.ensure_authenticated()
                check(f"portal session reused (no re-login)", mode == "reused", mode)

            detail = portal.open_invoice(work.portal_href, expected_invoice_no=work.invoice_no)
            check("Portal exact invoice identifier", detail.invoice_no == work.invoice_no,
                  f"{detail.invoice_no} vs {work.invoice_no}")
            check("EFT net == Portal detail total", detail.total == work.eft_net,
                  f"{work.eft_net} vs {detail.total}")
            check("currently NOT Paid",
                  (detail.payment_status or "").strip().lower() != "paid",
                  detail.payment_status)
            check("submission status is Submitted",
                  (detail.submission_status or "").strip() == "Submitted",
                  detail.submission_status)
            check("Portal invoice id matches stored",
                  detail.portal_invoice_id == work.portal_invoice_id,
                  f"{detail.portal_invoice_id} vs {work.portal_invoice_id}")

            page = portal.page
            trig = portal.action_trigger()
            check("action trigger count == 1", trig.count() == 1, str(trig.count()))
            if (trig.get_attribute("aria-expanded") or "") != "true":
                trig.click()
                page.wait_for_timeout(700)
            menu = portal.action_menu()
            items = [t.strip() for t in menu.locator("[role=menuitem]").all_inner_texts()]
            tgt = menu.get_by_role("menuitem", name=RECORD_PAYMENT, exact=True)
            n = tgt.count()
            check(f"exact {RECORD_PAYMENT!r} count == 1", n == 1, str(n))
            if n == 1:
                check("target visible", tgt.first.is_visible())
                check("target enabled", tgt.first.is_enabled())
            portal.page.keyboard.press("Escape")
            print(f"action menu items ({len(items)}): {items}\n")
            b.close()

    width = max(len(n) for n, _, _ in checks)
    for name, verdict, detail in checks:
        print(f"  {verdict}  {name:<{width}}  {detail}")
    failed = [n for n, v, _ in checks if v == BAD]
    print()
    if failed:
        print(f"ABORT — {len(failed)} check(s) failed: {failed}")
        print("* No substitute invoice will be chosen. Fix the cause and re-check.")
        return 1
    print(f"ALL {len(checks)} CHECKS PASS — work_id={args.work_id}"
          f" invoice={work.invoice_no} net={work.eft_net}")
    print("This script performed no clicks and created no claims.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
