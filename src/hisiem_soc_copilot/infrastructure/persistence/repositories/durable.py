"""SQLAlchemy durable-runtime repositories (events/outbox/receipt/audit/binding).

Each method is called INSIDE an existing UnitOfWork transaction (the handler owns
commit/rollback), so the rows they insert are atomic with the domain rows
(persistence-schema.md §31). The outbox claim/mark methods used by the dispatcher
each run in their OWN transaction via a session factory — never inside a graph/
LLM/network transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....application.ports.durable import (
    CommandReceiptStore,
    DomainEventEnvelope,
    DurableCommand,
    EventLedger,
    OrchestrationBinding,
    OrchestrationBindingStore,
    OutboxRecord,
    OutboxStore,
    ToolInvocationRecord,
    ToolInvocationStore,
)
from ....domain.investigation.events import InvestigationEvent
from ..orm.events import (
    CommandReceiptRow,
    DomainEventRow,
    OrchestrationBindingRow,
    OutboxMessageRow,
    ToolInvocationRow,
)
from ..orm.investigation import InvestigationRow

_OUTBOX_DESTINATION = "investigation.graph.run"

# Events that must trigger an outbound delivery. In V1 the only outbox consumer
# is the durable graph runner, which is launched once when an investigation is
# created. Graph-internal events (phase/evidence/hypothesis/finding...) are
# persisted as domain_event rows for audit but never enqueue a delivery, so a
# running investigation does not pile up duplicate dispatch work.
_ORCHESTRATION_EVENTS: frozenset[str] = frozenset({"investigation_created"})


class SqlAlchemyEventLedger(EventLedger):
    """Appends domain_event + outbox rows in the current transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self, event: InvestigationEvent, *, aggregate_revision: int
    ) -> None:
        now = datetime.now(UTC)
        self._session.add(
            DomainEventRow(
                event_id=event.event_id,
                event_type=event.event_type,
                event_version=event.version,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                aggregate_revision=aggregate_revision,
                tenant_id=event.tenant_id or "",
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
                actor_subject_id=event.actor_subject_id,
                payload=event.payload,
                occurred_at=event.occurred_at,
            )
        )
        if event.event_type in _ORCHESTRATION_EVENTS:
            self._session.add(
                OutboxMessageRow(
                    id=uuid4(),
                    event_id=event.event_id,
                    destination=_OUTBOX_DESTINATION,
                    status="PENDING",
                    attempt_count=0,
                    available_at=now,
                    created_at=now,
                )
            )

    async def get(self, *, event_id: UUID) -> DomainEventEnvelope | None:
        result = await self._session.execute(
            select(DomainEventRow).where(DomainEventRow.event_id == event_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return DomainEventEnvelope(
            event_id=row.event_id,
            event_type=row.event_type,
            aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id,
            tenant_id=row.tenant_id,
            correlation_id=row.correlation_id,
            causation_id=row.causation_id,
            actor_subject_id=row.actor_subject_id,
            payload=dict(row.payload or {}),
            occurred_at=row.occurred_at,
        )


class SqlAlchemyCommandReceiptStore(CommandReceiptStore):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(self, *, idempotency_key: str) -> bool:
        result = await self._session.execute(
            select(CommandReceiptRow.idempotency_key).where(
                CommandReceiptRow.idempotency_key == idempotency_key
            )
        )
        return result.scalar_one_or_none() is not None

    async def get_safe_result(
        self, *, idempotency_key: str
    ) -> dict[str, Any] | None:
        result = await self._session.execute(
            select(CommandReceiptRow.safe_result).where(
                CommandReceiptRow.idempotency_key == idempotency_key
            )
        )
        return result.scalar_one_or_none()

    async def record(self, receipt: DurableCommand) -> None:
        self._session.add(
            CommandReceiptRow(
                idempotency_key=receipt.idempotency_key,
                command_id=receipt.command_id,
                command_type=receipt.command_type,
                tenant_id=receipt.tenant_id,
                aggregate_type=receipt.aggregate_type,
                aggregate_id=receipt.aggregate_id,
                result_ref_type=None,
                result_ref_id=None,
                safe_result=receipt.safe_result,
                completed_at=datetime.now(UTC),
            )
        )


class SqlAlchemyOrchestrationBindingStore(OrchestrationBindingStore):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> OrchestrationBinding | None:
        row = await self._session.execute(
            select(OrchestrationBindingRow)
            .join(
                InvestigationRow,
                InvestigationRow.id == OrchestrationBindingRow.investigation_id,
            )
            .where(
                InvestigationRow.tenant_id == tenant_id,
                OrchestrationBindingRow.investigation_id == investigation_id,
            )
        )
        obj = row.scalar_one_or_none()
        return _binding_row(obj) if obj is not None else None

    async def get_by_thread_id(
        self, *, thread_id: str
    ) -> OrchestrationBinding | None:
        row = await self._session.execute(
            select(OrchestrationBindingRow).where(
                OrchestrationBindingRow.thread_id == thread_id
            )
        )
        obj = row.scalar_one_or_none()
        return _binding_row(obj) if obj is not None else None

    async def put(self, binding: OrchestrationBinding) -> None:
        self._session.add(
            OrchestrationBindingRow(
                investigation_id=binding.investigation_id,
                thread_id=binding.thread_id,
                graph_name=binding.graph_name,
                graph_version=binding.graph_version,
                state_schema_version=binding.state_schema_version,
                created_at=datetime.now(UTC),
            )
        )


def _binding_row(row: OrchestrationBindingRow) -> OrchestrationBinding:
    return OrchestrationBinding(
        investigation_id=row.investigation_id,
        thread_id=row.thread_id,
        graph_name=row.graph_name,
        graph_version=row.graph_version,
        state_schema_version=row.state_schema_version,
    )


class SqlAlchemyToolInvocationStore(ToolInvocationStore):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_started(
        self, *, tenant_id: str, record: ToolInvocationRecord
    ) -> None:
        # The caller may have run this exact call before a crash (its checkpoint
        # was lost but the audit row committed). UNIQUE(investigation_id,
        # idempotency_key) then conflicts → keep the surviving row, never error.
        await self._session.execute(
            pg_insert(ToolInvocationRow)
            .values(
                id=record.id,
                investigation_id=record.investigation_id,
                tool_name=record.tool_name,
                tool_version=record.tool_version,
                idempotency_key=record.idempotency_key,
                arguments=record.arguments or {},
                status="RUNNING",
                provider_request_id=record.provider_request_id,
                started_at=record.started_at,
                finished_at=None,
            )
            .on_conflict_do_nothing(
                constraint="uq_tool_invocation_investigation_key"
            )
        )

    async def finish(
        self,
        *,
        tenant_id: str,
        investigation_id: UUID,
        idempotency_key: str,
        status: str,
        finished_at: datetime,
        error_code: str | None = None,
        safe_error_message: str | None = None,
        result_metadata: dict[str, Any] | None = None,
    ) -> None:
        if status not in ("SUCCEEDED", "FAILED"):
            raise ValueError(
                f"tool invocation must finish SUCCEEDED or FAILED, got {status}"
            )
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                update(ToolInvocationRow)
                .where(
                    ToolInvocationRow.investigation_id == investigation_id,
                    ToolInvocationRow.idempotency_key == idempotency_key,
                )
                .values(
                    status=status,
                    finished_at=finished_at,
                    error_code=error_code,
                    safe_error_message=safe_error_message,
                    result_metadata=result_metadata,
                )
            ),
        )
        if result.rowcount == 0:
            raise KeyError(
                f"tool invocation {investigation_id}/{idempotency_key} not found"
            )

    async def find_by_key(
        self,
        *,
        tenant_id: str,
        investigation_id: UUID,
        idempotency_key: str,
    ) -> ToolInvocationRecord | None:
        row = await self._session.execute(
            select(ToolInvocationRow)
            .join(
                InvestigationRow,
                InvestigationRow.id == ToolInvocationRow.investigation_id,
            )
            .where(
                InvestigationRow.tenant_id == tenant_id,
                ToolInvocationRow.investigation_id == investigation_id,
                ToolInvocationRow.idempotency_key == idempotency_key,
            )
        )
        obj = row.scalar_one_or_none()
        return _tool_row(obj) if obj is not None else None


