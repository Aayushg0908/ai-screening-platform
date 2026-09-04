"""Deterministic score aggregation.

The LLM emits per-dimension integer scores with reasoning; this module does the
weighted arithmetic. Weights come from :mod:`backend.core.config` so they are
tunable and auditable. Nothing here calls an LLM.
"""

from __future__ import annotations

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.models.schemas import (
    FinalScore,
    GitHubEvaluation,
    ResumeEvaluation,
)
from backend.models.tables import Candidate, TestResult

logger = get_logger(__name__)


def compute_pre_test_score(
    candidate: Candidate,
    resume_eval: ResumeEvaluation,
    github_eval: GitHubEvaluation,
) -> FinalScore:
    """Weighted aggregation of resume, GitHub, projects and academics (0-100).

    When ``github_eval.analyzed`` is ``False`` the GitHub weight is redistributed
    across the remaining dimensions rather than counted as zero.
    """
    _ = get_settings()
    raise NotImplementedError


def blend_with_test_result(
    pre: FinalScore, test_result: TestResult | None
) -> FinalScore:
    """Blend the pre-test score with test sub-scores.

    ``test_result`` is ``None`` / ``NO_RESULT`` for candidates missing from the
    Test Result sheet; those keep their pre-test score unblended, flagged in
    :attr:`FinalScore.notes`.
    """
    _ = get_settings()
    raise NotImplementedError


def rank_evaluations(scores: list[FinalScore]) -> list[FinalScore]:
    """Return the scores sorted descending by ``final_score`` (rank order)."""
    raise NotImplementedError
