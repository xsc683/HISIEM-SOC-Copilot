"""ToolPolicy — deterministic policy validation over model-selected tool calls.

The policy layer is where "the model chose a candidate, the system binds the
bounds". It enforces the bounded search windows / limits from
investigation-tool-contract.md §15 on top of the strict per-tool argument schema,
and it refuses execution when the investigation budget is exhausted.
"""

from __future__ import annotations

from datetime import datetime

from .args import SearchEventsArgs
from .registry import ToolRegistry

MAX_SINGLE_CALL_SPAN_HOURS = 24
SECONDS_PER_HOUR = 3600


class ToolPolicyError(ValueError):
    """Raised when a candidate violates a deterministic tool policy."""


class ToolBudgetExhausted(ToolPolicyError):
    """Raised when the investigation budget has no room for another tool call."""


def validate_search_span(args: SearchEventsArgs) -> None:
    """A single agent search must not exceed the Copilot 24h bounded window."""
    try:
        start = datetime.fromisoformat(args.from_.replace("Z", "+00:00"))
        end = datetime.fromisoformat(args.to.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolPolicyError(f"invalid search window: {exc}") from exc
    if end <= start:
        raise ToolPolicyError("search 'to' must be after 'from'")
    if (end - start).total_seconds() > MAX_SINGLE_CALL_SPAN_HOURS * SECONDS_PER_HOUR:
        raise ToolPolicyError(
            f"single search call must not exceed {MAX_SINGLE_CALL_SPAN_HOURS}h"
        )


def assert_budget_available(budget_remaining: int) -> None:
    if budget_remaining <= 0:
        raise ToolBudgetExhausted("investigation tool budget is exhausted")


def validate_candidate(
    registry: ToolRegistry, tool_name: str, budget_remaining: int
) -> None:
    """Registry + model-selectability + budget gates for a model-chosen tool."""
    if not registry.is_registered(tool_name):
        from .registry import UnknownToolError

        raise UnknownToolError(f"tool '{tool_name}' is not a registered read tool")
    assert_budget_available(budget_remaining)
