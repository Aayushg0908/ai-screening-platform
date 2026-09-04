"""Streamlit entry point.

Thin UI only — all HTTP goes through :mod:`frontend.lib.api_client`, and this
package never imports from ``backend``. Streamlit auto-discovers the numbered
modules in ``pages/`` for the sidebar; this file adds the app title and a
backend health indicator.

Run with::

    streamlit run frontend/app.py
"""

from __future__ import annotations

import streamlit as st

# Streamlit puts this file's directory (frontend/) on sys.path, so ``lib`` is
# importable directly. This package must never import from ``backend``.
from lib.api_client import ApiError, get_client

st.set_page_config(page_title="AI Screening Platform", page_icon="🧑‍💻", layout="wide")


def render_health() -> None:
    """Show a backend connectivity badge in the sidebar."""
    with st.sidebar:
        st.subheader("Backend")
        try:
            health = get_client().health()
        except ApiError as exc:
            st.error(f"Unreachable\n\n{exc}")
            return
        db_ok = bool(health.get("db"))
        st.success("API: ok")
        (st.success if db_ok else st.warning)(f"DB: {'ok' if db_ok else 'down'}")


def main() -> None:
    """Render the landing page."""
    st.title("AI Screening Platform")
    st.caption(
        "Ingest candidates, evaluate against a job description, rank, send tests, "
        "and schedule interviews."
    )
    render_health()
    st.markdown(
        """
        **Workflow**

        1. **Upload Candidates** — ingest the candidate dataset.
        2. **Job Description** — define the role to screen against.
        3. **Evaluation** — run the LLM + GitHub pipeline.
        4. **Rankings** — review explainable scores and the shortlist.
        5. **Test Results** — upload results and email the test link.
        6. **Interviews** — schedule calendar invites with Meet links.
        """
    )


if __name__ == "__main__":
    main()
