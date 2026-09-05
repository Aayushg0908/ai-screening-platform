"""Upload the candidate dataset (§4.1)."""

from __future__ import annotations

import streamlit as st

from lib.api_client import ApiError, get_client
from lib.sidebar import render_session_indicator

st.set_page_config(page_title="Upload Candidates", page_icon="📤", layout="wide")
st.title("1. Upload Candidates")

for _key in ("batch_id", "job_id", "run_id"):
    st.session_state.setdefault(_key, None)

client = get_client()
render_session_indicator()


@st.cache_data(ttl=15)
def _cached_candidates(batch_id: int) -> list[dict]:
    """A batch's candidates never change after ingest - cache briefly per
    batch_id so re-rendering this page doesn't refetch the same 10 rows."""
    return get_client().list_candidates(batch_id=batch_id)


st.write(
    "Upload a candidate dataset (CSV or XLSX). Columns are matched by "
    "synonym, not position, so a differently-labelled file still works."
)

uploaded = st.file_uploader("Candidate file", type=["csv", "xlsx", "xls"])

if uploaded is not None:
    if st.button("Upload", type="primary"):
        try:
            with st.spinner("Uploading and ingesting..."):
                report = client.upload_candidates(uploaded.name, uploaded.getvalue())
        except ApiError as exc:
            st.error(f"Upload failed: {exc}")
        else:
            _cached_candidates.clear()
            if report["batch_id"] != st.session_state.get("batch_id"):
                st.session_state.pop("resumes_processed_batch_id", None)
                st.session_state.pop("_resume_report", None)
            st.session_state["_last_ingest_report"] = report
            st.session_state["_last_ingest_file"] = (uploaded.name, uploaded.getvalue())
            st.session_state["batch_id"] = report["batch_id"]
            st.success(
                f"Batch {report['batch_id']}: inserted {report['inserted']}/"
                f"{report['total_rows']} rows."
            )

report = st.session_state.get("_last_ingest_report")

if report and report.get("available_sheets") and len(report["available_sheets"]) > 1:
    st.info(
        f"This workbook has multiple sheets: {', '.join(report['available_sheets'])}. "
        f"Currently ingested: **{report.get('sheet')}**. Pick another to re-upload."
    )
    choice = st.selectbox(
        "Sheet to ingest", report["available_sheets"], key="_sheet_choice"
    )
    if st.button("Re-upload with this sheet"):
        filename, content = st.session_state["_last_ingest_file"]
        try:
            with st.spinner(f"Ingesting sheet '{choice}'..."):
                report = client.upload_candidates(filename, content, sheet_name=choice)
        except ApiError as exc:
            st.error(f"Upload failed: {exc}")
        else:
            if report["batch_id"] != st.session_state.get("batch_id"):
                st.session_state.pop("resumes_processed_batch_id", None)
                st.session_state.pop("_resume_report", None)
            st.session_state["_last_ingest_report"] = report
            st.session_state["batch_id"] = report["batch_id"]
            st.success(f"Batch {report['batch_id']}: inserted {report['inserted']} rows.")
            if "already uploaded" in " ".join(report.get("warnings", [])).lower():
                st.caption(
                    "Note: the backend deduplicates uploads by file content, so "
                    "re-uploading the same bytes with a different sheet name "
                    "returns the same batch rather than re-ingesting."
                )

if report:
    st.subheader("Ingest report")
    m1, m2, m3 = st.columns(3)
    m1.metric("Total rows", report["total_rows"])
    m2.metric("Inserted", report["inserted"])
    m3.metric("Batch ID", report["batch_id"])

    with st.expander("Column mapping"):
        st.json(report.get("column_mapping", {}))
    if report.get("unmapped_columns"):
        with st.expander(f"Unmapped columns ({len(report['unmapped_columns'])})"):
            st.write(report["unmapped_columns"])
    if report.get("warnings"):
        with st.expander(f"Warnings ({len(report['warnings'])})", expanded=True):
            st.caption(
                "These matter - they show which candidates had no GitHub "
                "profile and which had one recovered from free text."
            )
            for w in report["warnings"]:
                st.warning(w)

st.divider()
batch_id = st.session_state.get("batch_id")
if not batch_id:
    st.info("Upload a candidate file first.")
else:
    st.subheader(f"Candidates in batch {batch_id}")
    candidates = []
    try:
        candidates = _cached_candidates(batch_id)
    except ApiError as exc:
        st.error(f"Could not load candidates: {exc}")
    else:
        cm1, cm2, cm3 = st.columns(3)
        cm1.metric("Total candidates", len(candidates))
        with_github = sum(1 for c in candidates if c.get("github_source") not in (None, "none"))
        cm2.metric("With GitHub", with_github)
        recovered = sum(1 for c in candidates if c.get("github_source") == "text_fallback")
        cm3.metric("Recovered from text", recovered)
        st.dataframe(candidates, width="stretch", hide_index=True)

    st.divider()
    st.subheader("Process resumes")
    resumes_processed = st.session_state.get("resumes_processed_batch_id") == batch_id
    if not resumes_processed and candidates:
        # session_state alone can't tell a fresh visitor that resumes were
        # already processed server-side (e.g. a seeded demo batch) - fall
        # back to a live check, same probe page 3 uses.
        try:
            if client.get_candidate_resume(candidates[0]["candidate_id"]) is not None:
                resumes_processed = True
                st.session_state["resumes_processed_batch_id"] = batch_id
        except ApiError:
            pass
    if resumes_processed:
        st.success(
            "Resumes processed for this batch - evaluation will read real "
            "resume text."
        )
    else:
        st.warning(
            "**Required before evaluation.** Without this step, the LLM "
            "evaluates from CSV fields alone and never reads the actual "
            "resume PDF behind each candidate's Drive link."
        )
    force_resumes = st.checkbox(
        "Force re-download (even if already processed)", key="_force_resumes"
    )
    if st.button("Process resumes", type="primary"):
        try:
            with st.spinner(
                f"Downloading and extracting resumes for batch {batch_id} - "
                "this can take 30-60s for 10 candidates..."
            ):
                resume_report = client.process_resumes(batch_id, force=force_resumes)
        except ApiError as exc:
            st.error(f"Resume processing failed: {exc}")
        else:
            st.session_state["resumes_processed_batch_id"] = batch_id
            st.session_state["_resume_report"] = resume_report
            st.success(
                f"Processed batch {batch_id}: {resume_report['succeeded']} "
                f"succeeded, {resume_report['failed']} failed, "
                f"{resume_report['skipped']} skipped, out of "
                f"{resume_report['total']}."
            )
            st.rerun()

    resume_report = st.session_state.get("_resume_report")
    if resume_report:
        st.markdown("#### Resume processing report")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total", resume_report["total"])
        m2.metric("Succeeded", resume_report["succeeded"])
        m3.metric("Failed", resume_report["failed"])
        m4.metric("Skipped", resume_report["skipped"])
        st.dataframe(
            resume_report.get("items") or [], width="stretch", hide_index=True
        )
        failed_items = [
            i for i in resume_report.get("items") or [] if i.get("error")
        ]
        if failed_items:
            with st.expander(f"Failure reasons ({len(failed_items)})", expanded=True):
                for item in failed_items:
                    st.warning(
                        f"s_no {item['s_no']} — {item['name']}: {item['error']}"
                    )
