"""Assembly of the per-candidate evaluation graph.

Topology::

    fetch_resume -> extract_text -> evaluate_vs_jd  ┐
                                 -> analyze_github  ┴-> aggregate -> END

``evaluate_vs_jd`` and ``analyze_github`` fan out from ``extract_text`` and fan
back in at ``aggregate``; LangGraph runs the two branches concurrently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langgraph.graph import END, START, StateGraph

from backend.graph.nodes import (
    aggregate,
    analyze_github,
    evaluate_vs_jd,
    extract_text,
    fetch_resume,
)
from backend.graph.state import CandidateState

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


def build_graph() -> "CompiledStateGraph":
    """Build and compile the candidate evaluation graph."""
    graph = StateGraph(CandidateState)

    graph.add_node("fetch_resume", fetch_resume)
    graph.add_node("extract_text", extract_text)
    graph.add_node("evaluate_vs_jd", evaluate_vs_jd)
    graph.add_node("analyze_github", analyze_github)
    graph.add_node("aggregate", aggregate)

    graph.add_edge(START, "fetch_resume")
    graph.add_edge("fetch_resume", "extract_text")

    # Fan out: JD scoring and GitHub analysis are independent.
    graph.add_edge("extract_text", "evaluate_vs_jd")
    graph.add_edge("extract_text", "analyze_github")

    # Fan in: aggregate waits for both branches.
    graph.add_edge("evaluate_vs_jd", "aggregate")
    graph.add_edge("analyze_github", "aggregate")
    graph.add_edge("aggregate", END)

    return graph.compile()
