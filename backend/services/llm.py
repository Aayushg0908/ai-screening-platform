"""LLM factory (§4.3) — a round-robin pool of Groq API keys.

Groq's rate limits (8000 TPM) are org-level, so N API keys from N separate
accounts give N independent buckets on the **same** ``gpt-oss-120b`` model.
Each ``invoke`` round-robins its starting key across the pool; on a 429 it moves
straight to the next key (no sleep) and puts the throttled key on a ~60s
cooldown shared process-wide. Mistral (a smaller model) is the last resort,
reached only when **every** Groq key is cooling down.

ARCHITECTURE NOTE — the provider abstraction supports horizontal scaling across
API keys: adding inference capacity is a config change (another key in
``GROQ_API_KEYS``) with no pipeline modification.

``get_llm`` returns an :class:`LLMClient` (Runnable-shaped: call ``.invoke``).
After a call, ``client.provider`` is e.g. ``"groq#1"`` / ``"mistral"`` and
``client.model_used`` is ``"<provider>:<model id>"`` so results show which
endpoint answered.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from pydantic import ValidationError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)

_FALLBACK = "mistral"
_DEFAULT_COOLDOWN_SECONDS = 60.0
_DISABLED_SECONDS = 86_400.0  # 402 / auth / unknown-model — out for the session

# Process-wide, shared by every LLMClient so a batch's many clients coordinate.
_state_lock = threading.Lock()
_cooldown_until: dict[str, float] = {}
_round_robin = {"n": 0}
#: How long a key is benched after a 429. run_batch sets this per profile.
_cooldown_seconds = {"value": _DEFAULT_COOLDOWN_SECONDS}


def set_cooldown_seconds(seconds: float) -> None:
    """Set the per-key 429 cooldown (called by run_batch from the active
    EVALUATION_MODE profile)."""
    with _state_lock:
        _cooldown_seconds["value"] = max(1.0, float(seconds))


def groq_pool_free_in() -> float:
    """Seconds until at least one Groq key is off cooldown. 0.0 if a key is free
    right now (or there are no Groq keys). Used by the pre-emptive-wait guard."""
    keys = get_settings().groq_key_pool
    if not keys:
        return 0.0
    now = time.monotonic()
    waits: list[float] = []
    for i in range(len(keys)):
        until = _cooldown_until.get(f"groq#{i}", 0.0)
        if until <= now:
            return 0.0
        waits.append(until - now)
    return min(waits)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "code", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _is_transient(exc: BaseException) -> bool:
    """Connection resets / timeouts — worth one quick retry on the same key."""
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


def _is_rate_limit(exc: BaseException) -> bool:
    if _status_code(exc) == 429:
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        token in text
        for token in (
            "ratelimit",
            "rate limit",
            "rate_limit",
            "429",
            "too many requests",
            "resourceexhausted",
        )
    )


def _is_provider_disabled(exc: BaseException) -> bool:
    """401/402/403/404 — the key can't use this endpoint at all. Won't fix
    itself; take it out of rotation for the rest of the process."""
    if _status_code(exc) in (401, 402, 403, 404):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        token in text
        for token in (
            "payment required",
            "payment_required",
            "invalid api key",
            "do not have access",
        )
    )


def _should_move_on(exc: BaseException) -> bool:
    """The current key/provider can't answer — try the next one."""
    if isinstance(exc, ValidationError):
        return True
    if _is_rate_limit(exc) or _is_provider_disabled(exc):
        return True
    status = _status_code(exc)
    if status is not None and 500 <= status <= 599:
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        token in text
        for token in (
            "serviceunavailable",
            "internalservererror",
            "outputparser",
            "outputparsing",
            "tool_use_failed",
            "did not match schema",
            "tool call validation failed",
        )
    )


# ---------------------------------------------------------------------------
# Cooldown + rotation
# ---------------------------------------------------------------------------


def _cooling(provider: str) -> bool:
    return _cooldown_until.get(provider, 0.0) > time.monotonic()


def _start_cooldown(provider: str, seconds: float | None = None) -> None:
    with _state_lock:
        _cooldown_until[provider] = time.monotonic() + (
            seconds if seconds is not None else _cooldown_seconds["value"]
        )


