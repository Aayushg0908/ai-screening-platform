"""Resume processing (assignment §4.2).

Download each candidate's resume PDF from its Google Drive link and extract the
raw text. That is the whole job: no regex skill/experience parsing happens here
— structured extraction is the LLM's job in Phase 3.

Drive is awkward on purpose:
- it serves HTML pages (an "is this you?" / virus-scan interstitial, or a
  "not shared" notice) with HTTP 200, so the response body must be sniffed for
  ``%PDF`` rather than trusting the content type;
- larger files need a second request carrying a ``confirm`` token.

Nothing in this module raises. Every failure is returned as a string so one bad
link never aborts a batch. PDFs are never written to disk (hosts have ephemeral
filesystems) — only the extracted text lands in ``ResumeText``.
"""

from __future__ import annotations

import asyncio
import io
import re
import unicodedata

import httpx
import pypdf
from sqlmodel import Session, select
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from backend.core.logging import get_logger
from backend.models.schemas import ResumeItem, ResumeReport
from backend.models.tables import Candidate, ResumeText

logger = get_logger(__name__)

MAX_PDF_BYTES = 20 * 1024 * 1024
FETCH_TIMEOUT = 30.0
MIN_TEXT_CHARS = 100
CONCURRENCY = 5

_DRIVE_PATH_ID_RE = re.compile(r"/file/d/([A-Za-z0-9_-]+)")
_DRIVE_QUERY_ID_RE = re.compile(r"[?&]id=([A-Za-z0-9_-]+)")
_CONFIRM_RE = re.compile(r"confirm=([0-9A-Za-z_-]+)")
_UUID_RE = re.compile(r"uuid=([0-9A-Za-z_-]+)")
_INLINE_WS_RE = re.compile(r"[ \t\r\f\v]+")
_EDGE_WS_RE = re.compile(r" *\n *")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

HTML_ERROR = "Drive returned HTML — link may not be publicly shared"
TOO_LARGE_ERROR = "File too large (>20MB)"

