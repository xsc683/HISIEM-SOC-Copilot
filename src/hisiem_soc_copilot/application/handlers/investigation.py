"""Investigation command handlers.

Thin orchestration: build trusted context → load aggregate → apply domain
method → persist via UnitOfWork. No SQL, no ORM, no infrastructure imports here.
"""

from __future__ import annotations

from typing import TypeVar
from uuid import UUID, uuid4

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
from ..errors import NotFoundError
from ..ports.durable import DurableCommand
from ..ports.hisiem import HisiemPort
from ..ports.unit_of_work import UnitOfWork
from .durable_support import _audit_only_key, flush_events

_C = TypeVar("_C", StartAlertInvestigation, CancelInvestigation)


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
        """Create (or return the existing active) investigation for one alert.

        Two-step concurrency guard (persistence-schema.md §6):
        1. application pre-check returns the existing active investigation;
        2. the PostgreSQL partial unique index converges the race.

        A successful create commits the aggregate + ``InvestigationCreated``
        domain_event + outbox row + command_receipt atomically, then returns
        quickly. The durable dispatcher picks up the outbox row and starts the
        graph asynchronously (HTTP never waits for the agent).
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
        await flush_events(self._uow, investigation)
        await self._record_receipt(command, investigation.id)
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
            await flush_events(self._uow, investigation)
            await self._record_receipt(command, investigation.id)
            await self._uow.commit()
        return investigation

    async def _record_receipt(self, command: _C, investigation_id: UUID) -> None:
        key = command.idempotency_key or _audit_only_key(
            investigation_id=investigation_id,
            command_type=type(command).__name__,
            command_id=command.command_id,
        )
        await self._uow.command_receipts.record(
            DurableCommand(
                command_id=command.command_id,
                command_type=type(command).__name__,
                idempotency_key=key,
                tenant_id=command.tenant_id,
                aggregate_type="investigation",
                aggregate_id=investigation_id,
                correlation_id=command.correlation_id,
            )
        )
