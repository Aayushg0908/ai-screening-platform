"""The per-candidate evaluation graph (§4.5).

Topology::

    load_resume ─┬─> evaluate_vs_jd ─┬─> aggregate ─> END
                 └─> analyze_github ──┘

``evaluate_vs_jd`` (resume vs JD) and ``analyze_github`` (repo-level GitHub) are
independent, so they fan out from ``load_resume`` and run concurrently, then
converge on ``aggregate`` which does the deterministic blend.

The graph is compiled once at import (:data:`GRAPH`) and reused for every
candidate — it is never rebuilt per candidate. ``run_batch`` invokes it once per
candidate, three at a time.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from backend.graph.nodes import aggregate, analyze_github, evaluate_vs_jd, load_resume
from backend.graph.state import CandidateState


def build_graph():
    """Build and compile the candidate evaluation graph."""
    graph = StateGraph(CandidateState)

    graph.add_node("load_resume", load_resume)
    graph.add_node("evaluate_vs_jd", evaluate_vs_jd)
    graph.add_node("analyze_github", analyze_github)
    graph.add_node("aggregate", aggregate)

    graph.add_edge(START, "load_resume")

    # Fan out: JD scoring and GitHub analysis are independent -> run concurrently.
    graph.add_edge("load_resume", "evaluate_vs_jd")
    graph.add_edge("load_resume", "analyze_github")

    # Fan in: aggregate waits for both branches.
    graph.add_edge("evaluate_vs_jd", "aggregate")
    graph.add_edge("analyze_github", "aggregate")
    graph.add_edge("aggregate", END)

    return graph.compile()


#: Compiled once, reused for every candidate. Do not rebuild per candidate.
GRAPH = build_graph()
