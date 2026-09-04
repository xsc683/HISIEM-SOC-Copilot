"""Tool Registry — the deterministic read-only allowlist.

V1 only registers READ_ONLY tools (investigation-tool-contract.md §3). The model
can never select an unregistered name. ``hisiem.get_alert_context`` is system-
controlled (used by the graph hydrate node, never offered to the model).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ToolCapability = Literal["READ_ONLY"]

SYSTEM_CONTROLLED_TOOL = "hisiem.get_alert_context"

AGENT_SELECTABLE_TOOLS: frozenset[str] = frozenset(
    {
        "hisiem.search_events",
        "hisiem.get_entity_activity",
        "hisiem.get_detection_rule",
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
            name: ToolSpec(name=name, description=_tool_description(name))
            for name in _REGISTERED_TOOLS
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


def _tool_description(name: str) -> str:
    return {
        "hisiem.get_alert_context": (
            "Authoritative alert context (system-controlled, not model-selectable)."
        ),
        "hisiem.search_events": "Search HISIEM events over a bounded window/conditions.",
        "hisiem.get_entity_activity": "Bounded activity for a known entity (IP/USER/HOST).",
        "hisiem.get_detection_rule": "Context for a detection rule referenced by the alert.",
        "threat_intel.lookup_ip": "External IP reputation lookup.",
        "knowledge.retrieve_security_guidance": "Retrieve bounded security guidance.",
        "knowledge.resolve_attack_technique": "Resolve a MITRE ATT&CK technique reference.",
    }.get(name, "")
