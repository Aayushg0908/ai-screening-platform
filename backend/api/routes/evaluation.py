"""Evaluation endpoints: single-candidate (§4.3/§4.4) and batch (§4.5)."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query
from sqlmodel import Session, select

from backend.core.config import get_settings
from backend.core.db import get_session
from backend.core.logging import get_logger
from backend.models.schemas import (
    BatchRequest,
    EvaluationOut,
    GitHubEvaluationOut,
    RerankWeights,
    RunResultItem,
    RunResultsOut,
    RunStatusOut,
    ScoreWeights,
)
from backend.models.schemas import GitHubEvaluation, ResumeEvaluation
from backend.models.tables import Candidate, Evaluation, JobDescription, PipelineRun, UploadBatch
from backend.services import evaluation as evaluation_service
from backend.services import github as github_service
from backend.services import scoring

logger = get_logger(__name__)
router = APIRouter(prefix="/evaluate", tags=["evaluation"])


# ---------------------------------------------------------------------------
# Single candidate (Phases 3 & 4)
# ---------------------------------------------------------------------------


@router.post("/candidate/{candidate_id}", response_model=EvaluationOut)
def evaluate_candidate(
    candidate_id: int,
    job_id: int = Query(..., description="Job description to evaluate against"),
    weights: ScoreWeights | None = Body(
        default=None, description="Optional per-dimension weight override"
    ),
    session: Session = Depends(get_session),
) -> EvaluationOut:
    """Evaluate one candidate's resume against one job description."""
    return evaluation_service.evaluate_candidate(
        session, candidate_id, job_id, weights
    )


@router.post("/github/{candidate_id}", response_model=GitHubEvaluationOut)
async def evaluate_github(
    candidate_id: int,
    job_id: int = Query(..., description="Job description to evaluate against"),
    force: bool = Query(False, description="Bypass the GitHub API response cache"),
    session: Session = Depends(get_session),
) -> GitHubEvaluationOut:
    """Repository-level GitHub analysis for one candidate (§4.4)."""
    return await github_service.analyze_candidate(
        session, candidate_id, job_id, force=force
    )


# ---------------------------------------------------------------------------
# Batch (Phase 5)
# ---------------------------------------------------------------------------


