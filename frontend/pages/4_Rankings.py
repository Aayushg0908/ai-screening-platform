"""Explainable scoring, ranking, and instant weight tuning (§4.5, §8 bonus).

The most important page: every score carries its reasoning and evidence, and
the weight sliders rerank from stored scores with zero LLM calls.
"""

from __future__ import annotations

import time

import streamlit as st

from lib.api_client import ApiError, get_client
from lib.sidebar import render_session_indicator

st.set_page_config(page_title="Rankings", page_icon="🏆", layout="wide")
st.title("4. Rankings")

for _key in ("batch_id", "job_id", "run_id"):
    st.session_state.setdefault(_key, None)

client = get_client()
render_session_indicator()
run_id = st.session_state.get("run_id")

if not run_id:
    st.info("Run an evaluation first (page 3).")
    st.stop()

try:
    results = client.run_results(run_id)
except ApiError as exc:
    st.error(f"Could not load results: {exc}")
    st.stop()

ranked = results.get("ranked") or []
unranked = results.get("unranked") or []

st.subheader(f"Run #{run_id} — {results.get('mode')} mode")

if not ranked and not unranked:
    st.info("No results yet — the evaluation may still be running (see page 3).")
    st.stop()

m1, m2, m3, m4 = st.columns(4)
m1.metric("Ranked", len(ranked))
m2.metric("Unranked", len(unranked))
top = ranked[0] if ranked else None
m3.metric("Top score", top["pre_test_score"] if top else "—")
avg = round(sum(r["pre_test_score"] for r in ranked) / len(ranked), 1) if ranked else None
m4.metric("Average", avg if avg is not None else "—")

st.markdown("### Ranked candidates")
if ranked:
    table = [
        {
            "rank": r["rank"],
            "s_no": r["s_no"],
            "name": r["name"],
            "resume_score": r.get("resume_score"),
            "github_score": r.get("github_score"),
            "github_status": r.get("github_status"),
            "pre_test_score": r.get("pre_test_score"),
            "status": r.get("status"),
            "model_used": r.get("model_used"),
        }
        for r in ranked
    ]
    st.dataframe(table, width="stretch", hide_index=True)
    st.caption("Pre-test score by candidate")
    st.bar_chart({f"s_no {r['s_no']}": r["pre_test_score"] for r in ranked})
else:
    st.caption("No ranked candidates yet.")

if unranked:
    st.markdown("### Unranked (unscorable) candidates")
    st.caption(
        "Both the resume and GitHub evaluation failed for these — never "
        "scored zero, and kept out of the ranking rather than buried at the "
        "bottom of it."
    )
    for r in unranked:
        with st.expander(f"s_no {r['s_no']} — {r['name']} (unscorable)"):
            st.write("Status:", r.get("status"))
            for e in r.get("errors") or []:
                st.error(e)

st.divider()
st.markdown("## Explainable scoring")
st.caption(
    "Every score below carries the LLM's reasoning and quoted evidence — "
    "nothing here is a bare number."
)


def render_dimension(label: str, dim: dict | None) -> None:
    if not dim:
        st.write(f"**{label}:** not available")
        return
    st.write(f"**{label}: {dim['score']}/10**")
    st.write(dim.get("reasoning", ""))
    for ev in dim.get("evidence") or []:
        st.markdown(f"> {ev}")


for r in ranked + unranked:
    header = f"s_no {r['s_no']} — {r['name']}"
    if r.get("rank"):
        header = f"#{r['rank']} · " + header
    with st.expander(header):
        resume_eval = r.get("resume_eval") or {}
        github_eval = r.get("github_eval") or {}

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("#### Resume vs job description")
            if resume_eval:
                for label, key in [
                    ("Skills match", "skills_match"),
                    ("Project depth", "project_depth"),
                    ("Experience relevance", "experience_relevance"),
                    ("Research", "research"),
                ]:
                    render_dimension(label, resume_eval.get(key))
                st.write("**Summary:**", resume_eval.get("summary", ""))
                if resume_eval.get("missing_requirements"):
                    st.write("**Missing requirements:**")
                    for m in resume_eval["missing_requirements"]:
                        st.write(f"- {m}")
            else:
                st.caption("Resume evaluation not available.")

        with col2:
            st.markdown("#### GitHub analysis")
            gh_status = github_eval.get("status") or r.get("github_status")
            if gh_status and gh_status != "ok":
                st.info(
                    f"GitHub status: **{gh_status}**. No repository-level "
                    "score — the GitHub weight was redistributed onto the "
                    "resume score rather than scored zero."
                )
            evaluation = github_eval.get("evaluation")
            if evaluation:
                for label, key in [
                    ("Repository quality", "repository_quality"),
                    ("Technical relevance", "technical_relevance"),
                    ("Activity consistency", "activity_consistency"),
                    ("Engineering practice", "engineering_practice"),
                ]:
                    render_dimension(label, evaluation.get(key))
                st.write("**Summary:**", evaluation.get("summary", ""))
            stats = github_eval.get("stats")
            if stats and stats.get("analyzed_repos"):
                st.write("**Repos analysed:**", ", ".join(stats["analyzed_repos"]))

        if r.get("errors"):
            st.markdown("#### Errors")
            for e in r["errors"]:
                st.warning(e)

st.divider()
st.markdown("## Weight tuning — instant, zero LLM calls")
st.caption(
    "Recomputes the ranking from the SAME stored dimension scores. No LLM or "
    "GitHub API calls are made — this is pure arithmetic."
)

with st.form("rerank_form"):
    st.markdown("**Resume dimension weights** (auto-normalised to sum to 1.0)")
    c1, c2, c3, c4 = st.columns(4)
    skills = c1.slider("Skills match", 0.0, 1.0, 0.30, 0.05)
    projects = c2.slider("Project depth", 0.0, 1.0, 0.30, 0.05)
    experience = c3.slider("Experience relevance", 0.0, 1.0, 0.25, 0.05)
    research = c4.slider("Research", 0.0, 1.0, 0.15, 0.05)

    st.markdown("**Pre-test blend** (resume score vs GitHub score)")
    b1, b2 = st.columns(2)
    resume_blend = b1.slider("Resume weight", 0.0, 1.0, 0.60, 0.05)
    github_blend = b2.slider("GitHub weight", 0.0, 1.0, 0.40, 0.05)

    rerank_clicked = st.form_submit_button("Rerank now", type="primary")

if rerank_clicked:
    start = time.perf_counter()
    try:
        new_results = client.rerank(
            run_id,
            resume=resume_blend,
            github=github_blend,
            dimensions={
                "skills_match": skills,
                "project_depth": projects,
                "experience_relevance": experience,
                "research": research,
            },
        )
    except ApiError as exc:
        st.error(f"Rerank failed: {exc}")
    else:
        elapsed = time.perf_counter() - start
        st.success(
            f"Reranked in {elapsed:.2f}s — zero LLM calls, zero GitHub calls."
        )
        new_ranked = new_results.get("ranked") or []
        table = [
            {
                "rank": r["rank"],
                "s_no": r["s_no"],
                "name": r["name"],
                "pre_test_score": r.get("pre_test_score"),
            }
            for r in new_ranked
        ]
        st.markdown("### New order")
        st.caption("Compare this against the 'Ranked candidates' table above.")
        st.dataframe(table, width="stretch", hide_index=True)
        st.bar_chart({f"s_no {r['s_no']}": r["pre_test_score"] for r in new_ranked})
