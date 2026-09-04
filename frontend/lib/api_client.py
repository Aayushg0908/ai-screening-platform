"""The only module that talks HTTP to the FastAPI backend.

No business logic lives here and nothing imports from ``backend`` - every
page goes through :class:`ApiClient`. The base URL is read from the
``API_BASE_URL`` environment variable (default ``http://localhost:8000``).
This module never imports Streamlit; caching, spinners, and error display are
a page concern.
"""

from __future__ import annotations

import os
from typing import Any

import requests

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 30


class ApiError(RuntimeError):
    """Raised when the backend returns a non-2xx response or is unreachable.
    Pages catch this and show ``str(exc)`` - never a raw traceback."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ApiClient:
    """Thin typed wrapper over the backend's HTTP API - one method per
    endpoint, no business logic."""

    def __init__(
        self, base_url: str | None = None, timeout: int = DEFAULT_TIMEOUT
    ) -> None:
        self.base_url = (
            base_url or os.getenv("API_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self.timeout = timeout

    # -- low-level -----------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Issue a request and return parsed JSON, raising :class:`ApiError`
        with a readable message on any failure - network, timeout, or a
        non-2xx response."""
        url = f"{self.base_url}{path}"
        # Query params with a None value (e.g. an unset optional filter)
        # should be omitted entirely, not sent as the literal string "None".
        clean_params = (
            {k: v for k, v in params.items() if v is not None} if params else None
        )
        try:
            resp = requests.request(
                method,
                url,
                json=json,
                files=files,
                params=clean_params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:  # network / DNS / timeout
            raise ApiError(f"Cannot reach backend at {url}: {exc}") from exc

        if not resp.ok:
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except ValueError:
                pass
            raise ApiError(
                f"{method} {path} -> {resp.status_code}: {detail}"[:500],
                status_code=resp.status_code,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.text

    # -- health ----------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    # -- candidates (§4.1) -----------------------------------------------------
    def upload_candidates(
        self, filename: str, content: bytes, sheet_name: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/candidates/upload",
            files={"file": (filename, content)},
            params={"sheet_name": sheet_name},
        )

    def list_candidates(self, batch_id: int | None = None) -> list[dict[str, Any]]:
        return self._request("GET", "/candidates", params={"batch_id": batch_id})

    def list_batches(self) -> list[dict[str, Any]]:
        return self._request("GET", "/candidates/batches")

    def process_resumes(self, batch_id: int, force: bool = False) -> dict[str, Any]:
        """Download + extract resume text for every candidate in the batch
        (§4.2). Required before evaluation reads real resume content rather
        than falling back to dataset fields alone."""
        return self._request(
            "POST",
            f"/candidates/batches/{batch_id}/resumes",
            params={"force": force},
        )

    def get_candidate_resume(self, candidate_id: int) -> dict[str, Any] | None:
        """Resume text status for one candidate. ``None`` if resume
        processing has never been run (404) - since a batch is always
        processed as a whole, checking one candidate is a cheap proxy for
        "has this batch been processed at all"."""
        try:
            return self._request("GET", f"/candidates/{candidate_id}/resume")
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise

    # -- jobs (§4.3) -------------------------------------------------------------
    def create_job(self, title: str, description: str) -> dict[str, Any]:
        return self._request(
            "POST", "/jobs", json={"title": title, "description": description}
        )

    def list_jobs(self) -> list[dict[str, Any]]:
        return self._request("GET", "/jobs")

    # -- evaluation (§4.3-4.5) -----------------------------------------------
    def start_batch_evaluation(
        self,
        batch_id: int,
        job_id: int,
        weights: dict[str, float] | None = None,
        force: bool = False,
        mode: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/evaluate/batch",
            json={
                "batch_id": batch_id,
                "job_id": job_id,
                "weights": weights,
                "force": force,
                "mode": mode,
            },
        )

    def run_status(self, run_id: int) -> dict[str, Any]:
        """Poll this - never cache it."""
        return self._request("GET", f"/evaluate/runs/{run_id}")

    def run_results(self, run_id: int) -> dict[str, Any]:
        return self._request("GET", f"/evaluate/runs/{run_id}/results")

    def rerank(
        self,
        run_id: int,
        resume: float = 0.60,
        github: float = 0.40,
        dimensions: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Instant, inference-free: recomputes from stored dimension scores."""
        return self._request(
            "POST",
            f"/evaluate/runs/{run_id}/rerank",
            json={"resume": resume, "github": github, "dimensions": dimensions},
        )

    # -- outreach (§4.6) -----------------------------------------------------
    def preview_shortlist(
        self,
        run_id: int,
        mode: str = "top_n",
        top_n: int = 5,
        threshold: float = 60.0,
    ) -> dict[str, Any]:
        """Never sends anything."""
        return self._request(
            "POST",
            "/outreach/preview",
            json={"run_id": run_id, "mode": mode, "top_n": top_n, "threshold": threshold},
        )

    def send_invitations(
        self,
        run_id: int,
        mode: str = "top_n",
        top_n: int = 5,
        threshold: float = 60.0,
        force: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/outreach/send",
            json={"run_id": run_id, "mode": mode, "top_n": top_n, "threshold": threshold},
            params={"force": force},
        )

    def email_log(self, run_id: int) -> list[dict[str, Any]]:
        return self._request("GET", "/outreach/log", params={"run_id": run_id})

    # -- test results (§4.7) --------------------------------------------------
    def upload_results(
        self,
        filename: str,
        content: bytes,
        batch_id: int,
        sheet_name: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/results/upload",
            files={"file": (filename, content)},
            params={"batch_id": batch_id, "sheet_name": sheet_name},
        )

    def list_results(self, batch_id: int) -> list[dict[str, Any]]:
        return self._request("GET", "/results", params={"batch_id": batch_id})

    def final_shortlist(
        self,
        run_id: int,
        weights: dict[str, float] | None = None,
        mode: str = "top_n",
        top_n: int = 5,
        threshold: float = 60.0,
    ) -> dict[str, Any]:
        """Instant, inference-free: recomputes final_score from stored scores."""
        return self._request(
            "POST",
            "/results/shortlist",
            json={
                "run_id": run_id,
                "weights": weights,
                "mode": mode,
                "top_n": top_n,
                "threshold": threshold,
            },
        )

    # -- interviews (§4.8) --------------------------------------------------
    def schedule_interviews(
        self,
        run_id: int,
        mode: str = "top_n",
        top_n: int = 3,
        threshold: float = 65.0,
        start_date: str | None = None,
        day_start_hour: int = 10,
        day_end_hour: int = 17,
        slot_minutes: int = 45,
        gap_minutes: int = 15,
        timezone: str = "Asia/Kolkata",
        send_invites: bool = True,
        force: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/interviews/schedule",
            json={
                "run_id": run_id,
                "mode": mode,
                "top_n": top_n,
                "threshold": threshold,
                "start_date": start_date,
                "day_start_hour": day_start_hour,
                "day_end_hour": day_end_hour,
                "slot_minutes": slot_minutes,
                "gap_minutes": gap_minutes,
                "timezone": timezone,
                "send_invites": send_invites,
            },
            params={"force": force},
        )

    def list_interviews(self, run_id: int) -> list[dict[str, Any]]:
        return self._request("GET", "/interviews", params={"run_id": run_id})

    def cancel_interview(self, interview_id: int) -> dict[str, Any]:
        return self._request("DELETE", f"/interviews/{interview_id}")


def get_client() -> ApiClient:
    """Return a default :class:`ApiClient` instance."""
    return ApiClient()
