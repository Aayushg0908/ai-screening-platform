"""FastAPI application entry point.

Registers the active route modules (Phase 1: candidates), enables CORS for the
Streamlit frontend, and initialises the database schema on startup.

Run with::

    uvicorn backend.main:app --reload --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes import candidates

# Phase 3: evaluation, interviews, jobs, outreach, results routers land with
# their schemas (Dimension/ResumeEvaluation/GitHubEvaluation/FinalScore, etc.).
from backend.core.config import get_settings
from backend.core.db import check_db, init_db
from backend.core.logging import configure_logging, get_logger

logger = get_logger(__name__)

#: Origins allowed to call this API (the Streamlit dev server).
ALLOWED_ORIGINS = ["http://localhost:8501"]


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
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(candidates.router)
# Phase 3: app.include_router(jobs.router) etc.


@app.get("/health", tags=["health"])
def health() -> dict[str, object]:
    """Liveness probe: reports API status and database connectivity."""
    return {"status": "ok", "db": check_db()}
