"""Interview scheduling endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from backend.core.db import get_session
from backend.models.schemas import InterviewOut, InterviewScheduleRequest
from backend.services import interviews as interviews_service

router = APIRouter(prefix="/interviews", tags=["interviews"])


@router.post("/schedule", response_model=InterviewOut, status_code=201)
def schedule_interview(
    payload: InterviewScheduleRequest,
    session: Session = Depends(get_session),
) -> InterviewOut:
    """Create a Google Calendar event with a Meet link and persist it."""
    return interviews_service.schedule_interview(payload, session)  # type: ignore[return-value]


@router.get("", response_model=list[InterviewOut])
def list_interviews(
    session: Session = Depends(get_session),
) -> list[InterviewOut]:
    """Return every scheduled interview."""
    return interviews_service.list_interviews(session)  # type: ignore[return-value]
