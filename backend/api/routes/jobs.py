"""Job description endpoints (§4.3). Thin — logic lives in services.jobs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from backend.core.db import get_session
from backend.models.schemas import JobDescriptionIn, JobDescriptionOut
from backend.services import jobs as jobs_service

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("", response_model=JobDescriptionOut, status_code=201)
def create_job(
    payload: JobDescriptionIn,
    session: Session = Depends(get_session),
) -> JobDescriptionOut:
    return jobs_service.create_job(payload, session)  # type: ignore[return-value]


@router.get("", response_model=list[JobDescriptionOut])
def list_jobs(session: Session = Depends(get_session)) -> list[JobDescriptionOut]:
    return jobs_service.list_jobs(session)  # type: ignore[return-value]


@router.get("/{job_id}", response_model=JobDescriptionOut)
def get_job(
    job_id: int, session: Session = Depends(get_session)
) -> JobDescriptionOut:
    job = jobs_service.get_job(job_id, session)
    if job is None:
        raise HTTPException(404, f"Job {job_id} not found.")
    return job  # type: ignore[return-value]
