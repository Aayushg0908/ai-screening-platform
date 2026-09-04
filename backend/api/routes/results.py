"""Test-result ingestion endpoint.

The uploaded ``Test Result`` sheet is the sole authority for test scores. It is
joined to candidates on ``s_no`` with a LEFT join: candidates missing from the
sheet get an explicit ``NO_RESULT`` state, never an imputed zero.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, UploadFile
from sqlmodel import Session

from backend.core.db import get_session
from backend.models.schemas import UploadSummary
from backend.services import results as results_service

router = APIRouter(prefix="/results", tags=["results"])


@router.post("/upload", response_model=UploadSummary)
async def upload_results(
    file: UploadFile,
    session: Session = Depends(get_session),
) -> UploadSummary:
    """Ingest the ``Test Result`` sheet (``.csv`` or ``.xlsx``)."""
    content = await file.read()
    return results_service.ingest_results(content, file.filename or "upload", session)
