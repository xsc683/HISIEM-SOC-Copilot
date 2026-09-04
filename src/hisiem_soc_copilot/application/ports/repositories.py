"""Repository / UnitOfWork ports.

Application code depends on these Protocols only — never on infrastructure. Every
public repository query is tenant-scoped (python-package-boundary.md §7); bare
``get(investigation_id)`` is forbidden.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from ...domain.investigation.aggregate import Investigation
from ...domain.investigation.entities import (
    Evidence,
    Finding,
    Hypothesis,
    InvestigationResult,
    PlanRevision,
)
from ...domain.investigation.value_objects import (
    ExternalResourceRef,
)


class InvestigationRepository(Protocol):
    """Loads and stores the Investigation aggregate."""

    async def get(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> Investigation | None: ...

    async def add(self, investigation: Investigation) -> None: ...

    async def update(self, investigation: Investigation) -> None: ...

    async def find_active_by_alert(
        self,
        *,
        tenant_id: str,
        source_alert_ref: ExternalResourceRef,
    ) -> Investigation | None: ...

    async def get_by_external_ref(
        self, *, tenant_id: str, provider: str, resource_type: str, address_id: str
    ) -> Investigation | None: ...


class EvidenceRepository(Protocol):
    """Append-only evidence ledger access."""

    async def add(self, evidence: Evidence) -> None: ...

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[Evidence]: ...


class FindingRepository(Protocol):
    async def add(self, finding: Finding) -> None: ...

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[Finding]: ...


class HypothesisRepository(Protocol):
    async def add(self, hypothesis: Hypothesis) -> None: ...

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[Hypothesis]: ...


class ResultRepository(Protocol):
    async def add(self, result: InvestigationResult) -> None: ...

    async def get_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> InvestigationResult | None: ...


class PlanRevisionRepository(Protocol):
    async def add(self, plan_revision: PlanRevision) -> None: ...

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[PlanRevision]: ...
