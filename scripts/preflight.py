"""
Deployment preflight checks for the AI Screening Platform.

Checks:
1. Environment variables
2. PostgreSQL / Neon
3. Groq LLM
4. GitHub API
5. SMTP email
6. Google Calendar OAuth + API
"""

import os
import sys
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from groq import Groq
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import requests


# ============================================================
# Load environment
# ============================================================

load_dotenv(override=True)


def check_env():
    print("\n========== ENVIRONMENT ==========")

    required = [
        "DATABASE_URL",
        "GROQ_API_KEYS",
        "GITHUB_TOKEN",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
        "GOOGLE_CALENDAR_ID",
    ]

    missing = []

    for key in required:
        value = os.getenv(key)

        if value:
            print(f"[ok] {key}")
        else:
            print(f"[FAIL] {key}")
            missing.append(key)

    if missing:
        print("\nMissing environment variables:")
        for key in missing:
            print(f" - {key}")

        return False

    return True


# ============================================================
# PostgreSQL / Neon
# ============================================================

def check_postgres():
    print("\n========== POSTGRES / NEON ==========")

    try:
        database_url = os.getenv("DATABASE_URL")

        engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_recycle=300,
        )

        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            value = result.scalar()

        if value == 1:
            print("[ok] PostgreSQL connection successful")
            return True

        print("[FAIL] PostgreSQL returned unexpected result")
        return False

    except Exception as e:
        print(f"[FAIL] PostgreSQL: {e}")
        return False


# ============================================================
# Groq LLM
# ============================================================

def check_groq():
    print("\n========== GROQ LLM ==========")

    try:
        keys_raw = os.getenv("GROQ_API_KEYS", "")

        keys = [
            key.strip()
            for key in keys_raw.split(",")
            if key.strip()
        ]

        # Fallback to single key if GROQ_API_KEYS is not used
        if not keys:
            single_key = os.getenv("GROQ_API_KEY")

            if single_key:
                keys = [single_key]

        if not keys:
            print("[FAIL] No Groq API key found")
            return False

        model = os.getenv(
            "GROQ_MODEL",
            "openai/gpt-oss-120b"
        )

        client = Groq(api_key=keys[0])

        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": "Reply with exactly: GROQ_OK"
                }
            ],
            max_tokens=20,
        )

        answer = response.choices[0].message.content.strip()

        print(f"[ok] Groq API reachable")
        print(f"[info] Model: {model}")
        print(f"[info] Response: {answer}")

        return True

    except Exception as e:
        print(f"[FAIL] Groq: {e}")
        return False


# ============================================================
# GitHub
# ============================================================

def check_github():
    print("\n========== GITHUB ==========")

    try:
        token = os.getenv("GITHUB_TOKEN")

        if not token:
            print("[FAIL] GITHUB_TOKEN missing")
            return False

        response = requests.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )

        if response.status_code == 200:
            data = response.json()

            print("[ok] GitHub API reachable")
            print(f"[info] Authenticated as: {data.get('login')}")

            return True

        print(
            f"[FAIL] GitHub returned HTTP "
            f"{response.status_code}"
        )

        return False

    except Exception as e:
        print(f"[FAIL] GitHub: {e}")
        return False


# ============================================================
# SMTP
# ============================================================

def check_smtp():
    print("\n========== SMTP / EMAIL ==========")

    try:
        host = os.getenv("SMTP_HOST")
        port = int(os.getenv("SMTP_PORT", "587"))
        username = os.getenv("SMTP_USER")
        password = os.getenv("SMTP_PASSWORD")

        if not host or not username or not password:
            print("[FAIL] SMTP configuration incomplete")
            return False

        print(f"[info] SMTP host: {host}")
        print(f"[info] SMTP port: {port}")
        print(f"[info] SMTP user: {username}")

        server = smtplib.SMTP(
            host,
            port,
            timeout=20,
        )

        server.ehlo()
        server.starttls()
        server.ehlo()

        server.login(
            username,
            password,
        )

        print("[ok] SMTP authentication successful")

        # ----------------------------------------------------
        # Send test email
        # ----------------------------------------------------

        msg = EmailMessage()

        msg["Subject"] = "AI Screening Platform - SMTP Test"
        msg["From"] = username
        msg["To"] = username

        msg.set_content(
            "This is a test email from the AI Screening Platform "
            "preflight check.\n\n"
            "If you received this email, SMTP is working correctly."
        )

        server.send_message(msg)

        server.quit()

        print("[ok] Test email sent successfully")

        return True

    except Exception as e:
        print(f"[FAIL] SMTP: {e}")
        return False


