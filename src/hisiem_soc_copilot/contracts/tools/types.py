"""Typed Tool contracts — the deterministic boundary between the LLM and providers.

Per investigation-tool-contract.md:
- the model only produces a ToolCandidate (tool_name + arguments + optional reason);
- each tool has its own strict schema (no generic ``execute(name, dict)``);
- arguments are validated here into typed dataclasses BEFORE any provider call;
- the typed ToolResult envelope is what flows out of the executor.

``from``/``to`` are ISO-8601 UTC strings accepted by the HISIEM log-search API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MAX_SEARCH_LIMIT = 200
MAX_SINGLE_CALL_SPAN_HOURS = 24
ALLOWED_LOGICAL_OPERATORS = frozenset(
    {
        "is",
        "contain",
        "exist",
        "is_one_of",
        "not_is",
        "not_contain",
        "not_exist",
        "not_is_one_of",
    }
)
# V1 agent-selectable event-search fields (a subset of the HISIEM catalog).
ALLOWED_EVENT_FIELDS = frozenset(
    {
        "event.category",
        "event.action",
        "event.outcome",
        "event.type",
        "source.ip",
        "destination.ip",
        "related.ip",
        "user.name",
        "host.name",
        "log.source_id",
        "message",
        "event.original",
    }
)

ToolResultStatus = Literal["SUCCESS", "NO_DATA", "PARTIAL", "REJECTED", "UNAVAILABLE"]


@dataclass(frozen=True)
class LogSearchCondition:
    field: str
    operator: str
    value: object


@dataclass(frozen=True)
class SearchEventsArgs:
    from_: str
    to: str
    conditions: list[LogSearchCondition]
    limit: int = 100
    sort: Literal["asc", "desc"] = "desc"


@dataclass(frozen=True)
class EntityActivityArgs:
    entity_type: str
    entity_value: str
    from_: str
    to: str
    limit: int = 100


@dataclass(frozen=True)
class DetectionRuleArgs:
    rule_id: str


@dataclass(frozen=True)
class ToolCandidate:
    """The ONLY thing the model produces before tool execution."""

    tool_name: str
    arguments: dict[str, object]
    reason_summary: str | None = None


@dataclass(frozen=True)
class ToolResult:
    """Unified typed envelope produced by the ToolExecutor (never raw provider JSON)."""

    tool_call_id: str
    tool_name: str
    status: ToolResultStatus
    fetched_at: str
    data: dict[str, object] = field(default_factory=dict)
    source_refs: list[dict[str, str]] = field(default_factory=list)
    truncated: bool = False
    continuation: str | None = None
    error: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class ToolExecutionContext:
    """System-injected context — never part of model arguments."""

    investigation_id: str
    tenant_id: str
    source_alert_ref: dict[str, str]
    tool_call_id: str
    budget_remaining: int
