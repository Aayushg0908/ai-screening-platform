"""Page 2 — create and browse job descriptions."""

from __future__ import annotations

import streamlit as st

from lib.api_client import ApiError, get_client

st.title("2 · Job Description")

client = get_client()

with st.form("new_job"):
    title = st.text_input("Title")
    description = st.text_area("Description", height=200)
    skills_raw = st.text_input("Required skills (comma-separated)")
    submitted = st.form_submit_button("Create", type="primary")

if submitted:
    skills = [s.strip() for s in skills_raw.split(",") if s.strip()]
    try:
        job = client.create_job(title, description, skills)
    except ApiError as exc:
        st.error(str(exc))
    else:
        st.success(f"Created job #{job.get('job_id')}")

st.divider()
st.subheader("Existing jobs")
try:
    jobs = client.list_jobs()
except ApiError as exc:
    st.info(f"No jobs yet ({exc}).")
else:
    st.dataframe(jobs, use_container_width=True)
