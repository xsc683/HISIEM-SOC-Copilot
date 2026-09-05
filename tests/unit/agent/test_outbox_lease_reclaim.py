"""Outbox lease reclaim + dead-letter semantics.

A worker crash after a claim leaves a message stuck in ``PROCESSING`` (the worker
died before it could mark it PUBLISHED/FAILED). The real store treats an expired
lease (``locked_at`` older than the lease deadline) as claimable again; the fake
mirrors that so these tests validate the store contract without Postgres.
Permanent failures move to the terminal ``DEAD_LETTER`` state and are never
re-claimed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from hisiem_soc_copilot.infrastructure.durable.dispatcher import AsyncOutboxDispatcher
from tests.fixtures.fakes import FakeOutboxStore


class _FakeResolver:
    def __init__(self, investigation_id: str, tenant_id: str = "tenant-a") -> None:
        self.investigation_id = investigation_id
        self.tenant_id = tenant_id

    async def resolve(self, *, event_id: Any) -> tuple[str, str] | None:
        return self.tenant_id, self.investigation_id


class _CountingRunner:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []

    async def run_investigation(
        self, *, investigation_id: str, tenant_id: str
    ) -> None:
        self.started.append((investigation_id, tenant_id))


class _AlwaysFailRunner:
    async def run_investigation(
        self, *, investigation_id: str, tenant_id: str
    ) -> None:
        raise RuntimeError("permanent failure")


async def test_worker_crash_then_lease_expiry_then_reclaim_executes() -> None:
    """claim → worker dies before marking (stranded PROCESSING) → lease expires →
    second worker reclaims → executes successfully → published."""
    outbox = FakeOutboxStore()
    event_id = uuid4()
    outbox.enqueue(event_id)

    # Worker 1 claims the message, then the whole worker dies before marking it —
    # simulated by claiming straight through the store and never calling
    # mark_published/mark_failed (the dispatcher-equivalent crash window).
    claimed = await outbox.claim_batch(
        worker="worker-1", limit=10, available_before=datetime.now(UTC)
    )
    assert len(claimed) == 1
    assert outbox.rows[event_id]["status"] == "PROCESSING"
    assert outbox.rows[event_id]["attempt_count"] == 1

    # The lease is still live, so worker 2 cannot reclaim it yet.
    early = await outbox.claim_batch(
        worker="worker-2", limit=10, available_before=datetime.now(UTC)
    )
    assert early == []

    # The lease expires.
    outbox.advance(61)  # > default 60s lease timeout

    # Worker 2 reclaims the expired lease.
    reclaimed = await outbox.claim_batch(
        worker="worker-2", limit=10, available_before=datetime.now(UTC)
    )
    assert len(reclaimed) == 1
    assert reclaimed[0].locked_by == "worker-2"
    assert outbox.rows[event_id]["attempt_count"] == 2  # safely incremented

    # The reclaimed run executes successfully and is published.
    assert await outbox.mark_published(
        outbox_id=reclaimed[0].id,
        lease_token=reclaimed[0].lease_token,
        published_at=datetime.now(UTC),
    )
    assert outbox.rows[event_id]["status"] == "PUBLISHED"


async def test_live_lease_is_not_reclaimed_under_duplicate_claim() -> None:
    """A live (non-expired) PROCESSING lease is never stolen by another worker."""
    outbox = FakeOutboxStore()
    event_id = uuid4()
    outbox.enqueue(event_id)

    await outbox.claim_batch(worker="w1", limit=10, available_before=datetime.now(UTC))
    assert outbox.rows[event_id]["status"] == "PROCESSING"
    assert outbox.rows[event_id]["locked_by"] == "w1"

    # Worker 2 attempts to claim while w1 holds a live lease → nothing.
    claimed = await outbox.claim_batch(
        worker="w2", limit=10, available_before=datetime.now(UTC)
    )
    assert claimed == []
    # The lease is still owned by w1, unchanged.
    assert outbox.rows[event_id]["locked_by"] == "w1"
    assert outbox.rows[event_id]["attempt_count"] == 1


async def test_reclaim_requires_expired_lease_not_just_processsing() -> None:
    """Two workers racing to reclaim the same expired lease converge to one holder."""
    outbox = FakeOutboxStore()
    event_id = uuid4()
    outbox.enqueue(event_id)

    await outbox.claim_batch(worker="w1", limit=10, available_before=datetime.now(UTC))
    outbox.advance(61)  # lease expires

    # Two workers race to reclaim the same expired lease. The fake is single-threaded
    # but sequential double-claim proves the second sees an already-held (now-live)
    # lease and cannot steal it.
    r1 = await outbox.claim_batch(worker="w2", limit=10, available_before=datetime.now(UTC))
    r2 = await outbox.claim_batch(worker="w3", limit=10, available_before=datetime.now(UTC))
    assert len(r1) == 1
    assert r1[0].locked_by == "w2"
    assert outbox.rows[event_id]["attempt_count"] == 2
    # w3 cannot reclaim the now-live lease held by w2.
    assert r2 == []
    assert outbox.rows[event_id]["locked_by"] == "w2"


async def test_permanent_failure_dead_letters_and_is_never_reclaimed() -> None:
    """attempt_count reaches the max → DEAD_LETTER (terminal) → subsequent claim
    does not return it."""
    outbox = FakeOutboxStore()
    event_id = uuid4()
    inv_id = str(uuid4())
    outbox.enqueue(event_id)
    outbox.rows[event_id]["attempt_count"] = 9  # dispatcher max is 10

    always_fail = _AlwaysFailRunner()
    dispatcher = AsyncOutboxDispatcher(
        outbox_store=outbox,
        resolver=_FakeResolver(inv_id),
        runner=always_fail,  # type: ignore[arg-type]
        worker_name="worker",
    )
    await dispatcher.drain_once()
    assert outbox.rows[event_id]["status"] == "DEAD_LETTER"
    assert outbox.rows[event_id]["id"] in outbox.dead_letter_ids

    # Subsequent claims never return the dead-lettered message.
    claimed = await outbox.claim_batch(
        worker="worker", limit=10, available_before=datetime.now(UTC)
    )
    assert claimed == []


async def test_dead_letter_is_a_terminal_state_distinct_from_failed() -> None:
    """FAILED is retryable (re-claimable once backoff elapses); DEAD_LETTER is not."""
    outbox = FakeOutboxStore()

    # A retryable FAILED message becomes re-claimable once its backoff elapses.
    retry_event = uuid4()
    outbox.enqueue(retry_event)
    claimed_retry = await outbox.claim_batch(
        worker="w1", limit=10, available_before=datetime.now(UTC)
    )
    assert len(claimed_retry) == 1
    await outbox.mark_failed(
        outbox_id=outbox.rows[retry_event]["id"],
        lease_token=claimed_retry[0].lease_token,
        error_code="RETRYABLE",
        next_available_at=outbox.now,  # backoff already elapsed on the fake clock
        attempt_count=1,
    )
    assert outbox.rows[retry_event]["status"] == "FAILED"
    claimed = await outbox.claim_batch(worker="w2", limit=10, available_before=datetime.now(UTC))
    assert any(c.event_id == retry_event for c in claimed)  # retried

    # A DEAD_LETTER message is terminal — never re-claimed.
    dead_event = uuid4()
    outbox.enqueue(dead_event)
    claimed_dead = await outbox.claim_batch(
        worker="w1", limit=10, available_before=datetime.now(UTC)
    )
    assert len(claimed_dead) == 1
    await outbox.mark_dead_letter(
        outbox_id=outbox.rows[dead_event]["id"],
        lease_token=claimed_dead[0].lease_token,
        error_code="PERMANENT",
    )
    assert outbox.rows[dead_event]["status"] == "DEAD_LETTER"
    claimed_again = await outbox.claim_batch(
        worker="w2", limit=10, available_before=datetime.now(UTC)
    )
    assert not any(c.event_id == dead_event for c in claimed_again)
