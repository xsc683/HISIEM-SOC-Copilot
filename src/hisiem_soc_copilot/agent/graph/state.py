"""LangGraph State — bounded, structured working state.

Per domain-model.md §51 and python-package-boundary.md §13: the graph state holds
ONLY cross-step working state. It is never a copy of the Domain database, never
holds prompts/CoT/full tool results, and never holds secrets.

Graph state != Domain state. Domain is the source of truth; the graph checkpoints
(``langgraph_checkpoint`` schema) only support fault recovery/interrupt/resume.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

SCHEMA_VERSION = 1


class AlertContext(TypedDict):
    """Bounded authoritative alert snapshot hydrated from HISIEM (read model).

    Only investigation-decision fields are carried; tenant_id is bound by the
    orchestrator scope and is NOT model-visible. All fields are optional so a
    degraded/partial hydrate never crashes the graph; the decide node reads the ones
    a real tool call needs (rule_id, detected_at, entity).
    """

    alert_id: str
    tenant_id: str
    title: NotRequired[str | None]
    severity: NotRequired[str | None]
    status: NotRequired[str | None]
    rule_name: NotRequired[str | None]
    rule_id: NotRequired[str | None]
    detected_at: NotRequired[str | None]
    source_ip: NotRequired[str | None]
    user_name: NotRequired[str | None]
    host_name: NotRequired[str | None]
    event_category: NotRequired[str | None]
    event_action: NotRequired[str | None]


class PendingToolRequest(TypedDict):
    tool: str
    arguments: dict[str, Any]
    step_key: str


class InvestigationGraphState(TypedDict, total=False):
    """The only state that LangGraph persists across nodes.

    Field meanings follow python-package-boundary.md §13. All identifiers reference
    domain objects persisted by the application layer — never duplicated inline.
    """

    schema_version: int
    investigation_id: str
    investigation_revision: int

    alert_context: AlertContext
    plan_revision_id: str | None

    iteration: int

    # Runtime budget counters (system-authoritative, never model-settable). They are
    # checkpointed with the graph state so a crash/restart/resume continues from the
    # CONSUMED budget — never reset to full. ``budget_deadline_at`` is the UTC
    # epoch-seconds wall-clock deadline derived from max_duration_seconds.
    budget_remaining_steps: int
    budget_remaining_tool_calls: int
    budget_remaining_llm_calls: int
    budget_deadline_at: float | None

    # Runtime routing signals (system-set, never model-authoritative).
    next_action: str | None  # "CALL_TOOL" | "ASSESS"
    assessment: str | None  # "CONTINUE" | "FINALIZE"

    pending_tool_request: PendingToolRequest | None
    last_tool_invocation_id: str | None
    last_tool_error: str | None
    # Failure-aware re-plan context. ``previous_tool_outcome`` is the bounded outcome
    # (tool_name/status/error_code/retryable) of the most recently executed tool —
    # never raw exceptions or full results. ``failing_call_fingerprint`` is the
    # deterministic request fingerprint of the last call that did not SUCCEED, and
    # ``same_call_retries`` counts how many consecutive times that exact call has
    # been re-scheduled — together they bound the repeated-attempt re-plan loop.
    previous_tool_outcome: NotRequired[dict[str, Any]]
    failing_call_fingerprint: NotRequired[str | None]
    same_call_retries: NotRequired[int]
    new_evidence_ids: list[str]

    result_id: str | None
    response_proposal_id: str | None
    proposal_revision: int | None
    approval_request_id: str | None
    response_execution_id: str | None

    stop_reason: str | None
