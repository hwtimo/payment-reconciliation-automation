#!/usr/bin/env python3
"""Open a visible browser on the dedicated profile so a person can complete an interactive login."""


from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from remittance_reconciler.config import load_config  # noqa: E402
from remittance_reconciler.portal import launch_persistent_session  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--timeout-minutes", type=int, default=15)
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    root = Path(args.config).resolve().parent
    profile = root / cfg.portal_profile_dir

    from playwright.sync_api import sync_playwright

    print("=" * 70)
    print("Interactive portal login")
    print("=" * 70)
    print(f"  profile: {profile}")
    print("  Sign in manually when the window opens. If a verification step (e.g. CAPTCHA) appears,")
    print("  complete it yourself as well.")
    print("  The window closes automatically once sign-in completes.\n")

    with sync_playwright() as p:
        context, portal = launch_persistent_session(p, profile, headless=False)
        try:
            portal.page.goto(f"{portal.base_url}/app", wait_until="domcontentloaded")

            if portal.is_authenticated(timeout_ms=5000):
                print("Already signed in. Nothing to do.")
                return 0

            print(f"Waiting for sign-in… (up to {args.timeout_minutes} min)")
            try:
                portal.page.wait_for_selector(
                    'a[href="/app#reports"]',
                    timeout=args.timeout_minutes * 60_000,
                )
            except Exception:
                print("\nSign-in did not complete in time. Nothing was changed.")
                return 1

            if not portal.is_authenticated():
                print("\nCould not confirm sign-in.")
                return 1

            print("\nSign-in confirmed. The authenticated session was saved to the profile.")
            print("Subsequent unattended runs reuse this session without signing in again.")
            return 0
        finally:
            context.close()
            try:
                profile.chmod(0o700)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
