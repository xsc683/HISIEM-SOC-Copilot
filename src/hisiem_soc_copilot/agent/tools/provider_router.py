"""Single provider router used by the existing ToolExecutor."""

from __future__ import annotations

from collections.abc import Iterable

from ...contracts.tools.types import ToolCandidate
from .providers import (
    AdmissionEntry,
    ProviderInvocationContext,
    ProviderInvocationResult,
    ToolProvider,
)


class ProviderRouter:
    """Routes only explicitly admitted names; it never routes by user input."""

    def __init__(self, providers: Iterable[tuple[AdmissionEntry, ToolProvider]] = ()) -> None:
        self._routes: dict[str, tuple[AdmissionEntry, ToolProvider]] = {}
        for admission, provider in providers:
            self.add(admission, provider)

    def add(self, admission: AdmissionEntry, provider: ToolProvider) -> None:
        if admission.internal_name in self._routes:
            raise ValueError(f"duplicate provider route: {admission.internal_name}")
        self._routes[admission.internal_name] = (admission, provider)

    def has(self, tool_name: str) -> bool:
        return tool_name in self._routes

    def admission(self, tool_name: str) -> AdmissionEntry | None:
        route = self._routes.get(tool_name)
        return route[0] if route else None

    def provider(self, tool_name: str) -> ToolProvider | None:
        route = self._routes.get(tool_name)
        return route[1] if route else None

    def model_admissions(self) -> list[AdmissionEntry]:
        return [entry for entry, _ in self._routes.values() if entry.is_model_selectable]

    async def invoke(
        self,
        *,
        candidate: ToolCandidate,
        context: ProviderInvocationContext,
    ) -> ProviderInvocationResult:
        route = self._routes.get(candidate.tool_name)
        if route is None:
            raise KeyError(f"tool '{candidate.tool_name}' has no admitted provider")
        admission, provider = route
        if not admission.is_model_selectable:
            return ProviderInvocationResult(
                tool_name=candidate.tool_name,
                status="REJECTED",
                provider=provider.identity,
            )
        return await provider.invoke(admission=admission, candidate=candidate, context=context)
