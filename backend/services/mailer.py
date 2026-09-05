"""Outbound email (§4.6) - Brevo, via either its HTTPS API or its SMTP relay.

No template engine. Sends the assessment link / interview details only -
never a candidate's score or evaluation reasoning, which is recruiter-facing,
not candidate-facing.

History, because the choice of transport here is load-bearing: two
independent SMTP providers (Gmail, then Brevo) both failed from Render on
both submission ports (587 STARTTLS, 465 implicit SSL) while both worked
fine from a residential connection - Gmail with an instant routing error
("Network is unreachable" on every resolved address), Brevo by hanging until
timeout despite its IP-authorization whitelist matching Render's documented
egress ranges exactly. Different symptom, same platform, same ports:
Render blocks outbound SMTP at the network level, so no destination-side fix
(provider, port, IP whitelist) can help. Resend's HTTP API avoided that
class of problem entirely (port 443 is never blocked) but its sandbox mode
only delivers to the account's own address without a verified domain -
unusable for arbitrary candidates, and domain verification wasn't an option
here. Brevo's own HTTPS API (``api.brevo.com``) gets the best of both: the
same account, same verified single sender, same "send to anyone" capability
as its SMTP relay, just reached over HTTPS instead of raw SMTP sockets - the
same port Groq, GitHub, and Google Calendar already use successfully from
Render.

``EMAIL_TRANSPORT`` selects between them: ``"api"`` (default - what the
deployed service uses) or ``"smtp"`` (kept fully working, useful locally and
as a demonstrated fallback with zero code changes if this platform's
restriction ever lifts). Only the transport changes; the sender identity
(``SMTP_FROM``/``SMTP_FROM_NAME``) and Bcc behaviour are shared and identical
either way.

The ``smtp`` path still resolves the host once and tries every distinct
address directly (keeping the real hostname for TLS SNI/certificate
validation), and still tries an implicit-SSL port as a fallback if the
configured port is the one being blocked. That machinery is provider-agnostic
and capped by both an attempt count and a wall-clock deadline so a failed
send reports back in seconds, not the ~82s a blind exponential-backoff retry
on a single dead route used to take.
"""

from __future__ import annotations

import smtplib
import socket
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.models.tables import Candidate, JobDescription

logger = get_logger(__name__)

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
_API_REQUEST_TIMEOUT_SECONDS = 15.0

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


class _BrevoServerError(RuntimeError):
    """A 5xx from Brevo's API itself - worth a retry. A 4xx (bad key, bad
    request, an IP-authorization rule that also applies to API calls) is
    not - it will not change on retry."""


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


def _send_via_smtp_transport(
    to: str, subject: str, text_body: str, html_body: str
) -> tuple[bool, str | None, str | None]:
    """The ``"smtp"`` transport: send via the multi-address/multi-port hunt
    above. Never raises. Returns ``(success, error, route)`` with ``route``
    prefixed ``"smtp:"`` so it's visible which transport handled the send.

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
        return True, None, f"smtp:{route}"
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP auth failed sending to %s: %s", to, exc)
        return False, f"SMTP auth failed: {exc}", None
    except Exception as exc:  # noqa: BLE001 - a failed send must never raise
        logger.exception("send_email (smtp) failed for %s", to)
        return False, f"{type(exc).__name__}: {exc}", None


def _is_transient_brevo_error(exc: BaseException) -> bool:
    """Retry a network-level hiccup reaching Brevo's API or a 5xx from Brevo
    itself - never a 4xx, which describes a request/account problem that
    retrying does not fix."""
    return isinstance(exc, (httpx.TransportError, _BrevoServerError))


@retry(
    retry=retry_if_exception(_is_transient_brevo_error),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, max=8),
    reraise=True,
)
def _post_to_brevo(payload: dict, api_key: str) -> httpx.Response:
    resp = httpx.post(
        BREVO_API_URL,
        headers={"api-key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=_API_REQUEST_TIMEOUT_SECONDS,
    )
    if resp.status_code >= 500:
        raise _BrevoServerError(f"Brevo {resp.status_code}: {resp.text[:200]}")
    return resp


def _send_via_brevo_api(
    to: str, subject: str, text_body: str, html_body: str
) -> tuple[bool, str | None, str | None]:
    """The ``"api"`` transport (default): send via Brevo's HTTPS API instead
    of raw SMTP sockets - immune to a platform blocking outbound SMTP ports,
    since it's just another HTTPS call like the ones already made to Groq,
    GitHub, and Google Calendar. Same account, same verified sender, same
    "send to anyone" capability as the SMTP transport - only the wire
    protocol differs. Never raises. Returns ``(success, error, route)`` with
    ``route`` prefixed ``"brevo-api:"``.

    Note: Brevo's "Authorized IPs" setting has been observed to apply to the
    SMTP relay; if it also gates the API and the calling IP isn't
    authorized, Brevo returns a 401 here - visible in ``error``, not a silent
    hang like the SMTP transport's timeout.
    """
    settings = get_settings()
    if not settings.brevo_api_key:
        return False, "Brevo API not configured (BREVO_API_KEY)", None
    if not (settings.smtp_from and settings.smtp_from_name):
        return False, "Sender not configured (SMTP_FROM/SMTP_FROM_NAME)", None

    payload: dict = {
        "sender": {"name": settings.smtp_from_name, "email": settings.smtp_from},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html_body,
        "textContent": text_body,
    }
    bcc = (settings.email_bcc or "").strip()
    if bcc and bcc.lower() != to.strip().lower():
        payload["bcc"] = [{"email": bcc}]

    try:
        resp = _post_to_brevo(payload, settings.brevo_api_key)
    except (httpx.TransportError, _BrevoServerError) as exc:
        logger.exception("send_email (api): Brevo unreachable for %s", to)
        return False, f"{type(exc).__name__}: {exc}", None

    if resp.status_code >= 400:
        detail = resp.text[:300]
        logger.error(
            "send_email (api): Brevo rejected send to %s (%s): %s",
            to, resp.status_code, detail,
        )
        return False, f"Brevo {resp.status_code}: {detail}", None

    message_id = resp.json().get("messageId", "?")
    route = f"brevo-api:{message_id}"
    logger.info("send_email: delivered to %s via %s", to, route)
    return True, None, route


def send_email(
    to: str, subject: str, text_body: str, html_body: str
) -> tuple[bool, str | None, str | None]:
    """Send one email via ``settings.email_transport`` (``"api"`` by
    default, or ``"smtp"``). Never raises. Returns ``(success, error,
    route)`` - ``route`` names which transport handled it
    (``"brevo-api:..."`` or ``"smtp:..."``), or ``None`` on failure.

    When ``settings.email_bcc`` is set, that address receives a real copy
    (a Brevo API ``bcc`` recipient, or an SMTP envelope recipient) without
    ever appearing in a visible header - the candidate's ``To`` is the only
    address they see, either way.
    """
    settings = get_settings()
    transport = (settings.email_transport or "api").strip().lower()
    if transport == "smtp":
        return _send_via_smtp_transport(to, subject, text_body, html_body)
    if transport != "api":
        logger.warning(
            "send_email: unknown EMAIL_TRANSPORT %r, defaulting to 'api'", transport
        )
    return _send_via_brevo_api(to, subject, text_body, html_body)