# ============================================================
# Google Calendar
# ============================================================

def check_google_calendar():
    print("\n========== GOOGLE CALENDAR ==========")

    try:
        client_id = os.getenv("GOOGLE_CLIENT_ID")
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
        refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN")
        calendar_id = os.getenv(
            "GOOGLE_CALENDAR_ID",
            "primary"
        )

        if not client_id:
            print("[FAIL] GOOGLE_CLIENT_ID missing")
            return False

        if not client_secret:
            print("[FAIL] GOOGLE_CLIENT_SECRET missing")
            return False

        if not refresh_token:
            print("[FAIL] GOOGLE_REFRESH_TOKEN missing")
            return False

        scopes = [
            "https://www.googleapis.com/auth/calendar.events"
        ]

        credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
        )

        print("[ok] OAuth credentials object created")

        # Force token refresh
        from google.auth.transport.requests import Request

        credentials.refresh(Request())

        print("[ok] Google access token obtained")

        service = build(
            "calendar",
            "v3",
            credentials=credentials,
        )

        print("[ok] Google Calendar API client created")

        # ----------------------------------------------------
        # Create temporary test event
        # ----------------------------------------------------

        start_time = datetime.now(timezone.utc) + timedelta(
            minutes=5
        )

        end_time = start_time + timedelta(
            minutes=10
        )

        event_body = {
            "summary": "AI Screening Platform - Preflight Test",
            "description": "Temporary event created by deployment preflight.",
            "start": {
                "dateTime": start_time.isoformat(),
                "timeZone": "UTC",
            },
            "end": {
                "dateTime": end_time.isoformat(),
                "timeZone": "UTC",
            },
        }

        event = service.events().insert(
            calendarId=calendar_id,
            body=event_body,
        ).execute()

        event_id = event.get("id")
        event_url = event.get("htmlLink")

        print("[ok] Calendar event created")
        print(f"[info] Event ID: {event_id}")
        print(f"[info] Event URL: {event_url}")

        # ----------------------------------------------------
        # Delete temporary event
        # ----------------------------------------------------

        print("[info] Deleting temporary event...")

        service.events().delete(
            calendarId=calendar_id,
            eventId=event_id,
        ).execute()

        print("[ok] Test event deleted")

        print("[SUCCESS] Google Calendar is working")

        return True

    except Exception as e:
        print(f"[FAIL] Google Calendar: {e}")
        return False


# ============================================================
# Main
# ============================================================

def main():

    print("=" * 60)
    print("AI SCREENING PLATFORM - DEPLOYMENT PREFLIGHT")
    print("=" * 60)

    results = {}

    # Environment
    results["Environment"] = check_env()

    if not results["Environment"]:
        print("\n[ABORT] Environment configuration incomplete.")
        sys.exit(1)

    # Database
    results["PostgreSQL"] = check_postgres()

    # LLM
    results["Groq"] = check_groq()

    # GitHub
    results["GitHub"] = check_github()

    # SMTP
    results["SMTP"] = check_smtp()

    # Google
    results["Google Calendar"] = check_google_calendar()

    # ========================================================
    # Summary
    # ========================================================

    print("\n" + "=" * 60)
    print("PREFLIGHT SUMMARY")
    print("=" * 60)

    all_passed = True

    for name, result in results.items():

        status = "[PASS]" if result else "[FAIL]"

        print(f"{status} {name}")

        if not result:
            all_passed = False

    print("=" * 60)

    if all_passed:
        print("[SUCCESS] ALL PREFLIGHT CHECKS PASSED")
        sys.exit(0)

    else:
        print("[FAIL] ONE OR MORE PREFLIGHT CHECKS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()