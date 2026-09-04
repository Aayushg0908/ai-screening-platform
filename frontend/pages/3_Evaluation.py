"""Page 3 — start a batch evaluation run and watch its progress.

Streamlit reruns this script on every interaction and cannot hold background
state, so progress is obtained by polling the run status endpoint.
"""

from __future__ import annotations

import streamlit as st

from lib.api_client import ApiError, get_client

st.title("3 · Evaluation")

client = get_client()

try:
    jobs = client.list_jobs()
except ApiError as exc:
    st.error(f"Cannot load jobs: {exc}")
    st.stop()

if not jobs:
    st.info("Create a job description first.")
    st.stop()

labels = {f"#{j['job_id']} · {j['title']}": j["job_id"] for j in jobs}
choice = st.selectbox("Job", list(labels))

if st.button("Start evaluation", type="primary"):
    try:
        run = client.start_evaluation(labels[choice])
    except ApiError as exc:
        st.error(str(exc))
    else:
        st.session_state["run_id"] = run.get("run_id")
        st.success(f"Started run #{run.get('run_id')}")

run_id = st.session_state.get("run_id")
if run_id:
    st.divider()
    if st.button("Refresh status"):
        pass  # triggers a rerun -> status re-fetched below
    try:
        status = client.run_status(run_id)
    except ApiError as exc:
        st.error(str(exc))
    else:
        st.metric("Status", status.get("status", "?"))
        st.progress(
            (status.get("completed", 0) / status["total"])
            if status.get("total")
            else 0.0
        )
        for err in status.get("errors", []):
            st.warning(err)
