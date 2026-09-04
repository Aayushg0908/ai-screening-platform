"""Job description endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from backend.core.db import get_session
from backend.models.schemas import JobDescriptionCreate, JobDescriptionOut
from backend.services import jobs as jobs_service

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("", response_model=JobDescriptionOut, status_code=201)
def create_job(
    payload: JobDescriptionCreate,
    session: Session = Depends(get_session),
) -> JobDescriptionOut:
    """Create a job description."""
    return jobs_service.create_job(payload, session)  # type: ignore[return-value]


@router.get("", response_model=list[JobDescriptionOut])
def list_jobs(
    session: Session = Depends(get_session),
) -> list[JobDescriptionOut]:
    """Return every job description."""
    return jobs_service.list_jobs(session)  # type: ignore[return-value]


@router.get("/{job_id}", response_model=JobDescriptionOut)
def get_job(
    job_id: int,
    session: Session = Depends(get_session),
) -> JobDescriptionOut:
    """Return one job description by id."""
    return jobs_service.get_job(job_id, session)  # type: ignore[return-value]
