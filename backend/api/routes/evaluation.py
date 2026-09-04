"""Single-candidate AI evaluation endpoint (§4.3).

Batch evaluation / ranking is Phase 5 and lives elsewhere.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query
from sqlmodel import Session

from backend.core.db import get_session
from backend.models.schemas import EvaluationOut, GitHubEvaluationOut, ScoreWeights
from backend.services import evaluation as evaluation_service
from backend.services import github as github_service

router = APIRouter(prefix="/evaluate", tags=["evaluation"])


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
