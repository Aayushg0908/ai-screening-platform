
"""
Fresh Google Calendar OAuth setup.

Run this locally to generate a fresh Google OAuth refresh token.

IMPORTANT:
- credentials.json must belong to the CURRENT Google OAuth client.
- Do NOT commit credentials.json or .env.
- The generated refresh token should be stored only in .env.
"""

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events"
]

ROOT = Path(__file__).resolve().parent.parent
CLIENT_SECRETS = ROOT / "credentials.json"


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def fail(message: str) -> None:
    print()
    print("=" * 60)
    print("[FAIL]")
    print(message)
    print("=" * 60)
    sys.exit(1)


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main() -> None:

    print("=" * 60)
    print("        Google Calendar Fresh OAuth Setup")
    print("=" * 60)
    print()

    # -----------------------------------------------------
    # 1. Check credentials.json
    # -----------------------------------------------------

    if not CLIENT_SECRETS.exists():
        fail(
            f"credentials.json was not found.\n\n"
            f"Expected location:\n{CLIENT_SECRETS}"
        )

    print("[ok] credentials.json found")
    print("[info] Using:", CLIENT_SECRETS)
    print()

    # -----------------------------------------------------
    # 2. Read OAuth client information
    # -----------------------------------------------------

    try:
        with open(CLIENT_SECRETS, "r", encoding="utf-8") as fh:
            data = json.load(fh)

    except Exception as exc:
        fail(f"Could not read credentials.json:\n{exc}")

    installed = data.get("installed") or data.get("web")

    if not installed:
        fail(
            "credentials.json does not contain "
            "'installed' or 'web' OAuth configuration."
        )

    client_id = installed.get("client_id")
    client_secret = installed.get("client_secret")

    if not client_id:
        fail("client_id is missing from credentials.json")

    if not client_secret:
        fail("client_secret is missing from credentials.json")

    print("[ok] OAuth configuration loaded")
    print()
    print("[info] Client ID:")
    print(client_id)
    print()
    print("[info] Client secret found")
    print("[info] OAuth scope:")
    print(SCOPES[0])
    print()

    # -----------------------------------------------------
    # 3. Start fresh OAuth authorization
    # -----------------------------------------------------

    print("=" * 60)
    print("Opening Google authorization in your browser...")
    print("=" * 60)
    print()

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CLIENT_SECRETS),
            SCOPES,
        )

        credentials = flow.run_local_server(
            port=0,
            access_type="offline",
            prompt="consent",
            open_browser=True,
        )

    except Exception as exc:
        fail(
            "Google OAuth authorization failed.\n\n"
            f"{type(exc).__name__}: {exc}"
        )

    # -----------------------------------------------------
    # 4. Check refresh token
    # -----------------------------------------------------

    if not credentials.refresh_token:
        fail(
            "Google did not return a refresh token.\n\n"
            "Run the authorization again and make sure "
            "consent is granted."
        )

    print()
    print("[ok] OAuth authorization completed")
    print("[ok] Fresh refresh token received")
    print()

    # -----------------------------------------------------
    # 5. Build Calendar API
    # -----------------------------------------------------

    try:
        service = build(
            "calendar",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )

    except Exception as exc:
        fail(
            "Could not create Google Calendar API client.\n\n"
            f"{type(exc).__name__}: {exc}"
        )

    print("[ok] Calendar API client created")

    # -----------------------------------------------------
    # 6. Test actual Calendar permission
    # -----------------------------------------------------

    test_event = {
        "summary": "AI Screening Platform - OAuth Test",
        "description": (
            "Temporary event created during Google OAuth setup. "
            "It will be deleted automatically."
        ),
        "start": {
            "dateTime": "2026-09-05T10:00:00+05:30",
            "timeZone": "Asia/Kolkata",
        },
        "end": {
            "dateTime": "2026-09-05T10:30:00+05:30",
            "timeZone": "Asia/Kolkata",
        },
    }

    event_id = None

    try:
        print()
        print("[info] Testing Calendar event creation...")

        created_event = service.events().insert(
            calendarId="primary",
            body=test_event,
        ).execute()

        event_id = created_event.get("id")

        if not event_id:
            fail("Calendar event was created but no event ID was returned.")

        print("[ok] Calendar event created")
        print("[ok] Event ID:", event_id)

        if created_event.get("htmlLink"):
            print("[ok] Event URL:", created_event["htmlLink"])

    except Exception as exc:
        fail(
            "Calendar event creation failed.\n\n"
            f"{type(exc).__name__}: {exc}"
        )

    # -----------------------------------------------------
    # 7. Delete test event
    # -----------------------------------------------------

    try:
        print()
        print("[info] Deleting temporary test event...")

        service.events().delete(
            calendarId="primary",
            eventId=event_id,
        ).execute()

        print("[ok] Test event deleted")

    except Exception as exc:
        print()
        print("[WARN] Event was created but could not be deleted.")
        print(type(exc).__name__, ":", exc)

    # -----------------------------------------------------
    # 8. Print values for .env
    # -----------------------------------------------------

    print()
    print("=" * 60)
    print("[SUCCESS] Google Calendar OAuth is working")
    print("=" * 60)
    print()

    print("Copy the following values into your .env file:")
    print()

    print(f"GOOGLE_CLIENT_ID={client_id}")
    print(f"GOOGLE_CLIENT_SECRET={client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN={credentials.refresh_token}")
    print("GOOGLE_CALENDAR_ID=primary")

    print()
    print("=" * 60)
    print("IMPORTANT")
    print("=" * 60)
    print("Do NOT commit .env or credentials.json.")
    print("Do NOT share the refresh token or client secret.")
    print()


if __name__ == "__main__":
    main()
