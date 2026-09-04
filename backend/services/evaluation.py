"""AI evaluation of one candidate's resume against one job description (§4.3).

The LLM returns per-dimension scores + reasoning + evidence ONLY. Every number
that ranks candidates is computed in :mod:`backend.services.scoring`. Output is
structured (``with_structured_output``) — no free-text parsing. temperature=0.

Batch evaluation, ranking, and GitHub belong to Phase 4/5 and are not here.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from backend.core.config import get_settings
from backend.core.db import engine
from backend.core.logging import get_logger
from backend.models.schemas import EvaluationOut, ResumeEvaluation, ScoreWeights
from backend.models.tables import (
    Candidate,
    Evaluation,
    JobDescription,
    PipelineRun,
    ResumeText,
)
from backend.services import scoring
from backend.services.llm import get_llm

logger = get_logger(__name__)

EVALUATION_PROMPT = """\
You are an experienced technical recruiter evaluating one candidate against
one job description. Score four dimensions independently, 0-10.

RULES
- Every score must be justified by evidence: direct quotes from the candidate's
  material. If you cannot quote it to support a claim, do not make the claim.
- Never infer skills from a job title. Score the material as written.
- If the material does not mention something the job description requires, list
  it in missing_requirements. Do not assume it is present.
- If a dimension has no supporting content at all (e.g. no research work),
  score it low, leave evidence empty, and state plainly that it is absent.
  Do not invent content.
- Use the FULL 0-10 range. Do not cluster scores between 6 and 8.

INPUT SECTIONS
- The candidate material has two parts: a CANDIDATE-PROVIDED SUMMARY (from the
  dataset) and RESUME TEXT. Both are candidate-provided. Evidence may be quoted
  from either; the rules on quoting still apply.
- The Research Work section is the authoritative signal for the research
  dimension. Work described there as a manuscript, preprint, "under validation",
  "accepted", or "in progress" counts as genuine research output even if
  unpublished — score it on substance, not publication status.
- If Research Work says "(not provided)", score research low and state it is
  absent. Do not infer research from projects alone.
- If the same piece of work appears in both sections, split it by aspect:
  * project_depth scores the ENGINEERING — what was built, the architecture,
    the technical ambition, whether it goes beyond tutorial-grade work.
  * research scores ONLY the RESEARCH OUTPUT — is there a paper, manuscript,
    preprint or submission; what venue; what stage; is the domain relevant to
    the JD. A project with no paper attached scores 0-2 on research no matter
    how technically impressive it is.
  Never cite the same aspect of one piece of work as evidence in both
  dimensions. Research evidence must reference a paper, manuscript, preprint or
  submission — not a system description.

CALIBRATION
  9-10  exceptional; clearly exceeds the requirement
  7-8   solid; meets the requirement with clear evidence
  5-6   partial; adjacent experience with real gaps
  3-4   weak; minimal relevant signal
  0-2   absent or unrelated

DIMENSIONS
  skills_match          technical skills vs the JD's stated requirements
  project_depth         substance and ambition of projects; tutorial-grade
                        work scores low, original systems score high
  experience_relevance  how relevant prior work/internships are to this role
  research              publications, papers, research work

reasoning: 1-2 concrete sentences referencing the JD's actual requirements.
evidence: at most 3 verbatim quotes from the resume; empty only if the
dimension is genuinely absent.
summary: 2-3 sentences, recruiter-facing.
missing_requirements: JD requirements with no supporting evidence in the resume.

LENGTH LIMITS (hard — the whole response is rejected if any is exceeded)
  summary               at most 400 characters (~3 sentences)
  each reasoning field   at most 300 characters (1-2 sentences)
  evidence               at most 3 quotes per dimension; keep each quote short,
                         trimming with "..." rather than pasting long passages
Count characters and stay comfortably under these limits.
"""

_USER_TEMPLATE = """\
JOB DESCRIPTION
Title: {title}

{description}

--- END JOB DESCRIPTION ---

=== CANDIDATE-PROVIDED SUMMARY (from dataset) ===
Best AI Project: {best_ai_project}
Research Work:   {research_work}

=== RESUME TEXT ===
{resume_text}

--- END CANDIDATE MATERIAL ---

