"""Real Google Calendar + Meet integration (§4.8, hard constraint §5).

No mocks, no fake links. ``create_interview_event`` inserts a real event via
the Calendar API and treats a missing ``hangoutLink`` as a failure - Google
silently drops the whole ``conferenceData`` block (valid event, no error, no
link) when ``conferenceDataVersion=1`` is omitted from the insert call, so
that parameter is mandatory here.

Server-side refresh only: ``get_credentials`` builds a
:class:`~google.oauth2.credentials.Credentials` from the client id/secret and
a long-lived refresh token obtained once via ``scripts/google_oauth_setup.py``
- no interactive browser flow at request time.
"""

from __future__ import annotations

import uuid
from datetime import date as date_type
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.models.tables import Candidate, JobDescription

logger = get_logger(__name__)

# calendar.events is enough to create/read/delete events with Meet links.
# Do NOT request full "calendar" scope - least privilege (matches
# scripts/google_oauth_setup.py, which minted the stored refresh token under
# this same scope).
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

_RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


def get_credentials() -> Credentials:
    """Build OAuth credentials from config. Server-side refresh, no
    interactive flow - the refresh token was obtained once, out of band."""
    settings = get_settings()
    if not (
        settings.google_client_id
        and settings.google_client_secret
        and settings.google_refresh_token
    ):
        raise RuntimeError(
            "Google Calendar not configured "
            "(GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET/GOOGLE_REFRESH_TOKEN)"
        )
    return Credentials(
        token=None,
        refresh_token=settings.google_refresh_token,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )


def _service():
    return build("calendar", "v3", credentials=get_credentials(), cache_discovery=False)


def _is_transient(exc: BaseException) -> bool:
    """Retry connection hiccups and Google's own 5xx/429 - never an auth
    failure, which will not fix itself on a second attempt."""
    if isinstance(exc, HttpError):
        status = exc.resp.status if exc.resp is not None else None
        return status in _RETRYABLE_HTTP_STATUSES
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


# --------------------------------------------------------------------------
# slot generation - pure, no I/O
# --------------------------------------------------------------------------


def generate_slots(
    start_date: date_type,
    day_start: int,
    day_end: int,
    slot_minutes: int,
    gap_minutes: int,
    count: int,
    tz: str,
) -> list[tuple[datetime, datetime]]:
    """Sequential interview slots within working hours, in ``tz``.

    Skips Saturdays and Sundays; rolls to the next working day once a day's
    window is full. Pure function - no I/O, unit testable.
    """
    if count <= 0:
        return []

    zone = ZoneInfo(tz)
    slots: list[tuple[datetime, datetime]] = []
    day = start_date

    while len(slots) < count:
        if day.weekday() >= 5:  # Saturday=5, Sunday=6
            day += timedelta(days=1)
            continue

        cursor = datetime(day.year, day.month, day.day, day_start, tzinfo=zone)
        day_end_dt = datetime(day.year, day.month, day.day, day_end, tzinfo=zone)

        while len(slots) < count and cursor + timedelta(minutes=slot_minutes) <= day_end_dt:
            slot_end = cursor + timedelta(minutes=slot_minutes)
            slots.append((cursor, slot_end))
            cursor = slot_end + timedelta(minutes=gap_minutes)

        day += timedelta(days=1)

    return slots


# --------------------------------------------------------------------------
# Calendar API
# --------------------------------------------------------------------------


@retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, max=20),
    reraise=True,
)
def create_interview_event(
    candidate: Candidate,
    job: JobDescription,
    final_score: float | None,
    start: datetime,
    end: datetime,
    tz: str,
) -> dict:
    """Create a real Calendar event with a Meet link and return it.

    ``conferenceDataVersion=1`` is MANDATORY on the insert call - without it
    Google silently drops the ``conferenceData`` block and returns a valid
    event with no Meet link and no error. A missing ``hangoutLink`` on the
    response is therefore treated as a failure here (the broken event is
    cleaned up before raising), never reported as a success with a null link.

    ``sendUpdates="none"``: the candidate's actual invitation is the branded
    email sent separately via ``services.mailer`` (Phase 6 pattern), not
    Google's own generic attendee notification - avoids a redundant,
    unbranded second email straight from the recruiter's personal account.
    """
    settings = get_settings()
    service = _service()

    score_line = f"{final_score:.2f}/100" if final_score is not None else "N/A"
    description = (
        f"Role: {job.title}\n"
        f"Candidate: {candidate.name}\n"
        f"Final score: {score_line}\n\n"
        "Scheduled automatically by the AI Screening Platform."
    )

    attendees = [{"email": candidate.email}]
    bcc = (settings.email_bcc or "").strip()
    if bcc and bcc.lower() != candidate.email.strip().lower():
        # My address, so the invite lands on my calendar too and the Meet
        # link can be verified independently of the candidate's inbox.
        attendees.append({"email": bcc})

    body = {
        "summary": f"Interview: {candidate.name} — {job.title}",
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": tz},
        "end": {"dateTime": end.isoformat(), "timeZone": tz},
        "attendees": attendees,
        "reminders": {"useDefault": True},
        "conferenceData": {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }

    event = (
        service.events()
        .insert(
            calendarId=settings.google_calendar_id,
            body=body,
            conferenceDataVersion=1,
            sendUpdates="none",
        )
        .execute()
    )

    if not event.get("hangoutLink"):
        event_id = event.get("id")
        logger.error(
            "create_interview_event: event %s created with no hangoutLink - "
            "deleting it rather than reporting a false success",
            event_id,
        )
        if event_id:
            delete_event(event_id)
        raise RuntimeError(
            f"event {event_id} was created but Google returned no Meet link "
            "(hangoutLink) - conferenceData was dropped"
        )

    return event


def get_event(event_id: str) -> dict | None:
    """Fetch a live event from the Calendar API. ``None`` if it no longer
    exists (already cancelled/deleted)."""
    try:
        return (
            _service()
            .events()
            .get(calendarId=get_settings().google_calendar_id, eventId=event_id)
            .execute()
        )
    except HttpError as exc:
        status = exc.resp.status if exc.resp is not None else None
        if status == 404:
            return None
        raise


def delete_event(event_id: str) -> None:
    """Cancel a Calendar event. Best-effort: an already-gone event (404/410)
    is treated as success, not an error."""
    try:
        _service().events().delete(
            calendarId=get_settings().google_calendar_id, eventId=event_id
        ).execute()
    except HttpError as exc:
        status = exc.resp.status if exc.resp is not None else None
        if status in (404, 410):
            return
        raise
