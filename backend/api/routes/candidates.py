"""Candidate upload, listing, and resume processing (assignment §4.1, §4.2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlmodel import Session, select

from backend.core.db import get_session
from backend.models.schemas import BatchOut, CandidateOut, IngestReport, ResumeReport
from backend.models.tables import Candidate, ResumeText, UploadBatch
from backend.services.ingest import ingest_file
from backend.services.resume import process_batch

router = APIRouter(prefix="/candidates", tags=["candidates"])


@router.post("/upload", response_model=IngestReport)
async def upload_candidates(
    file: UploadFile = File(...),
    sheet_name: str | None = Query(None, description="XLSX sheet; defaults to first"),
    session: Session = Depends(get_session),
) -> IngestReport:
    content = await file.read()
    if not content:
        raise HTTPException(400, "Uploaded file is empty.")
    try:
        return ingest_file(session, content, file.filename or "upload", sheet_name)
    except ValueError as exc:  # unsupported type, bad sheet, missing column
        raise HTTPException(400, str(exc)) from exc


@router.get("/batches", response_model=list[BatchOut])
def list_batches(session: Session = Depends(get_session)) -> list[UploadBatch]:
    return list(
        session.exec(select(UploadBatch).order_by(UploadBatch.batch_id.desc())).all()
    )


@router.post("/batches/{batch_id}/resumes", response_model=ResumeReport)
async def process_batch_resumes(
    batch_id: int,
    force: bool = Query(False, description="Re-download even if text already stored"),
    session: Session = Depends(get_session),
) -> ResumeReport:
    if session.get(UploadBatch, batch_id) is None:
        raise HTTPException(404, f"Batch {batch_id} not found.")
    return await process_batch(session, batch_id, force=force)


@router.get("", response_model=list[CandidateOut])
def list_candidates(
    batch_id: int | None = Query(None),
    session: Session = Depends(get_session),
) -> list[Candidate]:
    stmt = select(Candidate)
    if batch_id is not None:
        stmt = stmt.where(Candidate.batch_id == batch_id)
    return list(session.exec(stmt.order_by(Candidate.s_no)).all())


@router.get("/{candidate_id}", response_model=CandidateOut)
def get_candidate(
    candidate_id: int, session: Session = Depends(get_session)
) -> Candidate:
    candidate = session.get(Candidate, candidate_id)
    if not candidate:
        raise HTTPException(404, f"Candidate {candidate_id} not found.")
    return candidate


@router.get("/{candidate_id}/resume")
def get_candidate_resume(
    candidate_id: int, session: Session = Depends(get_session)
) -> dict:
    row = session.exec(
        select(ResumeText)
        .where(ResumeText.candidate_id == candidate_id)
        .order_by(ResumeText.id.desc())
    ).first()
    if row is None:
        raise HTTPException(404, f"No resume text stored for candidate {candidate_id}.")
    return {
        "candidate_id": candidate_id,
        "text": row.text,
        "error": row.error,
        "chars": len(row.text or ""),
        "fetched_at": row.fetched_at,
    }
