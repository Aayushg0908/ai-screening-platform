"""Outreach orchestration (§4.6): shortlist a run's candidates, mail the test
link, and log every attempt.

Shortlisting reads a run's STORED ``Evaluation`` rows only - it never re-runs
evaluation. Dedup and "already emailed" tracking is by ``candidate_id``, never
by email address: every sample candidate shares one inbox
(``rishabh.choudhary+arnav@mynachiketa.com``), so email-based dedupe would
collapse all ten into one record.
"""

from __future__ import annotations

import time

from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.models.schemas import (
    EmailLogOut,
    SendReport,
    SendResult,
    ShortlistItem,
    ShortlistPreview,
    ShortlistRequest,
)
from backend.models.tables import Candidate, EmailLog, Evaluation, JobDescription, PipelineRun
from backend.services import mailer
from backend.services import scoring

logger = get_logger(__name__)

#: Gmail throttles bursts - a small, deliberate delay between sequential sends.
SEND_DELAY_SECONDS = 1.0

EMAIL_TYPE_TEST_INVITE = "test_invite"


@retry(
    retry=retry_if_exception_type(DBAPIError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, max=8),
    reraise=True,
)
def _exec_all(session: Session, statement) -> list:
    """Run a read-only SELECT and return every row. Retries a transient DB
    connectivity failure (e.g. Neon DNS resolution hiccups, seen repeatedly in
    practice) a few times before giving up - the same failure mode
    ``core/db.py`` already retries around at startup. Safe to retry: read-only,
    no side effects.
    """
    return list(session.exec(statement).all())


def _run_evaluations(session: Session, run_id: int) -> list[Evaluation]:
    return _exec_all(session, select(Evaluation).where(Evaluation.run_id == run_id))


def _already_sent_candidate_ids(session: Session, run_id: int) -> set[int]:
    rows = _exec_all(
        session,
        select(EmailLog)
        .where(EmailLog.run_id == run_id)
        .where(EmailLog.email_type == EMAIL_TYPE_TEST_INVITE)
        .where(EmailLog.status == "sent"),
    )
    return {row.candidate_id for row in rows}


def list_email_log(session: Session, run_id: int) -> list[EmailLogOut]:
    """All send attempts (sent or failed) recorded for a run."""
    rows = _exec_all(
        session, select(EmailLog).where(EmailLog.run_id == run_id).order_by(EmailLog.id)
    )
    return [EmailLogOut.model_validate(row) for row in rows]


def get_shortlist(session: Session, req: ShortlistRequest) -> ShortlistPreview:
    """Resolve a run's shortlist from STORED results. Never re-runs evaluation.

    ``top_n`` - highest ``pre_test_score`` first. ``threshold`` - every
    candidate with ``pre_test_score >= req.threshold``. Candidates with a
    ``None`` ``pre_test_score`` (unscorable) are never shortlisted.
    """
    rows = _run_evaluations(session, req.run_id)
    scorable = [ev for ev in rows if ev.pre_test_score is not None]

    ranked, _unranked = scoring.rank_candidates(
        [
            {"candidate_id": ev.candidate_id, "pre_test_score": ev.pre_test_score}
            for ev in scorable
        ]
    )

    if req.mode == "threshold":
        chosen = [r for r in ranked if r["pre_test_score"] >= req.threshold]
        criterion = f"score >= {req.threshold}"
    else:
        n = max(0, req.top_n)
        chosen = ranked[:n]
        criterion = f"top {n}"

    candidates_by_id = {
        c.candidate_id: c
        for c in (
            _exec_all(
                session,
                select(Candidate).where(
                    Candidate.candidate_id.in_([r["candidate_id"] for r in chosen])
                ),
            )
            if chosen
            else []
        )
    }

    already_sent = _already_sent_candidate_ids(session, req.run_id)

    items: list[ShortlistItem] = []
    for r in chosen:
        cand = candidates_by_id.get(r["candidate_id"])
        if cand is None:
            logger.warning(
                "get_shortlist: candidate %s in run %s has no Candidate row",
                r["candidate_id"],
                req.run_id,
            )
            continue
        items.append(
            ShortlistItem(
                candidate_id=cand.candidate_id,
                s_no=cand.s_no,
                name=cand.name,
                email=cand.email,
                pre_test_score=r["pre_test_score"],
                rank=r["rank"],
                already_emailed=cand.candidate_id in already_sent,
            )
        )

    return ShortlistPreview(
        run_id=req.run_id,
        mode=req.mode,
        criterion=criterion,
        shortlisted=items,
        excluded_count=len(rows) - len(items),
    )


def send_test_invites(
    session: Session, req: ShortlistRequest, force: bool = False
) -> SendReport:
    """Email the test link to a run's shortlist. Never raises.

    Skips a candidate with an existing ``sent`` :class:`EmailLog` row for this
    run unless ``force``. Sends sequentially with a ~1s delay between attempts.
    Writes one ``EmailLog`` row per attempt, success or failure, so a crash
    mid-batch leaves an accurate partial record.
    """
    settings = get_settings()
    bcc = (settings.email_bcc or "").strip() or None
    preview = get_shortlist(session, req)

    run = session.get(PipelineRun, req.run_id)
    job = session.get(JobDescription, run.job_id) if run and run.job_id else None

    if job is None:
        logger.error("send_test_invites: no job description for run %s", req.run_id)
        results = [
            SendResult(
                candidate_id=item.candidate_id,
                name=item.name,
                recipient=item.email,
                status="failed",
                error="job description not found for this run",
            )
            for item in preview.shortlisted
        ]
        return SendReport(
            run_id=req.run_id,
            attempted=len(results),
            sent=0,
            failed=len(results),
            skipped=0,
            results=results,
            bcc=bcc,
        )

    results: list[SendResult] = []
    sent = failed = skipped = 0

    for item in preview.shortlisted:
        if item.already_emailed and not force:
            skipped += 1
            results.append(
                SendResult(
                    candidate_id=item.candidate_id,
                    name=item.name,
                    recipient=item.email,
                    status="skipped",
                    error=None,
                )
            )
            continue

        candidate = session.get(Candidate, item.candidate_id)
        subject, text_body, html_body = mailer.build_test_invite(
            candidate, job, settings.test_link_url
        )
        ok, error = mailer.send_email(item.email, subject, text_body, html_body)
        status = "sent" if ok else "failed"

        session.add(
            EmailLog(
                candidate_id=item.candidate_id,
                run_id=req.run_id,
                email_type=EMAIL_TYPE_TEST_INVITE,
                recipient=item.email,
                subject=subject,
                status=status,
                error=error,
            )
        )
        session.commit()

        results.append(
            SendResult(
                candidate_id=item.candidate_id,
                name=item.name,
                recipient=item.email,
                status=status,
                error=error,
            )
        )
        if ok:
            sent += 1
        else:
            failed += 1
            logger.warning(
                "send_test_invites: send failed for candidate %s: %s",
                item.candidate_id,
                error,
            )

        time.sleep(SEND_DELAY_SECONDS)

    return SendReport(
        run_id=req.run_id,
        attempted=sent + failed,
        sent=sent,
        failed=failed,
        skipped=skipped,
        results=results,
        bcc=bcc,
    )
