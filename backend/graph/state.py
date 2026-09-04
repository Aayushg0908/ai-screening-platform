"""Shared state for the per-candidate evaluation graph (§4.5).

``evaluate_vs_jd`` and ``analyze_github`` fan out from ``load_resume`` and run
concurrently; both may append to ``errors``, so it carries an ``operator.add``
reducer that merges the two branches' writes instead of raising a concurrent
update error. Everything else is scalar and written by exactly one node.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class CandidateState(TypedDict, total=False):
    """State threaded through one candidate's evaluation."""

    candidate_id: int
    job_id: int
    resume_text: str | None
    resume_eval: dict | None  # ResumeEvaluation, dumped
    github_eval: dict | None  # GitHubEvaluationOut, dumped
    resume_score: float | None
    github_score: float | None
    pre_test_score: float | None
    errors: Annotated[list[str], operator.add]
