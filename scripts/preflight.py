"""
Preflight credential check.

Verifies every external dependency by actually USING it, not just checking that
an env var is non-empty. Run this before writing business logic — a wrong
credential found now costs 2 minutes, found at hour 30 it costs an evening.

Usage:
    python scripts/preflight.py            # non-destructive checks only
    python scripts/preflight.py --full     # also sends an email + creates a
                                           # throwaway calendar event (deleted after)

Windows-safe output: ASCII only, no unicode symbols.
"""

from __future__ import annotations

import argparse
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from dotenv import load_dotenv

load_dotenv()

PASS, FAIL, SKIP = "[ok]  ", "[FAIL]", "[skip]"
results: list[tuple[str, bool]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    tag = PASS if ok else FAIL
    print(f"{tag} {name:<22} {detail}")
    results.append((name, ok))


def skip(name: str, why: str) -> None:
    print(f"{SKIP} {name:<22} {why}")


def env(key: str) -> str | None:
    v = os.getenv(key)
    return v.strip() if v and v.strip() else None


# --------------------------------------------------------------------------
def check_database() -> None:
    url = env("DATABASE_URL")
    if not url:
        return report("Postgres", False, "DATABASE_URL not set")
    try:
        from sqlalchemy import create_engine, text

        eng = create_engine(url, pool_pre_ping=True)
        with eng.connect() as conn:
            ver = conn.execute(text("select version()")).scalar() or ""
            conn.execute(text("create table if not exists _preflight (id int)"))
            conn.execute(text("drop table _preflight"))
            conn.commit()
        report("Postgres", True, ver.split(",")[0])
    except Exception as exc:
        report("Postgres", False, str(exc)[:110])


def _groq_keys() -> list[str]:
    """GROQ_API_KEYS (comma-separated) plus the singular GROQ_API_KEY, deduped."""
    keys: list[str] = []
    for raw in (env("GROQ_API_KEYS") or "").split(","):
        k = raw.strip()
        if k and k not in keys:
            keys.append(k)
    single = env("GROQ_API_KEY")
    if single and single not in keys:
        keys.append(single)
    return keys


def check_groq() -> None:
    keys = _groq_keys()
    if not keys:
        return report("Groq LLM", False, "no GROQ_API_KEYS / GROQ_API_KEY set")
    try:
        from langchain_groq import ChatGroq

        model_id = env("GROQ_MODEL") or "openai/gpt-oss-120b"
        for i, k in enumerate(keys):
            llm = ChatGroq(
                model=model_id, api_key=k, temperature=0, max_tokens=16, max_retries=0
            )
            llm.invoke("Reply with exactly: OK")
        report(
            "Groq LLM pool", True, f"{len(keys)} key(s), all responding — {model_id}"
        )
    except Exception as exc:
        report("Groq LLM pool", False, f"{len(keys)} key(s); {str(exc)[:90]}")


def check_mistral() -> None:
    key = env("MISTRAL_API_KEY")
    if not key:
        return skip("Mistral fallback", "MISTRAL_API_KEY not set (optional)")
    try:
        from langchain_mistralai import ChatMistralAI

        llm = ChatMistralAI(
            model=env("MISTRAL_MODEL") or "mistral-large-latest",
            api_key=key,
            temperature=0,
            max_retries=0,
        )
        out = llm.invoke("Reply with exactly: OK").content.strip()
        report("Mistral fallback", True, f"responded: {out[:30]!r}")
    except Exception as exc:
        report("Mistral fallback", False, str(exc)[:110])


def check_github() -> None:
    token = env("GITHUB_TOKEN")
    if not token:
        return report("GitHub API", False, "GITHUB_TOKEN not set")
    try:
        import httpx

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        with httpx.Client(timeout=20, headers=headers) as cli:
            rl = cli.get("https://api.github.com/rate_limit")
            rl.raise_for_status()
            limit = rl.json()["resources"]["core"]["limit"]

            # Exercise the exact calls the analyzer will make.
            repos = cli.get(
                "https://api.github.com/users/torvalds/repos",
                params={"per_page": 3, "sort": "updated"},
            )
            repos.raise_for_status()
            n = len(repos.json())

        if limit < 1000:
            return report(
                "GitHub API", False, f"limit={limit}/hr - token not applied (need 5000)"
            )
        report("GitHub API", True, f"limit={limit}/hr, fetched {n} repos")
    except Exception as exc:
        report("GitHub API", False, str(exc)[:110])


def check_smtp(send: bool) -> None:
    host, user, pwd = env("SMTP_HOST"), env("SMTP_USER"), env("SMTP_PASSWORD")
    if not all([host, user, pwd]):
        return report("Gmail SMTP", False, "SMTP_HOST/USER/PASSWORD incomplete")
    try:
        port = int(env("SMTP_PORT") or 587)
        with smtplib.SMTP(host, port, timeout=25) as srv:
            srv.starttls()
            srv.login(user, pwd)
            if send:
                msg = MIMEText("Preflight check passed. Delete me.")
                msg["Subject"] = "[preflight] AI Screening Platform"
                msg["From"] = user
                msg["To"] = user
                srv.send_message(msg)
        report("Gmail SMTP", True, f"sent test email to {user}" if send else "auth ok")
    except smtplib.SMTPAuthenticationError:
        report("Gmail SMTP", False, "auth rejected - use a 16-char App Password, not your login password")
    except Exception as exc:
        report("Gmail SMTP", False, str(exc)[:110])


def check_calendar(create: bool) -> None:
    cid, csec, rtok = (
        env("GOOGLE_CLIENT_ID"),
        env("GOOGLE_CLIENT_SECRET"),
        env("GOOGLE_REFRESH_TOKEN"),
    )
    if not all([cid, csec, rtok]):
        return report("Google Calendar", False, "CLIENT_ID/SECRET/REFRESH_TOKEN incomplete")
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=rtok,
            client_id=cid,
            client_secret=csec,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/calendar.events"],
        )
        svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        cal_id = env("GOOGLE_CALENDAR_ID") or "primary"

        if not create:
            svc.events().list(calendarId=cal_id, maxResults=1).execute()
            return report("Google Calendar", True, "refresh token valid (read)")

        start = datetime.now(timezone.utc) + timedelta(days=1)
        body = {
            "summary": "[preflight] delete me",
            "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": (start + timedelta(minutes=30)).isoformat(), "timeZone": "UTC"},
            # This block is what generates the Meet link. Without
            # conferenceDataVersion=1 below, Google silently ignores it.
            "conferenceData": {
                "createRequest": {
                    "requestId": f"preflight-{int(start.timestamp())}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
        }
        ev = svc.events().insert(
            calendarId=cal_id, body=body, conferenceDataVersion=1
        ).execute()

        link = ev.get("hangoutLink")
        svc.events().delete(calendarId=cal_id, eventId=ev["id"]).execute()

        if not link:
            return report("Google Calendar", False, "event created but NO Meet link returned")
        report("Google Calendar", True, f"Meet link generated: {link}")
    except Exception as exc:
        report("Google Calendar", False, str(exc)[:130])


def check_test_link() -> None:
    url = env("TEST_LINK_URL")
    if not url or "your-google-form-id" in url:
        return report("Test link", False, "TEST_LINK_URL still a placeholder")
    report("Test link", True, url[:60])


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--full",
        action="store_true",
        help="also send a real email and create+delete a real calendar event",
    )
    args = ap.parse_args()

    print("\n" + "=" * 72)
    print("PREFLIGHT" + ("  (full: will send email + create calendar event)" if args.full else "  (read-only; use --full to test sending)"))
    print("=" * 72)

    check_database()
    check_groq()
    check_mistral()
    check_github()
    check_smtp(send=args.full)
    check_calendar(create=args.full)
    check_test_link()

    failed = [n for n, ok in results if not ok]
    print("=" * 72)
    if failed:
        print(f"{len(failed)} FAILED: {', '.join(failed)}")
        sys.exit(1)
    print(f"All {len(results)} checks passed. Safe to start building.")


if __name__ == "__main__":
    main()