"""Pydantic v2 API schemas. Phases 1-4."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


def _truncate_at_word(value: object, limit: int) -> object:
    """Trim an overlong string back to ``limit`` chars at a word boundary.

    Used by ``field_validator(mode="before")`` on LLM-authored text fields: the
    prompt still asks for brevity, but a model that overshoots by a few
    characters no longer fails the whole candidate. Non-strings pass through
    untouched for normal validation to handle.
    """
    if not isinstance(value, str) or len(value) <= limit:
        return value
    head = value[:limit]
    left, _, _ = head.rpartition(" ")
    return (left or head).rstrip()


def _first_n(value: object, n: int) -> object:
    """Keep only the first ``n`` items of a list; pass non-lists through."""
    if value is None:
        return []
    if isinstance(value, list) and len(value) > n:
        return value[:n]
    return value


class IngestReport(BaseModel):
    """What POST /candidates/upload returns.

    Deliberately not {"status": "ok"} - the recruiter needs to see which columns
    were recognised and what was skipped, and it doubles as the debugging surface
    for later phases.
    """

    batch_id: int
    filename: str
    sheet: str | None = None
    available_sheets: list[str] = Field(default_factory=list)
    total_rows: int
    inserted: int
    column_mapping: dict[str, str] = Field(default_factory=dict)
    unmapped_columns: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CandidateOut(BaseModel):
    candidate_id: int
    batch_id: int
    s_no: int
    name: str
    email: str
    college: str | None = None
    branch: str | None = None
    cgpa: float | None = None
    best_ai_project: str | None = None
    research_work: str | None = None
    github_url: str | None = None
    github_username: str | None = None
    github_source: str
    resume_url: str | None = None
    resume_direct_url: str | None = None

    model_config = {"from_attributes": True}


class BatchOut(BaseModel):
    batch_id: int
    filename: str
    sheet_name: str | None = None
    row_count: int

    model_config = {"from_attributes": True}


class ResumeItem(BaseModel):
    """Per-candidate outcome of resume download + text extraction (§4.2)."""

    candidate_id: int
    s_no: int
    name: str
    status: str  # "ok" | "empty" | "failed" | "skipped"
    chars: int = 0
    error: str | None = None


class ResumeReport(BaseModel):
    """What POST /candidates/batches/{batch_id}/resumes returns.

    ``failed`` also counts ``"empty"`` items (extracted text under 100 chars),
    so ``succeeded + failed + skipped == total``.
    """

    batch_id: int
    total: int
    succeeded: int
    failed: int
    skipped: int
    items: list[ResumeItem]


# ---------------------------------------------------------------------------
# Phase 3 — AI evaluation of a resume against a job description (§4.3)
# ---------------------------------------------------------------------------


class JobDescriptionIn(BaseModel):
    """Recruiter-supplied job description."""

    title: str
    description: str


class JobDescriptionOut(BaseModel):
    job_id: int
    title: str
    description: str
    created_at: datetime

    model_config = {"from_attributes": True}


class Dimension(BaseModel):
    """One scored evaluation axis. LLM-authored; never carries arithmetic."""

    score: int = Field(ge=0, le=10)  # correctness constraint — NOT truncated
    reasoning: str = Field(
        description="1-2 sentences, at most ~300 characters. Overruns are "
        "truncated at a word boundary rather than rejected."
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="Direct quotes from the source material; at most 3. Extra "
        "items are dropped, not rejected. Empty only when the dimension is "
        "genuinely absent.",
    )

    @field_validator("reasoning", mode="before")
    @classmethod
    def _cap_reasoning(cls, v: object) -> object:
        return _truncate_at_word(v, 300)

    @field_validator("evidence", mode="before")
    @classmethod
    def _cap_evidence(cls, v: object) -> object:
        return _first_n(v, 3)


class ResumeEvaluation(BaseModel):
    """Structured LLM output for one candidate vs one job description."""

    skills_match: Dimension
    project_depth: Dimension
    experience_relevance: Dimension
    research: Dimension
    summary: str = Field(
        description="2-3 sentences, recruiter-facing, at most ~400 characters. "
        "Overruns are truncated at a word boundary rather than rejected."
    )
    missing_requirements: list[str] = Field(default_factory=list)
    # JD requirements with no supporting evidence in the resume

    @field_validator("summary", mode="before")
    @classmethod
    def _cap_summary(cls, v: object) -> object:
        return _truncate_at_word(v, 400)


class ScoreWeights(BaseModel):
    """Per-dimension weights for the deterministic resume score.

    Normalised so the four sum to exactly 1.0; negative weights are rejected.
    Persisted alongside every score so the score stays reproducible.
    """

    skills_match: float = 0.30
    project_depth: float = 0.30
    experience_relevance: float = 0.25
    research: float = 0.15

    @model_validator(mode="after")
    def _normalise(self) -> "ScoreWeights":
        values = (
            self.skills_match,
            self.project_depth,
            self.experience_relevance,
            self.research,
        )
        if any(v < 0 for v in values):
            raise ValueError("score weights must be non-negative")
        total = sum(values)
        if total <= 0:
            raise ValueError("score weights must sum to a positive value")
        if abs(total - 1.0) > 1e-6:
            self.skills_match /= total
            self.project_depth /= total
            self.experience_relevance /= total
            self.research /= total
        return self


class EvaluationOut(BaseModel):
    """Response for ``POST /evaluate/candidate/{candidate_id}``.

    ``evaluation`` / ``model_used`` are ``None`` when ``error`` is set (no resume
    text, or the LLM failed after retries and fallback).
    """

    candidate_id: int
    s_no: int
    name: str
    job_id: int
    evaluation: ResumeEvaluation | None = None
    weights: ScoreWeights
    resume_score: float  # 0-100
    model_used: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Phase 4 — repository-level GitHub analysis (§4.4, hard constraint §5)
# ---------------------------------------------------------------------------


class GitHubStats(BaseModel):
    """Deterministic signals, computed in Python. No LLM."""

    original_repos: int  # non-fork
    forked_repos: int
    total_stars: int  # summed over original repos only
    languages: list[str]
    days_since_last_push: int | None
    repos_with_readme: int  # among the repos read in depth
    analyzed_repos: list[str]  # the repos actually read in depth


class GitHubEvaluation(BaseModel):
    """Structured LLM output — a qualitative, repository-level assessment."""

    repository_quality: Dimension
    technical_relevance: Dimension
    activity_consistency: Dimension
    engineering_practice: Dimension
    summary: str = Field(
        description="2-3 sentences, at most ~400 characters. Overruns are "
        "truncated at a word boundary rather than rejected."
    )

    @field_validator("summary", mode="before")
    @classmethod
    def _cap_summary(cls, v: object) -> object:
        return _truncate_at_word(v, 400)


class GitHubStatus(str, Enum):
    OK = "ok"
    NO_PROFILE = "no_profile"  # candidate never provided a GitHub URL
    NOT_FOUND = "not_found"  # username 404s
    EMPTY = "empty"  # valid account, zero public original repos
    ERROR = "error"


class GitHubEvaluationOut(BaseModel):
    """Response for ``POST /evaluate/github/{candidate_id}``.

    ``github_score`` is ``None`` unless ``status`` is ``OK`` — a missing or
    empty profile is deliberately not scored 0 (see scoring.combine_pretest_score).
    """

    candidate_id: int
    s_no: int
    name: str
    username: str | None
    status: GitHubStatus
    stats: GitHubStats | None = None
    evaluation: GitHubEvaluation | None = None
    github_score: float | None = None  # 0-100, None when status != OK
    model_used: str | None = None
    error: str | None = None