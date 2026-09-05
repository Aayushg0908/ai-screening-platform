"""Create or pick a job description (§4.3)."""

from __future__ import annotations

import streamlit as st

from lib.api_client import ApiError, get_client
from lib.sidebar import render_session_indicator

st.set_page_config(page_title="Job Description", page_icon="📝", layout="wide")
st.title("2. Job Description")

for _key in ("batch_id", "job_id", "run_id"):
    st.session_state.setdefault(_key, None)

client = get_client()
render_session_indicator()


@st.cache_data(ttl=10)
def _cached_jobs() -> list[dict]:
    """Job descriptions barely change - cache briefly so switching pages
    doesn't refetch the same list on every rerun."""
    return get_client().list_jobs()


st.subheader("Existing job descriptions")
try:
    jobs = _cached_jobs()
except ApiError as exc:
    st.error(f"Could not load jobs: {exc}")
    jobs = []

if jobs:
    st.metric("Total job descriptions", len(jobs))
    options = {f"#{j['job_id']} — {j['title']}": j["job_id"] for j in jobs}
    choice = st.selectbox("Use an existing job", ["(choose one)"] + list(options.keys()))
    if choice != "(choose one)" and st.button("Use this job"):
        st.session_state["job_id"] = options[choice]
        st.success(f"Using job #{options[choice]}.")
    st.dataframe(jobs, width="stretch", hide_index=True)
else:
    st.caption("No job descriptions yet.")

st.divider()
st.subheader("Create a new job description")
with st.form("new_job"):
    title = st.text_input("Title", placeholder="e.g. Machine Learning Engineer")
    description = st.text_area(
        "Description", height=220, placeholder="Paste the full job description here..."
    )
    submitted = st.form_submit_button("Create job", type="primary")

if submitted:
    if not title.strip() or not description.strip():
        st.warning("Title and description are both required.")
    else:
        try:
            job = client.create_job(title.strip(), description.strip())
        except ApiError as exc:
            st.error(f"Could not create job: {exc}")
        else:
            _cached_jobs.clear()  # the new job must show up immediately below
            st.session_state["job_id"] = job["job_id"]
            st.success(f"Created job #{job['job_id']}: {job['title']}")
            st.rerun()

st.divider()
job_id = st.session_state.get("job_id")
if job_id:
    st.info(f"Active job for the rest of the pipeline: **#{job_id}**")
else:
    st.info("Create or select a job description to continue.")
