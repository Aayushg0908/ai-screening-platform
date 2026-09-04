"""Test-results upload and final (post-test) scoring endpoints (§4.7)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlmodel import Session

from backend.core.db import get_session
from backend.models.schemas import (
    FinalResultsOut,
    FinalShortlistRequest,
    ResultsReport,
    TestResultOut,
)
from backend.services import results as results_service

router = APIRouter(prefix="/results", tags=["results"])


@router.post("/upload", response_model=ResultsReport)
async def upload_results(
    batch_id: int = Query(..., description="Candidate batch to join test results onto"),
    sheet_name: str | None = Query(None, description="XLSX sheet; defaults to first"),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> ResultsReport:
    """Upload a test-results CSV/XLSX and left-join it onto a candidate batch
    by ``s_no``. Never joins on email or name - the two files use different
    plus-tags on the same address."""
    content = await file.read()
    if not content:
        raise HTTPException(400, "Uploaded file is empty.")
    try:
        return results_service.ingest_results(
            session, content, file.filename or "results", batch_id, sheet_name
        )
    except ValueError as exc:  # unsupported type, bad sheet, missing column, no batch
        raise HTTPException(400, str(exc)) from exc


@router.post("/shortlist", response_model=FinalResultsOut)
def final_shortlist(
    payload: FinalShortlistRequest, session: Session = Depends(get_session)
) -> FinalResultsOut:
    """Recompute the final (post-test) ranked list from STORED scores. Zero
    LLM calls, zero GitHub calls - the recruiter's weight-tuning path, exactly
    like ``/evaluate/runs/{run_id}/rerank``."""
    try:
        return results_service.compute_final_shortlist(session, payload)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("", response_model=list[TestResultOut])
def list_results(
    batch_id: int = Query(...), session: Session = Depends(get_session)
) -> list[TestResultOut]:
    """Stored TestResult rows for a candidate batch."""
    return results_service.get_results_for_batch(session, batch_id)
