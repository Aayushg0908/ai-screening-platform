"""Page 4 — ranked results with explainable per-dimension scores."""

from __future__ import annotations

import streamlit as st

from lib.api_client import ApiError, get_client

st.title("4 · Rankings")

client = get_client()

run_id = st.session_state.get("run_id")
run_id = st.number_input(
    "Run id", min_value=1, value=int(run_id) if run_id else 1, step=1
)

if st.button("Load results", type="primary"):
    try:
        results = client.run_results(int(run_id))
    except ApiError as exc:
        st.error(str(exc))
    else:
        st.session_state["results"] = results

results = st.session_state.get("results", [])
if results:
    st.dataframe(results, use_container_width=True)
    for row in results:
        with st.expander(
            f"#{row.get('rank', '?')} · candidate {row['candidate_id']} · "
            f"final {row.get('final_score', 'n/a')}"
        ):
            st.json(row.get("final_breakdown") or row)
