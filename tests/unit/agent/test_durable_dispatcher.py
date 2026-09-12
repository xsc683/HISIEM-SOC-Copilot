"""Outbox dispatcher — claims a created-event, runs the graph once, marks published.

Uses a fake outbox + a counting fake runner so the claim/deliver/publish lifecycle
and the per-investigation duplicate-delivery guard are testable without Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from hisiem_soc_copilot.infrastructure.durable.dispatcher import (
    _MAX_ATTEMPTS,
    AsyncOutboxDispatcher,
)
from hisiem_soc_copilot.infrastructure.durable.investigation_runner import (
    NonRetryableRunError,
)
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

    async def run(self, *, aggregate_id: str, tenant_id: str) -> None:
        import asyncio
        import time

        start = time.monotonic()
        self.started.append((aggregate_id, tenant_id))
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


class _NonRetryableRunner:
    """A runner that raises a safe non-retryable run error (MODEL_CONFIGURATION)."""

    async def run(self, *, aggregate_id: str, tenant_id: str) -> None:
        raise NonRetryableRunError(code="MODEL_CONFIGURATION")


class _RecoverableRunner:
    """A runner that raises a generic (recoverable) exception."""

    async def run(self, *, aggregate_id: str, tenant_id: str) -> None:
        raise RuntimeError("boom transient")


async def test_dispatcher_dead_letters_nonretryable_immediately() -> None:
    """§8: a NonRetryableRunError → DEAD_LETTER immediately (no backoff/retry loop)."""
    outbox = FakeOutboxStore()
    event_id = uuid4()
    inv_id = str(uuid4())
    outbox.enqueue(event_id)
    dispatcher = AsyncOutboxDispatcher(
        outbox_store=outbox,
        resolver=_FakeResolver(inv_id),
        runner=_NonRetryableRunner(),  # type: ignore[arg-type]
        worker_name="test",
    )

    processed = await dispatcher.drain_once()
    assert processed == 1
    assert len(outbox.dead_letter_ids) == 1
    assert outbox.failed_ids == []
    assert outbox.published_ids == []
    # The DEAD_LETTER row is terminal: a second drain claims nothing → no retry.
    assert await dispatcher.drain_once() == 0
    assert len(outbox.dead_letter_ids) == 1


async def test_dispatcher_recoverable_failure_retries_not_dead_letter() -> None:
    """A generic recoverable runner error is still FAILED-with-backoff (never an
    immediate DEAD_LETTER) — the E1-C2 non-retry semantics must not change that."""
    outbox = FakeOutboxStore()
    event_id = uuid4()
    inv_id = str(uuid4())
    outbox.enqueue(event_id)
    dispatcher = AsyncOutboxDispatcher(
        outbox_store=outbox,
        resolver=_FakeResolver(inv_id),
        runner=_RecoverableRunner(),  # type: ignore[arg-type]
        worker_name="test",
    )

    processed = await dispatcher.drain_once()
    assert processed == 1
    assert outbox.failed_ids  # scheduled for retry with backoff
    assert outbox.dead_letter_ids == []
    assert outbox.published_ids == []


# ---------------------------------------------------------------------------
# resolver failure is NOT a terminal condition
# ---------------------------------------------------------------------------


class _FlakyResolver:
    """A resolver whose READ fails — as opposed to one that finds nothing.

    ``failures`` counts how many calls still raise; afterwards it resolves normally.
    The distinction is the whole point: ``resolve() -> None`` means "that event is
    gone" (settle the delivery), while RAISING means "the read failed" (evidence
    about nothing).
    """

    def __init__(
        self, investigation_id: str, tenant_id: str = "tenant-a", *, failures: int
    ) -> None:
        self.investigation_id = investigation_id
        self.tenant_id = tenant_id
        self.failures = failures

    async def resolve(self, *, event_id: Any) -> tuple[str, str] | None:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("domain_event read failed")
        return self.tenant_id, self.investigation_id


class _SpyExhaustion:
    """Records exhaustion notifications; never writes anything."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def on_retry_exhausted(
        self, *, aggregate_id: str, tenant_id: str, error_code: str
    ) -> None:
        self.calls.append((aggregate_id, tenant_id, error_code))


def _dispatcher(
    outbox: FakeOutboxStore,
    resolver: Any,
    runner: Any,
    *,
    exhaustion: Any = None,
) -> AsyncOutboxDispatcher:
    return AsyncOutboxDispatcher(
        outbox_store=outbox,
        resolver=resolver,
        runner=runner,  # type: ignore[arg-type]
        worker_name="test",
        exhaustion=exhaustion,
    )


