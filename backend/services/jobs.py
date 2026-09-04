"""Job description persistence."""

from __future__ import annotations

from sqlmodel import Session

from backend.core.logging import get_logger
from backend.models.schemas import JobDescriptionCreate
from backend.models.tables import JobDescription

logger = get_logger(__name__)


def create_job(payload: JobDescriptionCreate, session: Session) -> JobDescription:
    """Persist a new job description and return it."""
    raise NotImplementedError


def list_jobs(session: Session) -> list[JobDescription]:
    """Return every job description."""
    raise NotImplementedError


def get_job(job_id: int, session: Session) -> JobDescription | None:
    """Return one job description by id, or ``None``."""
    raise NotImplementedError
