"""Outbound email via Resend's HTTP API (§4.6).

Switched from Gmail SMTP after confirming Render's egress cannot reliably
reach Gmail's SMTP frontends: even after retrying every resolved address
across both submission ports (587 STARTTLS, 465 implicit SSL), the deployed
service's send success rate was 0/9 in a clean test, 1/13 (7.7%) across a
full day of testing - "OSError: [Errno 101] Network is unreachable" on
essentially every attempt. That is a network-layer block on SMTP egress, not
something a client-side retry strategy can fix. Resend's API is a single
HTTPS POST on port 443 - the same port every other call in this codebase
already uses - and is immune to the entire class of problem.

Sends the assessment link / interview details only - never a candidate's
score or evaluation reasoning, which is recruiter-facing, not
candidate-facing.
"""

from __future__ import annotations

from datetime import datetime

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.models.tables import Candidate, JobDescription

logger = get_logger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
_REQUEST_TIMEOUT_SECONDS = 15.0


class _ResendServerError(RuntimeError):
    """A 5xx from Resend itself - worth a retry. A 4xx (bad request, bad key,
    sandbox restriction, rate limit) is not - it won't change on retry."""


def build_test_invite(
    candidate: Candidate, job: JobDescription, test_link: str
) -> tuple[str, str, str]:
    """Return ``(subject, text_body, html_body)`` for a test invitation.

    Short and professional; no mention of the candidate's score or any
    evaluation reasoning.
    """
    first_name = (candidate.name or "there").strip().split(" ")[0] or "there"
    from_label = get_settings().smtp_from_name
    subject = f"Next step: online assessment for the {job.title} role"

    text_body = (
        f"Hi {first_name},\n\n"
        f"Thanks for applying for the {job.title} role. We would like to invite "
        "you to complete a short online assessment as the next step in the "
        "process.\n\n"
        f"Assessment link: {test_link}\n\n"
        "Please complete it within 3 days of receiving this email.\n\n"
        "Best regards,\n"
        f"{from_label}"
    )

    html_body = f"""\
<html>
  <body style="font-family: Arial, Helvetica, sans-serif; color: #1a1a1a; line-height: 1.5;">
    <p>Hi {first_name},</p>
    <p>Thanks for applying for the <strong>{job.title}</strong> role. We would
       like to invite you to complete a short online assessment as the next
       step in the process.</p>
    <p>
      <a href="{test_link}"
         style="display:inline-block;padding:10px 20px;background:#2563eb;
                color:#ffffff;text-decoration:none;border-radius:6px;">
        Start the assessment
      </a>
    </p>
    <p>Or copy this link into your browser:<br><a href="{test_link}">{test_link}</a></p>
    <p>Please complete it within <strong>3 days</strong> of receiving this email.</p>
    <p>Best regards,<br>{from_label}</p>
  </body>
</html>"""

    return subject, text_body, html_body


def build_interview_invite(
    candidate: Candidate,
    job: JobDescription,
    start: datetime,
    end: datetime,
    tz: str,
    meet_link: str,
) -> tuple[str, str, str]:
    """Return ``(subject, text_body, html_body)`` for an interview
    invitation: candidate name, role, date/time with timezone, and the Meet
    link. No score or evaluation reasoning - that is recruiter-facing.
    """
    first_name = (candidate.name or "there").strip().split(" ")[0] or "there"
    from_label = get_settings().smtp_from_name
    when = f"{start.strftime('%A, %d %B %Y, %I:%M %p')} - {end.strftime('%I:%M %p')} ({tz})"
    subject = f"Interview scheduled: {job.title} role"

    text_body = (
        f"Hi {first_name},\n\n"
        f"Congratulations on progressing to the interview stage for the "
        f"{job.title} role. Your interview has been scheduled:\n\n"
        f"When: {when}\n"
        f"Where: Google Meet - {meet_link}\n\n"
        "Please join a few minutes early to test your camera and microphone.\n\n"
        "Best regards,\n"
        f"{from_label}"
    )

    html_body = f"""\
<html>
  <body style="font-family: Arial, Helvetica, sans-serif; color: #1a1a1a; line-height: 1.5;">
    <p>Hi {first_name},</p>
    <p>Congratulations on progressing to the interview stage for the
       <strong>{job.title}</strong> role. Your interview has been scheduled:</p>
    <p><strong>When:</strong> {when}<br>
       <strong>Where:</strong> Google Meet</p>
    <p>
      <a href="{meet_link}"
         style="display:inline-block;padding:10px 20px;background:#2563eb;
                color:#ffffff;text-decoration:none;border-radius:6px;">
        Join the interview
      </a>
    </p>
    <p>Or copy this link into your browser:<br><a href="{meet_link}">{meet_link}</a></p>
    <p>Please join a few minutes early to test your camera and microphone.</p>
    <p>Best regards,<br>{from_label}</p>
  </body>
</html>"""

    return subject, text_body, html_body


def _is_transient_resend_error(exc: BaseException) -> bool:
    """Retry a network-level hiccup reaching Resend or a 5xx from Resend
    itself - never a 4xx, which describes a request/account problem that
    retrying does not fix."""
    return isinstance(exc, (httpx.TransportError, _ResendServerError))


@retry(
    retry=retry_if_exception(_is_transient_resend_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, max=8),
    reraise=True,
)
def _post_to_resend(payload: dict, api_key: str) -> httpx.Response:
    resp = httpx.post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    if resp.status_code >= 500:
        raise _ResendServerError(f"Resend {resp.status_code}: {resp.text[:200]}")
    return resp


def send_email(
    to: str, subject: str, text_body: str, html_body: str
) -> tuple[bool, str | None, str | None]:
    """Send one email via Resend. Never raises. Returns
    ``(success, error, route)``.

    ``route`` is ``"resend:<email_id>"`` on success, ``None`` on failure.
    When ``settings.email_bcc`` is set, it is added as a real ``bcc``
    recipient in the Resend payload - a normal header-level Bcc, invisible to
    the ``to`` recipient, so a copy of every outgoing message can still be
    inspected during development.
    """
    settings = get_settings()
    if not settings.resend_api_key:
        return False, "Resend not configured (RESEND_API_KEY)", None

    payload: dict = {
        "from": settings.resend_from,
        "to": [to],
        "subject": subject,
        "text": text_body,
        "html": html_body,
    }
    bcc = (settings.email_bcc or "").strip()
    if bcc and bcc.lower() != to.strip().lower():
        payload["bcc"] = [bcc]

    try:
        resp = _post_to_resend(payload, settings.resend_api_key)
    except (httpx.TransportError, _ResendServerError) as exc:
        logger.exception("send_email: Resend unreachable for %s", to)
        return False, f"{type(exc).__name__}: {exc}", None

    if resp.status_code >= 400:
        detail = resp.text[:300]
        logger.error(
            "send_email: Resend rejected send to %s (%s): %s", to, resp.status_code, detail
        )
        return False, f"Resend {resp.status_code}: {detail}", None

    email_id = resp.json().get("id", "?")
    route = f"resend:{email_id}"
    logger.info("send_email: delivered to %s via %s", to, route)
    return True, None, route
