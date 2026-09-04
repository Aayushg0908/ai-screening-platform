"""Pydantic v2 API schemas. Phases 1-4."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

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


# ---------------------------------------------------------------------------
# Phase 5 — batch evaluation, scoring and ranking (§4.5)
# ---------------------------------------------------------------------------


class BatchRequest(BaseModel):
    """Body for ``POST /evaluate/batch``."""

    batch_id: int
    job_id: int
    weights: ScoreWeights | None = None  # resume dimension weights
    force: bool = False  # bypass the GitHub API cache
    mode: str | None = None  # "fast" | "quality"; None => EVALUATION_MODE env


class RunStatusOut(BaseModel):
    """Polling view of a :class:`~backend.models.tables.PipelineRun`."""

    run_id: int
    status: str  # pending | running | completed | failed
    mode: str | None = None
    processed: int
    total: int
    errors: list[str] = Field(default_factory=list)


class RunResultItem(BaseModel):
    """One candidate in a run's results.

    ``rank`` is ``None`` for unscorable candidates (both resume and GitHub
    evaluation failed) — they are returned in ``RunResultsOut.unranked``.
    ``status`` is one of: ``scored`` / ``partial (resume failed)`` /
    ``partial (github failed)`` / ``unscorable``.
    """

    rank: int | None = None
    candidate_id: int
    s_no: int
    name: str
    status: str | None = None
    model_used: str | None = None  # provider that produced the resume score
    resume_score: float | None = None
    github_score: float | None = None
    pre_test_score: float | None = None
    github_status: str | None = None
    resume_eval: dict | None = None
    github_eval: dict | None = None
    errors: list[str] = Field(default_factory=list)


class RunResultsOut(BaseModel):
    """A run's results, split into ranked and unscorable candidates."""

    run_id: int
    mode: str | None = None  # which EVALUATION_MODE profile ran this batch
    ranked: list[RunResultItem]
    unranked: list[RunResultItem]  # pre_test_score None; not placed in the order


class RerankWeights(BaseModel):
    """Body for ``POST /evaluate/runs/{run_id}/rerank`` — recruiter weight tuning.

    ``resume`` / ``github`` set the pre-test blend (renormalised internally);
    ``dimensions`` optionally retunes the four resume sub-dimension weights.
    Applied by recomputing from stored dimension scores — no LLM / GitHub calls.
    """

    resume: float = 0.60
    github: float = 0.40
    dimensions: ScoreWeights | None = None

    @model_validator(mode="after")
    def _check(self) -> "RerankWeights":
        if self.resume < 0 or self.github < 0:
            raise ValueError("blend weights must be non-negative")
        if self.resume + self.github <= 0:
            raise ValueError("blend weights must sum to a positive value")
        return self


# ---------------------------------------------------------------------------
# Phase 6 — shortlisting and test-invite emails (§4.6)
# ---------------------------------------------------------------------------


class ShortlistRequest(BaseModel):
    """Body for ``POST /outreach/preview`` and ``POST /outreach/send``.

    Reads a run's STORED ranked results only - never re-runs evaluation.
    """

    run_id: int
    mode: Literal["top_n", "threshold"] = "top_n"
    top_n: int = 5
    threshold: float = 60.0


class ShortlistItem(BaseModel):
    candidate_id: int
    s_no: int
    name: str
    email: str
    pre_test_score: float
    rank: int
    already_emailed: bool


class ShortlistPreview(BaseModel):
    """Response for ``POST /outreach/preview``. Sends nothing."""

    run_id: int
    mode: str
    criterion: str  # human readable, e.g. "top 5" / "score >= 60"
    shortlisted: list[ShortlistItem]
    excluded_count: int


class SendResult(BaseModel):
    candidate_id: int
    name: str
    recipient: str
    status: str  # "sent" | "failed" | "skipped"
    error: str | None = None


class SendReport(BaseModel):
    """Response for ``POST /outreach/send``."""

    run_id: int
    attempted: int
    sent: int
    failed: int
    skipped: int  # already emailed, force not set
    results: list[SendResult]
    bcc: str | None = None
    # Which address received a Bcc copy of every message in this send, so a
    # copy is visibly taken rather than silently hidden. None when EMAIL_BCC
    # is unset.


class EmailLogOut(BaseModel):
    id: int
    candidate_id: int
    run_id: int
    email_type: str
    recipient: str
    subject: str
    status: str
    error: str | None = None
    sent_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Phase 7 — test results ingest and final (post-test) scoring (§4.7)
# ---------------------------------------------------------------------------


class ResultsReport(BaseModel):
    """What POST /results/upload returns.

    A test-results upload is authoritative and complete for its batch: every
    candidate in the batch gets exactly one outcome - matched (RECEIVED) or
    no_result (absent from the file, e.g. s_no 4 and 10) - and a row present
    in the file but absent from the batch (e.g. s_no 99) is skipped rather
    than creating an orphan.
    """

    batch_id: int
    filename: str
    sheet: str | None = None
    available_sheets: list[str] = Field(default_factory=list)
    matched: int
    no_result: int
    skipped_unknown: int
    column_mapping: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class TestResultOut(BaseModel):
    id: int
    candidate_id: int
    s_no: int
    name: str
    test_la: float | None
    test_code: float | None
    status: str
    created_at: datetime


class FinalWeights(BaseModel):
    """Recruiter-adjustable blend of the pre-test score with the two test
    components. Same pattern as :class:`ScoreWeights` - overridable per
    request, persisted alongside the score it produced so it stays
    reproducible."""

    pre_test: float = 0.60
    test_la: float = 0.20
    test_code: float = 0.20

    @model_validator(mode="after")
    def _normalise(self) -> "FinalWeights":
        values = (self.pre_test, self.test_la, self.test_code)
        if any(v < 0 for v in values):
            raise ValueError("final weights must be non-negative")
        total = sum(values)
        if total <= 0:
            raise ValueError("final weights must sum to a positive value")
        if abs(total - 1.0) > 1e-6:
            self.pre_test /= total
            self.test_la /= total
            self.test_code /= total
        return self


class FinalShortlistRequest(BaseModel):
    """Body for ``POST /results/shortlist``.

    Recomputes final scores from STORED pre_test_score + TestResult rows -
    zero LLM calls, zero GitHub calls. The recruiter's weight-tuning path for
    the final list, exactly like ``POST /evaluate/runs/{run_id}/rerank`` is
    for the pre-test blend.
    """

    run_id: int
    weights: FinalWeights | None = None
    mode: Literal["top_n", "threshold"] = "top_n"
    top_n: int = 5
    threshold: float = 60.0


class FinalResultItem(BaseModel):
    """One candidate in the final (post-test) results.

    ``rank`` is ``None`` for ``awaiting_result`` and ``unscorable`` candidates
    - they are returned separately, never interleaved into the ranked order.
    ``note`` explains a weight redistribution when exactly one test score is
    missing.
    """

    rank: int | None = None
    candidate_id: int
    s_no: int
    name: str
    pre_test_score: float | None = None
    test_la: float | None = None
    test_code: float | None = None
    final_score: float | None = None
    status: str  # "scored" | "awaiting_result" | "unscorable"
    note: str | None = None


class FinalResultsOut(BaseModel):
    """Response for ``POST /results/shortlist``."""

    run_id: int
    weights: FinalWeights
    criterion: str  # human readable, e.g. "top 5" / "score >= 60"
    ranked: list[FinalResultItem]  # every scored candidate, desc by final_score
    shortlisted: list[FinalResultItem]  # the mode/top_n|threshold subset of ranked
    awaiting_result: list[FinalResultItem]
    unscorable: list[FinalResultItem]