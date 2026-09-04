"""Batch evaluation orchestration.

Creates a :class:`PipelineRun`, runs the compiled LangGraph pipeline once per
candidate, and persists each :class:`Evaluation` incrementally as it completes
(not at the end of the batch). Ranking and shortlisting happen once all
candidates are done.
"""

from __future__ import annotations

from fastapi import BackgroundTasks
from sqlmodel import Session

from backend.core.logging import get_logger
from backend.models.tables import Evaluation, PipelineRun

logger = get_logger(__name__)


def start_run(
    job_id: int, background_tasks: BackgroundTasks, session: Session
) -> PipelineRun:
    """Create a pending run and schedule :func:`execute_run` in the background."""
    raise NotImplementedError


def execute_run(run_id: int) -> None:
    """Evaluate every candidate for the run, persisting results incrementally."""
    raise NotImplementedError


def get_run_status(run_id: int, session: Session) -> PipelineRun:
    """Return the run row (progress counters + accumulated errors)."""
    raise NotImplementedError


def get_run_results(run_id: int, session: Session) -> list[Evaluation]:
    """Return the run's evaluations ordered by rank."""
    raise NotImplementedError
