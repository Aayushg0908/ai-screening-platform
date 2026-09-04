"""Pydantic v2 API schemas. Phase 1 subset."""

from __future__ import annotations

from pydantic import BaseModel, Field


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