Evaluate this candidate against this job description now."""


def _latest_resume(session: Session, candidate_id: int) -> ResumeText | None:
    return session.exec(
        select(ResumeText)
        .where(ResumeText.candidate_id == candidate_id)
        .order_by(ResumeText.id.desc())
    ).first()


def _persist(
    session: Session,
    *,
    candidate_id: int,
    job_id: int,
    evaluation: ResumeEvaluation | None,
    resume_score: float,
    weights: ScoreWeights,
    model_used: str | None,
    error: str | None,
) -> None:
    """Upsert the (candidate, job) row so a score is reproducible vs its weights."""
    row = session.exec(
        select(Evaluation)
        .where(Evaluation.candidate_id == candidate_id)
        .where(Evaluation.job_id == job_id)
    ).first()
    if row is None:
        row = Evaluation(candidate_id=candidate_id, job_id=job_id)

    row.resume_eval = evaluation.model_dump() if evaluation is not None else {}
    row.pre_test_score = resume_score
    row.weights = weights.model_dump()
    row.model_used = model_used
    row.errors = [error] if error else []
    session.add(row)
    session.commit()


def evaluate_candidate(
    session: Session,
    candidate_id: int,
    job_id: int,
    weights: ScoreWeights | None = None,
) -> EvaluationOut:
    """Evaluate one candidate against one job. Never raises.

    Returns an :class:`EvaluationOut` with ``error`` set (and ``resume_score``
    0.0) when the candidate/job is missing, there is no resume text, or the LLM
    fails after retries and fallback.
    """
    weights = weights or ScoreWeights()

    candidate = session.get(Candidate, candidate_id)
    if candidate is None:
        return EvaluationOut(
            candidate_id=candidate_id,
            s_no=-1,
            name="<unknown>",
            job_id=job_id,
            weights=weights,
            resume_score=0.0,
            error=f"candidate {candidate_id} not found",
        )

    job = session.get(JobDescription, job_id)
    if job is None:
        return EvaluationOut(
            candidate_id=candidate_id,
            s_no=candidate.s_no,
            name=candidate.name,
            job_id=job_id,
            weights=weights,
            resume_score=0.0,
            error=f"job {job_id} not found",
        )

    resume_row = _latest_resume(session, candidate_id)
    resume_text = (resume_row.text if resume_row else None) or ""
    best_ai_project = (candidate.best_ai_project or "").strip()
    research_work = (candidate.research_work or "").strip()

    has_resume = bool(resume_text.strip())
    has_dataset = bool(best_ai_project or research_work)

    if not has_resume and not has_dataset:
        reason = (
            resume_row.error
            if resume_row and resume_row.error
            else "no resume text, best_ai_project, or research_work on file"
        )
        logger.warning(
            "candidate %s: nothing to evaluate, skipping LLM (%s)",
            candidate_id,
            reason,
        )
        _persist(
            session,
            candidate_id=candidate_id,
            job_id=job_id,
            evaluation=None,
            resume_score=0.0,
            weights=weights,
            model_used=None,
            error=f"no evaluable material: {reason}",
        )
        return EvaluationOut(
            candidate_id=candidate_id,
            s_no=candidate.s_no,
            name=candidate.name,
            job_id=job_id,
            weights=weights,
            resume_score=0.0,
            error=f"no evaluable material: {reason}",
        )

    max_resume_chars = get_settings().resume_max_chars
    if has_resume and len(resume_text) > max_resume_chars:
        logger.info(
            "candidate %s: resume truncated %d -> %d chars",
            candidate_id,
            len(resume_text),
            max_resume_chars,
        )
        resume_text = resume_text[:max_resume_chars]

    if not has_resume:
        logger.info(
            "candidate %s: no resume text; evaluating from dataset fields only",
            candidate_id,
        )
        resume_section = (
            "(resume text was unavailable — evaluate from the "
            "candidate-provided summary above)"
        )
    else:
        resume_section = resume_text

    messages = [
        SystemMessage(content=EVALUATION_PROMPT),
        HumanMessage(
            content=_USER_TEMPLATE.format(
                title=job.title,
                description=job.description,
                best_ai_project=best_ai_project or "(not provided)",
                research_work=research_work or "(not provided)",
                resume_text=resume_section,
            )
        ),
    ]

    client = get_llm(structured_output_model=ResumeEvaluation)
    try:
        evaluation = client.invoke(messages)
    except Exception as exc:  # noqa: BLE001 - never raise out of this function
        logger.exception("LLM evaluation failed for candidate %s", candidate_id)
        _persist(
            session,
            candidate_id=candidate_id,
            job_id=job_id,
            evaluation=None,
            resume_score=0.0,
            weights=weights,
            model_used=client.model_used,
            error=f"LLM evaluation failed: {type(exc).__name__}: {exc}",
        )
        return EvaluationOut(
            candidate_id=candidate_id,
            s_no=candidate.s_no,
            name=candidate.name,
            job_id=job_id,
            weights=weights,
            resume_score=0.0,
            model_used=client.model_used,
            error=f"LLM evaluation failed: {type(exc).__name__}: {exc}",
        )

    if evaluation is None:
        # structured-output call returned nothing parseable, on every provider
        logger.error("candidate %s: LLM returned no response after retries", candidate_id)
        _persist(
            session,
            candidate_id=candidate_id,
            job_id=job_id,
            evaluation=None,
            resume_score=0.0,
            weights=weights,
            model_used=client.model_used,
            error="LLM returned no response after retries",
        )
        return EvaluationOut(
            candidate_id=candidate_id,
            s_no=candidate.s_no,
            name=candidate.name,
            job_id=job_id,
            weights=weights,
            resume_score=0.0,
            model_used=client.model_used,
            error="LLM returned no response after retries",
        )

    resume_score = scoring.compute_resume_score(evaluation, weights)
    _persist(
        session,
        candidate_id=candidate_id,
        job_id=job_id,
        evaluation=evaluation,
        resume_score=resume_score,
        weights=weights,
        model_used=client.model_used,
        error=None,
    )
    logger.info(
        "candidate %s evaluated: resume_score=%.2f via %s",
        candidate_id,
        resume_score,
        client.model_used,
    )
    return EvaluationOut(
        candidate_id=candidate_id,
        s_no=candidate.s_no,
        name=candidate.name,
        job_id=job_id,
        evaluation=evaluation,
        weights=weights,
        resume_score=resume_score,
        model_used=client.model_used,
        error=None,
    )


# ---------------------------------------------------------------------------
# Phase 5 — batch orchestration
# ---------------------------------------------------------------------------

#: Per-candidate graph retries when a branch comes back with a transient LLM
#: failure (rate limit, or a structured-output validation miss). Kept at 1: each
#: retry re-runs the whole graph = two more LLM calls, and on the Groq free tier
#: a candidate that retries repeatedly just keeps the token bucket empty for
#: every candidate behind it.
_TRANSIENT_RETRIES = 1

_TRANSIENT_MARKERS = (
    "429",
    "rate limit",
    "validationerror",
    "field required",
    "did not match schema",
    "tool_use_failed",
    "nonetype",
    "operationalerror",  # Neon scale-to-zero / transient DNS
    "could not translate host name",
    "connection",
    "timed out",
)


#: Bookkeeping DB writes retry through Neon's scale-to-zero cold starts and the
#: transient DNS blips seen on this host.
_db_retry = retry(
    retry=retry_if_exception_type(OperationalError),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, max=15),
    reraise=True,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _has_transient_error(errors: list[str]) -> bool:
    return any(
        marker in err.lower() for err in errors for marker in _TRANSIENT_MARKERS
    )


def _find_or_create_run(
    session: Session, batch_id: int, job_id: int, weights: ScoreWeights
) -> PipelineRun:
    """The newest pending run for this (batch, job), or a fresh one."""
    run = session.exec(
        select(PipelineRun)
        .where(PipelineRun.batch_id == batch_id)
        .where(PipelineRun.job_id == job_id)
        .where(PipelineRun.status == "pending")
        .order_by(PipelineRun.run_id.desc())
    ).first()
    if run is None:
        run = PipelineRun(batch_id=batch_id, job_id=job_id, status="pending")
        session.add(run)
        session.commit()
        session.refresh(run)
    return run


@_db_retry
def _start_run(
    batch_id: int, job_id: int, weights: ScoreWeights, mode: str
) -> tuple[int, list[int]]:
    """Claim the pending run, load its candidates, flip it to ``running``."""
    with Session(engine) as session:
        run = _find_or_create_run(session, batch_id, job_id, weights)
        candidate_ids = [
            c.candidate_id
            for c in session.exec(
                select(Candidate)
                .where(Candidate.batch_id == batch_id)
                .order_by(Candidate.s_no)
            ).all()
        ]
        run.total = len(candidate_ids)
        run.processed = 0
        run.status = "running"
        run.errors = []
        run.weights = weights.model_dump()
        run.mode = mode
        session.add(run)
        session.commit()
        return run.run_id, candidate_ids


@_db_retry
def _persist_candidate(
    run_id: int, job_id: int, candidate_id: int, weights: ScoreWeights, state: dict
) -> None:
    """Upsert one candidate's Evaluation row and bump the run's ``processed``.

    Called the moment a candidate's graph completes, so a mid-batch crash keeps
    every candidate finished so far.
    """
    with Session(engine) as session:
        row = session.exec(
            select(Evaluation)
            .where(Evaluation.candidate_id == candidate_id)
            .where(Evaluation.job_id == job_id)
        ).first()
        if row is None:
            row = Evaluation(candidate_id=candidate_id, job_id=job_id)

        row.run_id = run_id
        if state.get("resume_eval") is not None:
            row.resume_eval = state["resume_eval"]
        row.github_eval = state.get("github_eval") or {}
        row.weights = weights.model_dump()
        row.resume_score = state.get("resume_score")
        row.github_score = state.get("github_score")
        row.github_status = (state.get("github_eval") or {}).get("status")
        row.pre_test_score = state.get("pre_test_score")
        row.status = scoring.evaluation_state(
            row.resume_score, row.github_score, row.github_status, row.pre_test_score
        )
        row.errors = list(state.get("errors") or [])
        session.add(row)

        session.exec(
            update(PipelineRun)
            .where(PipelineRun.run_id == run_id)
            .values(processed=PipelineRun.processed + 1)
        )
        session.commit()


def run_batch_in_thread(
    batch_id: int,
    job_id: int,
    weights: ScoreWeights | None = None,
    force: bool = False,
    mode: str | None = None,
) -> None:
    """Sync entrypoint for ``BackgroundTasks``.

    ``run_batch`` is async and its graph nodes make blocking LLM / DB calls; run
    inside the API's event loop those would starve request handling (status
    polls would time out). Starlette runs a *sync* background task in a worker
    thread, so this spins up its own event loop there — fully isolated from the
    server's.
    """
    asyncio.run(run_batch(None, batch_id, job_id, weights, force, mode))


async def run_batch(
    session: Session | None,
    batch_id: int,
    job_id: int,
    weights: ScoreWeights | None = None,
    force: bool = False,
    mode: str | None = None,
) -> None:
    """Evaluate every candidate in ``batch_id`` against ``job_id``. Never raises.

    Runs the compiled graph once per candidate, ``settings.batch_concurrency``
    at a time (default 1 — see the config note),
    and persists each candidate's result (and increments ``PipelineRun.processed``)
    the instant it finishes. ``PipelineRun.status`` walks
    pending -> running -> completed, or -> failed only if the run infrastructure
    itself breaks (a per-candidate error never fails the run).

    The ``session`` argument is accepted for signature stability but not relied
    on — a background task outlives the request session, so this opens its own.
    """
    from backend.graph.pipeline import GRAPH  # lazy: avoids an import cycle
    from backend.services import llm as llm_pool

    settings = get_settings()
    weights = weights or ScoreWeights()
    blend = settings.pretest_blend
    concurrency = max(1, settings.batch_concurrency)

    profile = settings.eval_profile(mode)
    stagger = profile["stagger"]
    llm_pool.set_cooldown_seconds(profile["cooldown"])
    preemptive = bool(profile["preemptive_wait"])
    preempt_cap = float(profile["preemptive_cap"])
    run_id: int | None = None
    try:
        run_id, candidate_ids = _start_run(batch_id, job_id, weights, profile["mode"])

        logger.info(
            "run %s: batch %s vs job %s — %s candidates, mode=%s, concurrency %s, "
            "stagger %ss, cooldown %ss, preemptive_wait=%s",
            run_id,
            batch_id,
            job_id,
            len(candidate_ids),
            profile["mode"],
            concurrency,
            stagger,
            profile["cooldown"],
            preemptive,
        )

        if not candidate_ids:
            _finish_run(run_id, "failed", ["batch has no candidates"])
            return

        semaphore = asyncio.Semaphore(concurrency)

        # ``stagger`` exists SOLELY for Groq's per-minute token ceiling (8000 TPM
        # per key), set by the EVALUATION_MODE profile. Each candidate fires its
        # resume + GitHub LLM calls in PARALLEL and the key pool round-robins
        # them onto *different* keys, so every candidate loads every key at once
        # — the stagger governs per-key recovery and must NOT scale with pool
        # size. In "quality" mode a pre-emptive wait (below) holds for a free
        # Groq key rather than dropping to Mistral.
        #
        # NOTE: the sleep below is held INSIDE the semaphore. That is correct at
        # concurrency 1 (it spaces the LLM calls), but at concurrency > 1 it
        # would serialise the batch — move it outside the ``async with`` (or gate
        # it on ``concurrency == 1``) before raising concurrency.
        async def process(candidate_id: int) -> None:
            async with semaphore:
                if preemptive:
                    free_in = llm_pool.groq_pool_free_in()
                    if free_in > 0:
                        wait = min(free_in, preempt_cap)
                        logger.info(
                            "run %s candidate %s: all Groq keys cooling, waiting "
                            "%.0fs for a free key",
                            run_id,
                            candidate_id,
                            wait,
                        )
                        await asyncio.sleep(wait)
                        if free_in > preempt_cap:
                            logger.warning(
                                "run %s candidate %s: Groq pool still cooling after "
                                "the %.0fs cap — falling through to Mistral",
                                run_id,
                                candidate_id,
                                preempt_cap,
                            )
                state: dict = {}
                for attempt in range(_TRANSIENT_RETRIES + 1):
                    state = await _invoke_graph(
                        GRAPH, candidate_id, job_id, weights, blend, force
                    )
                    if not _has_transient_error(state.get("errors") or []):
                        break
                    wait = 15 * (attempt + 1)
                    logger.warning(
                        "run %s candidate %s transient failure, retry in %ss: %s",
                        run_id,
                        candidate_id,
                        wait,
                        state.get("errors"),
                    )
                    await asyncio.sleep(wait)
                _persist_candidate(run_id, job_id, candidate_id, weights, state)
                logger.info(
                    "run %s candidate %s done: pre_test=%s errors=%s",
                    run_id,
                    candidate_id,
                    state.get("pre_test_score"),
                    len(state.get("errors") or []),
                )
                # Drain Groq's per-minute token window before the next candidate.
                # Held inside the semaphore so it actually spaces the LLM calls.
                if stagger:
                    await asyncio.sleep(stagger)

        await asyncio.gather(*(process(cid) for cid in candidate_ids))
        _finish_run(run_id, "completed", [])
        logger.info("run %s: completed", run_id)
    except Exception as exc:  # noqa: BLE001 - never raise out of a background task
        logger.exception("run_batch crashed (run_id=%s)", run_id)
        if run_id is not None:
            _finish_run(run_id, "failed", [f"run_batch: {type(exc).__name__}: {exc}"])


async def _invoke_graph(
    graph,
    candidate_id: int,
    job_id: int,
    weights: ScoreWeights,
    blend: dict[str, float],
    force: bool,
) -> dict:
    """One graph run for one candidate; a graph-level crash becomes an error dict."""
    try:
        return await graph.ainvoke(
            {"candidate_id": candidate_id, "job_id": job_id, "errors": []},
            config={
                "configurable": {"weights": weights, "blend": blend, "force": force}
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("graph crashed for candidate %s", candidate_id)
        return {
            "candidate_id": candidate_id,
            "resume_eval": None,
            "github_eval": None,
            "resume_score": None,
            "github_score": None,
            "pre_test_score": None,
            "errors": [f"graph: {type(exc).__name__}: {exc}"],
        }


@_db_retry
def _finish_run(run_id: int, status: str, errors: list[str]) -> None:
    with Session(engine) as session:
        run = session.get(PipelineRun, run_id)
        if run is None:
            return
        run.status = status
        run.finished_at = _utcnow()
        if errors:
            run.errors = list(run.errors or []) + errors
        session.add(run)
        session.commit()
