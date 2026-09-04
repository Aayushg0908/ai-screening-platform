"""Interview scheduling endpoints (§4.8): Google Calendar + Meet."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from backend.core.db import get_session
from backend.models.schemas import InterviewOut, ScheduleReport, ScheduleRequest
from backend.models.tables import Candidate, Interview
from backend.services import calendar as calendar_service
from backend.services import interviews as interviews_service

router = APIRouter(prefix="/interviews", tags=["interviews"])


@router.post("/schedule", response_model=ScheduleReport)
def schedule(
    payload: ScheduleRequest,
    force: bool = Query(
        False, description="Cancel and re-book an already-scheduled interview"
    ),
    session: Session = Depends(get_session),
) -> ScheduleReport:
    """Schedule interviews for a run's qualified candidates (stored final
    scores only - zero re-evaluation)."""
    try:
        return interviews_service.schedule_interviews(session, payload, force=force)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("", response_model=list[InterviewOut])
def list_interviews(
    run_id: int = Query(...), session: Session = Depends(get_session)
) -> list[InterviewOut]:
    """Stored Interview rows for a run."""
    rows = session.exec(
        select(Interview, Candidate)
        .join(Candidate, Candidate.candidate_id == Interview.candidate_id)
        .where(Interview.run_id == run_id)
        .order_by(Interview.starts_at)
    ).all()
    return [
        InterviewOut(
            id=iv.id,
            candidate_id=iv.candidate_id,
            run_id=iv.run_id,
            s_no=cand.s_no,
            name=cand.name,
            email=cand.email,
            event_id=iv.event_id,
            meet_link=iv.meet_link,
            starts_at=iv.starts_at,
            ends_at=iv.ends_at,
            status=iv.status,
            error=iv.error,
            created_at=iv.created_at,
        )
        for iv, cand in rows
    ]


@router.delete("/{interview_id}")
def cancel_interview(
    interview_id: int, session: Session = Depends(get_session)
) -> dict:
    """Cancel the real Calendar event and delete the Interview row - for
    cleaning up test events."""
    row = session.get(Interview, interview_id)
    if row is None:
        raise HTTPException(404, f"interview {interview_id} not found")
    if row.event_id:
        try:
            calendar_service.delete_event(row.event_id)
        except Exception as exc:  # noqa: BLE001 - surface it, don't fake success
            raise HTTPException(
                502, f"failed to cancel calendar event {row.event_id}: {exc}"
            ) from exc
    event_id = row.event_id
    session.delete(row)
    session.commit()
    return {"deleted": interview_id, "event_id": event_id}
