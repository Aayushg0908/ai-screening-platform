"""Candidate outreach endpoints (§4.6): shortlist preview + test-link email.

Preview and send are separate endpoints so a recruiter can inspect the
shortlist before anything actually goes out.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from backend.core.db import get_session
from backend.models.schemas import EmailLogOut, SendReport, ShortlistPreview, ShortlistRequest
from backend.models.tables import EmailLog
from backend.services import outreach as outreach_service

router = APIRouter(prefix="/outreach", tags=["outreach"])


@router.post("/preview", response_model=ShortlistPreview)
def preview_shortlist(
    payload: ShortlistRequest, session: Session = Depends(get_session)
) -> ShortlistPreview:
    """Preview a run's shortlist from stored results. Sends nothing."""
    return outreach_service.get_shortlist(session, payload)


@router.post("/send", response_model=SendReport)
def send_shortlist(
    payload: ShortlistRequest,
    force: bool = Query(False, description="Resend even if already emailed"),
    session: Session = Depends(get_session),
) -> SendReport:
    """Email the assessment link to a run's shortlist."""
    return outreach_service.send_test_invites(session, payload, force=force)


@router.get("/log", response_model=list[EmailLogOut])
def email_log(
    run_id: int = Query(..., description="Evaluation run to filter by"),
    session: Session = Depends(get_session),
) -> list[EmailLog]:
    """All send attempts (sent or failed) recorded for a run."""
    return list(
        session.exec(
            select(EmailLog).where(EmailLog.run_id == run_id).order_by(EmailLog.id)
        ).all()
    )
