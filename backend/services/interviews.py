"""Interview scheduling orchestration (§4.8).

Resolves qualified candidates from STORED final scores (Phase 7 - zero
re-evaluation), books real Google Calendar events with Meet links, and emails
the invite via the Phase 6 mailer. One :class:`Interview` row per (candidate,
run): the table's unique constraint means a re-schedule updates that row
rather than inserting a duplicate.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.models.schemas import ScheduledInterview, ScheduleReport, ScheduleRequest
from backend.models.tables import Candidate, Evaluation, Interview, JobDescription, PipelineRun
from backend.services import calendar as calendar_service
from backend.services import mailer

logger = get_logger(__name__)

EMAIL_TYPE_INTERVIEW_INVITE = "interview_invite"

#: Be polite to the Calendar API between sequential inserts.
SCHEDULE_DELAY_SECONDS = 0.5


def _qualified_candidates(session: Session, req: ScheduleRequest) -> list[dict]:
    """Resolve qualified candidates from STORED final scores (Phase 7).

    Candidates with ``final_score is None`` (AWAITING_RESULT or unscorable)
    are NEVER scheduled. ``rank`` reflects the candidate's true position
    among every scored candidate in the run, assigned before ``mode``
    filtering - so a threshold cut never renumbers the survivors.
    """
    evaluations = session.exec(
        select(Evaluation).where(Evaluation.run_id == req.run_id)
    ).all()
    scored = [ev for ev in evaluations if ev.final_score is not None]
    if not scored:
        return []

    candidates = {
        c.candidate_id: c
        for c in session.exec(
            select(Candidate).where(
                Candidate.candidate_id.in_([ev.candidate_id for ev in scored])
            )
        ).all()
    }

    ranked = sorted(scored, key=lambda ev: (-ev.final_score, ev.candidate_id))
    items: list[dict] = []
    for position, ev in enumerate(ranked, start=1):
        cand = candidates.get(ev.candidate_id)
        if cand is None:
            continue
        items.append({"candidate": cand, "final_score": ev.final_score, "rank": position})

    if req.mode == "threshold":
        return [it for it in items if it["final_score"] >= req.threshold]
    return items[: max(0, req.top_n)]


def _default_start_date(tz: str) -> date:
    """Tomorrow's date in ``tz`` - "tomorrow" means the candidate's local
    tomorrow, not the server's."""
    return datetime.now(ZoneInfo(tz)).date() + timedelta(days=1)


def schedule_interviews(
    session: Session, req: ScheduleRequest, force: bool = False
) -> ScheduleReport:
    """Schedule interviews for a run's qualified candidates. Never raises -
    a failed event or email is recorded, not propagated."""
    run = session.get(PipelineRun, req.run_id)
    if run is None:
        raise ValueError(f"run {req.run_id} not found")
    job = session.get(JobDescription, run.job_id) if run.job_id else None
    if job is None:
        raise ValueError(f"run {req.run_id} has no job description")

    qualified = _qualified_candidates(session, req)
    start_date = req.start_date or _default_start_date(req.timezone)
    slots = calendar_service.generate_slots(
        start_date=start_date,
        day_start=req.day_start_hour,
        day_end=req.day_end_hour,
        slot_minutes=req.slot_minutes,
        gap_minutes=req.gap_minutes,
        count=len(qualified),
        tz=req.timezone,
    )

    existing_by_candidate = {
        row.candidate_id: row
        for row in session.exec(
            select(Interview).where(Interview.run_id == req.run_id)
        ).all()
    }

    results: list[ScheduledInterview] = []
    scheduled = failed = skipped = 0

    for item, (start, end) in zip(qualified, slots):
        cand: Candidate = item["candidate"]
        existing = existing_by_candidate.get(cand.candidate_id)

        if existing and existing.status == "scheduled" and not force:
            skipped += 1
            results.append(
                ScheduledInterview(
                    candidate_id=cand.candidate_id,
                    s_no=cand.s_no,
                    name=cand.name,
                    email=cand.email,
                    rank=item["rank"],
                    final_score=item["final_score"],
                    starts_at=existing.starts_at,
                    ends_at=existing.ends_at,
                    meet_link=existing.meet_link,
                    event_id=existing.event_id,
                    status="skipped",
                    error=None,
                    invite_sent=True,
                )
            )
            continue

        # Force-reschedule: cancel the stale event before booking a new one,
        # so a repeated force call never orphans a real Calendar event.
        if existing and existing.event_id and force:
            try:
                calendar_service.delete_event(existing.event_id)
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                logger.warning(
                    "schedule_interviews: could not cancel stale event %s for "
                    "candidate %s: %s",
                    existing.event_id,
                    cand.candidate_id,
                    exc,
                )

        try:
            event = calendar_service.create_interview_event(
                cand, job, item["final_score"], start, end, req.timezone
            )
            meet_link = event.get("hangoutLink")
            event_id = event.get("id")
            status = "scheduled"
            error = None
        except Exception as exc:  # noqa: BLE001 - never raise, record it
            logger.exception(
                "schedule_interviews: event creation failed for candidate %s",
                cand.candidate_id,
            )
            meet_link = None
            event_id = None
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"

        invite_sent = False
        if status == "scheduled" and req.send_invites:
            subject, text_body, html_body = mailer.build_interview_invite(
                cand, job, start, end, req.timezone, meet_link
            )
            invite_sent, mail_error = mailer.send_email(
                cand.email, subject, text_body, html_body
            )
            if not invite_sent:
                # The event is real and stays scheduled either way - a mail
                # hiccup must not roll back a successful Calendar booking.
                logger.warning(
                    "schedule_interviews: invite email failed for candidate "
                    "%s: %s",
                    cand.candidate_id,
                    mail_error,
                )

        if existing:
            existing.event_id = event_id
            existing.meet_link = meet_link
            existing.starts_at = start
            existing.ends_at = end
            existing.status = status
            existing.error = error
            session.add(existing)
        else:
            session.add(
                Interview(
                    candidate_id=cand.candidate_id,
                    run_id=req.run_id,
                    event_id=event_id,
                    meet_link=meet_link,
                    starts_at=start,
                    ends_at=end,
                    status=status,
                    error=error,
                )
            )
        session.commit()

        results.append(
            ScheduledInterview(
                candidate_id=cand.candidate_id,
                s_no=cand.s_no,
                name=cand.name,
                email=cand.email,
                rank=item["rank"],
                final_score=item["final_score"],
                starts_at=start,
                ends_at=end,
                meet_link=meet_link,
                event_id=event_id,
                status=status,
                error=error,
                invite_sent=invite_sent,
            )
        )
        if status == "scheduled":
            scheduled += 1
        else:
            failed += 1

        time.sleep(SCHEDULE_DELAY_SECONDS)

    return ScheduleReport(
        run_id=req.run_id,
        attempted=scheduled + failed,
        scheduled=scheduled,
        failed=failed,
        skipped=skipped,
        interviews=results,
    )
