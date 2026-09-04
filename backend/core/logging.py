"""Stdlib logging configuration.

Call :func:`configure_logging` once at application startup. Every module should
obtain its logger with ``logging.getLogger(__name__)`` and never use ``print``.
"""

from __future__ import annotations

import logging

from backend.core.config import get_settings

_CONFIGURED = False

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"


def configure_logging() -> None:
    """Configure the root logger from ``LOG_LEVEL``; idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format=_LOG_FORMAT)

    # Third-party libraries are noisy at DEBUG; keep them one notch quieter.
    for noisy in ("httpx", "httpcore", "urllib3", "googleapiclient.discovery_cache"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger, configuring logging on first use."""
    configure_logging()
    return logging.getLogger(name)
