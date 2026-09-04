"""In-memory fakes of application ports, shared across application unit tests."""

from __future__ import annotations

from uuid import UUID

from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.value_objects import ExternalResourceRef


class FakeInvestigationRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, Investigation] = {}
        self.added: list[Investigation] = []
        self.updated: list[Investigation] = []

    async def get(self, *, tenant_id: str, investigation_id: UUID) -> Investigation | None:
        inv = self._store.get(investigation_id)
        if inv is None or inv.tenant_id != tenant_id:
            return None
        return inv

    async def add(self, investigation: Investigation) -> None:
        self._store[investigation.id] = investigation
        self.added.append(investigation)

    async def update(self, investigation: Investigation) -> None:
        if investigation.id not in self._store:
            raise KeyError("update of missing investigation")
        self._store[investigation.id] = investigation
        self.updated.append(investigation)

    async def find_active_by_alert(
        self, *, tenant_id: str, source_alert_ref: ExternalResourceRef
    ) -> Investigation | None:
        for inv in self._store.values():
            if inv.tenant_id != tenant_id:
                continue
            if inv.source_alert_ref != source_alert_ref:
                continue
            if inv.status.is_active:
                return inv
        return None

    async def get_by_external_ref(
        self, *, tenant_id: str, provider: str, resource_type: str, address_id: str
    ) -> Investigation | None:
        for inv in self._store.values():
            if (
                inv.tenant_id == tenant_id
                and inv.source_alert_ref.provider == provider
                and inv.source_alert_ref.resource_type == resource_type
                and inv.source_alert_ref.address_id == address_id
            ):
                return inv
        return None


class FakeUnitOfWork:
    """Minimal UoW with just the investigation repo (no real transaction)."""

    def __init__(self) -> None:
        self.investigations = FakeInvestigationRepository()
        self._commits = 0

    @property
    def commits(self) -> int:
        return self._commits

    async def commit(self) -> None:
        self._commits += 1

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        pass
