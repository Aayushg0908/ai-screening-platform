"""Streamlit entry point.

Thin UI only - all HTTP goes through :mod:`lib.api_client`, and this package
never imports from ``backend``. Streamlit auto-discovers the numbered modules
in ``pages/`` for the sidebar; this file adds the title, a backend health
badge, and the session-wide pipeline state.

Run with::

    streamlit run frontend/app.py
"""

from __future__ import annotations

import streamlit as st

# Streamlit puts this file's directory (frontend/) on sys.path, so ``lib`` is
# importable directly. This package must never import from ``backend``.
from lib.api_client import ApiError, get_client
from lib.sidebar import render_session_indicator

st.set_page_config(page_title="AI Screening Platform", page_icon="🧑‍💻", layout="wide")

# Pipeline state shared across every page. Each page must degrade gracefully
# (show guidance, not a KeyError) when a prior step hasn't run yet.
for _key in ("batch_id", "job_id", "run_id"):
    st.session_state.setdefault(_key, None)


def _auto_discover_state(client) -> None:
    """On a brand-new session (nothing selected yet), default each of
    batch_id/job_id/run_id independently to the most recent value the
    backend already has, so a reviewer's first visit shows results
    immediately instead of "upload a file first" guidance.

    Runs at most once per session (subsequent reruns are a no-op) and never
    overwrites a value already present - whether set by an earlier pass of
    this same function or an explicit choice the user made on a page. Any
    discovery call that fails or returns nothing is skipped silently: a
    genuinely empty database still correctly falls through to each page's
    normal empty-state message.
    """
    if st.session_state.get("_auto_discovery_done"):
        return
    st.session_state["_auto_discovery_done"] = True
    discovered: dict[str, int] = {}

    if st.session_state.get("batch_id") is None:
        try:
            batches = client.list_batches()
        except ApiError:
            batches = []
        if batches:
            st.session_state["batch_id"] = batches[0]["batch_id"]
            discovered["batch_id"] = batches[0]["batch_id"]

    if st.session_state.get("job_id") is None:
        try:
            jobs = client.list_jobs()
        except ApiError:
            jobs = []
        if jobs:
            st.session_state["job_id"] = jobs[0]["job_id"]
            discovered["job_id"] = jobs[0]["job_id"]

    if st.session_state.get("run_id") is None:
        try:
            runs = client.list_runs()
        except ApiError:
            runs = []
        completed = next((r for r in runs if r.get("status") == "completed"), None)
        if completed:
            st.session_state["run_id"] = completed["run_id"]
            discovered["run_id"] = completed["run_id"]

    st.session_state["_auto_discovered"] = discovered


st.title("AI Screening Platform")
st.caption(
    "Ingest a candidate dataset, evaluate against a job description with an "
    "LLM, analyse GitHub at repository level, score and rank, email a test "
    "link, ingest results, and schedule interviews with a real Google Meet "
    "link."
)

client = get_client()
_auto_discover_state(client)
render_session_indicator()

try:
    health = client.health()
except ApiError as exc:
    st.error(f"Backend unreachable at {client.base_url}: {exc}")
else:
    api_ok = health.get("status") == "ok"
    db_ok = bool(health.get("db"))
    if api_ok and db_ok:
        st.success(f"Backend OK ({client.base_url}) - database reachable.")
    elif api_ok:
        st.warning(f"Backend OK ({client.base_url}) - database NOT reachable.")
    else:
        st.error(f"Backend reported an unhealthy status: {health}")

st.divider()
st.subheader("Pipeline overview")

batch_id = st.session_state.get("batch_id")
job_id = st.session_state.get("job_id")
run_id = st.session_state.get("run_id")

if not batch_id:
    st.info(
        "Nothing uploaded yet - head to **1. Upload Candidates** to get started."
    )
else:
    candidates = None
    try:
        candidates = client.list_candidates(batch_id=batch_id)
    except ApiError:
        pass

    run_status = results = None
    if run_id:
        try:
            run_status = client.run_status(run_id)
        except ApiError:
            pass
        try:
            results = client.run_results(run_id)
        except ApiError:
            pass

    email_log = interviews = []
    if run_id:
        try:
            email_log = client.email_log(run_id)
        except ApiError:
            pass
        try:
            interviews = client.list_interviews(run_id)
        except ApiError:
            pass

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Candidates", len(candidates) if candidates is not None else "—")
    with_github = (
        sum(1 for c in candidates if c.get("github_source") != "none")
        if candidates is not None
        else None
    )
    m2.metric("With GitHub", with_github if with_github is not None else "—")
    m3.metric(
        "Evaluated",
        f"{run_status['processed']}/{run_status['total']}" if run_status else "—",
    )
    scored = len(results.get("ranked", [])) if results else None
    m4.metric("Scored", scored if scored is not None else "—")
    sent = sum(1 for e in email_log if e.get("status") == "sent") if email_log else 0
    m5.metric("Emails sent", sent)
    scheduled = (
        sum(1 for i in interviews if i.get("status") == "scheduled")
        if interviews
        else 0
    )
    m6.metric("Interviews", scheduled)

    if results and results.get("ranked"):
        st.caption("Pre-test score by candidate (current run)")
        chart_data = {
            f"s_no {r['s_no']}": r["pre_test_score"] for r in results["ranked"]
        }
        st.bar_chart(chart_data)

st.divider()
st.markdown(
    """
    ### Workflow

    Use the sidebar to move through the pipeline in order - each page picks
    up `batch_id` / `job_id` / `run_id` from the step before it automatically,
    and every page shows what's already been done there so far, not just the
    latest action.

    1. **Upload Candidates** — ingest the candidate dataset (CSV/XLSX).
    2. **Job Description** — define or pick the role to screen against.
    3. **Evaluation** — run the LLM + GitHub analysis pipeline.
    4. **Rankings** — explainable scores and weight tuning / reranking.
    5. **Outreach** — preview and email the test link to the shortlist.
    6. **Test Results** — upload results and blend them into a final score.
    7. **Interviews** — schedule real Google Calendar events with Meet links.
    """
)
