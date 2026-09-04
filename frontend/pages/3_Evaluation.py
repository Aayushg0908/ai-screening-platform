"""Run the batch evaluation pipeline and watch it progress (§4.3-4.5)."""

from __future__ import annotations

import time

import streamlit as st

from lib.api_client import ApiError, get_client

st.set_page_config(page_title="Evaluation", page_icon="🧪", layout="wide")
st.title("3. Evaluation")

for _key in ("batch_id", "job_id", "run_id"):
    st.session_state.setdefault(_key, None)

client = get_client()
batch_id = st.session_state.get("batch_id")
job_id = st.session_state.get("job_id")

if not batch_id:
    st.info("Upload a candidate file first (page 1).")
    st.stop()
if not job_id:
    st.info("Create or select a job description first (page 2).")
    st.stop()

st.write(f"Batch **#{batch_id}** vs job **#{job_id}**")

mode = st.radio("Evaluation mode", options=["fast", "quality"], index=0, horizontal=True)
st.caption(
    "**fast** (default): falls back to a smaller model under Groq's free-tier "
    "rate limits, ~3 min for 10 candidates. **quality**: waits for the primary "
    "model to free up so every candidate is scored the same way, ~8-10 min."
)

if st.button("Start evaluation", type="primary"):
    try:
        status = client.start_batch_evaluation(batch_id, job_id, mode=mode)
    except ApiError as exc:
        st.error(f"Could not start evaluation: {exc}")
    else:
        st.session_state["run_id"] = status["run_id"]
        st.success(f"Started run #{status['run_id']} ({status.get('mode')} mode).")

run_id = st.session_state.get("run_id")
if not run_id:
    st.stop()

st.divider()
st.subheader(f"Run #{run_id}")
st.button("Poll / refresh status now")
auto_track = st.checkbox("Track live until done (polls every 2s)", value=False)

progress_bar = st.empty()
errors_box = st.container()
results_box = st.container()


def render_progress(status: dict) -> None:
    total = max(status.get("total", 0), 1)
    processed = status.get("processed", 0)
    progress_bar.progress(
        min(processed / total, 1.0),
        text=f"{processed}/{status.get('total', 0)} candidates — status: {status.get('status')}",
    )
    errors = status.get("errors") or []
    errors_box.empty()
    if errors:
        with errors_box:
            with st.expander(f"Errors so far ({len(errors)})"):
                for e in errors:
                    st.warning(e)


def render_partial_results(run_id: int) -> None:
    try:
        results = client.run_results(run_id)
    except ApiError:
        return
    rows = (results.get("ranked") or []) + (results.get("unranked") or [])
    results_box.empty()
    if not rows:
        return
    with results_box:
        st.caption("Scored so far — updates as the run progresses:")
        table = [
            {
                "rank": r.get("rank"),
                "s_no": r["s_no"],
                "name": r["name"],
                "status": r.get("status"),
                "resume_score": r.get("resume_score"),
                "github_score": r.get("github_score"),
                "pre_test_score": r.get("pre_test_score"),
            }
            for r in rows
        ]
        st.dataframe(table, width="stretch", hide_index=True)


try:
    status = client.run_status(run_id)
except ApiError as exc:
    st.error(f"Could not fetch run status: {exc}")
    st.stop()

render_progress(status)
render_partial_results(run_id)

if auto_track and status.get("status") not in ("completed", "failed"):
    while status.get("status") not in ("completed", "failed"):
        time.sleep(2)
        try:
            status = client.run_status(run_id)
        except ApiError as exc:
            st.error(f"Lost contact with backend while polling: {exc}")
            break
        render_progress(status)
        render_partial_results(run_id)
    else:
        st.success(f"Run finished with status: {status.get('status')}")
        st.balloons()
