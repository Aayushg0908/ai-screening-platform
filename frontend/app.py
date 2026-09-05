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

# The deployment has one shared database and no accounts, so every visitor
# upload is a permanent batch row. Rather than list all of them (and show
# strangers' test uploads to everyone), the "Load an existing batch" picker
# only offers this curated allowlist of demo batches. A visitor who uploads
# their own file still gets its batch_id set on their session automatically
# by page 1 - they don't need this picker at all.
_PREVIEW_BATCH_IDS = (1, 12)


def _load_existing_data(client) -> None:
    """Explicit, opt-in loader for one of the curated demo batches.

    A fresh session starts completely empty - every page shows its normal
    "upload a file first" guidance - so a brand-new visitor to the public
    URL is never shown someone else's data. Picking a batch here sets
    batch_id, then links its most recent completed run (and that run's
    job_id) so the results pages have something to show without re-running
    the pipeline.
    """
    with st.expander("Load a demo batch (optional)", expanded=False):
        try:
            batches = client.list_batches()
        except ApiError as exc:
            st.caption(f"Could not list batches: {exc}")
            return
        batches = [b for b in batches if b["batch_id"] in _PREVIEW_BATCH_IDS]
        if not batches:
            st.caption("No demo batch available right now.")
            return

        labels = {
            f"Batch {b['batch_id']} — {b['filename']} ({b['row_count']} candidates)": b[
                "batch_id"
            ]
            for b in batches
        }
        choice = st.selectbox(
            "Existing batch", ["(none)"] + list(labels), key="_load_batch_choice"
        )
        if choice != "(none)" and st.button("Load this batch"):
            picked = labels[choice]
            st.session_state["batch_id"] = picked
            try:
                runs = client.list_runs()
            except ApiError:
                runs = []
            run = next(
                (
                    r
                    for r in runs
                    if r["batch_id"] == picked and r.get("status") == "completed"
                ),
                None,
            )
            if run:
                st.session_state["run_id"] = run["run_id"]
                if run.get("job_id"):
                    st.session_state["job_id"] = run["job_id"]
            st.rerun()


st.title("AI Screening Platform")
st.caption(
    "Ingest a candidate dataset, evaluate against a job description with an "
    "LLM, analyse GitHub at repository level, score and rank, email a test "
    "link, ingest results, and schedule interviews with a real Google Meet "
    "link."
)

client = get_client()
render_session_indicator()
_load_existing_data(client)

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
st.markdown("### Workflow")
st.graphviz_chart(
    """
    digraph {
        rankdir=LR;
        bgcolor="transparent";
        node [shape=box style="rounded,filled" fillcolor="#eef2ff"
              color="#c7d2fe" fontname="Helvetica" fontsize=10 margin="0.15,0.1"];
        edge [color="#94a3b8" arrowsize=0.7];
        a [label="1. Upload\nCandidates"];
        b [label="2. Job\nDescription"];
        c [label="3. Evaluation"];
        d [label="4. Rankings"];
        e [label="5. Outreach"];
        f [label="6. Test\nResults"];
        g [label="7. Interviews"];
        a -> b -> c -> d -> e -> f -> g;
    }
    """,
    width="stretch",
)
st.markdown(
    """
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
