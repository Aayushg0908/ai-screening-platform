"""Shared UI helper - not an HTTP client, so it lives apart from
api_client.py. Streamlit re-runs each page as an independent script, so
content rendered into the sidebar by one page never appears on another;
every page that wants the "Viewing batch/job/run" indicator calls this.
"""

from __future__ import annotations

import streamlit as st


def render_session_indicator() -> None:
    """A small, always-visible "Viewing batch X, job Y, run Z" line so a
    page's results are never a mystery - especially important right after
    app.py's auto-discovery silently picks a batch/job/run for a brand-new
    session with nothing selected yet.
    """
    with st.sidebar:
        st.caption("Viewing")
        st.write(
            f"Batch **{st.session_state.get('batch_id') or '—'}** · "
            f"Job **{st.session_state.get('job_id') or '—'}** · "
            f"Run **{st.session_state.get('run_id') or '—'}**"
        )
        if st.session_state.get("_auto_discovered"):
            st.caption("(auto-selected from the most recent data)")
