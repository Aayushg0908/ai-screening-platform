"""Shortlist preview and test-invite emails (§4.6)."""

from __future__ import annotations

import streamlit as st

from lib.api_client import ApiError, get_client
from lib.sidebar import render_session_indicator

st.set_page_config(page_title="Outreach", page_icon="✉️", layout="wide")
st.title("5. Outreach")

for _key in ("batch_id", "job_id", "run_id"):
    st.session_state.setdefault(_key, None)

client = get_client()
render_session_indicator()
run_id = st.session_state.get("run_id")

if not run_id:
    st.info("Run an evaluation first (page 3) before shortlisting.")
    st.stop()

st.write(f"Shortlisting from run **#{run_id}**.")

mode = st.radio("Shortlist by", ["top_n", "threshold"], horizontal=True)
top_n, threshold = 5, 60.0
if mode == "top_n":
    top_n = st.number_input("Top N candidates", min_value=1, max_value=50, value=5)
else:
    threshold = st.slider("Minimum pre-test score", 0.0, 100.0, 60.0, 1.0)

if st.button("Preview shortlist", type="primary"):
    try:
        preview = client.preview_shortlist(
            run_id, mode=mode, top_n=int(top_n), threshold=float(threshold)
        )
    except ApiError as exc:
        st.error(f"Could not preview shortlist: {exc}")
    else:
        st.session_state["_shortlist_preview"] = preview

preview = st.session_state.get("_shortlist_preview")
if preview and preview.get("run_id") == run_id:
    st.subheader(f"Preview — {preview['criterion']}")
    st.caption(f"{preview.get('excluded_count', 0)} candidate(s) not shortlisted.")
    rows = preview.get("shortlisted") or []
    if not rows:
        st.warning("No candidates match this criterion.")
    else:
        table = [
            {
                "rank": r["rank"],
                "s_no": r["s_no"],
                "name": r["name"],
                "email": r["email"],
                "pre_test_score": r["pre_test_score"],
                "already_emailed": r["already_emailed"],
            }
            for r in rows
        ]
        st.dataframe(table, width="stretch", hide_index=True)
        if any(r["already_emailed"] for r in rows):
            st.caption(
                "Rows flagged already_emailed were sent a test invite in a "
                "previous send — use force to resend."
            )

    st.divider()
    st.markdown("### Send invitations")
    st.caption("Preview and send are separate actions — previewing never sends anything.")
    force = st.checkbox("Force resend (even if already emailed)")
    if st.button("Send invitations", type="primary"):
        try:
            with st.spinner("Sending..."):
                report = client.send_invitations(
                    run_id,
                    mode=mode,
                    top_n=int(top_n),
                    threshold=float(threshold),
                    force=force,
                )
        except ApiError as exc:
            st.error(f"Send failed: {exc}")
        else:
            st.session_state["_send_report"] = report

report = st.session_state.get("_send_report")
if report and report.get("run_id") == run_id:
    st.subheader("Send report")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Attempted", report["attempted"])
    m2.metric("Sent", report["sent"])
    m3.metric("Failed", report["failed"])
    m4.metric("Skipped", report["skipped"])
    if report.get("bcc"):
        st.caption(f"A copy of every message was Bcc'd to {report['bcc']}.")
    st.dataframe(report.get("results") or [], width="stretch", hide_index=True)

st.divider()
st.subheader("Email log")
try:
    log = client.email_log(run_id)
except ApiError as exc:
    st.error(f"Could not load email log: {exc}")
else:
    if log:
        st.dataframe(log, width="stretch", hide_index=True)
    else:
        st.caption("No emails sent yet for this run.")
