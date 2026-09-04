"""LangGraph builder — the real read-only Investigation graph.

Wiring (application-commands...md §19, read-only prefix):
    START → load_investigation → hydrate_alert → plan → decide_next
        decide_next.next_action:
            execute_and_ingest → decide_next   (investigate loop)
            assess → assess → finalize_result → complete → END

``execute_and_ingest`` performs one allowlisted read tool AND records its
normalized Evidence in a single node, so the bounded ToolResult is consumed within
one checkpoint step — a crash/replay re-runs the whole node, and both the tool
audit (by-key) and Evidence (by dedup key) are idempotent, so a checkpointed
resume never loses or duplicates evidence.

Routing is driven by deterministic ``next_action`` / ``assessment`` markers that
nodes write into state (never by free text). The model is consulted only inside
nodes; edges never ask the model.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from . import nodes
from .runtime import GraphRuntime
from .state import InvestigationGraphState

NodeFn = Callable[..., Awaitable[dict[str, Any]]]


def _route_decide(state: InvestigationGraphState) -> str:
    return state.get("next_action") or nodes.CONVERGE


def _route_after_tool(state: InvestigationGraphState) -> str:
    # After one tool call + evidence ingestion the investigation loops back to
    # decide_next (which routes to CONVERGE when the model/budget says FINALIZE).
    action = state.get("next_action")
    if action == nodes.CONVERGE:
        return nodes.CONVERGE
    return "decide_next"


def _route_after_load(state: InvestigationGraphState) -> str:
    # Reconciliation: a terminal investigation stops immediately (no re-run).
    if state.get("stop_reason"):
        return END
    return "hydrate_alert"


def _bind_node(runtime: GraphRuntime, fn: NodeFn) -> Any:
    """Adapt ``(runtime, state)`` node coroutines to LangGraph's ``(state)``."""

    async def _node(state: InvestigationGraphState) -> dict[str, Any]:
        return await fn(runtime, state)

    return _node


def build_investigation_graph(runtime: GraphRuntime, checkpointer: Any = None) -> Any:
    """Compile the read-only investigation graph bound to a runtime context.

    ``checkpointer`` is optional; when supplied (e.g. LangGraph's
    AsyncPostgresSaver, owned by the ``langgraph_checkpoint`` schema) the compiled
    graph persists its bounded state across node executions for restart/resume.
    """
    builder = StateGraph(InvestigationGraphState)

    def _add(name: str, fn: NodeFn) -> None:
        builder.add_node(name, _bind_node(runtime, fn))

    _add("load_investigation", nodes.load_investigation)
    _add("hydrate_alert", nodes.hydrate_alert)
    _add("plan", nodes.plan)
    _add("decide_next", nodes.decide_next)
    _add("execute_and_ingest", nodes.execute_and_ingest)
    _add("assess", nodes.assess)
    _add("finalize_result", nodes.finalize_result)
    _add("complete", nodes.complete)

    builder.add_edge(START, "load_investigation")
    builder.add_conditional_edges(
        "load_investigation",
        _route_after_load,
        {"hydrate_alert": "hydrate_alert", END: END},
    )
    builder.add_edge("hydrate_alert", "plan")
    builder.add_edge("plan", "decide_next")
    builder.add_conditional_edges(
        "decide_next",
        _route_decide,
        {
            nodes.EXECUTE_TOOL: "execute_and_ingest",
            nodes.CONVERGE: "assess",
        },
    )
    builder.add_conditional_edges(
        "execute_and_ingest",
        _route_after_tool,
        {"decide_next": "decide_next", nodes.CONVERGE: "assess"},
    )
    builder.add_edge("assess", "finalize_result")
    builder.add_edge("finalize_result", "complete")
    builder.add_edge("complete", END)

    if checkpointer is not None:
        return builder.compile(checkpointer=checkpointer)
    return builder.compile()


def thread_config(investigation_id: str) -> dict[str, Any]:
    """Return the RunnableConfig dict binding a run to a thread.

    Investigation ID and LangGraph thread_id remain distinct identities; the
    orchestration_binding table records their mapping. Until a full runtime is
    wired, threads are keyed by the investigation id as a stable placeholder.
    """
    return {
        "configurable": {
            "thread_id": investigation_id,
            "checkpoint_ns": "",
        }
    }
