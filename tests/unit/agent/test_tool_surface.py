"""Registry == actually-executable tool surface.

The tool surface a model may select must be EXACTLY the tools the executor
implements (schema + policy + provider adapter exist). A future-catalog tool that
has no executor must never appear in ``model_selectable_names``; the system-
controlled alert-context helper is never model-selectable either.

Pinning this prevents a registry/executor drift where the model is offered a tool
the runtime cannot actually execute.
"""

from __future__ import annotations

from hisiem_soc_copilot.agent.tools.registry import (
    AGENT_SELECTABLE_TOOLS,
    FUTURE_CATALOG_TOOLS,
    SYSTEM_CONTROLLED_TOOL,
    ToolRegistry,
)

# The set of tools the executor actually dispatches (schema + policy + adapter).
_EXECUTOR_CAPABLE = frozenset(
    {
        "hisiem.search_events",
        "hisiem.get_detection_rule",
    }
)


def test_model_selectable_surface_is_exactly_executor_capable() -> None:
    registry = ToolRegistry()
    # The model can only select tools the executor can run.
    selectable = set(registry.model_selectable_names)
    assert selectable == _EXECUTOR_CAPABLE
    # And every model-selectable tool is advertised as agent-selectable.
    assert selectable == set(AGENT_SELECTABLE_TOOLS)


def test_system_controlled_tool_never_model_selectable() -> None:
    registry = ToolRegistry()
    assert registry.is_registered(SYSTEM_CONTROLLED_TOOL)
    assert SYSTEM_CONTROLLED_TOOL not in registry.model_selectable_names


def test_future_catalog_tools_are_not_registered_or_selectable() -> None:
    """Spec'd-but-unimplemented tools stay OUT of the runtime registry."""
    registry = ToolRegistry()
    for tool in FUTURE_CATALOG_TOOLS:
        assert not registry.is_registered(tool)
        assert tool not in registry.model_selectable_names


def test_registry_selectable_has_executor_backing() -> None:
    """Structural guard: selecting any model-selectable name must not raise a
    'no executor' error (it must map to an executor dispatch branch)."""
    registry = ToolRegistry()
    for name in registry.model_selectable_names:
        assert name in _EXECUTOR_CAPABLE
