"""Test-results ingest and the final (post-test) scoring pass (§4.7).

CRITICAL JOIN RULE: the test-results file uses a DIFFERENT email plus-tag
(``+assignment``) than the candidate file (``+arnav``) - joining on email
matches ZERO rows. The join key is ``s_no``, scoped to ``batch_id``. Never
email, never name.

Each upload is authoritative and complete for its batch: every candidate in
the batch gets a :class:`TestResult` row - RECEIVED (matched in the file) or
NO_RESULT (absent, e.g. s_no 4 and 10) - never dropped, never imputed to
zero. A row whose s_no isn't in the batch (e.g. s_no 99) is skipped with a
warning rather than creating an orphan row. Re-uploading a batch's results
reconciles all of its candidates again, so re-running an upload is safe.
"""

from __future__ import annotations

import logging
import re

from sqlmodel import Session, select

from backend.models.schemas import (
    FinalResultItem,
    FinalResultsOut,
    FinalShortlistRequest,
    FinalWeights,
    ResultsReport,
    TestResultOut,
)
from backend.models.tables import Candidate, Evaluation, TestResult, TestStatus
from backend.services import scoring
from backend.services.ingest import clean_text, normalize_header, read_table

log = logging.getLogger(__name__)

# Canonical field -> accepted header synonyms (all normalized before matching).
COLUMN_SYNONYMS: dict[str, set[str]] = {
    "s_no": {"s_no", "sno", "sr_no", "serial", "serial_no", "id", "index"},
    "test_la": {
        "test_la",
        "logical_aptitude_score",
        "aptitude",
        "la_score",
        "logical",
    },
    "test_code": {
        "test_code",
        "coding_test_score",
        "coding",
        "code_score",
        "programming",
    },
}
REQUIRED = {"s_no", "test_la", "test_code"}


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------


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


def parse_score(value: object) -> float | None:
    """Tolerate str/float/int/None/NaN raw marks. A null score is not a zero
    - only a genuinely absent value returns ``None``; a literal ``"0"`` still
    parses to ``0.0``."""
    s = clean_text(value)
    if s is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return round(float(m.group()), 2)
    except ValueError:
        return None


def parse_s_no(value: object) -> int | None:
    s = clean_text(value)
    if s is None:
        return None
    m = re.search(r"-?\d+", s)
    if not m:
        return None
    try:
        return int(m.group())
    except ValueError:
        return None


# --------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------


