#!/usr/bin/env python3
"""Supervised live run for an explicit allow-list of work rows.

Usage:
  python tools/run_smoke.py --work-id N [--work-id M ...] --rehearse
  python tools/run_smoke.py --work-id N --arm --i-authorize-one-financial-click --cap <exact total>

Rehearsal performs every read and changes nothing. Arming requires an operator-typed cap equal
to the exact total of the targets. No target is ever substituted.
"""


from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from remittance_reconciler.config import load_config  # noqa: E402
from remittance_reconciler.database import Database  # noqa: E402
from remittance_reconciler.portal import (  # noqa: E402
    LOGIN_SUCCESS_NAV, PortalSession, LoginRequired, launch_persistent_session,
)
from remittance_reconciler.main import (  # noqa: E402
    SmokeAbort, acquire_lock, clean_scratch, resolve_smoke_targets,
)
from remittance_reconciler.smoke import (  # noqa: E402
    SmokeValidationError, execute_smoke_batch, validate_smoke_target,
)

log = logging.getLogger("smoke")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    ap.add_argument("--work-id", type=int, action="append", dest="work_ids",
                    required=True, metavar="ID",
                    help="target invoice_work.id (repeat the flag for a batch).")
    ap.add_argument("--config", default="config.yaml")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rehearse", action="store_true",
                      help="run the full path with dry_run kept on; no clicks.")
    mode.add_argument("--arm", action="store_true",
                      help="disable dry_run for this process only.")
    ap.add_argument("--cap", metavar="AMOUNT", default=None,
                    help="required with --arm; must equal the exact SUM of the targets' EFT net. "
                         "Typing the amount by hand makes arming a deliberate act.")
    ap.add_argument("--expect-invoice", metavar="NO", action="append",
                    dest="expect_invoices", default=None,
                    help="operator-typed cross-check value, in the same order as --work-id.")
    ap.add_argument("--i-authorize-one-financial-click", action="store_true",
                    help="only together with --arm does the run become live.")
    ap.add_argument("--headed", action="store_true",
                    help="show the browser window; the default is headless, as in production.")
    ap.add_argument("--cdp", metavar="URL", default=None,
                    help="attach to an already-authenticated browser; without it the "
                         "dedicated persistent profile is used, as in production.")
    args = ap.parse_args(argv)

    if args.arm and not args.i_authorize_one_financial_click:
        print("--arm requires --i-authorize-one-financial-click. Aborting.")
        return 2
    if args.arm and args.cap is None:
        print("--arm requires --cap <amount>. Aborting.")
        return 2

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(Path("logs") / "smoke.log"),
                  logging.StreamHandler()],
    )

    root = Path(args.config).resolve().parent
    cfg = load_config(Path(args.config))
    db = Database(root / "data" / "eft.db")
    db.migrate()

    work_ids = list(args.work_ids)
    if len(set(work_ids)) != len(work_ids):
        print(f"duplicate --work-id: {work_ids}. Aborting.")
        return 2

    works = []
    for wid in work_ids:
        w = db.work_row_by_id(wid)
        if w is None:
            print(f"work_id={wid} does not exist. Aborting.")
            return 1
        works.append(w)

    expects = list(args.expect_invoices or ())
    if expects and len(expects) != len(works):
        print(f"got {len(expects)} --expect-invoice value(s) for {len(works)} target(s). Aborting.")
        return 2
    for w, want in zip(works, expects):
        if want != w.invoice_no:
            print(f"work_id={w.id} is {w.invoice_no!r}, expected {want!r}. Aborting.")
            return 1

    total = sum((w.eft_net for w in works), Decimal("0.00"))
    if args.arm:
        try:
            cap = Decimal(str(args.cap))
        except (ArithmeticError, ValueError):
            print(f"--cap {args.cap!r} is not a valid amount. Aborting.")
            return 2
        if cap != total:
            print(f"--cap {cap} differs from the target total {total}. Aborting.")
            return 2
    else:
        cap = total

    cfg = replace(
        cfg,
        smoke_work_ids=tuple(work_ids),
        smoke_work_id=None,
        smoke_expect_invoice_no="",
        smoke_expect_eft_net=None,
        max_invoices_per_run=len(works),
        max_total_amount_per_run=cap,
        dry_run=not args.arm,
    )

    try:
        targets = resolve_smoke_targets(db, cfg)
    except SmokeAbort as exc:
        print(f"SMOKE ABORTED: {exc}")
        print("* No substitute invoice will be chosen.")
        return 1

    n = len(targets)
    banner = f"ARMED — up to {n} live click(s)" if args.arm else "REHEARSAL — no clicks"
    print(f"\n{'='*74}\n{banner}\n{'='*74}")
    for t, pv in targets:
        print(f"  work_id {t.id:<4} {t.invoice_no:<14} {t.eft_net:>9}   {pv.audit_ref()}")
    print(f"  {'-'*70}")
    print(f"  targets {n}   cap {cfg.max_total_amount_per_run} (== total)   "
          f"dry_run {cfg.dry_run}\n{'='*74}\n")

    before = _snapshot(db)
    batch: list = []
    lock = acquire_lock(root / "data" / ".run.lock")
    try:
        clean_scratch(root / "scratch")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            if args.cdp:
                browser = p.chromium.connect_over_cdp(args.cdp)
                page = browser.contexts[0].pages[0]
                portal = PortalSession(page, success_signals=cfg.post_click_success_signals)
            else:
                browser, portal = launch_persistent_session(
                    p, root / cfg.portal_profile_dir,
                    headless=cfg.portal_headless and not args.headed,
                    success_signals=cfg.post_click_success_signals,
                )
                page = portal.page
            try:
                if args.cdp:
                    nav = page.locator(LOGIN_SUCCESS_NAV)
                    if not (nav.count() and nav.first.is_visible()):
                        raise SmokeValidationError(
                            f"attached session at {args.cdp} is not authenticated")
                    log.info("attached to an authenticated session at %s", args.cdp)
                else:
                    log.info("portal session: %s", portal.ensure_authenticated())
                log.info("processing exactly these work ids: %s", work_ids)

                if args.arm:
                    batch = execute_smoke_batch(db, portal, cfg, targets)
                else:
                    for t, pv in targets:
                        try:
                            readout = validate_smoke_target(portal, cfg, t, pv)
                            print(f"  work_id {t.id:<4} {t.invoice_no}: PASS  "
                                  f"{readout.summary()}")
                            batch.append({"work_id": t.id, "invoice_no": t.invoice_no,
                                          "clicked": False, "outcome": "REHEARSAL_OK"})
                        except SmokeValidationError as exc:
                            print(f"  work_id {t.id:<4} {t.invoice_no}: FAIL  {exc}")
                            batch.append({"work_id": t.id, "invoice_no": t.invoice_no,
                                          "clicked": False,
                                          "outcome": "ABORTED_BEFORE_CLICK",
                                          "reason": str(exc)})
                        try:
                            page.keyboard.press("Escape")
                        except Exception:  # noqa: BLE001
                            pass
                    print()
            except LoginRequired as exc:
                print(f"\nLOGIN REQUIRED: {exc.marker}")
                print("  The automation never solves this check. A staff member must sign in manually:")
                print("    uv run python tools/portal_login.py")
                batch.append({"work_id": None, "outcome": "LOGIN_REQUIRED",
                              "clicked": False, "reason": exc.marker})
            except BaseException as exc:  # noqa: BLE001
                log.exception("smoke run failed")
                print(f"\nSMOKE FAILED: {type(exc).__name__}: {exc}")
                batch.append({"work_id": None, "outcome": "FAILED", "clicked": None,
                              "reason": f"{type(exc).__name__}: {exc}"})
            finally:
                if not args.cdp:
                    browser.close()
    finally:
        lock.close()

    after = _snapshot(db)
    target_ids = {t.id for t, _ in targets}
    changed = {i for i in (set(before) | set(after)) - target_ids
               if before.get(i) != after.get(i)}
    clicks = sum(1 for r in batch if r.get("clicked"))

    print(f"\n{'='*74}\nRESULT\n{'='*74}")
    for r in batch:
        wid = r.get("work_id")
        row = db.work_row_by_id(wid) if wid else None
        claim = db.get_claim(row.invoice_no) if row else None
        print(f"  work_id {str(wid):<5} {r.get('invoice_no',''):<14} "
              f"{r.get('outcome'):<22} clicked={r.get('clicked')}"
              + (f"  state={row.state.value}" if row else "")
              + (f"  claim={claim.final_outcome}" if claim else ""))
        if r.get("reason") or r.get("error"):
            print(f"        reason: {r.get('reason') or r.get('error')}")
    print(f"  {'-'*70}")
    print(f"  total clicks       : {clicks}  (cap {cfg.max_invoices_per_run})")
    print(f"  all write_claims   : "
          f"{db.conn.execute('SELECT COUNT(*) FROM write_claims').fetchone()[0]}")
    print(f"  * non-target rows changed: {len(changed)}"
          + (f"  -> {sorted(changed)}" if changed else "  (required: 0)"))

    if changed:
        print("\n** Rows outside the target set changed. This is a contract violation.")
        return 1
    outcomes = {r.get("outcome") for r in batch}
    if "UNKNOWN_OUTCOME" in outcomes:
        print("\n** A click was sent but its outcome is unconfirmed. A human must verify.")
        print("   Do not re-click: a write_claim already exists. The batch was halted.")
        return 3
    if outcomes <= {"REHEARSAL_OK", "CONFIRMED"}:
        return 0
    return 1


def _snapshot(db) -> dict[int, tuple]:
    cur = db.conn.execute("SELECT * FROM invoice_work ORDER BY id")
    return {r["id"]: tuple(r) for r in cur}


def _diff(before: dict, after: dict, *, exclude_id: int) -> set[int]:
    ids = (set(before) | set(after)) - {exclude_id}
    return {i for i in ids if before.get(i) != after.get(i)}


if __name__ == "__main__":
    raise SystemExit(main())
