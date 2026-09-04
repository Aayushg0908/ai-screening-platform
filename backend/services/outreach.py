"""Outreach orchestration: resolve a run's shortlist and mail the test link."""

from __future__ import annotations

from sqlmodel import Session

from backend.core.logging import get_logger
from backend.models.schemas import SendTestsResult

logger = get_logger(__name__)


def send_tests(
    run_id: int, top_n: int | None, session: Session
) -> SendTestsResult:
    """Email ``settings.test_link_url`` to the run's top-N shortlisted candidates."""
    raise NotImplementedError