def ingest_results(
    session: Session,
    content: bytes,
    filename: str,
    batch_id: int,
    sheet_name: str | None = None,
) -> ResultsReport:
    df, sheet_used, available = read_table(content, filename, sheet_name)
    mapping = map_columns(list(df.columns))  # raises ValueError if a required column is missing

    candidates = session.exec(
        select(Candidate).where(Candidate.batch_id == batch_id)
    ).all()
    if not candidates:
        raise ValueError(f"batch {batch_id} has no candidates - upload it first.")
    by_sno = {c.s_no: c for c in candidates}

    existing_results = {
        r.candidate_id: r
        for r in session.exec(
            select(TestResult).where(
                TestResult.candidate_id.in_([c.candidate_id for c in candidates])
            )
        ).all()
    }

    warnings: list[str] = []
    matched = 0
    skipped_unknown = 0
    seen_candidate_ids: set[int] = set()

    for i, row in df.iterrows():
        vals = {canon: row.get(src) for src, canon in mapping.items()}
        s_no = parse_s_no(vals.get("s_no"))
        if s_no is None:
            warnings.append(f"Row {i + 1}: invalid or missing s_no - skipped.")
            continue

        candidate = by_sno.get(s_no)
        if candidate is None:
            skipped_unknown += 1
            warnings.append(
                f"Row {i + 1}: s_no {s_no} not found in batch {batch_id} - skipped "
                "(orphan row, not created)."
            )
            continue

        test_la = parse_score(vals.get("test_la"))
        test_code = parse_score(vals.get("test_code"))
        if test_la is None:
            warnings.append(
                f"Row {i + 1} (s_no {s_no}): test_la is null - recorded as missing, "
                "not zero."
            )
        if test_code is None:
            warnings.append(
                f"Row {i + 1} (s_no {s_no}): test_code is null - recorded as missing, "
                "not zero."
            )

        existing = existing_results.get(candidate.candidate_id)
        if existing:
            existing.test_la = test_la
            existing.test_code = test_code
            existing.status = TestStatus.RECEIVED
            session.add(existing)
        else:
            session.add(
                TestResult(
                    candidate_id=candidate.candidate_id,
                    test_la=test_la,
                    test_code=test_code,
                    status=TestStatus.RECEIVED,
                )
            )
        matched += 1
        seen_candidate_ids.add(candidate.candidate_id)

    # Every candidate in the batch not seen in this upload -> explicit
    # NO_RESULT. Never dropped, never imputed to zero (dataset trap: s_no 4
    # and 10 are absent from the Test Result sheet).
    no_result = 0
    for candidate in candidates:
        if candidate.candidate_id in seen_candidate_ids:
            continue
        existing = existing_results.get(candidate.candidate_id)
        if existing:
            existing.test_la = None
            existing.test_code = None
            existing.status = TestStatus.NO_RESULT
            session.add(existing)
        else:
            session.add(
                TestResult(
                    candidate_id=candidate.candidate_id,
                    test_la=None,
                    test_code=None,
                    status=TestStatus.NO_RESULT,
                )
            )
        no_result += 1

    session.commit()
    log.info(
        "Results upload batch=%s: matched=%s no_result=%s skipped_unknown=%s",
        batch_id,
        matched,
        no_result,
        skipped_unknown,
    )

    return ResultsReport(
        batch_id=batch_id,
        filename=filename,
        sheet=sheet_used,
        available_sheets=available,
        matched=matched,
        no_result=no_result,
        skipped_unknown=skipped_unknown,
        column_mapping={str(k): v for k, v in mapping.items()},
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# stored results / final scoring
# --------------------------------------------------------------------------


def get_results_for_batch(session: Session, batch_id: int) -> list[TestResultOut]:
    """Stored TestResult rows for a candidate batch, joined for s_no/name."""
    rows = session.exec(
        select(TestResult, Candidate)
        .join(Candidate, Candidate.candidate_id == TestResult.candidate_id)
        .where(Candidate.batch_id == batch_id)
        .order_by(Candidate.s_no)
    ).all()
    return [
        TestResultOut(
            id=tr.id,
            candidate_id=tr.candidate_id,
            s_no=c.s_no,
            name=c.name,
            test_la=tr.test_la,
            test_code=tr.test_code,
            status=tr.status,
            created_at=tr.created_at,
        )
        for tr, c in rows
    ]


def compute_final_shortlist(
    session: Session, req: FinalShortlistRequest
) -> FinalResultsOut:
    """Recompute every run candidate's final_score from STORED
    ``pre_test_score`` and :class:`TestResult` rows - zero LLM calls, zero
    GitHub calls. Persists the recomputed final_score/final_weights back onto
    the run's :class:`Evaluation` rows, exactly like
    ``/evaluate/runs/{run_id}/rerank`` does for the pre-test blend.
    """
    weights = req.weights or FinalWeights()
    w = {
        "pre_test": weights.pre_test,
        "test_la": weights.test_la,
        "test_code": weights.test_code,
    }

    evaluations = session.exec(
        select(Evaluation).where(Evaluation.run_id == req.run_id)
    ).all()
    if not evaluations:
        raise ValueError(f"run {req.run_id} has no evaluation rows")

    candidate_ids = [ev.candidate_id for ev in evaluations]
    candidates = {
        c.candidate_id: c
        for c in session.exec(
            select(Candidate).where(Candidate.candidate_id.in_(candidate_ids))
        ).all()
    }
    test_results = {
        r.candidate_id: r
        for r in session.exec(
            select(TestResult).where(TestResult.candidate_id.in_(candidate_ids))
        ).all()
    }

    items: list[dict] = []
    for ev in evaluations:
        cand = candidates.get(ev.candidate_id)
        if cand is None:
            log.warning(
                "compute_final_shortlist: evaluation %s has no Candidate row", ev.id
            )
            continue
        tr = test_results.get(ev.candidate_id)
        test_la = tr.test_la if tr else None
        test_code = tr.test_code if tr else None

        final_score = scoring.compute_final_score(
            ev.pre_test_score, test_la, test_code, w
        )
        status = scoring.final_evaluation_state(ev.pre_test_score, test_la, test_code)
        note = (
            scoring.final_score_note(test_la, test_code) if status == "scored" else None
        )

        ev.final_score = final_score
        ev.final_weights = weights.model_dump()
        ev.final_status = status
        ev.final_note = note
        session.add(ev)

        items.append(
            {
                "candidate_id": cand.candidate_id,
                "s_no": cand.s_no,
                "name": cand.name,
                "pre_test_score": ev.pre_test_score,
                "test_la": test_la,
                "test_code": test_code,
                "final_score": final_score,
                "final_status": status,
                "note": note,
            }
        )

    session.commit()

    ranked, awaiting, unscorable = scoring.rank_by_final_score(items)

    if req.mode == "threshold":
        shortlisted = [r for r in ranked if (r["final_score"] or 0.0) >= req.threshold]
        criterion = f"score >= {req.threshold}"
    else:
        n = max(0, req.top_n)
        shortlisted = ranked[:n]
        criterion = f"top {n}"

    def _to_item(r: dict) -> FinalResultItem:
        return FinalResultItem(
            rank=r.get("rank"),
            candidate_id=r["candidate_id"],
            s_no=r["s_no"],
            name=r["name"],
            pre_test_score=r["pre_test_score"],
            test_la=r["test_la"],
            test_code=r["test_code"],
            final_score=r["final_score"],
            status=r["final_status"],
            note=r["note"],
        )

    return FinalResultsOut(
        run_id=req.run_id,
        weights=weights,
        criterion=criterion,
        ranked=[_to_item(r) for r in ranked],
        shortlisted=[_to_item(r) for r in shortlisted],
        awaiting_result=[_to_item(r) for r in awaiting],
        unscorable=[_to_item(r) for r in unscorable],
    )