@router.post("/batch", response_model=RunStatusOut, status_code=202)
def start_batch(
    payload: BatchRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> RunStatusOut:
    """Create a :class:`PipelineRun` and kick off ``run_batch`` in the background.

    Returns immediately with ``run_id`` — it does not wait for the batch.
    """
    if session.get(UploadBatch, payload.batch_id) is None:
        raise HTTPException(404, f"batch {payload.batch_id} not found")
    if session.get(JobDescription, payload.job_id) is None:
        raise HTTPException(404, f"job {payload.job_id} not found")

    total = len(
        session.exec(
            select(Candidate).where(Candidate.batch_id == payload.batch_id)
        ).all()
    )
    resolved_mode = get_settings().eval_profile(payload.mode)["mode"]
    run = PipelineRun(
        batch_id=payload.batch_id,
        job_id=payload.job_id,
        status="pending",
        mode=resolved_mode,
        total=total,
        weights=(payload.weights or ScoreWeights()).model_dump(),
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    background_tasks.add_task(
        evaluation_service.run_batch_in_thread,
        payload.batch_id,
        payload.job_id,
        payload.weights,
        payload.force,
        payload.mode,
    )
    return RunStatusOut(
        run_id=run.run_id,
        status=run.status,
        mode=resolved_mode,
        processed=0,
        total=total,
        errors=[],
    )


def _get_run(session: Session, run_id: int) -> PipelineRun:
    run = session.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(404, f"run {run_id} not found")
    return run


@router.get("/runs/{run_id}", response_model=RunStatusOut)
def run_status(run_id: int, session: Session = Depends(get_session)) -> RunStatusOut:
    """Polling view: status, progress, and accumulated errors."""
    run = _get_run(session, run_id)
    rows = session.exec(
        select(Evaluation).where(Evaluation.run_id == run_id)
    ).all()
    errors = list(run.errors or [])
    for row in rows:
        for err in row.errors or []:
            errors.append(f"candidate {row.candidate_id}: {err}")
    return RunStatusOut(
        run_id=run.run_id,
        status=run.status,
        mode=run.mode,
        processed=run.processed,
        total=run.total,
        errors=errors,
    )


def _result_rows(session: Session, run_id: int) -> list[dict]:
    """Join a run's Evaluation rows to their candidates -> plain dicts."""
    rows = session.exec(
        select(Evaluation, Candidate)
        .join(Candidate, Candidate.candidate_id == Evaluation.candidate_id)
        .where(Evaluation.run_id == run_id)
    ).all()
    return [
        {
            "candidate_id": ev.candidate_id,
            "s_no": cand.s_no,
            "name": cand.name,
            "status": ev.status,
            "model_used": ev.model_used,
            "resume_score": ev.resume_score,
            "github_score": ev.github_score,
            "pre_test_score": ev.pre_test_score,
            "github_status": ev.github_status,
            "resume_eval": ev.resume_eval or None,
            "github_eval": ev.github_eval or None,
            "errors": list(ev.errors or []),
        }
        for ev, cand in rows
    ]


def _results_payload(session: Session, run_id: int) -> RunResultsOut:
    ranked, unranked = scoring.rank_candidates(_result_rows(session, run_id))
    run = session.get(PipelineRun, run_id)
    return RunResultsOut(
        run_id=run_id,
        mode=run.mode if run else None,
        ranked=[RunResultItem(**row) for row in ranked],
        unranked=[RunResultItem(**row) for row in unranked],
    )


@router.get("/runs/{run_id}/results", response_model=RunResultsOut)
def run_results(
    run_id: int, session: Session = Depends(get_session)
) -> RunResultsOut:
    """The run's candidates: ``ranked`` by ``pre_test_score`` desc, plus any
    ``unranked`` (unscorable — both resume and GitHub evaluation failed)."""
    _get_run(session, run_id)
    return _results_payload(session, run_id)


@router.post("/runs/{run_id}/rerank", response_model=RunResultsOut)
def rerank(
    run_id: int,
    payload: RerankWeights,
    session: Session = Depends(get_session),
) -> RunResultsOut:
    """Recompute scores and ordering from STORED dimension scores.

    The recruiter weight-tuning path: pure arithmetic, instant, makes **zero**
    LLM or GitHub API calls. Persists the recomputed scores back to the run.
    """
    _get_run(session, run_id)
    dims = payload.dimensions or ScoreWeights()
    blend = {"resume": payload.resume, "github": payload.github}
    logger.info(
        "rerank run %s: blend=%s dims=%s (no LLM/GitHub calls)",
        run_id,
        blend,
        dims.model_dump(),
    )

    rows = session.exec(
        select(Evaluation).where(Evaluation.run_id == run_id)
    ).all()
    for ev in rows:
        resume_score: float | None = ev.resume_score
        if ev.resume_eval:
            try:
                resume_score = scoring.compute_resume_score(
                    ResumeEvaluation(**ev.resume_eval), dims
                )
            except Exception:  # noqa: BLE001 - keep the stored score on bad data
                logger.warning(
                    "rerank: candidate %s has unparseable resume_eval", ev.candidate_id
                )

        github_score: float | None = None
        gh = ev.github_eval or {}
        if gh.get("evaluation"):
            try:
                github_score = scoring.compute_github_score(
                    GitHubEvaluation(**gh["evaluation"])
                )
            except Exception:  # noqa: BLE001
                github_score = ev.github_score

        ev.resume_score = resume_score
        ev.github_score = github_score
        ev.pre_test_score = scoring.compute_pre_test_score(
            resume_score, github_score, blend  # None stays None — never 0.0
        )
        ev.status = scoring.evaluation_state(
            resume_score, github_score, ev.github_status, ev.pre_test_score
        )
        ev.weights = dims.model_dump()
        session.add(ev)

    run = session.get(PipelineRun, run_id)
    run.weights = {**dims.model_dump(), "_blend": blend}
    session.add(run)
    session.commit()

    return _results_payload(session, run_id)
