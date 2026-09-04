"""Outbound email via Gmail SMTP over STARTTLS (§4.6).

Plain smtplib, no template engine. Sends the assessment link only - never a
candidate's score or evaluation reasoning, which is recruiter-facing, not
candidate-facing.
"""

from __future__ import annotations

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.models.tables import Candidate, JobDescription

logger = get_logger(__name__)

#: Errors worth a retry - connection hiccups, not bad credentials.
_TRANSIENT_SMTP_ERRORS = (
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
    smtplib.SMTPHeloError,
    smtplib.SMTPResponseException,
    TimeoutError,
    ConnectionError,
    OSError,
)


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


@retry(
    retry=retry_if_exception_type(_TRANSIENT_SMTP_ERRORS),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, max=20),
    reraise=True,
)
def _send_via_smtp(msg: MIMEMultipart, envelope_recipients: list[str]) -> None:
    settings = get_settings()
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=25) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(settings.smtp_user, settings.smtp_password)
        server.sendmail(settings.smtp_user, envelope_recipients, msg.as_string())


def send_email(
    to: str, subject: str, text_body: str, html_body: str
) -> tuple[bool, str | None]:
    """Send one email. Never raises. Returns ``(success, error)``.

    Multipart plain+HTML (some clients block HTML). Retries transient SMTP
    errors; an auth failure is not retried since the credentials will not fix
    themselves on a second attempt. When ``settings.email_bcc`` is set, that
    address is added to the SMTP envelope recipients (so it receives a real
    copy) but never written into a visible header - the candidate's ``To`` is
    the only address they see.
    """
    settings = get_settings()
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_password):
        return False, "SMTP not configured (SMTP_HOST/SMTP_USER/SMTP_PASSWORD)"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_user}>"
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    envelope_recipients = [to]
    bcc = (settings.email_bcc or "").strip()
    if bcc and bcc.lower() != to.strip().lower():
        envelope_recipients.append(bcc)  # envelope only - no Bcc header written

    try:
        _send_via_smtp(msg, envelope_recipients)
        return True, None
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP auth failed sending to %s: %s", to, exc)
        return False, f"SMTP auth failed: {exc}"
    except Exception as exc:  # noqa: BLE001 - a failed send must never raise
        logger.exception("send_email failed for %s", to)
        return False, f"{type(exc).__name__}: {exc}"
