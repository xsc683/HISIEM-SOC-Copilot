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
    """Bounded authoritative alert snapshot hydrated from HISIEM (read model)."""

    alert_id: str
    tenant_id: str
    title: NotRequired[str | None]
    severity: NotRequired[str | None]
    status: NotRequired[str | None]
    rule_name: NotRequired[str | None]
    detected_at: NotRequired[str | None]


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
    budget_remaining_steps: int

    pending_tool_request: PendingToolRequest | None
    last_tool_invocation_id: str | None
    last_tool_error: str | None
    new_evidence_ids: list[str]

    result_id: str | None
    response_proposal_id: str | None
    proposal_revision: int | None
    approval_request_id: str | None
    response_execution_id: str | None

    stop_reason: str | None
