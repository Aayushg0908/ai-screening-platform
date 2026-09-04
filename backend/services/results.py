"""Test-result ingestion and merge.

Parses the uploaded ``Test Result`` sheet and LEFT-joins it to candidates on
``s_no``. Candidates absent from the sheet (e.g. s_no 4 and 10 in the sample)
are recorded with ``TestResultState.NO_RESULT`` — never dropped, never zeroed.
"""

from __future__ import annotations

from sqlmodel import Session

from backend.core.logging import get_logger
from backend.models.schemas import UploadSummary

logger = get_logger(__name__)


def parse_results_file(content: bytes, filename: str) -> list[dict]:
    """Parse raw upload bytes into a list of cleaned test-result dicts."""
    raise NotImplementedError


def ingest_results(
    content: bytes, filename: str, session: Session
) -> UploadSummary:
    """Parse, LEFT-join on ``s_no`` and persist test results."""
    raise NotImplementedError
