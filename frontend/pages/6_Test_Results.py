"""Upload test results and blend them into a final score (§4.7)."""

from __future__ import annotations

import streamlit as st

from lib.api_client import ApiError, get_client

st.set_page_config(page_title="Test Results", page_icon="📊", layout="wide")
st.title("6. Test Results")

for _key in ("batch_id", "job_id", "run_id"):
    st.session_state.setdefault(_key, None)

client = get_client()
batch_id = st.session_state.get("batch_id")
run_id = st.session_state.get("run_id")

if not batch_id:
    st.info("Upload a candidate file first (page 1).")
    st.stop()

st.write(f"Uploading results for batch **#{batch_id}**. Joined on `s_no`, never on email.")

uploaded = st.file_uploader("Test results file", type=["csv", "xlsx", "xls"])
sheet_name = None
if uploaded is not None and uploaded.name.lower().endswith((".xlsx", ".xls")):
    sheet_name = (
        st.text_input("Sheet name (optional — defaults to the first sheet)", value="").strip()
        or None
    )

if uploaded is not None and st.button("Upload results", type="primary"):
    try:
        with st.spinner("Uploading and joining on s_no..."):
            report = client.upload_results(
                uploaded.name, uploaded.getvalue(), batch_id, sheet_name=sheet_name
            )
    except ApiError as exc:
        st.error(f"Upload failed: {exc}")
    else:
        st.session_state["_results_report"] = report

report = st.session_state.get("_results_report")
if report:
    st.subheader("Results report")
    m1, m2, m3 = st.columns(3)
    m1.metric("Matched", report["matched"])
    m2.metric("No result", report["no_result"])
    m3.metric("Skipped (unknown s_no)", report["skipped_unknown"])
    with st.expander("Column mapping"):
        st.json(report.get("column_mapping", {}))
    if report.get("warnings"):
        with st.expander(f"Warnings ({len(report['warnings'])})", expanded=True):
            for w in report["warnings"]:
                st.warning(w)

st.divider()
st.subheader(f"Stored test results for batch #{batch_id}")
try:
    stored = client.list_results(batch_id)
except ApiError as exc:
    st.error(f"Could not load stored results: {exc}")
else:
    st.dataframe(stored, width="stretch", hide_index=True)

st.divider()
if not run_id:
    st.info("Run an evaluation first (page 3) to compute final scores.")
    st.stop()

st.markdown("## Final scoring — instant, zero LLM calls")
st.caption(
    "Blends the stored pre-test score with test_la / test_code. A missing "
    "test score's weight is redistributed onto the other test component, "
    "never scored zero."
)

with st.form("final_weights_form"):
    c1, c2, c3 = st.columns(3)
    pre_test_w = c1.slider("Pre-test weight", 0.0, 1.0, 0.60, 0.05)
    test_la_w = c2.slider("Aptitude test weight", 0.0, 1.0, 0.20, 0.05)
    test_code_w = c3.slider("Coding test weight", 0.0, 1.0, 0.20, 0.05)

    fmode = st.radio("Rank by", ["top_n", "threshold"], horizontal=True)
    ftop_n, fthreshold = 10, 60.0
    if fmode == "top_n":
        ftop_n = st.number_input("Top N", min_value=1, max_value=50, value=10)
    else:
        fthreshold = st.slider("Minimum final score", 0.0, 100.0, 60.0, 1.0)

    submitted = st.form_submit_button("Recompute final ranking", type="primary")

if submitted:
    try:
        final = client.final_shortlist(
            run_id,
            weights={
                "pre_test": pre_test_w,
                "test_la": test_la_w,
                "test_code": test_code_w,
            },
            mode=fmode,
            top_n=int(ftop_n),
            threshold=float(fthreshold),
        )
    except ApiError as exc:
        st.error(f"Could not recompute final scores: {exc}")
    else:
        st.session_state["_final_results"] = final

final = st.session_state.get("_final_results")
if final and final.get("run_id") == run_id:
    st.subheader(f"Final ranked table — {final['criterion']}")
    table = [
        {
            "rank": r["rank"],
            "s_no": r["s_no"],
            "name": r["name"],
            "pre_test_score": r["pre_test_score"],
            "test_la": r["test_la"],
            "test_code": r["test_code"],
            "final_score": r["final_score"],
            "note": r.get("note"),
        }
        for r in final.get("ranked") or []
    ]
    st.dataframe(table, width="stretch", hide_index=True)

    awaiting = final.get("awaiting_result") or []
    if awaiting:
        st.markdown("### Awaiting result")
        st.caption(
            "No test result yet — pre_test_score is kept for display, "
            "final_score stays blank rather than being scored zero."
        )
        st.dataframe(
            [
                {"s_no": r["s_no"], "name": r["name"], "pre_test_score": r["pre_test_score"]}
                for r in awaiting
            ],
            width="stretch",
            hide_index=True,
        )
    if final.get("unscorable"):
        st.markdown("### Unscorable")
        st.dataframe(final["unscorable"], width="stretch", hide_index=True)
