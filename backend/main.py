"""FastAPI application entry point.

Registers the active route modules (candidates, jobs, evaluate, outreach),
enables CORS for the Streamlit frontend, and initialises the database schema on
startup.

Run locally with::

    uvicorn backend.main:app --reload --port 8000

Render (or any host injecting $PORT) needs the process bound to 0.0.0.0, not
the 127.0.0.1 uvicorn defaults to - the platform can't reach the port
otherwise and marks the service unhealthy::

    uvicorn backend.main:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes import candidates, evaluation, interviews, jobs, outreach, results
from backend.core.config import get_settings
from backend.core.db import check_db, init_db
from backend.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

_settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Configure logging and create database tables on startup."""
    configure_logging()
    settings = get_settings()
    logger.info("Starting API (environment=%s)", settings.environment)
    init_db()
    yield


app = FastAPI(title="AI Screening Platform", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # Origins allowed to call this API - comma-separated in CORS_ORIGINS, e.g.
    # "https://my-frontend.onrender.com,http://localhost:8501" so local dev
    # keeps working after deploy. Defaults to the Streamlit dev server alone.
    allow_origins=_settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(candidates.router)
app.include_router(jobs.router)
app.include_router(evaluation.router)
app.include_router(outreach.router)
app.include_router(results.router)
app.include_router(interviews.router)


@app.get("/health", tags=["health"])
def health() -> dict[str, object]:
    """Liveness probe: reports API status and database connectivity."""
    return {"status": "ok", "db": check_db()}
