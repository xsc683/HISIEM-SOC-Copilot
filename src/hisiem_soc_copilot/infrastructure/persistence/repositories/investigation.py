"""SQLAlchemy Investigation repository implementing the application port.

Optimistic locking: updates carry the aggregate's expected ``lock_version``; a zero
rowcount update raises OptimisticConcurrencyError. Reads are always tenant-scoped.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ....application.ports.repositories import InvestigationRepository
from ....domain.investigation.aggregate import Investigation
from ....domain.investigation.value_objects import ExternalResourceRef
from ....domain.shared.errors import OptimisticConcurrencyError
from ..mappers.investigation import to_domain, to_row
from ..orm.investigation import InvestigationRow


class SqlAlchemyInvestigationRepository(InvestigationRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> Investigation | None:
        result = await self._session.execute(
            select(InvestigationRow).where(
                InvestigationRow.id == investigation_id,
                InvestigationRow.tenant_id == tenant_id,
            )
        )
        row = result.scalar_one_or_none()
        return to_domain(row) if row is not None else None

    async def add(self, investigation: Investigation) -> None:
        self._session.add(to_row(investigation))

    async def update(self, investigation: Investigation) -> None:
        """Persist mutable lifecycle columns with an optimistic-lock UPDATE.

        The persisted ``lock_version`` must equal the aggregate's value; if it does
        we set it to ``lock_version + 1``. A 0-row update means another writer
        committed first → raise OptimisticConcurrencyError (never last-write-wins).
        """
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                update(InvestigationRow)
                .where(
                    InvestigationRow.id == investigation.id,
                    InvestigationRow.tenant_id == investigation.tenant_id,
                    InvestigationRow.lock_version == investigation.lock_version,
                )
                .values(
                    status=investigation.status.value,
                    phase=investigation.phase.value if investigation.phase else None,
                    current_plan_revision=investigation.current_plan_revision,
                    termination_reason=(
                        investigation.termination_reason.value
                        if investigation.termination_reason
                        else None
                    ),
                    revision=investigation.revision,
                    lock_version=investigation.lock_version + 1,
                    result_id=investigation.result_id,
                    response_proposal_id=investigation.response_proposal_id,
                    started_at=investigation.started_at,
                    finished_at=investigation.finished_at,
                    cancelled_at=investigation.cancelled_at,
                )
            ),
        )
        if result.rowcount == 0:
            raise OptimisticConcurrencyError(
                aggregate_type="investigation",
                aggregate_id=str(investigation.id),
            )
        investigation.lock_version += 1

    async def find_active_by_alert(
        self,
        *,
        tenant_id: str,
        source_alert_ref: ExternalResourceRef,
    ) -> Investigation | None:
        from ....domain.investigation.enums import InvestigationStatus

        active = tuple(s.value for s in InvestigationStatus if s.is_active)
        result = await self._session.execute(
            select(InvestigationRow).where(
                InvestigationRow.tenant_id == tenant_id,
                InvestigationRow.source_provider == source_alert_ref.provider,
                InvestigationRow.source_resource_type == source_alert_ref.resource_type,
                InvestigationRow.source_address_id == source_alert_ref.address_id,
                InvestigationRow.status.in_(active),
            )
        )
        row = result.scalars().first()
        return to_domain(row) if row is not None else None

    async def get_by_external_ref(
        self,
        *,
        tenant_id: str,
        provider: str,
        resource_type: str,
        address_id: str,
    ) -> Investigation | None:
        result = await self._session.execute(
            select(InvestigationRow).where(
                InvestigationRow.tenant_id == tenant_id,
                InvestigationRow.source_provider == provider,
                InvestigationRow.source_resource_type == resource_type,
                InvestigationRow.source_address_id == address_id,
            )
        )
        row = result.scalar_one_or_none()
        return to_domain(row) if row is not None else None
