"""LangGraph builder — a minimal, honest orchestration skeleton.

This round deliberately does NOT implement the full investigation agent. Instead
it provides a compiled StateGraph that:
- binds the ``InvestigationGraphState`` TypedDict and the investigation identifier,
- proves the state/checkpoint wiring compiles against the installed LangGraph,
- is the seam where the V1 investigation nodes (hydrate → plan → investigate →
  verify → finalize) will attach next round.

V1 will NOT use ``MessagesState`` as the core state (python-package-boundary.md §13).
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from .state import SCHEMA_VERSION, InvestigationGraphState


def _pending_stop(state: InvestigationGraphState) -> dict[str, Any]:
    """Skeleton entry: assert we are bound to a real investigation then stop.

    A real hydrate/plan node replaces this; for the initial skeleton it only
    verifies the graph machinery can carry the investigation_id forward.
    """
    if not state.get("investigation_id"):
        raise ValueError("graph state requires an investigation_id")
    return {
        "schema_version": SCHEMA_VERSION,
        "stop_reason": "skeleton_noop",
    }


def build_investigation_graph() -> Any:
    """Compile the (minimal) investigation graph bound to a checkpointer."""
    builder = StateGraph(InvestigationGraphState)
    builder.add_node("skeleton_entry", _pending_stop)
    builder.add_edge(START, "skeleton_entry")
    builder.add_edge("skeleton_entry", END)
    return builder.compile()


def thread_config(investigation_id: str) -> dict[str, Any]:
    """Return the RunnableConfig dict binding a run to a thread.

    Investigation ID and LangGraph thread_id remain distinct identities; the
    orchestration_binding table records their mapping. Until the full runtime is
    wired, threads are keyed by the investigation id as a stable placeholder.
    """
    return {
        "configurable": {
            "thread_id": investigation_id,
            "checkpoint_ns": "",
        }
    }
