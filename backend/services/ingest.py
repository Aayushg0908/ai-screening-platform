"""Candidate file ingest (assignment §4.1, §2).

Turns an uploaded CSV/XLSX into normalized Candidate rows.

Design notes:
- Column names are matched via synonyms, not position, so recruiters can upload
  their own CSV layouts (§2 "support uploading similar CSV datasets dynamically").
- Unrecognised columns are preserved in `extra_fields` rather than dropped.
- A bad row is skipped with a warning; it never aborts the batch.
- Uploads are idempotent: the same file bytes return the existing batch.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re

import pandas as pd
from sqlmodel import Session, select

from backend.models.schemas import IngestReport
from backend.models.tables import Candidate, GitHubSource, UploadBatch

log = logging.getLogger(__name__)

# Canonical field -> accepted header synonyms (all normalized before matching).
COLUMN_SYNONYMS: dict[str, set[str]] = {
    "s_no": {"s_no", "sno", "sr_no", "serial", "serial_no", "id", "index"},
    "name": {"name", "full_name", "candidate_name", "student_name"},
    "email": {"email", "email_address", "mail", "email_id"},
    "college": {"college", "university", "institute", "institution"},
    "branch": {"branch", "department", "dept", "stream", "major"},
    "cgpa": {"cgpa", "gpa", "cgpa_10", "grade", "academic_score"},
    "best_ai_project": {
        "best_ai_project", "ai_project", "best_project", "project", "projects",
    },
    "research_work": {
        "research_work", "research", "publications", "papers", "research_paper",
    },
    "github": {"github", "github_profile", "github_url", "github_link", "git"},
    "resume": {"resume", "resume_link", "resume_url", "cv", "cv_link", "resume_drive"},
}

REQUIRED = {"name", "email"}

# Stale columns that appear in the candidate sheet but are authoritative only in
# the separate test-results upload (§4.7). Preserved, never used for scoring.
STALE_TEST_COLUMNS = {"test_la", "test_code"}

_GITHUB_RE = re.compile(
    r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)", re.I
)
_DRIVE_RE = re.compile(r"/file/d/([A-Za-z0-9_-]+)")
_RESERVED_GH = {"orgs", "topics", "search", "about", "features", "pricing"}


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------


def normalize_header(header: str) -> str:
    """'GitHub Profile ' -> 'github_profile'"""
    s = str(header).strip().lower()
    s = re.sub(r"[\s\-./]+", "_", s)
    return re.sub(r"[^a-z0-9_]", "", s).strip("_")


def map_columns(headers: list[str]) -> dict[str, str]:
    """Map source headers to canonical names. Returns {source: canonical}."""
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for raw in headers:
        norm = normalize_header(raw)
        for canonical, synonyms in COLUMN_SYNONYMS.items():
            if canonical in taken:
                continue
            if norm in synonyms:
                mapping[raw] = canonical
                taken.add(canonical)
                break
    missing = REQUIRED - taken
    if missing:
        raise ValueError(
            f"Missing required column(s): {', '.join(sorted(missing))}. "
            f"Found headers: {', '.join(str(h) for h in headers)}"
        )
    return mapping


def clean_text(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    return s or None


def normalize_college(value) -> str | None:
    """'netaji subhas university of technology ' -> title case, whitespace collapsed."""
    s = clean_text(value)
    if not s:
        return None
    s = re.sub(r"\s+", " ", s)
    return s if s.isupper() else s.title()


def parse_cgpa(value) -> float | None:
    """8.2200000000000006 -> 8.22. Tolerates strings and junk."""
    s = clean_text(value)
    if s is None:
        return None
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return round(float(m.group()), 2)
    except ValueError:
        return None


def drive_url_to_direct(url) -> str | None:
    """Drive share link -> downloadable link. Non-Drive URLs pass through."""
    s = clean_text(url)
    if not s:
        return None
    m = _DRIVE_RE.search(s)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    if "drive.google.com" in s and "id=" in s:
        fid = re.search(r"id=([A-Za-z0-9_-]+)", s)
        if fid:
            return f"https://drive.google.com/uc?export=download&id={fid.group(1)}"
    return s


def extract_github_username(url) -> str | None:
    s = clean_text(url)
    if not s:
        return None
    m = _GITHUB_RE.search(s)
    if not m:
        return None
    user = m.group(1)
    return None if user.lower() in _RESERVED_GH else user


def find_github_in_text(*texts) -> str | None:
    """Fallback: some candidates put their GitHub link inside a free-text field."""
    for t in texts:
        user = extract_github_username(t)
        if user:
            return user
    return None


# --------------------------------------------------------------------------
# file reading
# --------------------------------------------------------------------------


def read_table(
    content: bytes, filename: str, sheet_name: str | None = None
) -> tuple[pd.DataFrame, str | None, list[str]]:
    """Return (dataframe, sheet_used, available_sheets)."""
    lower = filename.lower()
    if lower.endswith(".csv"):
        return pd.read_csv(io.BytesIO(content)), None, []
    if lower.endswith((".xlsx", ".xls")):
        book = pd.ExcelFile(io.BytesIO(content))
        sheets = list(book.sheet_names)
        chosen = sheet_name or sheets[0]
        if chosen not in sheets:
            raise ValueError(f"Sheet {chosen!r} not found. Available: {sheets}")
        return book.parse(chosen), chosen, sheets
    raise ValueError(f"Unsupported file type: {filename}. Use .csv, .xlsx or .xls")


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------


def ingest_file(
    session: Session,
    content: bytes,
    filename: str,
    sheet_name: str | None = None,
) -> IngestReport:
    file_hash = hashlib.sha256(content).hexdigest()

    existing = session.exec(
        select(UploadBatch).where(UploadBatch.file_hash == file_hash)
    ).first()
    if existing:
        log.info("Duplicate upload %s -> batch %s", filename, existing.batch_id)
        return IngestReport(
            batch_id=existing.batch_id,
            filename=existing.filename,
            sheet=existing.sheet_name,
            total_rows=existing.row_count,
            inserted=0,
            warnings=["Identical file already uploaded; returning existing batch."],
        )

    df, sheet_used, available = read_table(content, filename, sheet_name)
    mapping = map_columns(list(df.columns))  # raises ValueError if name/email absent

    unmapped = [str(c) for c in df.columns if c not in mapping]
    warnings: list[str] = []
    if stale := [c for c in unmapped if normalize_header(c) in STALE_TEST_COLUMNS]:
        warnings.append(
            f"Ignored stale test score column(s) {stale} - upload test results "
            f"separately via /results/upload."
        )

    batch = UploadBatch(
        filename=filename, file_hash=file_hash, sheet_name=sheet_used, row_count=len(df)
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)

    inserted = 0
    for i, row in df.iterrows():
        try:
            vals = {canon: row.get(src) for src, canon in mapping.items()}

            name = clean_text(vals.get("name"))
            email = clean_text(vals.get("email"))
            if not name or not email:
                warnings.append(f"Row {i + 1}: skipped, missing name or email.")
                continue

            s_no = parse_cgpa(vals.get("s_no"))
            s_no = int(s_no) if s_no is not None else i + 1

            research = clean_text(vals.get("research_work"))
            project = clean_text(vals.get("best_ai_project"))

            github_url = clean_text(vals.get("github"))
            username = extract_github_username(github_url)
            source = GitHubSource.COLUMN if username else GitHubSource.NONE
            if not username:
                # Some candidates paste their GitHub link into a text field.
                username = find_github_in_text(research, project)
                if username:
                    github_url = f"https://github.com/{username}"
                    source = GitHubSource.TEXT
                    warnings.append(
                        f"Row {i + 1}: GitHub recovered from text -> {username}"
                    )
                else:
                    warnings.append(f"Row {i + 1}: no GitHub profile found.")

            resume_url = clean_text(vals.get("resume"))

            session.add(
                Candidate(
                    batch_id=batch.batch_id,
                    s_no=s_no,
                    name=name,
                    email=email,
                    college=normalize_college(vals.get("college")),
                    branch=clean_text(vals.get("branch")),
                    cgpa=parse_cgpa(vals.get("cgpa")),
                    best_ai_project=project,
                    research_work=research,
                    github_url=github_url,
                    github_username=username,
                    github_source=source,
                    resume_url=resume_url,
                    resume_direct_url=drive_url_to_direct(resume_url),
                    extra_fields={
                        c: clean_text(row.get(c)) for c in unmapped
                    },
                )
            )
            inserted += 1
        except Exception as exc:  # one bad row must not abort the batch
            log.warning("Row %s failed: %s", i + 1, exc)
            warnings.append(f"Row {i + 1}: skipped ({exc}).")

    session.commit()
    log.info("Batch %s: inserted %s/%s rows", batch.batch_id, inserted, len(df))

    return IngestReport(
        batch_id=batch.batch_id,
        filename=filename,
        sheet=sheet_used,
        available_sheets=available,
        total_rows=len(df),
        inserted=inserted,
        column_mapping={str(k): v for k, v in mapping.items()},
        unmapped_columns=unmapped,
        warnings=warnings,
    )