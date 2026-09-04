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

st.set_page_config(page_title="AI Screening Platform", page_icon="🧑‍💻", layout="wide")

# Pipeline state shared across every page. Each page must degrade gracefully
# (show guidance, not a KeyError) when a prior step hasn't run yet.
for _key in ("batch_id", "job_id", "run_id"):
    st.session_state.setdefault(_key, None)

st.title("AI Screening Platform")
st.caption(
    "Ingest a candidate dataset, evaluate against a job description with an "
    "LLM, analyse GitHub at repository level, score and rank, email a test "
    "link, ingest results, and schedule interviews with a real Google Meet "
    "link."
)

client = get_client()
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
st.subheader("Current session")
c1, c2, c3 = st.columns(3)
c1.metric("Candidate batch", st.session_state["batch_id"] or "—")
c2.metric("Job description", st.session_state["job_id"] or "—")
c3.metric("Evaluation run", st.session_state["run_id"] or "—")

st.markdown(
    """
    ### Workflow

    Use the sidebar to move through the pipeline in order - each page picks
    up `batch_id` / `job_id` / `run_id` from the step before it automatically.

    1. **Upload Candidates** — ingest the candidate dataset (CSV/XLSX).
    2. **Job Description** — define or pick the role to screen against.
    3. **Evaluation** — run the LLM + GitHub analysis pipeline.
    4. **Rankings** — explainable scores and weight tuning / reranking.
    5. **Outreach** — preview and email the test link to the shortlist.
    6. **Test Results** — upload results and blend them into a final score.
    7. **Interviews** — schedule real Google Calendar events with Meet links.
    """
)
