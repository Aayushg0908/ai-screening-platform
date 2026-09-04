"""Batch evaluation endpoints (start a run, poll status, read results)."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlmodel import Session

from backend.core.db import get_session
from backend.models.schemas import (
    EvaluateRequest,
    EvaluationOut,
    RunStatusOut,
)
from backend.services import evaluation as evaluation_service

router = APIRouter(prefix="/evaluate", tags=["evaluation"])


@router.post("", response_model=RunStatusOut, status_code=202)
def start_run(
    payload: EvaluateRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> RunStatusOut:
    """Create a :class:`PipelineRun` for a job and start it in the background."""
    return evaluation_service.start_run(payload.job_id, background_tasks, session)  # type: ignore[return-value]


@router.get("/{run_id}/status", response_model=RunStatusOut)
def get_run_status(
    run_id: int,
    session: Session = Depends(get_session),
) -> RunStatusOut:
    """Return progress for a run (frontend polls this)."""
    return evaluation_service.get_run_status(run_id, session)  # type: ignore[return-value]


@router.get("/{run_id}/results", response_model=list[EvaluationOut])
def get_run_results(
    run_id: int,
    session: Session = Depends(get_session),
) -> list[EvaluationOut]:
    """Return the ranked per-candidate results for a completed run."""
    return evaluation_service.get_run_results(run_id, session)  # type: ignore[return-value]
