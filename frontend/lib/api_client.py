"""The only module that talks HTTP to the FastAPI backend.

No business logic lives here and nothing imports from ``backend``. Every page
goes through :class:`ApiClient`. The base URL is read from the ``API_BASE_URL``
environment variable (default ``http://localhost:8000``).
"""

from __future__ import annotations

import os
from typing import Any

import requests

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 30


class ApiError(RuntimeError):
    """Raised when the backend returns a non-2xx response or is unreachable."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ApiClient:
    """Thin typed wrapper over the backend's HTTP API."""

    def __init__(
        self, base_url: str | None = None, timeout: int = DEFAULT_TIMEOUT
    ) -> None:
        self.base_url = (
            base_url or os.getenv("API_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self.timeout = timeout

    # -- low-level ---------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Issue a request and return parsed JSON, raising :class:`ApiError`."""
        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(
                method,
                url,
                json=json,
                files=files,
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:  # network / DNS / timeout
            raise ApiError(f"Cannot reach backend at {url}: {exc}") from exc

        if not resp.ok:
            raise ApiError(
                f"{method} {path} -> {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
            )
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.text

    # -- health ----------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        """Return ``{"status": ..., "db": bool}`` or raise :class:`ApiError`."""
        return self._request("GET", "/health")

    # -- candidates ----------------------------------------------------------
    def upload_candidates(self, filename: str, content: bytes) -> dict[str, Any]:
        """POST a candidate dataset file."""
        return self._request(
            "POST", "/candidates/upload", files={"file": (filename, content)}
        )

    def list_candidates(self) -> list[dict[str, Any]]:
        """GET every ingested candidate."""
        return self._request("GET", "/candidates")

    def get_candidate(self, candidate_id: int) -> dict[str, Any]:
        """GET one candidate by id."""
        return self._request("GET", f"/candidates/{candidate_id}")

    # -- jobs ----------------------------------------------------------------
    def create_job(
        self, title: str, description: str, required_skills: list[str]
    ) -> dict[str, Any]:
        """POST a new job description."""
        return self._request(
            "POST",
            "/jobs",
            json={
                "title": title,
                "description": description,
                "required_skills": required_skills,
            },
        )

    def list_jobs(self) -> list[dict[str, Any]]:
        """GET every job description."""
        return self._request("GET", "/jobs")

    def get_job(self, job_id: int) -> dict[str, Any]:
        """GET one job description by id."""
        return self._request("GET", f"/jobs/{job_id}")

    # -- evaluation ----------------------------------------------------------
    def start_evaluation(self, job_id: int) -> dict[str, Any]:
        """POST to start a batch run for a job; returns the run row."""
        return self._request("POST", "/evaluate", json={"job_id": job_id})

    def run_status(self, run_id: int) -> dict[str, Any]:
        """GET a run's progress (poll this)."""
        return self._request("GET", f"/evaluate/{run_id}/status")

    def run_results(self, run_id: int) -> list[dict[str, Any]]:
        """GET a run's ranked results."""
        return self._request("GET", f"/evaluate/{run_id}/results")

    # -- outreach ----------------------------------------------------------
    def send_tests(
        self, run_id: int, top_n: int | None = None
    ) -> dict[str, Any]:
        """POST to email the test link to a run's shortlist."""
        return self._request(
            "POST",
            "/outreach/send-tests",
            json={"run_id": run_id, "top_n": top_n},
        )

    # -- results ----------------------------------------------------------
    def upload_results(self, filename: str, content: bytes) -> dict[str, Any]:
        """POST the Test Result sheet."""
        return self._request(
            "POST", "/results/upload", files={"file": (filename, content)}
        )

    # -- interviews ----------------------------------------------------------
    def schedule_interview(
        self,
        candidate_id: int,
        start_iso: str,
        duration_minutes: int = 45,
        job_id: int | None = None,
    ) -> dict[str, Any]:
        """POST to schedule an interview (calendar event + Meet link)."""
        return self._request(
            "POST",
            "/interviews/schedule",
            json={
                "candidate_id": candidate_id,
                "job_id": job_id,
                "start": start_iso,
                "duration_minutes": duration_minutes,
            },
        )

    def list_interviews(self) -> list[dict[str, Any]]:
        """GET every scheduled interview."""
        return self._request("GET", "/interviews")


def get_client() -> ApiClient:
    """Return a default :class:`ApiClient` instance."""
    return ApiClient()
