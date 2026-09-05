"""Schedule real Google Calendar interviews with Meet links (§4.8)."""

from __future__ import annotations

import datetime as dt

import streamlit as st

from lib.api_client import ApiError, get_client
from lib.sidebar import render_session_indicator

st.set_page_config(page_title="Interviews", page_icon="📅", layout="wide")
st.title("7. Interviews")

for _key in ("batch_id", "job_id", "run_id"):
    st.session_state.setdefault(_key, None)

client = get_client()
render_session_indicator()
run_id = st.session_state.get("run_id")

if not run_id:
    st.info(
        "Run an evaluation first (page 3) — interviews are scheduled from a "
        "run's final scores."
    )
    st.stop()

st.write(f"Scheduling interviews from run **#{run_id}**'s final scores.")

with st.form("schedule_form"):
    mode = st.radio("Qualify by", ["top_n", "threshold"], horizontal=True)
    top_n, threshold = 3, 65.0
    if mode == "top_n":
        top_n = st.number_input("Top N", min_value=1, max_value=50, value=3)
    else:
        threshold = st.slider("Minimum final score", 0.0, 100.0, 65.0, 1.0)

    start_date = st.date_input("Start date", value=dt.date.today() + dt.timedelta(days=1))
    c1, c2 = st.columns(2)
    day_start = c1.number_input("Working day starts (hour, 24h)", 0, 23, 10)
    day_end = c2.number_input("Working day ends (hour, 24h)", 1, 24, 17)
    c3, c4 = st.columns(2)
    slot_minutes = c3.number_input("Slot length (minutes)", 15, 180, 45, step=15)
    gap_minutes = c4.number_input("Gap between slots (minutes)", 0, 60, 15, step=5)
    timezone = st.text_input("Timezone", value="Asia/Kolkata")
    send_invites = st.checkbox("Send invitation emails", value=True)
    force = st.checkbox("Force (cancel and re-book if already scheduled)")

    submitted = st.form_submit_button("Schedule interviews", type="primary")

if submitted:
    try:
        with st.spinner("Booking Calendar events and sending invites..."):
            report = client.schedule_interviews(
                run_id,
                mode=mode,
                top_n=int(top_n),
                threshold=float(threshold),
                start_date=start_date.isoformat(),
                day_start_hour=int(day_start),
                day_end_hour=int(day_end),
                slot_minutes=int(slot_minutes),
                gap_minutes=int(gap_minutes),
                timezone=timezone.strip() or "Asia/Kolkata",
                send_invites=send_invites,
                force=force,
            )
    except ApiError as exc:
        st.error(f"Scheduling failed: {exc}")
    else:
        st.session_state["_schedule_report"] = report

report = st.session_state.get("_schedule_report")
if report and report.get("run_id") == run_id:
    st.subheader("Schedule report")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Attempted", report["attempted"])
    m2.metric("Scheduled", report["scheduled"])
    m3.metric("Failed", report["failed"])
    m4.metric("Skipped", report["skipped"])

    for iv in report.get("interviews") or []:
        cols = st.columns([3, 2, 2, 2])
        cols[0].write(f"**{iv['name']}** (s_no {iv['s_no']}, rank {iv.get('rank')})")
        cols[1].write(iv.get("starts_at") or "—")
        cols[2].write(iv.get("status"))
        if iv.get("meet_link"):
            cols[3].link_button("Join Meet", iv["meet_link"])
        elif iv.get("error"):
            cols[3].error((iv["error"] or "")[:60])

st.divider()
st.subheader(f"Scheduled interviews for run #{run_id}")
try:
    interviews = client.list_interviews(run_id)
except ApiError as exc:
    st.error(f"Could not load interviews: {exc}")
    interviews = []

if interviews:
    m1, m2, m3 = st.columns(3)
    m1.metric("Total", len(interviews))
    m2.metric(
        "Scheduled", sum(1 for i in interviews if i.get("status") == "scheduled")
    )
    m3.metric("With Meet link", sum(1 for i in interviews if i.get("meet_link")))

if not interviews:
    st.caption("No interviews scheduled yet.")
else:
    for iv in interviews:
        cols = st.columns([3, 2, 2, 2, 1])
        cols[0].write(f"**{iv['name']}** (s_no {iv['s_no']})")
        cols[1].write(iv.get("starts_at") or "—")
        cols[2].write(iv.get("status"))
        if iv.get("meet_link"):
            cols[3].link_button("Join Meet", iv["meet_link"])
        else:
            cols[3].write(iv.get("error") or "—")
        if cols[4].button("Cancel", key=f"cancel_{iv['id']}"):
            try:
                client.cancel_interview(iv["id"])
            except ApiError as exc:
                st.error(f"Cancel failed: {exc}")
            else:
                st.success(f"Cancelled interview {iv['id']}.")
                st.rerun()
