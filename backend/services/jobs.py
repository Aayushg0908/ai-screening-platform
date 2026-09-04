"""Job description persistence (§4.3)."""

from __future__ import annotations

from sqlmodel import Session, select

from backend.core.logging import get_logger
from backend.models.schemas import JobDescriptionIn
from backend.models.tables import JobDescription

logger = get_logger(__name__)


def create_job(payload: JobDescriptionIn, session: Session) -> JobDescription:
    """Persist a new job description and return it."""
    job = JobDescription(title=payload.title, description=payload.description)
    session.add(job)
    session.commit()
    session.refresh(job)
    logger.info("created job %s: %s", job.job_id, job.title)
    return job


def list_jobs(session: Session) -> list[JobDescription]:
    """Return every job description, newest first."""
    return list(
        session.exec(select(JobDescription).order_by(JobDescription.job_id.desc())).all()
    )


def get_job(job_id: int, session: Session) -> JobDescription | None:
    """Return one job description by id, or ``None``."""
    return session.get(JobDescription, job_id)
