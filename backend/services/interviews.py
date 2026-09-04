"""Interview scheduling orchestration.

Wraps :mod:`backend.services.calendar` event creation and persists the
resulting :class:`Interview` row with its Meet link.
"""

from __future__ import annotations

from sqlmodel import Session

from backend.core.logging import get_logger
from backend.models.schemas import InterviewScheduleRequest
from backend.models.tables import Interview

logger = get_logger(__name__)


def schedule_interview(
    payload: InterviewScheduleRequest, session: Session
) -> Interview:
    """Create a calendar event with a Meet link and persist the interview."""
    raise NotImplementedError


def list_interviews(session: Session) -> list[Interview]:
    """Return every scheduled interview."""
    raise NotImplementedError
