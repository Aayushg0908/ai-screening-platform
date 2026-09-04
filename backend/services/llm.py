"""LLM factory.

One place to obtain a chat model. Groq is primary (model id from
``settings.groq_model``); Gemini is the fallback used when Groq returns a
rate-limit error (model id from ``settings.gemini_model``). Model ids are never
hardcoded. Callers should use ``get_llm().with_structured_output(Model)`` and
never parse free text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from backend.core.config import get_settings
from backend.core.logging import get_logger

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

logger = get_logger(__name__)


class LLMRateLimitError(RuntimeError):
    """Raised when the primary provider signals a rate limit / quota exhaustion."""


def _build_groq() -> "BaseChatModel":
    """Instantiate the primary Groq chat model from config."""
    raise NotImplementedError


def _build_gemini() -> "BaseChatModel":
    """Instantiate the Gemini fallback chat model from config."""
    raise NotImplementedError


@retry(
    retry=retry_if_exception_type(ConnectionError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, max=15),
)
def get_llm(*, prefer_fallback: bool = False) -> "BaseChatModel":
    """Return a chat model, falling back Groq -> Gemini on rate limits.

    Args:
        prefer_fallback: Skip Groq and return Gemini directly (used after the
            primary has already rate-limited within a batch).
    """
    _ = get_settings()
    raise NotImplementedError