# Résumé PDFs exported from Word / LaTeX routinely embed *subsetted* fonts whose
# ToUnicode CMap is broken or absent. pypdf then recovers each ligature glyph as
# whatever codepoint the font's private encoding happened to reuse, so "ti", "tt",
# "tf", "ft" and the bullet come back as unrelated Latin-Extended / PUA letters.
# This table maps the mis-decoded glyphs seen across the sample set back to the
# characters they actually stand for; presentation-form ligatures (ﬁ ﬂ ﬀ ﬃ ﬄ)
# are handled separately by NFKD normalisation.
_GLYPH_FIXES = str.maketrans(
    {
        "Ɵ": "ti",  # Ɵ  LATIN CAPITAL LETTER O WITH MIDDLE TILDE
        "Ʃ": "tt",  # Ʃ  LATIN CAPITAL LETTER ESH
        "ƞ": "tf",  # ƞ  LATIN SMALL LETTER N WITH LONG RIGHT LEG
        "Ō": "ft",  # Ō  LATIN CAPITAL LETTER O WITH MACRON
        "": "- ",  # PUA Symbol-font bullet (U+F0B7)
    }
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def is_pdf_bytes(data: bytes) -> bool:
    """True when ``data`` begins with the ``%PDF`` magic number."""
    return bool(data) and data[:4] == b"%PDF"


def _drive_file_id(url: str) -> str | None:
    """Pull the Drive file id out of a ``/file/d/{id}`` or ``?id={id}`` URL."""
    match = _DRIVE_PATH_ID_RE.search(url) or _DRIVE_QUERY_ID_RE.search(url)
    return match.group(1) if match else None


def extract_confirm_url(html: str, file_id: str) -> str | None:
    """Build the follow-up download URL from a Drive interstitial page.

    Returns ``None`` when the page carries no ``confirm`` token.
    """
    if not html:
        return None
    token_match = _CONFIRM_RE.search(html)
    if not token_match:
        return None
    token = token_match.group(1)
    uuid_match = _UUID_RE.search(html)
    if uuid_match:
        return (
            "https://drive.usercontent.google.com/download"
            f"?id={file_id}&export=download&confirm={token}&uuid={uuid_match.group(1)}"
        )
    return (
        f"https://drive.google.com/uc?export=download&confirm={token}&id={file_id}"
    )


def extract_text(data: bytes) -> tuple[str | None, str | None]:
    """Extract text from PDF bytes with pypdf. Returns ``(text, error)``.

    Pages are joined with a blank line and whitespace runs are collapsed. If the
    result is shorter than 100 characters the text is still returned, paired with
    a warning that the PDF is probably scanned / image-based.
    """
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 - never raise
        logger.warning("pypdf failed: %s", exc)
        return None, f"PDF parse failed: {exc}"

    text = "\n\n".join(pages)
    # Repair glyphs mis-decoded from broken font CMaps, then fold the standard
    # presentation-form ligatures (ﬁ ﬂ ﬀ ﬃ ﬄ) into plain ASCII. Both run before
    # the whitespace collapse so the substitutions can introduce spaces freely.
    text = text.translate(_GLYPH_FIXES)
    text = unicodedata.normalize("NFKD", text)
    text = _INLINE_WS_RE.sub(" ", text)
    text = _EDGE_WS_RE.sub("\n", text)
    text = _BLANK_LINES_RE.sub("\n\n", text).strip()

    if len(text) < MIN_TEXT_CHARS:
        return text, (
            f"Extracted only {len(text)} chars — PDF may be scanned/image-based"
        )
    return text, None


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, max=8),
    reraise=True,
)
async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET with redirect following; retried on transport errors only."""
    return await client.get(url, follow_redirects=True, timeout=FETCH_TIMEOUT)


async def fetch_pdf(
    client: httpx.AsyncClient, url: str
) -> tuple[bytes | None, str | None]:
    """Download a resume PDF. Returns ``(pdf_bytes, error)`` and never raises."""
    if not url:
        return None, "no resume URL"

    try:
        response = await _get(client, url)
    except httpx.TransportError as exc:
        return None, f"network error: {exc}"
    except Exception as exc:  # noqa: BLE001 - never raise
        logger.warning("fetch failed for %s: %s", url, exc)
        return None, f"fetch failed: {exc}"

    data = response.content
    if len(data) > MAX_PDF_BYTES:
        return None, TOO_LARGE_ERROR
    if is_pdf_bytes(data):
        return data, None

    # HTML interstitial: try the confirm-token follow-up exactly once.
    file_id = _drive_file_id(str(response.url)) or _drive_file_id(url)
    confirm_url = extract_confirm_url(response.text, file_id) if file_id else None
    if confirm_url:
        try:
            response = await _get(client, confirm_url)
        except httpx.TransportError as exc:
            return None, f"network error: {exc}"
        except Exception as exc:  # noqa: BLE001 - never raise
            return None, f"fetch failed: {exc}"
        data = response.content
        if len(data) > MAX_PDF_BYTES:
            return None, TOO_LARGE_ERROR
        if is_pdf_bytes(data):
            return data, None

    return None, HTML_ERROR


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------


async def _fetch_and_extract(
    client: httpx.AsyncClient, url: str
) -> tuple[str, int, str | None, str | None]:
    """Return ``(status, chars, text, error)`` for one resume URL."""
    data, fetch_error = await fetch_pdf(client, url)
    if data is None:
        return "failed", 0, None, fetch_error

    text, extract_error = extract_text(data)
    if text is None:
        return "failed", 0, None, extract_error
    if extract_error:  # text kept, but under the usable threshold
        return "empty", len(text), text, extract_error
    return "ok", len(text), text, None


def _replace_resume_row(
    session: Session, candidate_id: int, text: str | None, error: str | None
) -> None:
    """Drop any prior ResumeText rows for the candidate and insert a fresh one."""
    for old in session.exec(
        select(ResumeText).where(ResumeText.candidate_id == candidate_id)
    ).all():
        session.delete(old)
    session.add(ResumeText(candidate_id=candidate_id, text=text, error=error))


async def process_batch(
    session: Session, batch_id: int, force: bool = False
) -> ResumeReport:
    """Download + extract resumes for every candidate in ``batch_id``.

    Candidates with an existing successful :class:`ResumeText` row are left
    untouched unless ``force`` is set — Phase 3 and Phase 5 re-run this often and
    must not re-download. Fetches run five at a time; each result is written and
    committed on its own so a failure is recorded, never raised.
    """
    candidates = list(
        session.exec(
            select(Candidate)
            .where(Candidate.batch_id == batch_id)
            .where(Candidate.resume_direct_url.is_not(None))
            .order_by(Candidate.s_no)
        ).all()
    )

    latest: dict[int, ResumeText] = {}
    candidate_ids = [c.candidate_id for c in candidates]
    if candidate_ids:
        for row in session.exec(
            select(ResumeText).where(ResumeText.candidate_id.in_(candidate_ids))
        ).all():
            current = latest.get(row.candidate_id)
            if current is None or (row.id or 0) > (current.id or 0):
                latest[row.candidate_id] = row

    items: list[ResumeItem] = []
    to_process: list[Candidate] = []
    for candidate in candidates:
        prior = latest.get(candidate.candidate_id)
        if not force and prior is not None and prior.text is not None:
            items.append(
                ResumeItem(
                    candidate_id=candidate.candidate_id,
                    s_no=candidate.s_no,
                    name=candidate.name,
                    status="skipped",
                    chars=len(prior.text),
                    error=prior.error,
                )
            )
            continue
        to_process.append(candidate)

    if to_process:
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def run(candidate: Candidate) -> tuple[Candidate, tuple]:
            async with semaphore:
                return candidate, await _fetch_and_extract(
                    client, candidate.resume_direct_url
                )

        async with httpx.AsyncClient(
            follow_redirects=True, timeout=FETCH_TIMEOUT
        ) as client:
            tasks = [asyncio.create_task(run(c)) for c in to_process]
            for future in asyncio.as_completed(tasks):
                candidate, (status, chars, text, error) = await future
                _replace_resume_row(session, candidate.candidate_id, text, error)
                session.commit()
                items.append(
                    ResumeItem(
                        candidate_id=candidate.candidate_id,
                        s_no=candidate.s_no,
                        name=candidate.name,
                        status=status,
                        chars=chars,
                        error=error,
                    )
                )
                logger.info(
                    "resume s_no=%s %s (%s chars)%s",
                    candidate.s_no,
                    status,
                    chars,
                    f" — {error}" if error else "",
                )

    items.sort(key=lambda item: item.s_no)
    succeeded = sum(1 for item in items if item.status == "ok")
    skipped = sum(1 for item in items if item.status == "skipped")
    failed = len(items) - succeeded - skipped

    return ResumeReport(
        batch_id=batch_id,
        total=len(items),
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        items=items,
    )
