"""Graph node implementations (§4.5).

Four nodes: ``load_resume`` -> (``evaluate_vs_jd`` ∥ ``analyze_github``) ->
``aggregate``. EVERY node wraps its work in try/except and appends to
``state["errors"]`` instead of raising — one candidate's failure must never kill
the batch. Each node returns a partial state dict for LangGraph to merge.

The nodes are thin: they delegate to the Phase 3 / Phase 4 services unchanged
and only reshape the result into graph state. Each opens its own short-lived DB
session (the graph runs many candidates concurrently; sessions are not shared).
"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from sqlmodel import Session, select

from backend.core.db import engine
from backend.core.logging import get_logger
from backend.graph.state import CandidateState
from backend.models.schemas import GitHubStatus, ScoreWeights
from backend.models.tables import ResumeText
from backend.services import evaluation as evaluation_service
from backend.services import github as github_service
from backend.services import scoring

logger = get_logger(__name__)


def _cfg(config: RunnableConfig) -> dict:
    return (config or {}).get("configurable", {}) or {}


def load_resume(state: CandidateState, config: RunnableConfig) -> CandidateState:
    """Read the latest ResumeText for the candidate.

    Missing text is noted in ``errors`` but is NOT fatal — Phase 3 already
    evaluates from the candidate's dataset fields alone when no resume text
    exists.
    """
    try:
        with Session(engine) as session:
            row = session.exec(
                select(ResumeText)
                .where(ResumeText.candidate_id == state["candidate_id"])
                .order_by(ResumeText.id.desc())
            ).first()
        text = (row.text if row else None) or None
        if not (text or "").strip():
            return {
                "resume_text": None,
                "errors": [
                    "load_resume: no resume text on file — evaluating from "
                    "dataset fields only"
                ],
            }
        return {"resume_text": text}
    except Exception as exc:  # noqa: BLE001 - nodes never raise
        logger.exception("load_resume failed for candidate %s", state.get("candidate_id"))
        return {"errors": [f"load_resume: {exc}"]}


def evaluate_vs_jd(state: CandidateState, config: RunnableConfig) -> CandidateState:
    """Score the resume against the job description (delegates to Phase 3).

    On failure ``resume_score`` is left ``None`` (never 0.0) so the aggregate
    can redistribute weight rather than bury the candidate with a zero.
    """
    try:
        weights = _cfg(config).get("weights") or ScoreWeights()
        with Session(engine) as session:
            out = evaluation_service.evaluate_candidate(
                session, state["candidate_id"], state["job_id"], weights
            )
        failed = out.error is not None or out.evaluation is None
        return {
            "resume_eval": out.evaluation.model_dump(mode="json")
            if out.evaluation
            else None,
            "resume_score": None if failed else out.resume_score,
            "errors": [f"evaluate_vs_jd: {out.error}"] if out.error else [],
        }
    except Exception as exc:  # noqa: BLE001 - nodes never raise
        logger.exception("evaluate_vs_jd failed for candidate %s", state.get("candidate_id"))
        return {"errors": [f"evaluate_vs_jd: {exc}"]}


async def analyze_github(
    state: CandidateState, config: RunnableConfig
) -> CandidateState:
    """Repository-level GitHub analysis (delegates to Phase 4).

    NO_PROFILE / NOT_FOUND / EMPTY are normal outcomes carried in ``github_eval``
    — only a genuine ERROR is appended to ``errors``.
    """
    try:
        force = bool(_cfg(config).get("force", False))
        with Session(engine) as session:
            out = await github_service.analyze_candidate(
                session, state["candidate_id"], state["job_id"], force=force
            )
        errors: list[str] = []
        if out.status == GitHubStatus.ERROR and out.error:
            errors = [f"analyze_github: {out.error}"]
        return {
            "github_eval": out.model_dump(mode="json"),
            "github_score": out.github_score,
            "errors": errors,
        }
    except Exception as exc:  # noqa: BLE001 - nodes never raise
        logger.exception("analyze_github failed for candidate %s", state.get("candidate_id"))
        return {"errors": [f"analyze_github: {exc}"]}


def aggregate(state: CandidateState, config: RunnableConfig) -> CandidateState:
    """Deterministically blend resume and GitHub scores into ``pre_test_score``.

    A missing GitHub score is redistributed, not treated as zero — see
    :func:`scoring.compute_pre_test_score`.
    """
    try:
        blend = _cfg(config).get("blend") or scoring.PRETEST_BLEND
        pre = scoring.compute_pre_test_score(
            state.get("resume_score"),  # None on failure — never coerce to 0.0
            state.get("github_score"),
            blend,
        )
        return {"pre_test_score": pre}
    except Exception as exc:  # noqa: BLE001 - nodes never raise
        logger.exception("aggregate failed for candidate %s", state.get("candidate_id"))
        return {"errors": [f"aggregate: {exc}"]}
