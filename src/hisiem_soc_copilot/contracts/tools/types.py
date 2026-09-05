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

# Ordered, human-stable list of allowed event fields (schema ordering).
_ORDERED_EVENT_FIELDS = (
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


@dataclass(frozen=True)
class ModelToolSpec:
    """Provider-neutral tool description handed to the model (decide prompt).

    Only REAL, currently-executable tools are exposed; ``arguments_schema`` is a
    bounded JSON-shape description (field name + type + constraints), NOT an
    executable function definition and NOT a provider-native tool. The model merely
    uses it to build well-formed ``arguments`` — execution still flows through the
    ToolRegistry / argument parser / policy / budget / executor.
    """

    name: str
    description: str
    arguments_schema: list[dict[str, str]]


def search_events_tool_spec() -> ModelToolSpec:
    """The exact argument shape for ``hisiem.search_events``.

    Mirrors parse_search_events (agent/tools/args.py) and the contracts types so the
    model can produce arguments the deterministic parser will accept. ``field`` /
    ``operator`` carry the real allowlists; ``limit`` the real cap; ``value`` is
    described loosely (its allowed shape depends on the operator) — the parser stays
    authoritative.
    """
    return ModelToolSpec(
        name="hisiem.search_events",
        description=(
            "Search HISIEM events over a bounded time window. Returns normalized "
            "events matching all conditions."
        ),
        arguments_schema=[
            {
                "name": "from",
                "type": "string",
                "required": "true",
                "description": "ISO-8601 UTC start of the window, e.g. "
                "2026-09-01T09:55:00Z.",
            },
            {
                "name": "to",
                "type": "string",
                "required": "true",
                "description": "ISO-8601 UTC end of the window, e.g. "
                "2026-09-01T10:05:00Z. Must be within 24h of 'from'.",
            },
            {
                "name": "conditions",
                "type": "array of {field, operator, value}",
                "required": "true",
                "description": "At least one condition. field ∈ "
                + ", ".join(_ORDERED_EVENT_FIELDS)
                + ". operator ∈ is, contain, exist, is_one_of, not_is, not_contain, "
                "not_exist, not_is_one_of. value depends on the operator.",
            },
            {
                "name": "limit",
                "type": "integer",
                "required": "false",
                "description": f"Max events to return, 1..{MAX_SEARCH_LIMIT} (default 100).",
            },
            {
                "name": "sort",
                "type": "string",
                "required": "false",
                "description": "'asc' or 'desc' (default 'desc').",
            },
        ],
    )


def detection_rule_tool_spec() -> ModelToolSpec:
    """The exact argument shape for ``hisiem.get_detection_rule``."""
    return ModelToolSpec(
        name="hisiem.get_detection_rule",
        description=(
            "Context for a detection rule referenced by the alert (rule metadata, "
            "never a decision instruction)."
        ),
        arguments_schema=[
            {
                "name": "rule_id",
                "type": "string",
                "required": "true",
                "description": "The detection rule id from the alert.",
            },
        ],
    )


def model_tool_specs() -> list[ModelToolSpec]:
    """The selectable tool specs the model may see (real, executable tools only).

    Kept in the contracts layer so the agent registry, the prompt builders, and the
    graph agree on the SAME catalog without importing infrastructure.
    """
    return [search_events_tool_spec(), detection_rule_tool_spec()]


def model_tool_specs_by_name() -> dict[str, ModelToolSpec]:
    return {spec.name: spec for spec in model_tool_specs()}
