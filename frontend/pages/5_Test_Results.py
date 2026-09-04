"""Page 5 — email the test link to the shortlist and upload test results."""

from __future__ import annotations

import streamlit as st

from lib.api_client import ApiError, get_client

st.title("5 · Test Results")

client = get_client()

st.subheader("Send the test link")
col1, col2 = st.columns(2)
with col1:
    run_id = st.number_input("Run id", min_value=1, value=1, step=1)
with col2:
    top_n = st.number_input("Top N (blank = config default)", min_value=0, value=0, step=1)

if st.button("Send tests", type="primary"):
    try:
        out = client.send_tests(int(run_id), int(top_n) or None)
    except ApiError as exc:
        st.error(str(exc))
    else:
        st.success(f"Sent {out.get('sent', 0)}, failed {out.get('failed', 0)}")
        for w in out.get("warnings", []):
            st.warning(w)

st.divider()
st.subheader("Upload the Test Result sheet")
uploaded = st.file_uploader("Test results", type=["csv", "xlsx"])
if uploaded is not None and st.button("Ingest results"):
    try:
        summary = client.upload_results(uploaded.name, uploaded.getvalue())
    except ApiError as exc:
        st.error(str(exc))
    else:
        st.success(
            f"Ingested {summary.get('ingested', 0)}, "
            f"skipped {summary.get('skipped', 0)}"
        )
        for w in summary.get("warnings", []):
            st.warning(w)
