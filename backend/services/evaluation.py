"""AI evaluation of one candidate's resume against one job description (§4.3).

The LLM returns per-dimension scores + reasoning + evidence ONLY. Every number
that ranks candidates is computed in :mod:`backend.services.scoring`. Output is
structured (``with_structured_output``) — no free-text parsing. temperature=0.

Batch evaluation, ranking, and GitHub belong to Phase 4/5 and are not here.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from sqlmodel import Session, select

from backend.core.logging import get_logger
from backend.models.schemas import EvaluationOut, ResumeEvaluation, ScoreWeights
from backend.models.tables import Candidate, Evaluation, JobDescription, ResumeText
from backend.services import scoring
from backend.services.llm import get_llm

logger = get_logger(__name__)

#: Resume text longer than this is truncated before the LLM call.
MAX_RESUME_CHARS = 12_000

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

    if has_resume and len(resume_text) > MAX_RESUME_CHARS:
        logger.info(
            "candidate %s: resume truncated %d -> %d chars",
            candidate_id,
            len(resume_text),
            MAX_RESUME_CHARS,
        )
        resume_text = resume_text[:MAX_RESUME_CHARS]

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
