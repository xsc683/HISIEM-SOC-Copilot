"""Asyncio outbox dispatcher — claims and delivers investigation graph runs.

A single worker loop owns outbox delivery. For each ready row (``claim_batch`` in
its own transaction) it resolves the underlying ``investigation_created`` event to
(tenant_id, investigation_id) in a short read, then hands the run to the
investigation runner (which does all graph/LLM/tool work outside DB transactions).
The outbox row is marked PUBLISHED on success and FAILED (with backoff) on a
recoverable error, each in its own transaction.

An in-process per-investigation lock guarantees that duplicate/concurrent outbox
delivery never launches two runs of the same investigation.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...application.ports.durable import OutboxRecord, OutboxStore
from ..persistence.orm.events import DomainEventRow
from .investigation_runner import AsyncInvestigationGraphRunner

logger = logging.getLogger(__name__)

_DISPATCHER_DESTINATION = "investigation.graph.run"
_MAX_ATTEMPTS = 10


class Resolver(Protocol):
    """Maps a claimed outbox record to the event's (tenant_id, investigation_id)."""

    async def resolve(self, *, event_id: UUID) -> tuple[str, str] | None: ...


class SqlAlchemyOutboxResolver:
    """Reads the persisted domain_event row to resolve dispatch target."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def resolve(self, *, event_id: UUID) -> tuple[str, str] | None:
        async with self._factory() as session:
            row = (
                await session.execute(
                    select(DomainEventRow).where(DomainEventRow.event_id == event_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return row.tenant_id, str(row.aggregate_id)


class AsyncOutboxDispatcher:
    """Claims outbox rows and delivers each to the investigation runner."""

    def __init__(
        self,
        *,
        outbox_store: OutboxStore,
        resolver: Resolver,
        runner: AsyncInvestigationGraphRunner,
        worker_name: str,
        poll_interval_seconds: float = 1.0,
        batch_size: int = 8,
    ) -> None:
        self._outbox = outbox_store
        self._resolver = resolver
        self._runner = runner
        self._worker = worker_name
        self._poll = poll_interval_seconds
        self._batch = batch_size
        # in-process guard so one investigation is never double-run concurrently
        self._running: dict[UUID, asyncio.Lock] = {}
        self._guard = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stop = False

    async def _lock_for(self, investigation_id: UUID) -> asyncio.Lock:
        async with self._guard:
            lock = self._running.get(investigation_id)
            if lock is None:
                lock = asyncio.Lock()
                self._running[investigation_id] = lock
            return lock

    async def drain_once(self) -> int:
        """Claim + deliver one batch; returns the number of messages processed."""
        now = datetime.now(UTC)
        claimed = await self._outbox.claim_batch(
            worker=self._worker, limit=self._batch, available_before=now
        )
        if not claimed:
            return 0
        delivered = 0
        for record in claimed:
            await self._deliver(record)
            delivered += 1
        return delivered

    async def _deliver(self, record: OutboxRecord) -> None:
        if record.destination != _DISPATCHER_DESTINATION:
            await self._outbox.settle_dead_letter(
                outbox_id=record.id, error_code="UNKNOWN_DESTINATION"
            )
            return
        try:
            target = await self._resolver.resolve(event_id=record.event_id)
        except Exception as exc:  # resolution is short/transient
            await self._fail(record, "RESOLVE_ERROR")
            logger.warning("outbox resolve failed: %s", exc)
            return
        if target is None:
            # The originating event vanished — nothing to dispatch.
            await self._outbox.mark_published(
                outbox_id=record.id, published_at=datetime.now(UTC)
            )
            return
        tenant_id, investigation_id = target
        lock = await self._lock_for(UUID(investigation_id))
        async with lock:
            try:
                await self._runner.run_investigation(
                    investigation_id=investigation_id, tenant_id=tenant_id
                )
            except Exception as exc:  # recoverable: retry with backoff
                await self._fail(record, "RUN_FAILED")
                logger.warning(
                    "investigation %s run failed: %s", investigation_id, exc
                )
                return
        await self._outbox.mark_published(
            outbox_id=record.id, published_at=datetime.now(UTC)
        )

    async def _fail(self, record: OutboxRecord, error_code: str) -> None:
        if record.attempt_count >= _MAX_ATTEMPTS:
            await self._outbox.settle_dead_letter(
                outbox_id=record.id, error_code=error_code
            )
            return
        backoff = timedelta(seconds=min(2 ** record.attempt_count, 120))
        await self._outbox.mark_failed(
            outbox_id=record.id,
            error_code=error_code,
            next_available_at=datetime.now(UTC) + backoff,
        )

    # ------------------------------------------------------------------
    # worker lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop = False
        self._task = asyncio.create_task(self._loop(), name=f"outbox-{self._worker}")

    async def stop(self) -> None:
        self._stop = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        while not self._stop:
            try:
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the worker alive on transient errors
                logger.warning("outbox drain error: %s", exc)
            await asyncio.sleep(self._poll)
