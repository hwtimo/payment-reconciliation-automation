#!/usr/bin/env python3
"""List write candidates authorized by provenance (database only; never queries the portal)."""


from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from remittance_reconciler.config import load_config  # noqa: E402
from remittance_reconciler.database import Database  # noqa: E402
from remittance_reconciler.provenance import verify_provenance  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--db", default=None, help="default: db_path from config")
    ap.add_argument("--audit", metavar="INVOICE_NO",
                    help="print the EFT statement that authorizes this invoice")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    db_path = Path(args.db) if args.db else Path(getattr(cfg, "db_path", "data/eft.db"))
    db = Database(db_path)
    db.migrate()

    if args.audit:
        return _audit(db, args.audit)

    candidates = db.authorized_write_candidates()
    if not candidates:
        print("No authorized write candidates.")
        print("\nZero candidates can be normal. There are none when:")
        print("  · forwarded EFTs have not been ingested yet")
        print("  · reconciliation/detail validation has not finished (not DETAIL_VALIDATED)")
        print("  · everything has already been claimed")
        print("\n* Never substitute unpaid invoices found by searching the portal.")
        return 0

    print(f"authorized write candidates: {len(candidates)} "
          f"(all derived from trusted forwarded EFT statements):\n")
    print(f"  {'invoice':<16} {'EFT net':>10}  {'vendor':<9} {'payment doc':<14} source message")
    print("  " + "-" * 88)
    for w in candidates:
        prov, why = verify_provenance(db, w)
        if why is not None:
            print(f"  {w.invoice_no:<16} {'-':>10}  REJECTED: {why}")
            continue
        print(f"  {prov.raw_eft_invoice_no:<16} {prov.eft_net:>10}  "
              f"{prov.vendor_no:<9} {prov.payment_document_no:<14} "
              f"{prov.source_message_id}")
    return 0


def _audit(db: Database, invoice_no: str) -> int:
    claim = db.get_claim(invoice_no)
    if claim is not None:
        print(f"write_claim for {invoice_no}:")
        print(f"  claimed_at          : {claim.claimed_at}")
        print(f"  claimed_amount      : {claim.claimed_amount}")
        print(f"  final_outcome       : {claim.final_outcome}")
        print(f"  authorized by ------------------------------------------")
        print(f"  source_message_id   : {claim.source_message_id}")
        print(f"  vendor_no           : {claim.vendor_no}")
        print(f"  payment_document_no : {claim.payment_document_no}")
        print(f"  content_fingerprint : {claim.content_fingerprint}")
        print(f"  raw_eft_invoice_no  : {claim.raw_eft_invoice_no}")
        return 0

    row = db.conn.execute(
        "SELECT statement_id FROM invoice_work WHERE invoice_no=?", (invoice_no,)
    ).fetchone()
    if row is None:
        print(f"{invoice_no}: not in invoice_work; it did not originate from any EFT statement.")
        print("  -> not authorized for writes. Whether it exists in the portal is irrelevant.")
        return 1

    work = next(w for w in db.work_rows(row["statement_id"]) if w.invoice_no == invoice_no)
    prov, why = verify_provenance(db, work)
    if why is not None:
        print(f"{invoice_no}: NOT AUTHORIZED — {why}")
        return 1
    print(f"{invoice_no}: AUTHORIZED (no claim yet)")
    print(f"  {prov.audit_ref()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
