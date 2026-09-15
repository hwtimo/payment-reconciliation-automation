#!/usr/bin/env python3
"""Read-only recovery for one unresolved write claim.

Confirms the claim only if the portal proves the payment under the full success-signal contract.
Never clicks.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from remittance_reconciler.config import load_config  # noqa: E402
from remittance_reconciler.database import Database  # noqa: E402
from remittance_reconciler.portal import launch_persistent_session  # noqa: E402
from remittance_reconciler.main import _recover_unresolved_claim, acquire_lock  # noqa: E402
from remittance_reconciler.models import ClaimOutcome  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    ap.add_argument("--invoice", required=True)
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.FileHandler(Path("logs") / "recover.log"),
                                  logging.StreamHandler()])

    root = Path(args.config).resolve().parent
    cfg = load_config(Path(args.config))
    db = Database(root / "data" / "eft.db"); db.migrate()

    claim = db.get_claim(args.invoice)
    if claim is None:
        print(f"{args.invoice}: no write_claim; nothing to recover."); return 1
    if claim.final_outcome is ClaimOutcome.CONFIRMED:
        print(f"{args.invoice}: already CONFIRMED; nothing to do."); return 0

    row = db.conn.execute("SELECT statement_id FROM invoice_work WHERE invoice_no=?",
                          (args.invoice,)).fetchone()
    work = next(w for w in db.work_rows(row["statement_id"])
                if w.invoice_no == args.invoice)
    print(f"{args.invoice}: claim={claim.final_outcome} state={work.state.value} "
          f"net={work.eft_net}")

    before = {r["id"]: tuple(r) for r in
              db.conn.execute("SELECT * FROM invoice_work ORDER BY id")}
    lock = acquire_lock(root / "data" / ".run.lock")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx, portal = launch_persistent_session(
                p, root / cfg.portal_profile_dir, headless=cfg.portal_headless,
                success_signals=cfg.post_click_success_signals)
            try:
                portal.ensure_authenticated()
                ok = _recover_unresolved_claim(db, portal, cfg, work, claim)
            finally:
                ctx.close()
    finally:
        lock.close()

    after = {r["id"]: tuple(r) for r in
             db.conn.execute("SELECT * FROM invoice_work ORDER BY id")}
    changed = {i for i in set(before) | set(after) if before.get(i) != after.get(i)}
    c2 = db.get_claim(args.invoice)
    w2 = next(w for w in db.work_rows(row["statement_id"])
              if w.invoice_no == args.invoice)
    print(f"\n  recovered      : {ok}")
    print(f"  claim outcome  : {c2.final_outcome}")
    print(f"  work state     : {w2.state.value}  attribution={w2.attribution}"
          f"  error={w2.error_code}")
    print(f"  changed rows   : {len(changed)} {sorted(changed)}  (must be 0 outside the target)")
    print(f"  total claims   : "
          f"{db.conn.execute('SELECT COUNT(*) c FROM write_claims').fetchone()['c']}"
          f"  (no new claim)")
    if changed - {work.id}:
        print("** Rows outside the target changed."); return 1
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
