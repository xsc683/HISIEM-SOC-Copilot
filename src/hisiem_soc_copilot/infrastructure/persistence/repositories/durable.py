"""SQLAlchemy durable-runtime repositories (events/outbox/receipt/audit/binding).

Each method is called INSIDE an existing UnitOfWork transaction (the handler owns
commit/rollback), so the rows they insert are atomic with the domain rows
(persistence-schema.md §31). The outbox claim/mark methods used by the dispatcher
each run in their OWN transaction via a session factory — never inside a graph/
LLM/network transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ....application.ports.durable import (
    CommandReceiptRecord,
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

    async def exists(
        self, *, tenant_id: str, command_type: str, idempotency_key: str
    ) -> bool:
        result = await self._session.execute(
            select(CommandReceiptRow.id).where(
                CommandReceiptRow.tenant_id == tenant_id,
                CommandReceiptRow.command_type == command_type,
                CommandReceiptRow.idempotency_key == idempotency_key,
            )
        )
        return result.scalar_one_or_none() is not None

    async def get_safe_result(
        self, *, tenant_id: str, command_type: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        result = await self._session.execute(
            select(CommandReceiptRow.safe_result).where(
                CommandReceiptRow.tenant_id == tenant_id,
                CommandReceiptRow.command_type == command_type,
                CommandReceiptRow.idempotency_key == idempotency_key,
            )
        )
        return result.scalar_one_or_none()

    async def find(
        self, *, tenant_id: str, command_type: str, idempotency_key: str
    ) -> CommandReceiptRecord | None:
        result = await self._session.execute(
            select(CommandReceiptRow).where(
                CommandReceiptRow.tenant_id == tenant_id,
                CommandReceiptRow.command_type == command_type,
                CommandReceiptRow.idempotency_key == idempotency_key,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return CommandReceiptRecord(
            idempotency_key=row.idempotency_key,
            command_type=row.command_type,
            tenant_id=row.tenant_id,
            aggregate_id=row.aggregate_id,
            request_fingerprint=row.request_fingerprint,
            safe_result=dict(row.safe_result) if row.safe_result else None,
        )

    async def record(self, receipt: DurableCommand) -> None:
        self._session.add(
            CommandReceiptRow(
                id=uuid4(),
                idempotency_key=receipt.idempotency_key,
                command_id=receipt.command_id,
                command_type=receipt.command_type,
                tenant_id=receipt.tenant_id,
                aggregate_type=receipt.aggregate_type,
                aggregate_id=receipt.aggregate_id,
                result_ref_type=None,
                result_ref_id=None,
                safe_result=receipt.safe_result,
                request_fingerprint=receipt.request_fingerprint,
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
        self,
        *,
        worker: str,
        limit: int,
        available_before: datetime,
        lease_timeout_seconds: int = 60,
    ) -> list[OutboxRecord]:
        """Atomically claim up to ``limit`` ready messages for one worker.

        Claimable states:
        - PENDING with ``available_at`` due;
        - FAILED with ``available_at`` due (retry backoff elapsed);
        - PROCESSING whose lease has EXPIRED (``locked_at <= available_before -
          lease``) — a worker that crashed after claiming no longer holds it.

        Every claim writes a FRESH ``lease_token`` (fencing token). The claim UPDATE
        is conditional on the row still being in a claimable state and, for an
        expired PROCESSING lease, on ``locked_at``/``locked_by`` still being the
        stale values observed — so two workers reclaiming at once converge to one
        holder. Settlements must later present this token.
        """
        async with self._factory() as session:
            rows = await session.execute(
                select(OutboxMessageRow)
                .where(
                    or_(
                        and_(
                            OutboxMessageRow.status == "PENDING",
                            OutboxMessageRow.available_at <= available_before,
                        ),
                        and_(
                            OutboxMessageRow.status == "FAILED",
                            OutboxMessageRow.available_at <= available_before,
                        ),
                        and_(
                            OutboxMessageRow.status == "PROCESSING",
                            OutboxMessageRow.locked_at
                            <= available_before
                            - timedelta(seconds=lease_timeout_seconds),
                        ),
                    )
                )
                .order_by(OutboxMessageRow.created_at)
                .limit(limit)
            )
            claimed: list[OutboxRecord] = []
            for row in rows.scalars().all():
                now = datetime.now(UTC)
                claimable = and_(
                    OutboxMessageRow.id == row.id,
                    OutboxMessageRow.status.in_(["PENDING", "FAILED"]),
                )
                if row.status == "PROCESSING":
                    # Reclaim an expired lease only if it is STILL the same stale
                    # lease (locked_by/locked_at unchanged) — never steal a live one.
                    claimable = and_(
                        OutboxMessageRow.id == row.id,
                        OutboxMessageRow.status == "PROCESSING",
                        OutboxMessageRow.locked_at == row.locked_at,
                        OutboxMessageRow.locked_by == row.locked_by,
                    )
                token = uuid4().hex
                updated = cast(
                    "CursorResult[object]",
                    await session.execute(
                        update(OutboxMessageRow)
                        .where(claimable)
                        .values(
                            status="PROCESSING",
                            locked_at=now,
                            locked_by=worker,
                            lease_token=token,
                            attempt_count=row.attempt_count + 1,
                        )
                    ),
                )
                if updated.rowcount == 0:
                    continue  # another worker claimed/reclaimed it first
                claimed.append(
                    OutboxRecord(
                        id=row.id,
                        event_id=row.event_id,
                        destination=row.destination,
                        status="PROCESSING",
                        attempt_count=row.attempt_count + 1,
                        available_at=row.available_at,
                        lease_token=token,
                        locked_at=now,
                        locked_by=worker,
                    )
                )
            await session.commit()
            return claimed

    async def renew_lease(
        self,
        *,
        outbox_id: UUID,
        lease_token: str,
        lease_timeout_seconds: int,
        now: datetime,
    ) -> bool:
        """Renew a PROCESSING lease — only while the caller still owns it.

        The UPDATE is conditioned on ``id + status=PROCESSING + lease_token``. A
        worker whose lease was reclaimed writes a STALE token → 0 rows → False, so
        a lost lease can never be silently extended (which would starve the real
        owner). ``locked_at`` is set to ``now`` — the timestamp of this successful
        renewal — so the expiry check ``locked_at <= now - lease_timeout`` measures
        the time since the last successful claim OR renewal (never written into the
        future). ``locked_by`` is left untouched.

        ``lease_timeout_seconds`` is accepted only so the call site stays symmetric
        with the claim; expiry is always computed from ``locked_at`` against the
        store's own clock at claim time.
        """
        async with self._factory() as session:
            result = cast(
                "CursorResult[object]",
                await session.execute(
                    update(OutboxMessageRow)
                    .where(
                        OutboxMessageRow.id == outbox_id,
                        OutboxMessageRow.status == "PROCESSING",
                        OutboxMessageRow.lease_token == lease_token,
                    )
                    .values(locked_at=now)
                ),
            )
            await session.commit()
            return result.rowcount > 0

    async def mark_published(
        self, *, outbox_id: UUID, lease_token: str, published_at: datetime
    ) -> bool:
        async with self._factory() as session:
            result = cast(
                "CursorResult[object]",
                await session.execute(
                    update(OutboxMessageRow)
                    .where(
                        OutboxMessageRow.id == outbox_id,
                        OutboxMessageRow.status == "PROCESSING",
                        OutboxMessageRow.lease_token == lease_token,
                    )
                    .values(
                        status="PUBLISHED",
                        published_at=published_at,
                        locked_at=None,
                        locked_by=None,
                        lease_token=None,
                        last_error_code=None,
                    )
                ),
            )
            await session.commit()
            return result.rowcount > 0

    async def mark_failed(
        self,
        *,
        outbox_id: UUID,
        lease_token: str,
        error_code: str,
        next_available_at: datetime,
        attempt_count: int,
    ) -> bool:
        """Move a PROCESSING message to retryable FAILED with backoff.

        Only ever re-claimed when its ``available_at`` becomes due again; a live
        (non-expired) PROCESSING lease is never touched. The settlement is fenced
        by ``lease_token``: a stale worker's attempt matches 0 rows.
        """
        async with self._factory() as session:
            result = cast(
                "CursorResult[object]",
                await session.execute(
                    update(OutboxMessageRow)
                    .where(
                        OutboxMessageRow.id == outbox_id,
                        OutboxMessageRow.status == "PROCESSING",
                        OutboxMessageRow.lease_token == lease_token,
                    )
                    .values(
                        status="FAILED",
                        available_at=next_available_at,
                        locked_at=None,
                        locked_by=None,
                        lease_token=None,
                        last_error_code=error_code,
                    )
                ),
            )
            await session.commit()
            return result.rowcount > 0

    async def mark_dead_letter(
        self, *, outbox_id: UUID, lease_token: str, error_code: str
    ) -> bool:
        """Terminal permanent-failure state — a dead letter is never re-claimed."""
        async with self._factory() as session:
            result = cast(
                "CursorResult[object]",
                await session.execute(
                    update(OutboxMessageRow)
                    .where(
                        OutboxMessageRow.id == outbox_id,
                        OutboxMessageRow.status == "PROCESSING",
                        OutboxMessageRow.lease_token == lease_token,
                    )
                    .values(
                        status="DEAD_LETTER",
                        locked_at=None,
                        locked_by=None,
                        lease_token=None,
                        last_error_code=error_code,
                    )
                ),
            )
            await session.commit()
            return result.rowcount > 0
