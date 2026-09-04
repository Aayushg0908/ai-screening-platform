"""Page 6 — schedule interviews and review booked slots."""

from __future__ import annotations

from datetime import datetime, time

import streamlit as st

from lib.api_client import ApiError, get_client

st.title("6 · Interviews")

client = get_client()

st.subheader("Schedule an interview")
candidate_id = st.number_input("Candidate id", min_value=1, value=1, step=1)
job_id = st.number_input("Job id (0 = none)", min_value=0, value=0, step=1)
day = st.date_input("Date")
slot = st.time_input("Start time", value=time(10, 0))
duration = st.number_input("Duration (minutes)", min_value=15, value=45, step=15)

if st.button("Schedule", type="primary"):
    start_iso = datetime.combine(day, slot).isoformat()
    try:
        interview = client.schedule_interview(
            int(candidate_id), start_iso, int(duration), int(job_id) or None
        )
    except ApiError as exc:
        st.error(str(exc))
    else:
        st.success(f"Scheduled interview #{interview.get('interview_id')}")
        if interview.get("meet_link"):
            st.write(f"Meet: {interview['meet_link']}")

st.divider()
st.subheader("Scheduled interviews")
try:
    interviews = client.list_interviews()
except ApiError as exc:
    st.info(f"None yet ({exc}).")
else:
    st.dataframe(interviews, use_container_width=True)
