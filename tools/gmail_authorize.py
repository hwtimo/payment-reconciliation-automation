#!/usr/bin/env python3
"""One-time interactive OAuth consent; stores the token under ``secrets/`` with owner-only permissions."""


from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from remittance_reconciler.gmail import SCOPES, GmailClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets"
CLIENT_SECRET = SECRETS / "client_secret.json"
TOKEN = SECRETS / "token.json"


def main() -> int:
    if not CLIENT_SECRET.is_file():
        print(f"missing {CLIENT_SECRET}", file=sys.stderr)
        print("Create an OAuth client (Desktop app) in Google Cloud Console and save the", file=sys.stderr)
        print("downloaded JSON to that path, then re-run.", file=sys.stderr)
        return 2

    SECRETS.mkdir(parents=True, exist_ok=True)
    SECRETS.chmod(0o700)
    print("requesting scopes:")
    for s in SCOPES:
        print("   ", s)
    print("\na browser window will open for consent...\n")

    client = GmailClient.authorize(CLIENT_SECRET, TOKEN)
    profile = client._service.users().getProfile(userId="me").execute()  # noqa: SLF001
    print(f"authorized as: {profile.get('emailAddress')}")
    print(f"token written : {TOKEN} (mode {oct(TOKEN.stat().st_mode & 0o777)})")
    print("\nReminder: if the consent screen is still in 'Testing', this token expires in 7 days.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
