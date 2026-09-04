"""Upload the candidate dataset (§4.1)."""

from __future__ import annotations

import streamlit as st

from lib.api_client import ApiError, get_client

st.set_page_config(page_title="Upload Candidates", page_icon="📤", layout="wide")
st.title("1. Upload Candidates")

for _key in ("batch_id", "job_id", "run_id"):
    st.session_state.setdefault(_key, None)

client = get_client()


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
    try:
        candidates = _cached_candidates(batch_id)
    except ApiError as exc:
        st.error(f"Could not load candidates: {exc}")
    else:
        st.dataframe(candidates, width="stretch", hide_index=True)
