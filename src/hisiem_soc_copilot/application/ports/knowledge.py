"""Security knowledge / MITRE / Runbook retrieval port."""

from __future__ import annotations

from typing import Protocol


class KnowledgePort(Protocol):
    async def retrieve(self, *, query: str, tenant_id: str) -> list[dict[str, object]]: ...


class MitrePort(Protocol):
    async def lookup_technique(
        self, *, technique_id: str, framework: str = "mitre-attack"
    ) -> dict[str, object] | None: ...