def _tool_row(row: ToolInvocationRow) -> ToolInvocationRecord:
    return ToolInvocationRecord(
        id=row.id,
        investigation_id=row.investigation_id,
        tool_name=row.tool_name,
        tool_version=row.tool_version,
        idempotency_key=row.idempotency_key,
        status=row.status,
        started_at=row.started_at,
        finished_at=row.finished_at,
        arguments=dict(row.arguments or {}),
        provider_request_id=row.provider_request_id,
        error_code=row.error_code,
        safe_error_message=row.safe_error_message,
        result_metadata=dict(row.result_metadata or {}),
    )


class SqlAlchemyOutboxStore(OutboxStore):
    """Outbox store that opens its OWN transactions per claim/state change."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def claim_batch(
        self, *, worker: str, limit: int, available_before: datetime
    ) -> list[OutboxRecord]:
        async with self._factory() as session:
            rows = await session.execute(
                select(OutboxMessageRow)
                .where(
                    OutboxMessageRow.status.in_(["PENDING", "FAILED"]),
                    OutboxMessageRow.available_at <= available_before,
                    OutboxMessageRow.attempt_count < 10,
                )
                .order_by(OutboxMessageRow.created_at)
                .limit(limit)
            )
            claimed: list[OutboxRecord] = []
            for row in rows.scalars().all():
                now = datetime.now(UTC)
                updated = cast(
                    "CursorResult[object]",
                    await session.execute(
                        update(OutboxMessageRow)
                        .where(
                            OutboxMessageRow.id == row.id,
                            OutboxMessageRow.status.in_(["PENDING", "FAILED"]),
                        )
                        .values(
                            status="PROCESSING",
                            locked_at=now,
                            locked_by=worker,
                            attempt_count=row.attempt_count + 1,
                        )
                    ),
                )
                if updated.rowcount == 0:
                    continue  # another worker claimed it first
                claimed.append(
                    OutboxRecord(
                        id=row.id,
                        event_id=row.event_id,
                        destination=row.destination,
                        status="PROCESSING",
                        attempt_count=row.attempt_count + 1,
                        available_at=row.available_at,
                        locked_at=now,
                        locked_by=worker,
                    )
                )
            await session.commit()
            return claimed

    async def mark_published(
        self, *, outbox_id: UUID, published_at: datetime
    ) -> bool:
        async with self._factory() as session:
            result = cast(
                "CursorResult[object]",
                await session.execute(
                    update(OutboxMessageRow)
                    .where(
                        OutboxMessageRow.id == outbox_id,
                        OutboxMessageRow.status == "PROCESSING",
                    )
                    .values(
                        status="PUBLISHED",
                        published_at=published_at,
                        locked_at=None,
                        locked_by=None,
                        last_error_code=None,
                    )
                ),
            )
            await session.commit()
            return result.rowcount > 0

    async def mark_failed(
        self, *, outbox_id: UUID, error_code: str, next_available_at: datetime
    ) -> bool:
        async with self._factory() as session:
            result = cast(
                "CursorResult[object]",
                await session.execute(
                    update(OutboxMessageRow)
                    .where(
                        OutboxMessageRow.id == outbox_id,
                        OutboxMessageRow.status == "PROCESSING",
                    )
                    .values(
                        status="FAILED",
                        available_at=next_available_at,
                        locked_at=None,
                        locked_by=None,
                        last_error_code=error_code,
                    )
                ),
            )
            await session.commit()
            return result.rowcount > 0

    async def settle_dead_letter(
        self, *, outbox_id: UUID, error_code: str
    ) -> bool:
        async with self._factory() as session:
            result = cast(
                "CursorResult[object]",
                await session.execute(
                    update(OutboxMessageRow)
                    .where(
                        OutboxMessageRow.id == outbox_id,
                        OutboxMessageRow.status == "PROCESSING",
                    )
                    .values(
                        status="FAILED",
                        locked_at=None,
                        locked_by=None,
                        last_error_code=error_code,
                        available_at=datetime.now(UTC),
                    )
                ),
            )
            await session.commit()
            return result.rowcount > 0