def _next_rotation(size: int) -> int:
    if size <= 0:
        return 0
    with _state_lock:
        start = _round_robin["n"] % size
        _round_robin["n"] += 1
    return start


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LLMClient:
    """Round-robin Groq key pool (all ``gpt-oss-120b``) with a Mistral last
    resort. Records which key answered."""

    def __init__(self, structured_output_model: type | None = None) -> None:
        self.structured_output_model = structured_output_model
        self._settings = get_settings()
        self._groq_keys = self._settings.groq_key_pool
        self._built: dict[str, Any] = {}
        self.provider: str | None = None
        self.model_used: str | None = None

    # -- model construction --------------------------------------------------
    def _model_id(self, provider: str) -> str:
        if provider == _FALLBACK:
            return self._settings.mistral_model
        return self._settings.groq_model

    def _build(self, provider: str) -> Any:
        if provider in self._built:
            return self._built[provider]

        if provider.startswith("groq#"):
            from langchain_groq import ChatGroq

            idx = int(provider.split("#", 1)[1])
            model: Any = ChatGroq(
                model=self._settings.groq_model,
                api_key=self._groq_keys[idx],
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

    # -- ordering ------------------------------------------------------------
    def _provider_order(self) -> list[str]:
        """Round-robined Groq keys (hot first), then Mistral, then any cooling
        Groq keys as an absolute last resort."""
        pool = [f"groq#{i}" for i in range(len(self._groq_keys))]
        if pool:
            start = _next_rotation(len(pool))
            pool = pool[start:] + pool[:start]

        hot = [p for p in pool if not _cooling(p)]
        cold = [p for p in pool if _cooling(p)]

        order = list(hot)
        if self._settings.mistral_api_key:
            order.append(_FALLBACK)  # only ever reached after every hot Groq key
        order += cold
        return order

    # -- invocation -------------------------------------------------------------
    @retry(
        retry=retry_if_exception(_is_transient),
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=0.5, max=4),
        reraise=True,
    )
    def _invoke_once(self, provider: str, messages: Any) -> Any:
        return self._build(provider).invoke(messages)

    def invoke(self, messages: Any) -> Any:
        order = self._provider_order()
        if not order:
            raise RuntimeError(
                "no LLM providers configured — set GROQ_API_KEYS / GROQ_API_KEY "
                "or MISTRAL_API_KEY"
            )

        last_exc: BaseException | None = None
        for index, provider in enumerate(order):
            is_last = index == len(order) - 1
            try:
                result = self._invoke_once(provider, messages)
            except Exception as exc:  # noqa: BLE001 - classified below
                last_exc = exc
                if _is_provider_disabled(exc):
                    _start_cooldown(provider, _DISABLED_SECONDS)
                    logger.warning(
                        "%s unusable (%s) — removed from rotation",
                        provider,
                        str(exc)[:140],
                    )
                elif _is_rate_limit(exc):
                    _start_cooldown(provider)
                    logger.warning(
                        "%s rate-limited; cooling %ds, trying next",
                        provider,
                        int(_cooldown_seconds["value"]),
                    )
                elif _should_move_on(exc):
                    logger.warning(
                        "%s failed (%s: %s); trying next",
                        provider,
                        type(exc).__name__,
                        str(exc)[:120],
                    )
                elif is_last:
                    raise
                else:
                    logger.warning(
                        "%s errored (%s); trying next", provider, type(exc).__name__
                    )
                continue

            self.provider = provider
            self.model_used = f"{provider}:{self._model_id(provider)}"
            if provider == _FALLBACK:
                logger.info("LLM answered via Mistral fallback (%s)", self._model_id(provider))
            else:
                logger.info(
                    "LLM answered via Groq key #%s (%s)",
                    provider.split("#", 1)[1],
                    self._model_id(provider),
                )
            return result

        assert last_exc is not None  # pragma: no cover
        raise last_exc


def get_llm(structured_output_model: type | None = None) -> LLMClient:
    """Return an :class:`LLMClient`. With ``structured_output_model`` every
    ``invoke`` returns a validated instance of that Pydantic model."""
    return LLMClient(structured_output_model)
