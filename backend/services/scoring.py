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

#: Pre-test blend weights per input component (must sum to 1.0). See CLAUDE.md.
PRETEST_WEIGHTS: dict[str, float] = {
    "resume": 0.40,
    "github": 0.25,
    "projects": 0.20,
    "academics": 0.15,
}


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


def combine_pretest_score(
    scores: dict[str, float | None], weights: dict[str, float] = PRETEST_WEIGHTS
) -> float:
    """Blend component scores (each 0-100) into one 0-100 pre-test score.

    DELIBERATE DESIGN DECISION — a missing input is NOT a zero. When a
    component's score is ``None`` (the common case: ``github`` for a candidate
    who never provided a GitHub link), that component is dropped and its weight
    is redistributed *proportionally* across the components that do have a
    score; the surviving weights are renormalised to sum to 1.0 (i.e. to 100%).

    A candidate who submitted no GitHub link is therefore judged on the inputs
    they did provide — neither penalised with a 0 nor credited for work they
    never showed. This is not a fallback or an error path; it is how absent
    inputs are meant to be handled in the aggregate score.
    """
    present = {k: v for k, v in scores.items() if v is not None}
    if not present:
        return 0.0
    active_weight = sum(weights.get(k, 0.0) for k in present)
    if active_weight <= 0:
        return 0.0
    return round(
        sum(v * (weights[k] / active_weight) for k, v in present.items()), 2
    )
