"""Tool argument schemas + strict parse (per-tool, no generic ``execute``).

The model only supplies a ToolCandidate; each tool parses + validates its own
arguments into a typed dataclass BEFORE any policy/provider call. Invalid or
unknown arguments are rejected deterministically here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...contracts.tools.types import (
    ALLOWED_EVENT_FIELDS,
    ALLOWED_LOGICAL_OPERATORS,
    MAX_SEARCH_LIMIT,
    LogSearchCondition,
)


class ToolArgumentError(ValueError):
    """Raised when a tool candidate's arguments fail its strict schema."""


@dataclass(frozen=True)
class SearchEventsArgs:
    from_: str
    to: str
    conditions: list[LogSearchCondition]
    limit: int = 100
    sort: str = "desc"


@dataclass(frozen=True)
class DetectionRuleArgs:
    rule_id: str


@dataclass(frozen=True)
class ResolveTechniqueArgs:
    technique_id: str


def _iso_datetime(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolArgumentError("from/to must be ISO-8601 UTC strings")
    return value


def _int_between(value: Any, *, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolArgumentError(f"{name} must be an integer")
    if not (low <= value <= high):
        raise ToolArgumentError(f"{name} must be between {low} and {high}")
    return value


def _str_required(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolArgumentError(f"{name} is required")
    return value.strip()


def parse_search_events(arguments: dict[str, object]) -> SearchEventsArgs:
    from_ = _iso_datetime(arguments.get("from") or arguments.get("from_"))
    to = _iso_datetime(arguments.get("to"))
    raw_conditions = arguments.get("conditions")
    if not isinstance(raw_conditions, list) or not raw_conditions:
        raise ToolArgumentError("search_events requires a non-empty conditions list")
    conditions: list[LogSearchCondition] = []
    for raw in raw_conditions:
        if not isinstance(raw, dict):
            raise ToolArgumentError("each condition must be an object")
        field = raw.get("field")
        operator = raw.get("operator")
        value = raw.get("value")
        if not isinstance(field, str):
            raise ToolArgumentError("condition.field must be a string")
        if field not in ALLOWED_EVENT_FIELDS:
            raise ToolArgumentError(f"condition.field '{field}' is not allowlisted")
        if not isinstance(operator, str) or operator not in ALLOWED_LOGICAL_OPERATORS:
            raise ToolArgumentError(f"condition.operator '{operator}' is not allowed")
        conditions.append(LogSearchCondition(field=field, operator=operator, value=value))
    limit = _int_between(arguments.get("limit", 100), low=1, high=MAX_SEARCH_LIMIT, name="limit")
    sort = arguments.get("sort", "desc")
    if sort not in ("asc", "desc"):
        raise ToolArgumentError("sort must be 'asc' or 'desc'")
    return SearchEventsArgs(from_=from_, to=to, conditions=conditions, limit=limit, sort=sort)


def parse_detection_rule(arguments: dict[str, object]) -> DetectionRuleArgs:
    rule_id = _str_required(arguments.get("rule_id"), name="rule_id")
    return DetectionRuleArgs(rule_id=rule_id)


def parse_resolve_technique(arguments: dict[str, object]) -> ResolveTechniqueArgs:
    technique_id = _str_required(arguments.get("technique_id"), name="technique_id")
    return ResolveTechniqueArgs(technique_id=technique_id.upper())
