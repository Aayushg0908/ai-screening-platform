"""Outbound email via SMTP (§4.6) - configured for Brevo's relay.

Plain smtplib, no template engine. Sends the assessment link / interview
details only - never a candidate's score or evaluation reasoning, which is
recruiter-facing, not candidate-facing.

History, because the choice of provider here is load-bearing: Gmail SMTP
failed from Render's egress almost entirely (0/9 in a clean test, 1/13
overall) with "OSError: [Errno 101] Network is unreachable" on every
resolved address, on BOTH port 587 (STARTTLS) and port 465 (implicit SSL) -
so it isn't one bad IP or one blocked port, Render's network can't reach
Gmail's mail infrastructure at all. Switching briefly to Resend's HTTP API
avoided that class of problem entirely (port 443 is never blocked) but its
sandbox mode only delivers to the account's own address without a verified
domain - unusable for sending to arbitrary candidates. Brevo's SMTP relay
(``smtp-relay.brevo.com``) is a single stable host, not a round-robin pool of
frontend IPs, and needs only single-sender verification (one email address)
rather than full domain verification to send to any recipient - the
combination this project actually needs.

``_send_via_smtp`` still resolves the host once and tries every distinct
address directly (keeping the real hostname for TLS SNI/certificate
validation), and still tries an implicit-SSL port as a fallback if the
configured port is the one being blocked. That machinery is kept
provider-agnostic and cost nothing to keep - if 587 ever gets blocked
somewhere, 465 is already wired in, and if Gmail becomes viable again, only
config changes, not code, are needed. The whole hunt is capped by both an
attempt count and a wall-clock deadline so a failed send reports back in
seconds, not the ~82s a blind exponential-backoff retry on a single dead
route used to take.
"""

from __future__ import annotations

import smtplib
import socket
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.models.tables import Candidate, JobDescription

logger = get_logger(__name__)

#: Socket/connection-level errors worth trying the next address or port -
#: never an auth failure, which will not fix itself anywhere else.
_TRANSIENT_CONNECT_ERRORS = (
    OSError,  # covers ConnectionError, socket.gaierror, "Network unreachable", etc.
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
    smtplib.SMTPHeloError,
)

#: Bound the whole multi-address/multi-port hunt - a recruiter is watching.
_MAX_ADDRESSES_PER_PORT = 4
_TOTAL_DEADLINE_SECONDS = 30.0
_CONNECT_TIMEOUT_SECONDS = 8.0


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


