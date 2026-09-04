import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events"
]

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "").strip()
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary").strip()


def fail(message):
    print(f"[FAIL] {message}")
    sys.exit(1)


print("========================================")
print(" Google Calendar Authentication Test")
print("========================================")
print()

# --------------------------------------------------
# 1. Check environment variables
# --------------------------------------------------

if not CLIENT_ID:
    fail("GOOGLE_CLIENT_ID is missing from .env")

if not CLIENT_SECRET:
    fail("GOOGLE_CLIENT_SECRET is missing from .env")

if not REFRESH_TOKEN:
    fail("GOOGLE_REFRESH_TOKEN is missing from .env")


print("[ok] Google environment variables found")
print("[info] Client ID:", CLIENT_ID)
print("[info] Calendar ID:", CALENDAR_ID)
print()


# --------------------------------------------------
# 2. Create OAuth credentials
# --------------------------------------------------

try:
    credentials = Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=SCOPES,
    )

    print("[ok] OAuth credentials object created")

except Exception as e:
    fail(f"Could not create OAuth credentials: {e}")


# --------------------------------------------------
# 3. Explicitly refresh access token
# --------------------------------------------------

try:
    print("[info] Requesting fresh Google access token...")

    credentials.refresh(Request())

    print("[ok] Google access token obtained")
    print()

except Exception as e:
    print()
    print("[FAIL] Google OAuth refresh failed")
    print(type(e).__name__)
    print(str(e))
    print()

    print("This means Google rejected the")
    print("CLIENT_ID + CLIENT_SECRET + REFRESH_TOKEN combination.")
    sys.exit(1)


# --------------------------------------------------
# 4. Build Calendar API
# --------------------------------------------------

try:
    service = build(
        "calendar",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )

    print("[ok] Google Calendar API client created")

except Exception as e:
    fail(f"Could not create Calendar API client: {e}")


# --------------------------------------------------
# 5. Test event creation
# --------------------------------------------------

event = {
    "summary": "AI Screening Platform - OAuth Test",
    "description": "Temporary OAuth test event. It will be deleted automatically.",
    "start": {
        "dateTime": "2026-09-05T10:00:00+05:30",
        "timeZone": "Asia/Kolkata",
    },
    "end": {
        "dateTime": "2026-09-05T10:30:00+05:30",
        "timeZone": "Asia/Kolkata",
    },
}


try:
    print("[info] Creating temporary calendar event...")

    created = service.events().insert(
        calendarId=CALENDAR_ID,
        body=event,
    ).execute()

    event_id = created["id"]

    print("[ok] Calendar event created")
    print("[info] Event ID:", event_id)
    print("[info] Event URL:", created.get("htmlLink"))

except Exception as e:
    fail(f"Calendar event creation failed: {e}")


# --------------------------------------------------
# 6. Delete test event
# --------------------------------------------------

try:
    print("[info] Deleting temporary event...")

    service.events().delete(
        calendarId=CALENDAR_ID,
        eventId=event_id,
    ).execute()

    print("[ok] Test event deleted")

except Exception as e:
    print("[WARN] Event was created but could not be deleted.")
    print(type(e).__name__)
    print(str(e))


print()
print("========================================")
print("[SUCCESS] Google Calendar is working")
print("========================================")