"""
One-time Google OAuth setup.

Run this ONCE locally to obtain a refresh token for the Google Calendar API.
The refresh token goes in .env; the deployed app then mints access tokens
server-side with no interactive login.

Usage:
    1. Place your downloaded OAuth client file at the repo root as credentials.json
    2. python scripts/google_oauth_setup.py
    3. A browser opens -> sign in with the SAME account you added as a test user
    4. Copy the three printed values into .env

Requires: google-auth-oauthlib, google-api-python-client
"""

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# calendar.events is enough to create events with Meet links.
# Do NOT request full "calendar" scope — least privilege.
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

ROOT = Path(__file__).resolve().parent.parent
CLIENT_SECRETS = ROOT / "credentials.json"


def main() -> None:
    if not CLIENT_SECRETS.exists():
        sys.exit(
            f"Missing {CLIENT_SECRETS}\n"
            "Download your OAuth client JSON from Google Cloud Console\n"
            "(APIs & Services > Credentials > your Desktop client > Download JSON)\n"
            "and save it at the repo root as credentials.json"
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), SCOPES)

    # access_type=offline is what makes Google issue a refresh token at all.
    # prompt=consent forces a NEW refresh token even if you've authorised before —
    # without it, a repeat run returns refresh_token=None and you'll think it broke.
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        open_browser=True,
    )

    if not creds.refresh_token:
        sys.exit(
            "No refresh token returned. Revoke this app's access at\n"
            "https://myaccount.google.com/permissions and run again."
        )

    # Prove the credentials actually work before you trust them.
    try:
        service = build("calendar", "v3", credentials=creds)
        cal = service.calendars().get(calendarId="primary").execute()
        print(f"\n[ok] Authenticated. Primary calendar: {cal.get('summary')}")
    except Exception as exc:  # noqa: BLE001
        print(f"\n[warn] Token issued but Calendar API call failed: {exc}")
        print("Check that the Google Calendar API is ENABLED for this project.")

    with open(ROOT / "credentials.json", encoding="utf-8") as fh:
        data = json.load(fh)
    installed = data.get("installed") or data.get("web") or {}

    print("\n" + "=" * 62)
    print("Copy these into your .env file:")
    print("=" * 62)
    print(f"GOOGLE_CLIENT_ID={installed.get('client_id', '')}")
    print(f"GOOGLE_CLIENT_SECRET={installed.get('client_secret', '')}")
    print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
    print("GOOGLE_CALENDAR_ID=primary")
    print("=" * 62)
    print("\nDo NOT commit credentials.json or .env.")


if __name__ == "__main__":
    main()