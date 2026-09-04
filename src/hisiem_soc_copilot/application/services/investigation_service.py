"""Read-side application service for Investigation workspace/overview.

Read models are built by loading the aggregate via the repository port — never by
querying ORM rows at this layer. Each read closes its UnitOfWork session.
"""

from __future__ import annotations

from uuid import UUID

from ...domain.investigation.aggregate import Investigation
from ..errors import NotFoundError
from ..ports.unit_of_work import UnitOfWork
from ..queries.investigation import InvestigationReadModel


def _to_read_model(investigation: Investigation) -> InvestigationReadModel:
    return InvestigationReadModel(
        investigation_id=investigation.id,
        tenant_id=investigation.tenant_id,
        source_provider=investigation.source_alert_ref.provider,
        source_resource_type=investigation.source_alert_ref.resource_type,
        source_address_id=investigation.source_alert_ref.address_id,
        status=investigation.status.value,
        phase=investigation.phase.value if investigation.phase else None,
        initiated_by=investigation.initiated_by.subject_id,
        created_at=investigation.created_at,
        started_at=investigation.started_at,
        finished_at=investigation.finished_at,
        current_plan_revision=investigation.current_plan_revision,
        termination_reason=(
            investigation.termination_reason.value
            if investigation.termination_reason
            else None
        ),
    )


class InvestigationReadService:
    def __init__(self, *, unit_of_work: UnitOfWork) -> None:
        self._uow = unit_of_work

    async def get(self, *, tenant_id: str, investigation_id: UUID) -> InvestigationReadModel:
        try:
            investigation = await self._uow.investigations.get(
                tenant_id=tenant_id, investigation_id=investigation_id
            )
        finally:
            await self._uow.close()
        if investigation is None:
            raise NotFoundError(
                "investigation not found",
                resource_type="investigation",
                resource_id=str(investigation_id),
            )
        return _to_read_model(investigation)

    async def find_active_by_alert(
        self, *, tenant_id: str, source_address_id: str
    ) -> InvestigationReadModel | None:
        from ...domain.investigation.value_objects import ExternalResourceRef

        ref = ExternalResourceRef(
            provider="hisiem", resource_type="alert", address_id=source_address_id
        )
        try:
            investigation = await self._uow.investigations.find_active_by_alert(
                tenant_id=tenant_id, source_alert_ref=ref
            )
        finally:
            await self._uow.close()
        if investigation is None:
            return None
        return _to_read_model(investigation)
