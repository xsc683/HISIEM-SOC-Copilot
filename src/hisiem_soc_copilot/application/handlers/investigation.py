"""Investigation command handlers.

Thin orchestration: build trusted context → load aggregate → apply domain
method → persist via UnitOfWork. No SQL, no ORM, no infrastructure imports here.
"""

from __future__ import annotations

from uuid import uuid4

from ...domain.investigation.aggregate import Investigation
from ...domain.investigation.enums import InvestigationStatus
from ...domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from ...domain.shared.identifiers import utc_now
from ..commands.investigation import (
    CancelInvestigation,
    StartAlertInvestigation,
)
from ..ports.hisiem import HisiemPort
from ..ports.unit_of_work import UnitOfWork


class InvestigationCommandHandler:
    """Coordinates investigation lifecycle commands against the UoW."""

    def __init__(
        self,
        *,
        unit_of_work: UnitOfWork,
        hisiem: HisiemPort,
        budget_limits: BudgetLimits,
    ) -> None:
        self._uow = unit_of_work
        self._hisiem = hisiem
        self._budget_limits = budget_limits

    async def start_alert_investigation(
        self, command: StartAlertInvestigation
    ) -> Investigation:
        """Start (or return the existing active) investigation for one alert.

        Two-step concurrency guard (persistence-schema.md §6):
        1. application pre-check returns the existing active investigation;
        2. the PostgreSQL partial unique index converges the race.
        """
        try:
            return await self._start(command)
        finally:
            await self._uow.close()

    async def _start(self, command: StartAlertInvestigation) -> Investigation:
        alert_ref = ExternalResourceRef(
            provider="hisiem",
            resource_type="alert",
            address_id=command.source_alert_id,
        )

        existing = await self._uow.investigations.find_active_by_alert(
            tenant_id=command.tenant_id, source_alert_ref=alert_ref
        )
        if existing is not None:
            return existing

        # Authoritative hydration — never trusts client-declared alert content.
        # Per v1-user-flow-and-scope.md §20: an alert that does not exist or is not
        # accessible must not start an authoritative-less investigation.
        alert = await self._hisiem.get_alert(
            tenant_id=command.tenant_id, alert_id=command.source_alert_id
        )
        if alert is None:
            from ..errors import NotFoundError

            raise NotFoundError(
                "alert not found or not accessible",
                resource_type="alert",
                resource_id=command.source_alert_id,
            )

        actor = ActorRef(
            subject_id=command.initiated_by_subject,
            tenant_id=command.tenant_id,
            display_name=command.initiated_by_display_name,
        )

        investigation = Investigation.create(
            id=uuid4(),
            tenant_id=command.tenant_id,
            source_alert_ref=alert_ref,
            initiated_by=actor,
            budget_limits=self._budget_limits,
            now=utc_now(),
        )
        await self._uow.investigations.add(investigation)
        await self._uow.commit()
        return investigation

    async def cancel_investigation(self, command: CancelInvestigation) -> Investigation:
        try:
            return await self._cancel(command)
        finally:
            await self._uow.close()

    async def _cancel(self, command: CancelInvestigation) -> Investigation:
        investigation = await self._uow.investigations.get(
            tenant_id=command.tenant_id,
            investigation_id=command.investigation_id,
        )
        if investigation is None:
            from ..errors import NotFoundError

            raise NotFoundError(
                "investigation not found",
                resource_type="investigation",
                resource_id=str(command.investigation_id),
            )
        if investigation.status in (
            InvestigationStatus.CREATED,
            InvestigationStatus.RUNNING,
            InvestigationStatus.WAITING_APPROVAL,
        ):
            actor = ActorRef(
                subject_id=command.initiated_by_subject,
                tenant_id=command.tenant_id,
            )
            investigation.cancel(actor=actor)
            await self._uow.investigations.update(investigation)
            await self._uow.commit()
        return investigation
