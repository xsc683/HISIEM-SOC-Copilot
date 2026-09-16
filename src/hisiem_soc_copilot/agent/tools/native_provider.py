"""Thin NativeToolProvider adapter around the sealed native executor."""

from __future__ import annotations

from typing import cast

from ...contracts.tools.types import ToolCandidate
from .executor import ToolExecution, ToolExecutor
from .providers import (
    AdmissionEntry,
    ProviderCapability,
    ProviderIdentity,
    ProviderInvocationContext,
    ProviderInvocationResult,
    ProviderResultStatus,
)


class NativeToolProvider:
    """Adapt the existing native ToolExecutor without duplicating its behavior."""

    identity = ProviderIdentity(provider_type="native", server_category="internal")

    def __init__(self, executor: ToolExecutor) -> None:
        self._executor = executor

    async def discover(self) -> list[ProviderCapability]:
        return []

    async def invoke(
        self,
        *,
        admission: AdmissionEntry,
        candidate: ToolCandidate,
        context: ProviderInvocationContext,
    ) -> ProviderInvocationResult:
        execution: ToolExecution = await self._executor.execute(
            candidate=candidate,
            tenant_id=context.tenant_id,
            source_alert_ref=dict(context.source_alert_ref),
            tool_call_id=context.tool_call_id,
            investigation_id=context.investigation_id,
            budget_remaining=context.budget_remaining,
            budget_already_reserved=context.budget_already_reserved,
        )
        result = execution.result
        status: ProviderResultStatus = (
            cast(ProviderResultStatus, result.status)
            if result.status in ("SUCCESS", "NO_DATA", "REJECTED")
            else "UNAVAILABLE"
        )
        return ProviderInvocationResult(
            tool_name=execution.tool_name,
            status=status,
            data=dict(result.data),
            provider=self.identity,
        )


_NATIVE_TOOL_NAMES = (
    "hisiem.search_events",
    "hisiem.get_detection_rule",
    "knowledge.retrieve_security_guidance",
    "knowledge.resolve_attack_technique",
)


def native_admissions() -> tuple[AdmissionEntry, ...]:
    """Return router-only admissions for existing native read tools."""
    return tuple(
        AdmissionEntry(
            internal_name=name,
            description="Existing native read-only tool",
            server_id="native",
            external_name=name,
            tenant_scope="TENANT_SCOPED",
        )
        for name in _NATIVE_TOOL_NAMES
    )
