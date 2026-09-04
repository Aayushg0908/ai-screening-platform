"""Candidate outreach endpoints (§4.6): shortlist preview + test-link email.

Preview and send are separate endpoints so a recruiter can inspect the
shortlist before anything actually goes out.

Every handler here converts a transient database connectivity failure (e.g. a
Neon DNS resolution hiccup - the query layer in ``services/outreach.py``
already retries a few times before this ever triggers) into a clean 503, not
a raw 500. A DB outage is never the caller's fault, so 503 (not a 4xx) is the
correct status - the frontend surfaces any non-2xx identically either way.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session

from backend.core.db import get_session
from backend.core.logging import get_logger
from backend.models.schemas import EmailLogOut, SendReport, ShortlistPreview, ShortlistRequest
from backend.services import outreach as outreach_service

logger = get_logger(__name__)
router = APIRouter(prefix="/outreach", tags=["outreach"])

_DB_UNAVAILABLE_MESSAGE = (
    "Database temporarily unreachable - please retry in a few seconds."
)


@router.post("/preview", response_model=ShortlistPreview)
def preview_shortlist(
    payload: ShortlistRequest, session: Session = Depends(get_session)
) -> ShortlistPreview:
    """Preview a run's shortlist from stored results. Sends nothing."""
    try:
        return outreach_service.get_shortlist(session, payload)
    except DBAPIError as exc:
        logger.error("preview_shortlist: database error: %s", exc)
        raise HTTPException(503, _DB_UNAVAILABLE_MESSAGE) from exc


@router.post("/send", response_model=SendReport)
def send_shortlist(
    payload: ShortlistRequest,
    force: bool = Query(False, description="Resend even if already emailed"),
    session: Session = Depends(get_session),
) -> SendReport:
    """Email the assessment link to a run's shortlist."""
    try:
        return outreach_service.send_test_invites(session, payload, force=force)
    except DBAPIError as exc:
        logger.error("send_shortlist: database error: %s", exc)
        raise HTTPException(503, _DB_UNAVAILABLE_MESSAGE) from exc


@router.get("/log", response_model=list[EmailLogOut])
def email_log(
    run_id: int = Query(..., description="Evaluation run to filter by"),
    session: Session = Depends(get_session),
) -> list[EmailLogOut]:
    """All send attempts (sent or failed) recorded for a run."""
    try:
        return outreach_service.list_email_log(session, run_id)
    except DBAPIError as exc:
        logger.error("email_log: database error: %s", exc)
        raise HTTPException(503, _DB_UNAVAILABLE_MESSAGE) from exc