async def test_a_resolver_read_failure_never_reaches_dead_letter() -> None:
    """Driven far past the retry budget, a failing READ is still never terminal.

    Resolution failing says nothing about the business outcome — we cannot even
    name the aggregate, so no destination hook may be consulted and no business
    fact can honestly be written. The delivery keeps being rescheduled instead:
    attempts climb, backoff pushes ``available_at`` forward, the runner is never
    invoked, and the row never dead-letters.

    (The persisted-vs-returned ``attempt_count`` invariant asserted here is verified
    against real PostgreSQL rows in
    ``tests/integration/persistence/test_outbox_lease_integration.py``.)
    """
    outbox = FakeOutboxStore()
    event_id = uuid4()
    inv_id = str(uuid4())
    outbox.enqueue(event_id)
    runner = _CountingRunner()
    exhaustion = _SpyExhaustion()
    dispatcher = _dispatcher(
        outbox, _FlakyResolver(inv_id, failures=10_000), runner, exhaustion=exhaustion
    )

    overshoot = 3
    for _ in range(_MAX_ATTEMPTS + overshoot):
        outbox.advance(3_600)
        assert await dispatcher.drain_once() == 1

    assert outbox.dead_letter_ids == []
    assert outbox.published_ids == []
    assert runner.started == []
    assert exhaustion.calls == []  # nothing to terminalize, so nothing to announce
    assert outbox.rows[event_id]["status"] == "FAILED"
    assert outbox.rows[event_id]["attempt_count"] == _MAX_ATTEMPTS + overshoot


async def test_a_resolver_read_failure_backs_off_within_bounds() -> None:
    """Retrying forever is safe only because the schedule is bounded and grows."""
    outbox = FakeOutboxStore()
    event_id = uuid4()
    outbox.enqueue(event_id)
    dispatcher = _dispatcher(
        outbox, _FlakyResolver(str(uuid4()), failures=10_000), _CountingRunner()
    )

    deltas = []
    for _ in range(8):
        before = datetime.now(UTC)
        outbox.advance(3_600)
        assert await dispatcher.drain_once() == 1
        _, code, scheduled = outbox.failed_schedules[-1]
        assert code == "RESOLVE_ERROR"
        deltas.append(scheduled - before)

    # 2, 4, 8, … seconds, capped — a bounded attempt rate, not a hot loop.
    assert [round(d.total_seconds()) for d in deltas] == [2, 4, 8, 16, 32, 64, 120, 120]
    assert deltas == sorted(deltas)


async def test_a_resolver_that_recovers_delivers_the_same_row() -> None:
    """A resolution outage longer than the whole budget must not lose the delivery."""
    outbox = FakeOutboxStore()
    event_id = uuid4()
    inv_id = str(uuid4())
    outbox.enqueue(event_id)
    runner = _CountingRunner()
    outage = _MAX_ATTEMPTS + 2
    dispatcher = _dispatcher(outbox, _FlakyResolver(inv_id, failures=outage), runner)

    for _ in range(outage):
        outbox.advance(3_600)
        assert await dispatcher.drain_once() == 1
    assert runner.started == []
    assert outbox.dead_letter_ids == []

    # Reads work again → the SAME row is claimed and delivered normally.
    outbox.advance(3_600)
    assert await dispatcher.drain_once() == 1
    assert runner.started == [(inv_id, "tenant-a")]
    assert outbox.published_ids
    assert outbox.dead_letter_ids == []


async def test_the_retry_budget_spends_exactly_max_attempts() -> None:
    """``_MAX_ATTEMPTS`` means MAX attempts — the budget is not spent a round early."""
    outbox = FakeOutboxStore()
    event_id = uuid4()
    inv_id = str(uuid4())
    outbox.enqueue(event_id)
    exhaustion = _SpyExhaustion()
    dispatcher = _dispatcher(
        outbox, _FakeResolver(inv_id), _RecoverableRunner(), exhaustion=exhaustion
    )

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        outbox.advance(3_600)
        assert await dispatcher.drain_once() == 1
        assert outbox.rows[event_id]["attempt_count"] == attempt
        if attempt < _MAX_ATTEMPTS:
            assert outbox.dead_letter_ids == [], "dead-lettered one round early"
            assert exhaustion.calls == []

    assert len(outbox.dead_letter_ids) == 1
    assert exhaustion.calls == [(inv_id, "tenant-a", "RUN_FAILED")]
