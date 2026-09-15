#!/usr/bin/env python3
"""Online SQLite backup with integrity check, row-count comparison, retention and restore drill.

Usage: python tools/backup_db.py [--db data/eft.db] [--out backups] [--keep 14] [--verify-restore]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _open_ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def _counts(conn: sqlite3.Connection) -> dict:
    q = lambda s: conn.execute(s).fetchone()[0]
    return {
        "write_claims": q("SELECT COUNT(*) FROM write_claims"),
        "confirmed": q("SELECT COUNT(*) FROM write_claims WHERE final_outcome='CONFIRMED'"),
        "invoice_work": q("SELECT COUNT(*) FROM invoice_work"),
        "statements": q("SELECT COUNT(*) FROM statements"),
        "claim_sum": q("SELECT COALESCE(SUM(CAST(claimed_amount AS REAL)),0) FROM write_claims"),
    }


def backup(db_path: Path, out_dir: Path, keep: int) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = out_dir / f"eft-{stamp}.db"

    src = _open_ro(db_path)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.chmod(dest, 0o600)

    journal = db_path.with_name(db_path.name + ".claims.jsonl")
    if journal.is_file():
        jdest = out_dir / f"eft-{stamp}.claims.jsonl"
        shutil.copy2(journal, jdest)
        os.chmod(jdest, 0o600)

    live = _open_ro(db_path); back = _open_ro(dest)
    try:
        integrity = back.execute("PRAGMA integrity_check").fetchone()[0]
        a, b = _counts(live), _counts(back)
    finally:
        live.close(); back.close()
    if integrity != "ok":
        dest.unlink(missing_ok=True)
        raise SystemExit(f"backup failed integrity_check: {integrity}")
    if a != b:
        dest.unlink(missing_ok=True)
        raise SystemExit(f"backup does not match live DB:\n  live={a}\n  backup={b}")

    print(f"  backup: {dest.name}  ({dest.stat().st_size} bytes, mode "
          f"{oct(dest.stat().st_mode & 0o777)})")
    print(f"  integrity_check: {integrity}")
    print(f"  counts match   : {a}")

    snaps = sorted(out_dir.glob("eft-*.db"))
    for old in snaps[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
        old.with_name(old.name.replace(".db", ".claims.jsonl")).unlink(missing_ok=True)
        print(f"  pruned: {old.name}")
    print(f"  retained: {len(sorted(out_dir.glob('eft-*.db')))} snapshot(s)")
    return dest


def verify_restore(snapshot: Path, live_db: Path) -> int:
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "restored.db"
    shutil.copy2(snapshot, tmp)
    live = _open_ro(live_db); rest = _open_ro(tmp)
    try:
        a, b = _counts(live), _counts(rest)
        integ = rest.execute("PRAGMA integrity_check").fetchone()[0]
        live_ids = {r[0] for r in live.execute("SELECT invoice_no FROM write_claims")}
        rest_ids = {r[0] for r in rest.execute("SELECT invoice_no FROM write_claims")}
    finally:
        live.close(); rest.close()
    print(f"  restored to : {tmp}   (the production DB was not touched)")
    print(f"  integrity   : {integ}")
    print(f"  counts      : live={a}\n                restored={b}")
    missing = live_ids - rest_ids
    print(f"  claims present in live but missing from restore: {len(missing)}")
    tmp.unlink(missing_ok=True)
    ok = integ == "ok" and a == b and not missing
    print(f"  RESTORE {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    ap.add_argument("--db", default="data/eft.db")
    ap.add_argument("--out", default="backups")
    ap.add_argument("--keep", type=int, default=14)
    ap.add_argument("--verify-restore", action="store_true")
    args = ap.parse_args(argv)

    db_path = Path(args.db).resolve()
    out_dir = Path(args.out).resolve()
    dest = backup(db_path, out_dir, args.keep)
    if args.verify_restore:
        print("\n--- restore verification ---")
        return verify_restore(dest, db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
