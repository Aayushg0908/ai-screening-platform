"""Database tables.

Phase 1 uses UploadBatch and Candidate. The rest are declared so the schema is
created once; later phases fill them in.

Key constraint: `email` is NOT unique. Every candidate in the sample dataset
shares one address (plus-addressing), so a unique constraint would collapse ten
candidates into one. The join key across uploads is `s_no`, scoped to a batch.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.utcnow()


class UploadBatch(SQLModel, table=True):
    """One candidate-file upload. Scopes s_no, which restarts at 1 per file."""

    __tablename__ = "upload_batch"

    batch_id: int | None = Field(default=None, primary_key=True)
    filename: str
    file_hash: str = Field(index=True, unique=True)  # sha256 -> idempotent upload
    sheet_name: str | None = None
    row_count: int = 0
    created_at: datetime = Field(default_factory=_now)


class GitHubSource(str, Enum):
    COLUMN = "column"  # found in the github column
    TEXT = "text_fallback"  # scraped out of a free-text field
    NONE = "none"


class Candidate(SQLModel, table=True):
    __tablename__ = "candidate"
    __table_args__ = (UniqueConstraint("batch_id", "s_no", name="uq_batch_sno"),)

    candidate_id: int | None = Field(default=None, primary_key=True)
    batch_id: int = Field(foreign_key="upload_batch.batch_id", index=True)
    s_no: int = Field(index=True)  # join key for the test-results upload

    name: str
    email: str = Field(index=True)  # NOT unique - see module docstring
    college: str | None = None
    branch: str | None = None
    cgpa: float | None = None

    best_ai_project: str | None = None
    research_work: str | None = None

    github_url: str | None = None
    github_username: str | None = None
    github_source: GitHubSource = Field(default=GitHubSource.NONE)

    resume_url: str | None = None
    resume_direct_url: str | None = None  # Drive share link -> downloadable

    # Columns we didn't recognise are preserved, not discarded (assignment §2).
    extra_fields: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------
# Declared now so create_all() builds the whole schema. Phases 2-8 use these.
# --------------------------------------------------------------------------


class JobDescription(SQLModel, table=True):
    __tablename__ = "job_description"

    job_id: int | None = Field(default=None, primary_key=True)
    title: str
    description: str
    created_at: datetime = Field(default_factory=_now)


class ResumeText(SQLModel, table=True):
    __tablename__ = "resume_text"

    id: int | None = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="candidate.candidate_id", index=True)
    text: str | None = None
    error: str | None = None
    fetched_at: datetime = Field(default_factory=_now)


class GitHubAnalysis(SQLModel, table=True):
    __tablename__ = "github_analysis"

    id: int | None = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="candidate.candidate_id", index=True)
    username: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error: str | None = None
    analyzed_at: datetime = Field(default_factory=_now)


class GitHubCache(SQLModel, table=True):
    """Raw API responses keyed by username - batches get re-run constantly."""

    __tablename__ = "github_cache"

    username: str = Field(primary_key=True)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    cached_at: datetime = Field(default_factory=_now)


class Evaluation(SQLModel, table=True):
    __tablename__ = "evaluation"

    id: int | None = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="candidate.candidate_id", index=True)
    job_id: int = Field(foreign_key="job_description.job_id", index=True)
    run_id: int | None = Field(
        default=None, foreign_key="pipeline_run.run_id", index=True
    )
    resume_eval: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    github_eval: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    weights: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    model_used: str | None = None
    resume_score: float | None = None  # 0-100, None when resume eval failed
    github_score: float | None = None  # 0-100, None when no/failed GitHub
    github_status: str | None = None  # GitHubStatus value
    status: str | None = None  # scored | partial (...) | unscorable
    pre_test_score: float | None = None  # blended; None when unscorable (Phase 5)
    final_score: float | None = None  # after test results (Phase 7)
    errors: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)


class TestStatus(str, Enum):
    RECEIVED = "received"
    NO_RESULT = "no_result"  # candidate absent from the results upload


class TestResult(SQLModel, table=True):
    __tablename__ = "test_result"

    id: int | None = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="candidate.candidate_id", index=True)
    test_la: float | None = None
    test_code: float | None = None
    status: TestStatus = Field(default=TestStatus.NO_RESULT)
    created_at: datetime = Field(default_factory=_now)


class Interview(SQLModel, table=True):
    __tablename__ = "interview"

    id: int | None = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="candidate.candidate_id", index=True)
    event_id: str | None = None
    meet_link: str | None = None
    starts_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)


class EmailLog(SQLModel, table=True):
    """One row per send attempt. Tracked by candidate_id, NEVER by email address -
    every sample candidate shares one inbox, so email-based dedupe would
    collapse all ten into one "already emailed" record."""

    __tablename__ = "email_log"

    id: int | None = Field(default=None, primary_key=True)
    candidate_id: int = Field(foreign_key="candidate.candidate_id", index=True)
    run_id: int = Field(index=True)  # which evaluation run produced the shortlist
    email_type: str  # "test_invite" (Phase 8 adds "interview_invite")
    recipient: str
    subject: str
    status: str  # "sent" | "failed"
    error: str | None = None
    sent_at: datetime = Field(default_factory=_now)


class PipelineRun(SQLModel, table=True):
    """Progress row the Streamlit frontend polls - it can't hold background state."""

    __tablename__ = "pipeline_run"

    run_id: int | None = Field(default=None, primary_key=True)
    batch_id: int = Field(foreign_key="upload_batch.batch_id", index=True)
    job_id: int | None = None
    status: str = "pending"  # pending -> running -> completed | failed
    mode: str | None = None  # EVALUATION_MODE profile: fast | quality
    processed: int = 0
    total: int = 0
    weights: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    errors: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None