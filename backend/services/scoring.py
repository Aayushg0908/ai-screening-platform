"""Deterministic score aggregation (§4.5 core).

The LLM emits per-dimension integer scores (0-10) with reasoning; this module
does the weighted sum. Pure arithmetic — no I/O, no LLM. Weights are passed in
explicitly (and persisted with every score) so a stored score can always be
recomputed and audited.

Phase 5 wires ranking on top of this.
"""

from __future__ import annotations

from backend.models.schemas import GitHubEvaluation, ResumeEvaluation, ScoreWeights

#: GitHub dimension weights (must sum to 1.0). Auditable, tunable in one place.
GITHUB_WEIGHTS: dict[str, float] = {
    "repository_quality": 0.35,
    "technical_relevance": 0.30,
    "activity_consistency": 0.20,
    "engineering_practice": 0.15,
}

#: Default pre-test blend: resume score vs GitHub score. Config overrides this
#: (settings.pretest_blend); /rerank overrides it per request.
PRETEST_BLEND: dict[str, float] = {"resume": 0.60, "github": 0.40}


def compute_resume_score(ev: ResumeEvaluation, w: ScoreWeights) -> float:
    """Weighted resume score on a 0-100 scale.

    ``weighted`` is the dimension scores (0-10) blended by weights that sum to
    1.0, so it stays in 0-10; multiplying by 10 puts it on 0-100. The LLM must
    never compute this number.
    """
    weighted = (
        ev.skills_match.score * w.skills_match
        + ev.project_depth.score * w.project_depth
        + ev.experience_relevance.score * w.experience_relevance
        + ev.research.score * w.research
    )
    return round(weighted * 10, 2)


def compute_github_score(
    ev: GitHubEvaluation, w: dict[str, float] = GITHUB_WEIGHTS
) -> float:
    """Weighted GitHub score on a 0-100 scale. Mirrors :func:`compute_resume_score`.

    Dimension scores are 0-10, weights sum to 1.0, so the blend stays in 0-10;
    ``* 10`` puts it on 0-100. The LLM never computes this number.
    """
    weighted = (
        ev.repository_quality.score * w["repository_quality"]
        + ev.technical_relevance.score * w["technical_relevance"]
        + ev.activity_consistency.score * w["activity_consistency"]
        + ev.engineering_practice.score * w["engineering_practice"]
    )
    return round(weighted * 10, 2)


def compute_pre_test_score(
    resume_score: float | None,
    github_score: float | None,
    weights: dict[str, float] = PRETEST_BLEND,
) -> float | None:
    """Blend the 0-100 resume and GitHub scores into one 0-100 pre-test score.

    Both present::   w_resume * resume_score + w_github * github_score
                     (weights renormalised to sum to 1.0 first)
    Only one present::  that score alone, renormalised to 100.
    Neither present::   ``None`` — the candidate is unscorable.

    DELIBERATE DESIGN DECISION — ``None`` is a missing input, NEVER a zero. It
    applies symmetrically:

    * No GitHub (status NO_PROFILE / NOT_FOUND / EMPTY): four of ten sample
      candidates never provided a link. The GitHub weight is redistributed onto
      the resume — judged on their resume alone, neither penalised nor credited.
    * Resume evaluation failed (e.g. a transient LLM rate limit) but GitHub
      succeeded: the resume weight is redistributed onto GitHub. A rate limit
      must not bury an otherwise strong candidate at the bottom of a shortlist.
    * Both failed: ``None`` — surfaced separately as unscorable, not ranked.

    A zero would say "we assessed this and it is bad". ``None`` says "we could
    not assess this part" — a categorically different thing for a hiring list.
    """
    if resume_score is None and github_score is None:
        return None
    if github_score is None:
        return round(resume_score, 2)
    if resume_score is None:
        return round(github_score, 2)

    w_resume = max(0.0, weights.get("resume", 0.60))
    w_github = max(0.0, weights.get("github", 0.40))
    total = w_resume + w_github
    if total <= 0:
        return round((resume_score + github_score) / 2, 2)
    return round(
        (w_resume / total) * resume_score + (w_github / total) * github_score, 2
    )


def rank_candidates(
    evaluations: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Split into ``(ranked, unranked)``.

    ``ranked`` — candidates with a non-``None`` ``pre_test_score``, sorted desc,
    each given a 1-based ``rank`` (tie-break: ``candidate_id`` asc).

    ``unranked`` — candidates whose ``pre_test_score`` is ``None`` (both the
    resume and GitHub evaluation failed). They are deliberately kept OUT of the
    ranking so a transient provider failure cannot bury a good candidate at
    rank 10. Their ``rank`` is ``None``.

    Pure: no LLM, no I/O, no DB. Plain dicts in and out, so the batch (fresh
    scores) and ``/rerank`` (recomputed-from-stored scores) both reuse it.
    """
    items = [dict(ev) for ev in evaluations]
    ranked = [ev for ev in items if ev.get("pre_test_score") is not None]
    unranked = [ev for ev in items if ev.get("pre_test_score") is None]

    ranked.sort(key=lambda ev: (-ev["pre_test_score"], ev["candidate_id"]))
    for position, ev in enumerate(ranked, start=1):
        ev["rank"] = position
    for ev in unranked:
        ev["rank"] = None

    return ranked, unranked


def evaluation_state(
    resume_score: float | None,
    github_score: float | None,
    github_status: str | None,
    pre_test_score: float | None,
) -> str:
    """One of ``scored`` / ``partial (resume failed)`` / ``partial (github failed)``
    / ``unscorable`` — for the results output. Pure.

    ``no_profile`` / ``not_found`` / ``empty`` GitHub is *absent*, not failed:
    a resume-only score for such a candidate is still ``scored``.
    """
    github_absent = github_status in ("no_profile", "not_found", "empty")
    if pre_test_score is None:
        return "unscorable"
    if resume_score is not None and (github_score is not None or github_absent):
        return "scored"
    if resume_score is None and github_score is not None:
        return "partial (resume failed)"
    if resume_score is not None and github_score is None and not github_absent:
        return "partial (github failed)"
    return "scored"
