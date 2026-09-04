"""Repository-level GitHub analysis (§4.4; hard constraint §5).

Profile stats alone (followers, total stars, repo count) do NOT satisfy the
requirement. This module fetches individual repositories, drops forks, ranks the
originals by a transparent heuristic, then reads the top 3 in depth (README +
languages + metadata) and hands those to the LLM for a qualitative judgement.

Every raw GitHub API response is cached in :class:`GitHubCache` keyed by
username — Phase 5 debugging re-runs this constantly and the authenticated rate
limit (5000/hr) should not be burned on unchanged profiles. ``force=True``
bypasses the cache.

Nothing here raises out to the caller: failures become a status + error string.
"""

from __future__ import annotations

import asyncio
import base64
import math
from datetime import datetime, timezone
from typing import Any

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from sqlmodel import Session, select
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.models.schemas import (
    GitHubEvaluation,
    GitHubEvaluationOut,
    GitHubStats,
    GitHubStatus,
)
from backend.models.tables import (
    Candidate,
    GitHubAnalysis,
    GitHubCache,
    JobDescription,
)
from backend.services import scoring
from backend.services.llm import get_llm

logger = get_logger(__name__)

GITHUB_API = "https://api.github.com"
TOP_N = 3
README_TRUNCATE = 3000
HTTP_TIMEOUT = 20.0

