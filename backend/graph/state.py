"""Shared state for the candidate evaluation graph.

``evaluate_vs_jd`` and ``analyze_github`` run in parallel and both append to
``errors``; the ``operator.add`` reducer merges their writes instead of raising
a concurrent-update error.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from backend.models.schemas import FinalScore, GitHubEvaluation, ResumeEvaluation
from backend.models.tables import Candidate, JobDescription


class CandidateState(TypedDict, total=False):
    """State threaded through the per-candidate pipeline."""

    candidate: Candidate
    jd: JobDescription
    resume_text: str
    resume_eval: ResumeEvaluation
    github_eval: GitHubEvaluation
    final: FinalScore
    errors: Annotated[list[str], operator.add]
