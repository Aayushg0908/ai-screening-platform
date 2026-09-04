"""Candidate outreach endpoints (email the test link to the shortlist)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from backend.core.db import get_session
from backend.models.schemas import SendTestsRequest, SendTestsResult
from backend.services import outreach as outreach_service

router = APIRouter(prefix="/outreach", tags=["outreach"])


@router.post("/send-tests", response_model=SendTestsResult)
def send_tests(
    payload: SendTestsRequest,
    session: Session = Depends(get_session),
) -> SendTestsResult:
    """Email the assessment link to the shortlisted candidates of a run."""
    return outreach_service.send_tests(
        payload.run_id, payload.top_n, session
    )  # type: ignore[return-value]