def _resolve_addresses(host: str) -> list[str]:
    """Every distinct IP ``host`` resolves to, IPv4 first (capped).

    A single stable relay host (Brevo's ``smtp-relay.brevo.com``) typically
    resolves to far fewer addresses than Gmail's round-robin pool did, but
    the same defence costs nothing to keep: IPv4 is tried before IPv6
    deliberately, since a common cloud-egress gap is no IPv6 route at all,
    which presents as "Network is unreachable" on every IPv6 address while
    IPv4 to the same host works fine.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        logger.error("mailer: could not resolve %s: %s", host, exc)
        return []
    ipv4: list[str] = []
    ipv6: list[str] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        ip = sockaddr[0]
        bucket = ipv4 if family == socket.AF_INET else ipv6
        if ip not in bucket:
            bucket.append(ip)
    return (ipv4 + ipv6)[:_MAX_ADDRESSES_PER_PORT]


def _connect_starttls(ip: str, port: int, host: str) -> smtplib.SMTP:
    """Connect to ``ip`` directly but keep ``host`` for TLS SNI/certificate
    validation - the relay's cert covers the hostname, not a raw IP.

    ``smtplib`` records ``server_hostname`` from ``self._host``, which is set
    once at construction time from the constructor's ``host`` argument and is
    NOT overwritten by a later ``connect(ip, port)`` call - so constructing
    with no host, then setting ``_host`` ourselves, then connecting to the IP,
    gives exactly "connect here, validate TLS against that" with no public
    API needed for it.
    """
    server = smtplib.SMTP(timeout=_CONNECT_TIMEOUT_SECONDS)
    server._host = host  # noqa: SLF001 - documented smtplib behaviour, not a hack around it
    server.connect(ip, port)
    server.ehlo()
    server.starttls()
    server.ehlo()
    return server


def _connect_ssl(ip: str, port: int, host: str) -> smtplib.SMTP_SSL:
    """Implicit-SSL counterpart of :func:`_connect_starttls`, same reasoning."""
    server = smtplib.SMTP_SSL(timeout=_CONNECT_TIMEOUT_SECONDS)
    server._host = host  # noqa: SLF001
    server.connect(ip, port)
    server.ehlo()
    return server


def _send_via_smtp(msg: MIMEMultipart, envelope_recipients: list[str]) -> str:
    """Send one message, hunting across ports and resolved IPs for a route
    this network can actually reach. Returns ``"host:port via ip"`` for the
    caller to log/report - which route actually worked matters for diagnosing
    a cloud host's egress restrictions.

    An auth failure raises immediately - different credentials will not
    change the outcome. A connection failure moves on to the next resolved
    address, then the next port (the configured one first, then the
    implicit-SSL fallback), bounded by both an attempt cap and a ~30s
    wall-clock deadline. If every route is exhausted, raises the last
    connection error with a summary of what was tried.
    """
    settings = get_settings()
    host = settings.smtp_host
    port_plan = [
        (settings.smtp_port, _connect_starttls),
        (settings.smtp_port_fallback, _connect_ssl),
    ]

    deadline = time.monotonic() + _TOTAL_DEADLINE_SECONDS
    last_error: Exception | None = None
    tried: list[str] = []

    for port, connector in port_plan:
        for ip in _resolve_addresses(host):
            if time.monotonic() >= deadline:
                logger.warning(
                    "mailer: %.0fs deadline reached hunting for a route to "
                    "%s - tried %s",
                    _TOTAL_DEADLINE_SECONDS, host, ", ".join(tried) or "nothing",
                )
                raise last_error or TimeoutError(
                    f"no reachable route to {host} within "
                    f"{_TOTAL_DEADLINE_SECONDS:.0f}s (tried {', '.join(tried)})"
                )

            tried.append(f"{ip}:{port}")
            try:
                with connector(ip, port, host) as server:
                    server.login(settings.smtp_user, settings.smtp_password)
                    server.sendmail(
                        settings.smtp_from, envelope_recipients, msg.as_string()
                    )
                route = f"{host}:{port} via {ip}"
                logger.info("mailer: sent via %s", route)
                return route
            except smtplib.SMTPAuthenticationError:
                raise  # credentials, not a route - trying elsewhere won't help
            except _TRANSIENT_CONNECT_ERRORS as exc:
                logger.warning(
                    "mailer: %s:%s (port %s) unreachable: %s - trying next",
                    ip, host, port, exc,
                )
                last_error = exc
                continue

    logger.error(
        "mailer: exhausted every route to %s - tried %s", host, ", ".join(tried)
    )
    raise last_error or OSError(
        f"no reachable route to {host} (tried {', '.join(tried) or 'nothing - DNS resolution failed'})"
    )


def send_email(
    to: str, subject: str, text_body: str, html_body: str
) -> tuple[bool, str | None, str | None]:
    """Send one email. Never raises. Returns ``(success, error, route)``.

    ``route`` is the ``"host:port via ip"`` that worked, or ``None`` on
    failure. Multipart plain+HTML (some clients block HTML). When
    ``settings.email_bcc`` is set, that address is added to the SMTP envelope
    recipients (so it receives a real copy) but never written into a visible
    header - the candidate's ``To`` is the only address they see.

    ``settings.smtp_from`` (NOT ``settings.smtp_user``) is used for both the
    envelope sender and the visible ``From`` header: with a relay like
    Brevo, the SMTP login is an account identifier, not necessarily a
    deliverable mailbox, while the verified single-sender address you're
    actually allowed to send as is a separate value.
    """
    settings = get_settings()
    if not (
        settings.smtp_host
        and settings.smtp_user
        and settings.smtp_password
        and settings.smtp_from
    ):
        return (
            False,
            "SMTP not configured (SMTP_HOST/SMTP_USER/SMTP_PASSWORD/SMTP_FROM)",
            None,
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_from}>"
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
        route = _send_via_smtp(msg, envelope_recipients)
        return True, None, route
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP auth failed sending to %s: %s", to, exc)
        return False, f"SMTP auth failed: {exc}", None
    except Exception as exc:  # noqa: BLE001 - a failed send must never raise
        logger.exception("send_email failed for %s", to)
        return False, f"{type(exc).__name__}: {exc}", None
