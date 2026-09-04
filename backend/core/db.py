"""Database engine and session management.

Postgres only (Neon free tier). The engine uses ``pool_pre_ping=True`` and
``pool_recycle=300`` because Neon scales an idle project to zero and the first
query afterwards would otherwise fail on a stale connection.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine
from tenacity import retry, stop_after_attempt, wait_exponential

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=300,
)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
def init_db() -> None:
    """Create any missing tables. Retries transient Neon cold-start failures.

    Importing :mod:`backend.models.tables` for its side effect registers every
    table on :class:`SQLModel.metadata` before ``create_all`` runs.
    """
    from backend.models import tables  # noqa: F401  (populate SQLModel.metadata)

    logger.info("Initialising database schema")
    SQLModel.metadata.create_all(engine)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
def check_db() -> bool:
    """Return ``True`` if a trivial ``SELECT 1`` succeeds, else ``False``."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - health probe must never raise
        logger.exception("Database health check failed")
        return False


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped :class:`~sqlmodel.Session`."""
    with Session(engine) as session:
        yield session
