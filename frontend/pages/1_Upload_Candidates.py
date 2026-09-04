"""Page 1 — upload and review the candidate dataset."""

from __future__ import annotations

import streamlit as st

from lib.api_client import ApiError, get_client

st.title("1 · Upload Candidates")

client = get_client()

uploaded = st.file_uploader("Candidate dataset", type=["csv", "xlsx"])
if uploaded is not None and st.button("Ingest", type="primary"):
    try:
        summary = client.upload_candidates(uploaded.name, uploaded.getvalue())
    except ApiError as exc:
        st.error(str(exc))
    else:
        st.success(
            f"Ingested {summary.get('ingested', 0)}, "
            f"skipped {summary.get('skipped', 0)}"
        )
        for warning in summary.get("warnings", []):
            st.warning(warning)

st.divider()
st.subheader("Ingested candidates")
try:
    candidates = client.list_candidates()
except ApiError as exc:
    st.info(f"No candidates yet ({exc}).")
else:
    st.dataframe(candidates, use_container_width=True)
