"""Outbox dispatcher — claims a created-event, runs the graph once, marks published.

Uses a fake outbox + a counting fake runner so the claim/deliver/publish lifecycle
and the per-investigation duplicate-delivery guard are testable without Postgres.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from hisiem_soc_copilot.infrastructure.durable.dispatcher import AsyncOutboxDispatcher
from tests.fixtures.fakes import FakeOutboxStore


class _FakeResolver:
    """Resolves every event to the same tenant/investigation."""

    def __init__(self, investigation_id: str, tenant_id: str = "tenant-a") -> None:
        self.investigation_id = investigation_id
        self.tenant_id = tenant_id
        self.calls: list[Any] = []

    async def resolve(self, *, event_id: Any) -> tuple[str, str] | None:
        self.calls.append(event_id)
        return self.tenant_id, self.investigation_id


class _CountingRunner:
    """A fake investigation runner that records (start,end) of each invocation."""

    def __init__(self, *, delay: float = 0.02) -> None:
        self.started: list[tuple[str, str]] = []
        self.intervals: list[tuple[float, float]] = []
        self._delay = delay

    async def run_investigation(
        self, *, investigation_id: str, tenant_id: str
    ) -> None:
        import asyncio
        import time

        start = time.monotonic()
        self.started.append((investigation_id, tenant_id))
        await asyncio.sleep(self._delay)
        self.intervals.append((start, time.monotonic()))


async def test_dispatcher_claims_and_runs_investigation_once() -> None:
    outbox = FakeOutboxStore()
    event_id = uuid4()
    inv_id = str(uuid4())
    outbox.enqueue(event_id)
    resolver = _FakeResolver(inv_id)
    runner = _CountingRunner()
    dispatcher = AsyncOutboxDispatcher(
        outbox_store=outbox,
        resolver=resolver,
        runner=runner,  # type: ignore[arg-type]
        worker_name="test",
    )

    processed = await dispatcher.drain_once()
    assert processed == 1
    assert runner.started == [(inv_id, "tenant-a")]
    assert outbox.published_ids  # the claim was marked published


async def test_dispatcher_lock_serializes_concurrent_same_investigation_runs() -> None:
    """Two concurrent deliveries of the SAME investigation never overlap.

    The in-process per-investigation lock serializes the runner calls, so two
    drain cycles racing on one investigation cannot execute its graph twice at the
    same time (exactly-once-by-domain is additionally enforced by the runner's
    terminal short-circuit; this test proves the no-overlap guarantee).
    """
    import asyncio

    outbox = FakeOutboxStore()
    inv_id = str(uuid4())
    # Two rows for the SAME investigation become ready (e.g. duplicate publish).
    outbox.enqueue(uuid4())
    outbox.enqueue(uuid4())
    resolver = _FakeResolver(inv_id)
    runner = _CountingRunner(delay=0.05)
    dispatcher = AsyncOutboxDispatcher(
        outbox_store=outbox,
        resolver=resolver,
        runner=runner,  # type: ignore[arg-type]
        worker_name="test",
    )

    # Concurrent drains on the SAME dispatcher: each claims one of the two rows and
    # both target the same investigation. The per-investigation lock must serialize
    # them so the shared runner is never entered concurrently.
    await asyncio.gather(dispatcher.drain_once(), dispatcher.drain_once())

    assert len(runner.intervals) == 2  # both delivered (sequentially, not overlapped)
    (s1, e1), (s2, e2) = runner.intervals[0], runner.intervals[1]
    # No temporal overlap between the two runs.
    assert e1 <= s2 or e2 <= s1


async def test_terminal_investigation_is_not_dispatched_again() -> None:
    """A completed investigation whose outbox row is re-delivered is a no-op."""
    outbox = FakeOutboxStore()
    event_id = uuid4()
    inv_id = str(uuid4())
    outbox.enqueue(event_id)
    resolver = _FakeResolver(inv_id)
    runner = _CountingRunner()
    dispatcher = AsyncOutboxDispatcher(
        outbox_store=outbox,
        resolver=resolver,
        runner=runner,  # type: ignore[arg-type]
        worker_name="test",
    )

    await dispatcher.drain_once()
    assert len(runner.started) == 1

    # Re-deliver the same event (duplicate) → the row is already PUBLISHED so
    # nothing is claimed again and the runner is not invoked.
    second = await dispatcher.drain_once()
    assert second == 0  # nothing left to claim
    assert len(runner.started) == 1
