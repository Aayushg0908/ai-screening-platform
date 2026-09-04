"""Outbound email via Gmail SMTP over STARTTLS.

Sends the assessment link (``settings.test_link_url``) to shortlisted
candidates. All sample candidates share one address — outreach is keyed on
``candidate_id``, and a single send failure must not abort the batch.
"""

from __future__ import annotations

from dataclasses import dataclass

from tenacity import retry, stop_after_attempt, wait_exponential

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class MailResult:
    """Outcome of a single send attempt."""

    candidate_id: int
    to_address: str
    ok: bool
    error: str | None = None


def build_test_invite(candidate_name: str, test_link: str) -> tuple[str, str]:
    """Return the ``(subject, html_body)`` for a test invitation email."""
    raise NotImplementedError


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30))
def send_email(to_address: str, subject: str, html_body: str) -> None:
    """Send one email through Gmail SMTP (STARTTLS, app password from config)."""
    _ = get_settings()
    raise NotImplementedError


def send_test_invites(
    recipients: list[tuple[int, str, str]]
) -> list[MailResult]:
    """Send the test link to each ``(candidate_id, name, email)``; never raises."""
    raise NotImplementedError
