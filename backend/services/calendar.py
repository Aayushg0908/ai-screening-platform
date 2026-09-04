"""Google Calendar event creation with Meet links.

The insert call **must** pass ``conferenceDataVersion=1`` or Google silently
drops the ``conferenceData`` block and no Meet link is created. Auth is a
refresh token from the Desktop-app OAuth client (see
``scripts/google_oauth_setup.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from tenacity import retry, stop_after_attempt, wait_exponential

from backend.core.config import get_settings
from backend.core.logging import get_logger

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

logger = get_logger(__name__)

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


@dataclass(slots=True)
class ScheduledEvent:
    """A created calendar event."""

    event_id: str
    meet_link: str | None
    html_link: str | None
    start: datetime
    end: datetime


def build_calendar_service() -> "Resource":
    """Build an authorised Calendar v3 service from the config refresh token."""
    _ = get_settings()
    raise NotImplementedError


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=15))
def create_interview_event(
    *,
    summary: str,
    description: str,
    start: datetime,
    end: datetime,
    attendee_emails: list[str],
) -> ScheduledEvent:
    """Insert an event with a Meet link (``conferenceDataVersion=1``)."""
    raise NotImplementedError