GITHUB_PROMPT = """\
You are evaluating a candidate's GitHub work against a job description.
Score four dimensions 0-10.

RULES
- Evidence must be repository names or direct quotes from their READMEs.
  If you cannot cite a repo or README, do not make the claim.
- Judge substance, not volume. One well-built original project beats ten
  tutorial follow-alongs.
- A repo that appears to be coursework or a tutorial clone scores low on
  repository_quality even if the code is clean.
- Do not infer skills from repo names alone — check what the README says
  was actually built.
- Use the FULL 0-10 range. Do not cluster between 6 and 8.

CALIBRATION
  9-10  substantial original systems, clearly relevant, well documented
  7-8   solid original projects with real engineering
  5-6   working projects but limited depth or relevance
  3-4   mostly small/coursework/tutorial repos
  0-2   little or no meaningful original work

DIMENSIONS
  repository_quality    substance and originality of their best repos
  technical_relevance   how well their work matches the JD's requirements
  activity_consistency  recency and sustained activity vs one dead repo
  engineering_practice  READMEs, structure, documentation, evidence of care

LENGTH LIMITS (hard — the ENTIRE response is rejected if any field exceeds its cap)
  each reasoning field  at most 280 characters — 1 to 2 short sentences
  evidence              AT MOST 3 items per dimension; each is a repo name or a
                        short "..."-trimmed README quote, never a long passage
  summary               at most 400 characters
Count characters as you write and stay well under every cap. A rejected response
scores the candidate nothing.
"""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    """Authenticated GitHub headers (5000 req/hr instead of 60)."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = get_settings().github_token
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a GitHub ISO timestamp (``...Z``) to an aware datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover - defensive
        return None


# --- repo selection: SUBSTANCE first, recency last -------------------------
#
# Methodology (defensible per §7 "GitHub analysis methodology"):
#
#   The job of this function is only to decide *which* repos the LLM reads in
#   depth. An earlier version weighted recency 0.40 and picked the three newest
#   repos; for accounts whose strongest work is a semester old (the common
#   student case) that surfaced throwaway repos — one had a README of just
#   "# X / BTECH PROJECT" — and starved the model of the real projects, which
#   then scored technical_relevance far too low. "How recently did they push"
#   is already measured by days_since_last_push in GitHubStats and belongs to
#   activity_consistency; it must not decide what gets read.
#
#   1. SUBSTANCE GATE. A repo must show at least one concrete sign that real
#      work exists. Gate-passers ALWAYS rank above gate-failers, so a fresh
#      2-line-README stub can never displace a substantive project.
#   2. WEIGHTED RANK within each tier:
#         substance 0.50   description text + README size + repo size
#         signal    0.30   stars (others found it useful) + repo size
#         recency   0.20   tie-breaker only, ~3-year linear decay
#   3. BREADTH. Once a language has two picks, a *near-tied* repo (within a
#      tight DIVERSITY_TOL) in another language is taken ahead of a third repo
#      in the same language. The tolerance is deliberately small: breadth only
#      breaks genuine ties — a clearly more substantive repo is never displaced
#      for language variety.
#
# README byte size (``_readme_bytes``) is attached upstream by _gather_raw via a
# cheap probe; when absent (old cache) the gate falls back to description/stars/
# size and still works.

_SUBSTANCE_W, _SIGNAL_W, _RECENCY_W = 0.50, 0.30, 0.20
_GATE_MIN_DESC_CHARS = 40
_GATE_MIN_README_BYTES = 200
_GATE_MIN_SIZE_KB = 40  # a repo with only a README / config is a few KB
_DIVERSITY_TOL = 0.05  # breadth breaks genuine ties only, not clear winners


def _repo_signals(repo: dict[str, Any], now: datetime) -> tuple[float, float, float]:
    """Return ``(substance, signal, recency)`` each in 0..1."""
    desc = (repo.get("description") or "").strip()
    readme_bytes = int(repo.get("_readme_bytes", 0) or 0)
    size_kb = max(0, repo.get("size", 0))

    size_sig = min(1.0, math.log1p(size_kb) / math.log1p(50_000))
    writeup = max(min(1.0, len(desc) / 200.0), min(1.0, readme_bytes / 2000.0))
    substance = 0.6 * writeup + 0.4 * size_sig

    stars_sig = min(1.0, math.log1p(max(0, repo.get("stargazers_count", 0))) / math.log1p(50))
    signal = 0.7 * stars_sig + 0.3 * size_sig

    pushed = _parse_ts(repo.get("pushed_at"))
    recency = max(0.0, 1.0 - (now - pushed).days / 1095.0) if pushed else 0.0
    return substance, signal, recency


def _passes_substance_gate(repo: dict[str, Any]) -> bool:
    desc = (repo.get("description") or "").strip()
    return (
        len(desc) > _GATE_MIN_DESC_CHARS
        or int(repo.get("_readme_bytes", 0) or 0) > _GATE_MIN_README_BYTES
        or repo.get("stargazers_count", 0) > 0
        or repo.get("size", 0) > _GATE_MIN_SIZE_KB
    )


def select_top_repos(repos: list[dict[str, Any]], n: int = TOP_N) -> list[dict[str, Any]]:
    """Pick the ``n`` original repos most likely to reflect real ability.

    Pure function (no I/O). See the methodology comment above for the weighting
    rationale: forks dropped, a substance gate, then a substance-led blend in
    which recency is only a tie-breaker, then a breadth preference.
    """
    # Drop forks and the special <username>/<username> profile-README repo — the
    # latter is a CV blurb, not repository work, and its large README would let
    # it sail through the substance gate.
    originals = [
        r
        for r in repos
        if not r.get("fork", False)
        and (r.get("name") or "").lower() != (r.get("owner") or {}).get("login", "").lower()
    ]
    if not originals:
        return []

    now = datetime.now(timezone.utc)
    scored: list[tuple[dict[str, Any], bool, float]] = []
    for repo in originals:
        substance, signal, recency = _repo_signals(repo, now)
        blended = _SUBSTANCE_W * substance + _SIGNAL_W * signal + _RECENCY_W * recency
        scored.append((repo, _passes_substance_gate(repo), blended))

    scored.sort(key=lambda t: (t[1], t[2]), reverse=True)  # gate-pass first, then blend

    picked: list[dict[str, Any]] = []
    lang_count: dict[str, int] = {}
    pending = scored[:]
    while pending and len(picked) < n:
        repo, gate, blended = pending[0]
        lang = (repo.get("language") or "").lower()
        if gate and lang and lang_count.get(lang, 0) >= 2:
            alt = next(
                (
                    t
                    for t in pending[1:]
                    if t[1]
                    and (t[0].get("language") or "").lower() != lang
                    and t[2] >= blended - _DIVERSITY_TOL
                ),
                None,
            )
            if alt is not None:
                repo, gate, blended = alt
                lang = (repo.get("language") or "").lower()
        pending = [t for t in pending if t[0] is not repo]
        picked.append(repo)
        if lang:
            lang_count[lang] = lang_count.get(lang, 0) + 1

    return picked


def compute_stats(
    repos: list[dict[str, Any]], details: list[dict[str, Any]]
) -> GitHubStats:
    """Deterministic account-level signals. Pure function — no LLM, no I/O."""
    originals = [r for r in repos if not r.get("fork", False)]
    forks = [r for r in repos if r.get("fork", False)]

    languages: set[str] = {r["language"] for r in originals if r.get("language")}
    for detail in details:
        languages.update((detail.get("languages") or {}).keys())

    pushes = [_parse_ts(r.get("pushed_at")) for r in originals]
    pushes = [p for p in pushes if p is not None]
    days_since_last_push: int | None = None
    if pushes:
        days_since_last_push = (datetime.now(timezone.utc) - max(pushes)).days

    return GitHubStats(
        original_repos=len(originals),
        forked_repos=len(forks),
        total_stars=sum(r.get("stargazers_count", 0) for r in originals),
        languages=sorted(languages),
        days_since_last_push=days_since_last_push,
        repos_with_readme=sum(1 for d in details if d.get("has_readme")),
        analyzed_repos=[d["name"] for d in details],
    )


# ---------------------------------------------------------------------------
# GitHub API (authenticated, async, transient-retry)
# ---------------------------------------------------------------------------

_retry_http = retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, max=10),
    reraise=True,
)


@_retry_http
async def fetch_user(client: httpx.AsyncClient, username: str) -> dict[str, Any] | None:
    """GET /users/{username}. ``None`` on 404."""
    resp = await client.get(f"{GITHUB_API}/users/{username}", headers=_headers())
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


@_retry_http
async def fetch_repos(client: httpx.AsyncClient, username: str) -> list[dict[str, Any]]:
    """GET /users/{username}/repos?per_page=100&sort=updated."""
    resp = await client.get(
        f"{GITHUB_API}/users/{username}/repos",
        params={"per_page": 100, "sort": "updated"},
        headers=_headers(),
    )
    resp.raise_for_status()
    return resp.json()


@_retry_http
async def probe_readme_size(
    client: httpx.AsyncClient, owner: str, repo: str
) -> int:
    """README byte size for one repo (0 if none). One cheap call, no decode.

    Feeds the substance gate in :func:`select_top_repos` so a repo whose write-up
    lives in the README (not the description) is not mistaken for a stub.
    """
    resp = await client.get(f"{GITHUB_API}/repos/{owner}/{repo}/readme", headers=_headers())
    if resp.status_code == 200:
        return int(resp.json().get("size", 0) or 0)
    return 0


@_retry_http
async def fetch_repo_detail(
    client: httpx.AsyncClient, owner: str, repo: str
) -> dict[str, Any]:
    """Repo metadata + README (base64-decoded, truncated) + language breakdown.

    A missing README is normal: ``has_readme=False``, no error.
    """
    base = f"{GITHUB_API}/repos/{owner}/{repo}"

    meta_resp = await client.get(base, headers=_headers())
    meta_resp.raise_for_status()
    meta = meta_resp.json()

    readme_text: str | None = None
    has_readme = False
    readme_resp = await client.get(f"{base}/readme", headers=_headers())
    if readme_resp.status_code == 200:
        try:
            raw = base64.b64decode(readme_resp.json().get("content", ""))
            readme_text = raw.decode("utf-8", errors="replace")[:README_TRUNCATE]
            has_readme = True
        except Exception:  # noqa: BLE001 - malformed base64 -> treat as no README
            readme_text = None

    langs_resp = await client.get(f"{base}/languages", headers=_headers())
    languages = langs_resp.json() if langs_resp.status_code == 200 else {}

    return {
        "name": meta.get("name", repo),
        "full_name": meta.get("full_name"),
        "description": meta.get("description"),
        "stars": meta.get("stargazers_count", 0),
        "forks": meta.get("forks_count", 0),
        "pushed_at": meta.get("pushed_at"),
        "languages": languages,
        "has_readme": has_readme,
        "readme": readme_text,
    }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _store_cache(session: Session, username: str, payload: dict[str, Any]) -> None:
    row = session.get(GitHubCache, username)
    if row is None:
        row = GitHubCache(username=username)
    row.payload = payload
    row.cached_at = datetime.utcnow()
    session.add(row)
    session.commit()


async def _gather_raw(
    client: httpx.AsyncClient, username: str, session: Session, force: bool
) -> dict[str, Any]:
    """Return ``{user, repos, details}`` for ``username``, from cache unless force."""
    if not force:
        cached = session.get(GitHubCache, username)
        if cached is not None and cached.payload:
            logger.info("github cache hit for %s", username)
            return cached.payload

    user = await fetch_user(client, username)
    payload: dict[str, Any] = {"user": user, "repos": [], "details": {}}
    if user is None:
        _store_cache(session, username, payload)
        return payload

    repos = await fetch_repos(client, username)

    # Probe README size for every original repo BEFORE ranking, so the substance
    # gate can see write-ups that live in the README rather than the description.
    originals = [r for r in repos if not r.get("fork", False)]
    sizes = await asyncio.gather(
        *(probe_readme_size(client, username, r["name"]) for r in originals)
    )
    for repo, size in zip(originals, sizes):
        repo["_readme_bytes"] = size
    payload["repos"] = repos

    details: dict[str, Any] = {}
    for repo in select_top_repos(repos, TOP_N):
        details[repo["name"]] = await fetch_repo_detail(client, username, repo["name"])
    payload["details"] = details

    _store_cache(session, username, payload)
    return payload


# ---------------------------------------------------------------------------
# LLM prompt assembly + persistence
# ---------------------------------------------------------------------------


def _build_user_prompt(
    job: JobDescription, stats: GitHubStats, details: list[dict[str, Any]]
) -> str:
    lines = [
        "JOB DESCRIPTION",
        f"Title: {job.title}",
        "",
        job.description,
        "",
        "--- END JOB DESCRIPTION ---",
        "",
        "DETERMINISTIC GITHUB STATS (already computed — do not recompute)",
        f"original repos: {stats.original_repos}   forked repos: {stats.forked_repos}   "
        f"total stars (original): {stats.total_stars}",
        f"languages: {', '.join(stats.languages) or '(none detected)'}",
        f"days since last push: {stats.days_since_last_push}",
        f"repos with README (of {len(details)} read in depth): {stats.repos_with_readme}",
        f"repositories read in depth: {', '.join(stats.analyzed_repos)}",
        "",
        "--- TOP REPOSITORIES (read these) ---",
    ]
    for detail in details:
        lines += [
            "",
            f"### {detail['name']}  "
            f"(stars {detail['stars']}, forks {detail['forks']}, last push {detail['pushed_at']})",
            f"description: {detail.get('description') or '(none)'}",
            f"languages: {', '.join((detail.get('languages') or {}).keys()) or '(none)'}",
        ]
        if detail.get("has_readme"):
            lines.append(f"README (truncated to {README_TRUNCATE} chars):")
            lines.append(detail["readme"])
        else:
            lines.append("README: (none — this repo has no README)")
    lines += ["", "--- END REPOSITORIES ---", "", "Score the four dimensions now."]
    return "\n".join(lines)


def _persist(
    session: Session, candidate_id: int, job_id: int, out: GitHubEvaluationOut
) -> None:
    """Upsert one GitHubAnalysis row per candidate (job_id kept in the payload)."""
    row = session.exec(
        select(GitHubAnalysis).where(GitHubAnalysis.candidate_id == candidate_id)
    ).first()
    if row is None:
        row = GitHubAnalysis(candidate_id=candidate_id)
    row.username = out.username
    row.error = out.error
    row.payload = {
        "job_id": job_id,
        "status": out.status.value,
        "github_score": out.github_score,
        "model_used": out.model_used,
        "weights": scoring.GITHUB_WEIGHTS,
        "stats": out.stats.model_dump() if out.stats else None,
        "evaluation": out.evaluation.model_dump() if out.evaluation else None,
    }
    session.add(row)
    session.commit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def analyze_candidate(
    session: Session, candidate_id: int, job_id: int, force: bool = False
) -> GitHubEvaluationOut:
    """Repository-level GitHub analysis for one candidate. Never raises.

    Status outcomes: ``NO_PROFILE`` (no username — not a 0, no LLM),
    ``NOT_FOUND`` (username 404s), ``EMPTY`` (valid account, no original repos —
    stats populated, no LLM), ``ERROR`` (fetch/LLM failed), ``OK`` (scored).
    """
    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        return GitHubEvaluationOut(
            candidate_id=candidate_id,
            s_no=-1,
            name="<unknown>",
            username=None,
            status=GitHubStatus.ERROR,
            error=f"candidate {candidate_id} not found",
        )

    ident = {
        "candidate_id": candidate_id,
        "s_no": candidate.s_no,
        "name": candidate.name,
    }
    username = (candidate.github_username or "").strip() or None

    if username is None:
        out = GitHubEvaluationOut(
            **ident,
            username=None,
            status=GitHubStatus.NO_PROFILE,
            github_score=None,
            error="candidate provided no GitHub URL",
        )
        _persist(session, candidate_id, job_id, out)
        logger.info("candidate %s: NO_PROFILE — no LLM call", candidate_id)
        return out

    job = session.get(JobDescription, job_id)
    if job is None:
        out = GitHubEvaluationOut(
            **ident, username=username, status=GitHubStatus.ERROR,
            error=f"job {job_id} not found",
        )
        _persist(session, candidate_id, job_id, out)
        return out

    try:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT, follow_redirects=True
        ) as client:
            raw = await _gather_raw(client, username, session, force)
    except Exception as exc:  # noqa: BLE001 - never raise
        logger.exception("github fetch failed for %s", username)
        out = GitHubEvaluationOut(
            **ident, username=username, status=GitHubStatus.ERROR,
            error=f"github fetch failed: {type(exc).__name__}: {exc}",
        )
        _persist(session, candidate_id, job_id, out)
        return out

    if raw.get("user") is None:
        out = GitHubEvaluationOut(
            **ident, username=username, status=GitHubStatus.NOT_FOUND,
            error=f"GitHub user '{username}' not found (404) — "
            "username may be wrong (Phase 1 regex-scraped it from free text)",
        )
        _persist(session, candidate_id, job_id, out)
        logger.warning("candidate %s: github username %r -> 404", candidate_id, username)
        return out

    repos = raw.get("repos", [])
    details = list(raw.get("details", {}).values())
    stats = compute_stats(repos, details)

    if stats.original_repos == 0:
        out = GitHubEvaluationOut(
            **ident, username=username, status=GitHubStatus.EMPTY,
            stats=stats, github_score=None,
            error="valid account with zero public original repositories",
        )
        _persist(session, candidate_id, job_id, out)
        logger.info("candidate %s: EMPTY — 0 original repos, no LLM call", candidate_id)
        return out

    llm = get_llm(structured_output_model=GitHubEvaluation)
    try:
        evaluation = llm.invoke(
            [
                SystemMessage(content=GITHUB_PROMPT),
                HumanMessage(content=_build_user_prompt(job, stats, details)),
            ]
        )
    except Exception as exc:  # noqa: BLE001 - never raise
        logger.exception("github LLM failed for candidate %s", candidate_id)
        out = GitHubEvaluationOut(
            **ident, username=username, status=GitHubStatus.ERROR,
            stats=stats, model_used=llm.model_used,
            error=f"github LLM failed: {type(exc).__name__}: {exc}",
        )
        _persist(session, candidate_id, job_id, out)
        return out

    github_score = scoring.compute_github_score(evaluation)
    out = GitHubEvaluationOut(
        **ident,
        username=username,
        status=GitHubStatus.OK,
        stats=stats,
        evaluation=evaluation,
        github_score=github_score,
        model_used=llm.model_used,
    )
    _persist(session, candidate_id, job_id, out)
    logger.info(
        "candidate %s github_score=%.2f via %s",
        candidate_id,
        github_score,
        llm.model_used,
    )
    return out
