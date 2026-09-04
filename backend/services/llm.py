"""LLM factory (§4.3).

One place to obtain a chat model. Groq is primary (model id from
``settings.groq_model``, temperature 0); Mistral (``settings.mistral_model``) is
the fallback, used when Groq rate-limits, returns 5xx, or produces output that
fails structured parsing. Model ids are never hardcoded.

``get_llm`` returns an :class:`LLMClient` wrapper rather than a bare LangChain
model so the caller can read which provider actually answered
(``client.provider`` / ``client.model_used``) and put a real value in
``EvaluationOut.model_used``. The wrapper is Runnable-shaped: call ``.invoke``.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)

_PROVIDERS = ("groq", "mistral")


def _status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status extraction from a provider SDK exception."""
    for attr in ("status_code", "code", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _is_transient(exc: BaseException) -> bool:
    """Connection resets / timeouts — worth retrying against the same provider."""
    name = type(exc).__name__.lower()
    return any(
        token in name
        for token in (
            "apiconnection",
            "apitimeout",
            "connecterror",
            "connecttimeout",
            "readtimeout",
            "timeouterror",
        )
    )


def _should_fallback(exc: BaseException) -> bool:
    """True when the primary provider should be abandoned for the fallback.

    Covers rate limits, 5xx, and "the model answered but the answer failed
    structured-output validation" — including Groq's server-side
    ``tool_use_failed`` (HTTP 400) where the tool call did not match the schema.
    """
    if isinstance(exc, ValidationError):
        return True
    status = _status_code(exc)
    if status is not None and (status == 429 or 500 <= status <= 599):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        token in text
        for token in (
            "ratelimit",
            "resourceexhausted",
            "serviceunavailable",
            "internalservererror",
            "outputparser",
            "outputparsing",
            "tool_use_failed",
            "did not match schema",
            "tool call validation failed",
        )
    )


class LLMClient:
    """Groq-primary, Mistral-fallback chat client that records who answered."""

    def __init__(self, structured_output_model: type | None = None) -> None:
        self.structured_output_model = structured_output_model
        self._settings = get_settings()
        self._built: dict[str, Any] = {}
        self.provider: str | None = None
        self.model_used: str | None = None

    # -- model construction --------------------------------------------------
    def _model_id(self, provider: str) -> str:
        return (
            self._settings.groq_model
            if provider == "groq"
            else self._settings.mistral_model
        )

    def _build(self, provider: str) -> Any:
        if provider in self._built:
            return self._built[provider]

        if provider == "groq":
            from langchain_groq import ChatGroq

            model: Any = ChatGroq(
                model=self._settings.groq_model,
                api_key=self._settings.groq_api_key,
                temperature=0,
                max_retries=0,
            )
        else:
            from langchain_mistralai import ChatMistralAI

            model = ChatMistralAI(
                model=self._settings.mistral_model,
                api_key=self._settings.mistral_api_key,
                temperature=0,
                max_retries=0,
            )

        if self.structured_output_model is not None:
            model = model.with_structured_output(self.structured_output_model)

        self._built[provider] = model
        return model

    # -- invocation -------------------------------------------------------------
    @retry(
        retry=retry_if_exception(_is_transient),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, max=10),
        reraise=True,
    )
    def _invoke_once(self, provider: str, messages: Any) -> Any:
        return self._build(provider).invoke(messages)

    def invoke(self, messages: Any) -> Any:
        """Invoke the primary provider, falling back to the secondary on failure.

        Records the provider that answered on ``self.provider`` /
        ``self.model_used``. Re-raises the last exception if every provider fails.
        """
        providers = [
            p
            for p in _PROVIDERS
            if p == "mistral" or self._settings.groq_api_key
        ]
        if not providers:
            providers = ["mistral"]

        last_exc: BaseException | None = None
        for index, provider in enumerate(providers):
            is_last = index == len(providers) - 1
            try:
                result = self._invoke_once(provider, messages)
            except Exception as exc:  # noqa: BLE001 - classified below
                last_exc = exc
                if is_last or not _should_fallback(exc):
                    raise
                logger.warning(
                    "LLM provider %r failed (%s: %s); falling back",
                    provider,
                    type(exc).__name__,
                    exc,
                )
                continue
            self.provider = provider
            self.model_used = self._model_id(provider)
            logger.info("LLM answered via %s (%s)", provider, self.model_used)
            return result

        assert last_exc is not None  # pragma: no cover
        raise last_exc


def get_llm(structured_output_model: type | None = None) -> LLMClient:
    """Return an :class:`LLMClient`.

    If ``structured_output_model`` is given, every ``invoke`` returns a validated
    instance of that Pydantic model (``with_structured_output`` under the hood).
    """
    return LLMClient(structured_output_model)
