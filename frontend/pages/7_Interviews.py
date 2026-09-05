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

st.write(f"Interviews are scheduled from run **#{run_id}**'s final scores.")

batch_id = st.session_state.get("batch_id")

_SLOT_MINUTES = {"30 minutes": 30, "45 minutes": 45, "1 hour": 60, "1.5 hours": 90}
_GAP_MINUTES = {"No break": 0, "10 minutes": 10, "15 minutes": 15, "30 minutes": 30}
_TIMEZONES = [
    "Asia/Kolkata",
    "Asia/Dubai",
    "Asia/Singapore",
    "Europe/London",
    "America/New_York",
    "America/Los_Angeles",
    "UTC",
]

# ---- Step 1: choose who to interview -----------------------------------
st.subheader("Step 1 — Choose who to interview")
mode = st.radio(
    "Qualify by",
    ["top_n", "threshold"],
    horizontal=True,
    format_func=lambda m: "Top N candidates" if m == "top_n" else "Minimum score",
)
top_n, threshold = 3, 65.0
if mode == "top_n":
    top_n = st.number_input(
        "How many of the top candidates?", min_value=1, max_value=50, value=3
    )
else:
    threshold = st.slider("Minimum final score", 0.0, 100.0, 65.0, 1.0)

qualifying: list[dict] = []
try:
    _final = client.final_shortlist(
        run_id, mode=mode, top_n=int(top_n), threshold=float(threshold)
    )
    qualifying = _final.get("shortlisted") or []
except ApiError as exc:
    st.error(f"Could not load qualifying candidates: {exc}")

_emails: dict[int, str] = {}
if batch_id:
    try:
        _emails = {
            c["candidate_id"]: c.get("email")
            for c in client.list_candidates(batch_id=batch_id)
        }
    except ApiError:
        pass

if not qualifying:
    st.info(
        "No candidates qualify yet. Upload test results on page 6, or widen the "
        "criteria above."
    )
else:
    st.caption(f"These {len(qualifying)} candidate(s) will be invited:")
    st.dataframe(
        [
            {
                "rank": c.get("rank"),
                "s_no": c["s_no"],
                "name": c["name"],
                "email": _emails.get(c["candidate_id"], "—"),
                "final_score": c.get("final_score"),
            }
            for c in qualifying
        ],
        width="stretch",
        hide_index=True,
    )

    # ---- Step 2: set the schedule ------------------------------------
    st.subheader("Step 2 — Set the interview times")
    first_date = st.date_input(
        "First interview date",
        value=dt.date.today() + dt.timedelta(days=1),
        min_value=dt.date.today(),
    )
    t1, t2 = st.columns(2)
    earliest = t1.time_input(
        "Earliest start time (each day)", value=dt.time(10, 0), step=3600
    )
    latest = t2.time_input(
        "Latest start time (each day)", value=dt.time(17, 0), step=3600
    )
    o1, o2, o3 = st.columns(3)
    length_label = o1.selectbox("Interview length", list(_SLOT_MINUTES), index=1)
    gap_label = o2.selectbox("Break between interviews", list(_GAP_MINUTES), index=2)
    timezone = o3.selectbox("Timezone", _TIMEZONES, index=0)
    st.caption(
        "Interviews are booked back-to-back from the first date, only between "
        "the earliest and latest start times each day, rolling to the next day "
        "once a day is full."
    )

    send_invites = st.checkbox(
        "Email a calendar invite to each candidate", value=True
    )
    force = st.checkbox("Reschedule anyone who is already booked", value=False)

    if st.button(
        f"Schedule {len(qualifying)} interview(s)", type="primary"
    ):
        if earliest.hour >= latest.hour:
            st.error("The latest start time must be after the earliest start time.")
        else:
            try:
                with st.spinner("Booking Calendar events and sending invites..."):
                    report = client.schedule_interviews(
                        run_id,
                        mode=mode,
                        top_n=int(top_n),
                        threshold=float(threshold),
                        start_date=first_date.isoformat(),
                        day_start_hour=earliest.hour,
                        day_end_hour=latest.hour,
                        slot_minutes=_SLOT_MINUTES[length_label],
                        gap_minutes=_GAP_MINUTES[gap_label],
                        timezone=timezone,
                        send_invites=send_invites,
                        force=force,
                    )
            except ApiError as exc:
                st.error(f"Scheduling failed: {exc}")
            else:
                st.session_state["_schedule_report"] = report
                st.rerun()

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
