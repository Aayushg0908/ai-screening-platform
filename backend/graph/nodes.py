"""Graph node implementations.

Every node catches its own exceptions and appends a message to
``state["errors"]`` instead of raising — one dead resume link must never kill a
batch run. Each node returns a partial state dict for LangGraph to merge.
"""

from __future__ import annotations

from backend.core.logging import get_logger
from backend.graph.state import CandidateState

logger = get_logger(__name__)


def fetch_resume(state: CandidateState) -> CandidateState:
    """Normalise the Drive URL and download the resume PDF bytes."""
    try:
        raise NotImplementedError
    except Exception as exc:  # noqa: BLE001 - nodes never raise
        logger.exception("fetch_resume failed")
        return {"errors": [f"fetch_resume: {exc}"]}


def extract_text(state: CandidateState) -> CandidateState:
    """Extract plain text from the downloaded PDF into ``resume_text``."""
    try:
        raise NotImplementedError
    except Exception as exc:  # noqa: BLE001 - nodes never raise
        logger.exception("extract_text failed")
        return {"errors": [f"extract_text: {exc}"]}


def evaluate_vs_jd(state: CandidateState) -> CandidateState:
    """Score the resume against the job description via the LLM."""
    try:
        raise NotImplementedError
    except Exception as exc:  # noqa: BLE001 - nodes never raise
        logger.exception("evaluate_vs_jd failed")
        return {"errors": [f"evaluate_vs_jd: {exc}"]}


def analyze_github(state: CandidateState) -> CandidateState:
    """Run repo-level GitHub analysis, degrading gracefully when there is no URL."""
    try:
        raise NotImplementedError
    except Exception as exc:  # noqa: BLE001 - nodes never raise
        logger.exception("analyze_github failed")
        return {"errors": [f"analyze_github: {exc}"]}


def aggregate(state: CandidateState) -> CandidateState:
    """Deterministically combine dimension scores into ``final``."""
    try:
        raise NotImplementedError
    except Exception as exc:  # noqa: BLE001 - nodes never raise
        logger.exception("aggregate failed")
        return {"errors": [f"aggregate: {exc}"]}
