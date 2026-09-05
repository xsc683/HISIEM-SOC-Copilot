"""Tool Registry — the deterministic read-only allowlist.

V1 only registers READ_ONLY tools (investigation-tool-contract.md §3), and the
registry's model-selectable surface is EXACTLY the set the executor actually
implements. ``hisiem.get_alert_context`` is system-controlled (the graph hydrate
node calls the HISIEM get_alert directly) and is never offered to the model.

Spec-defined but NOT-yet-implemented tools (entity activity, threat-intel,
knowledge lookups) are cataloged separately (``FUTURE_CATALOG_TOOLS``) purely as
documentation — they are NOT registered, so the model can never select a tool with
no executor, schema, or policy backing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ToolCapability = Literal["READ_ONLY"]

SYSTEM_CONTROLLED_TOOL = "hisiem.get_alert_context"

# Exactly the tools the ToolExecutor implements (investigation-tool-contract.md §6).
AGENT_SELECTABLE_TOOLS: frozenset[str] = frozenset(
    {
        "hisiem.search_events",
        "hisiem.get_detection_rule",
    }
)

# Spec'd but not-yet-implemented read tools. Kept as documentation/catalog only —
# never registered, so a model can never select a tool without a real executor.
FUTURE_CATALOG_TOOLS: frozenset[str] = frozenset(
    {
        "hisiem.get_entity_activity",
        "threat_intel.lookup_ip",
        "knowledge.retrieve_security_guidance",
        "knowledge.resolve_attack_technique",
    }
)

_REGISTERED_TOOLS: frozenset[str] = AGENT_SELECTABLE_TOOLS | {SYSTEM_CONTROLLED_TOOL}

FORBIDDEN_TOOLS: frozenset[str] = frozenset(
    {
        "execute_shell",
        "run_script",
        "raw_http_request",
        "raw_elasticsearch_query",
        "write_alert",
        "set_alert_verdict",
        "close_alert",
        "create_case",
        "modify_case",
        "block_ip",
        "disable_user",
        "isolate_host",
        "start_soar_execution",
        "approve_response",
    }
)


@dataclass(frozen=True)
class ToolSpec:
    """Metadata for one registered read tool."""

    name: str
    capability: ToolCapability = "READ_ONLY"
    description: str = ""
    model_selectable: bool = True


class UnknownToolError(KeyError):
    """Raised when the model selects a tool name that is not registered."""


class ToolRegistry:
    """Owns the allowlist and resolves a candidate tool name to its executor."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {
            "hisiem.search_events": ToolSpec(
                name="hisiem.search_events",
                description=(
                    "Search HISIEM events over a bounded window/conditions."
                ),
            ),
            "hisiem.get_detection_rule": ToolSpec(
                name="hisiem.get_detection_rule",
                description=(
                    "Context for a detection rule referenced by the alert."
                ),
            ),
            SYSTEM_CONTROLLED_TOOL: ToolSpec(
                name=SYSTEM_CONTROLLED_TOOL,
                description=(
                    "Authoritative alert context (system-controlled, not model-selectable)."
                ),
                model_selectable=False,
            ),
        }

    def is_registered(self, tool_name: str) -> bool:
        return tool_name in self._tools

    def get(self, tool_name: str) -> ToolSpec:
        spec = self._tools.get(tool_name)
        if spec is None:
            raise UnknownToolError(
                f"tool '{tool_name}' is not a registered read tool"
            )
        return spec

    @property
    def model_selectable_names(self) -> list[str]:
        return sorted(
            spec.name
            for spec in self._tools.values()
            if spec.model_selectable
        )
