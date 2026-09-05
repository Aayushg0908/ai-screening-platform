"""Shared UI helper - not an HTTP client, so it lives apart from
api_client.py. Streamlit re-runs each page as an independent script, so
content rendered into the sidebar by one page never appears on another;
every page that wants the "Viewing batch/job/run" indicator (and the
pipeline progress bar below it) calls this.
"""

from __future__ import annotations

import streamlit as st

from lib.api_client import ApiError, get_client


def render_session_indicator() -> None:
    """A small, always-visible "Viewing batch X, job Y, run Z" line plus a
    pipeline-progress bar, so a page's results are never a mystery -
    especially important right after app.py's auto-discovery silently picks
    a batch/job/run for a brand-new session with nothing selected yet.
    """
    with st.sidebar:
        st.caption("Viewing")
        st.write(
            f"Batch **{st.session_state.get('batch_id') or '—'}** · "
            f"Job **{st.session_state.get('job_id') or '—'}** · "
            f"Run **{st.session_state.get('run_id') or '—'}**"
        )
        batch_id = st.session_state.get("batch_id")
        if batch_id:
            _render_progress(
                batch_id, st.session_state.get("job_id"), st.session_state.get("run_id")
            )


def _render_progress(batch_id: int, job_id: int | None, run_id: int | None) -> None:
    stages = _pipeline_stages(batch_id, job_id, run_id)
    done = sum(1 for ok in stages.values() if ok)
    total = len(stages)
    st.progress(done / total, text=f"Pipeline: {done}/{total} stages")
    with st.expander("Stage detail"):
        for label, ok in stages.items():
            st.write(("✅ " if ok else "⬜ ") + label)


@st.cache_data(ttl=15)
def _pipeline_stages(
    batch_id: int, job_id: int | None, run_id: int | None
) -> dict[str, bool]:
    """Best-effort per-stage completion check for the sidebar progress bar.

    Every probe below is independently try/excepted - one failing call
    (e.g. a cold Render backend) degrades that single stage to "not done"
    rather than breaking the whole sidebar, same pattern as app.py's
    Pipeline overview. Cached briefly since this runs on every page.
    """
    client = get_client()
    stages: dict[str, bool] = {"1. Candidates uploaded": bool(batch_id)}

    resumes_done = False
    try:
        candidates = client.list_candidates(batch_id=batch_id)
        if candidates:
            resumes_done = (
                client.get_candidate_resume(candidates[0]["candidate_id"]) is not None
            )
    except ApiError:
        pass
    stages["2. Resumes processed"] = resumes_done

    stages["3. Job description selected"] = bool(job_id)

    eval_done = False
    if run_id:
        try:
            eval_done = client.run_status(run_id).get("status") == "completed"
        except ApiError:
            pass
    stages["4. Evaluation completed"] = eval_done

    outreach_done = False
    if run_id:
        try:
            outreach_done = any(
                e.get("status") == "sent" for e in client.email_log(run_id)
            )
        except ApiError:
            pass
    stages["5. Outreach sent"] = outreach_done

    results_done = False
    try:
        results_done = any(
            r.get("status") == "received" for r in client.list_results(batch_id)
        )
    except ApiError:
        pass
    stages["6. Test results uploaded"] = results_done

    interviews_done = False
    if run_id:
        try:
            interviews_done = any(
                i.get("status") == "scheduled" for i in client.list_interviews(run_id)
            )
        except ApiError:
            pass
    stages["7. Interviews scheduled"] = interviews_done

    return stages
