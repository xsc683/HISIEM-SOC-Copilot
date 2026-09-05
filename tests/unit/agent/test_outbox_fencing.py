"""Outbox lease fencing (lease_token) + long-run renewal.

A stale worker whose lease was reclaimed must NEVER be able to settle or renew the
row it lost: every settlement (published / failed / dead-letter) and every renewal
is conditioned on ``id + status=PROCESSING + lease_token``. The recommended fencing
scheme from the follow-up brief:

    Worker A claims (token-A)
    → A lease expires
    → Worker B reclaims (token-B)
    → A later returns and tries mark_published(token-A) → REJECTED
    → B settlement succeeds

Long investigations must renew the lease so a legitimately running worker is not
stolen by the fixed 60s lease; renewal is also token-fenced.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from hisiem_soc_copilot.application.ports.durable import OutboxRecord
from tests.fixtures.fakes import FakeOutboxStore


async def _claim(outbox: FakeOutboxStore, worker: str) -> OutboxRecord:
    claimed = await outbox.claim_batch(
        worker=worker, limit=10, available_before=datetime.now(UTC)
    )
    assert len(claimed) == 1
    return claimed[0]


async def test_stale_worker_cannot_settle_after_lease_reclaimed() -> None:
    """A (token-A) → lease expires → B reclaims (token-B) → A's mark_published with
    its stale token is REJECTED; B's settlement succeeds."""
    outbox = FakeOutboxStore()
    event_id = uuid4()
    outbox.enqueue(event_id)

    record_a = await _claim(outbox, "worker-a")
    token_a = record_a.lease_token
    assert token_a

    # A's lease expires while it is still "working" (e.g. a long graph run).
    outbox.advance(61)  # > default 60s lease timeout

    # Worker B reclaims the expired lease and now owns the row.
    record_b = await _claim(outbox, "worker-b")
    assert record_b.lease_token != token_a  # a fresh fencing token
    assert outbox.rows[event_id]["status"] == "PROCESSING"
    assert outbox.rows[event_id]["locked_by"] == "worker-b"

    # A (now stale) returns and tries to mark the row published with token-A.
    rejected = await outbox.mark_published(
        outbox_id=record_a.id,
        lease_token=token_a,
        published_at=datetime.now(UTC),
    )
    assert rejected is False  # fencing rejected the stale worker
    assert outbox.rows[event_id]["status"] == "PROCESSING"  # unchanged
    assert outbox.rows[event_id]["locked_by"] == "worker-b"

    # B's own settlement succeeds.
    assert await outbox.mark_published(
        outbox_id=record_b.id,
        lease_token=record_b.lease_token,
        published_at=datetime.now(UTC),
    )
    assert outbox.rows[event_id]["status"] == "PUBLISHED"


async def test_stale_worker_cannot_mark_failed_or_dead_letter() -> None:
    """The same fencing applies to mark_failed and mark_dead_letter."""
    outbox = FakeOutboxStore()
    event_id = uuid4()
    outbox.enqueue(event_id)
    record_a = await _claim(outbox, "worker-a")

    outbox.advance(61)
    await _claim(outbox, "worker-b")  # B owns it now

    # A's stale-token attempts all fail against the PROCESSING row held by B.
    assert not await outbox.mark_failed(
        outbox_id=record_a.id,
        lease_token=record_a.lease_token,
        error_code="RUN_FAILED",
        next_available_at=outbox.now + timedelta(seconds=5),
        attempt_count=record_a.attempt_count,
    )
    assert not await outbox.mark_dead_letter(
        outbox_id=record_a.id,
        lease_token=record_a.lease_token,
        error_code="PERMANENT",
    )
    assert outbox.rows[event_id]["status"] == "PROCESSING"


async def test_long_running_worker_renews_lease_and_is_not_stolen() -> None:
    """A legitimately long-running worker renews its lease (token-fenced) so a fixed
    60s lease never causes another worker to reclaim its in-flight row.

    ``locked_at`` is the LAST successful claim/renewal time (never the future): a
    renewal at T re-arms the expiry window from T, and the worker is only reclaimable
    once ``locked_at + lease_timeout`` has fully passed with NO further renewal."""
    outbox = FakeOutboxStore()
    event_id = uuid4()
    outbox.enqueue(event_id)
    record = await _claim(outbox, "worker-long")

    # The run outlasts a single 60s lease; the worker renews at t=30s, t=90s, ...
    for elapsed in (30, 90, 150):
        outbox.advance(60 if elapsed > 30 else 30)  # step the clock forward
        renewed = await outbox.renew_lease(
            outbox_id=record.id,
            lease_token=record.lease_token,
            lease_timeout_seconds=60,
            now=outbox.now,
        )
        assert renewed is True
        # locked_at == the renewal time; the lease is still LIVE (not reclaimable).
        assert outbox.rows[event_id]["locked_at"] == outbox.now
        assert not await outbox.claim_batch(
            worker="worker-other", limit=10, available_before=datetime.now(UTC)
        )
        # ... and the row is still owned by the long worker with the SAME token.
        assert outbox.rows[event_id]["locked_by"] == "worker-long"
        assert outbox.rows[event_id]["lease_token"] == record.lease_token

    # The long run completes and publishes with its own token.
    assert await outbox.mark_published(
        outbox_id=record.id,
        lease_token=record.lease_token,
        published_at=datetime.now(UTC),
    )
    assert outbox.rows[event_id]["status"] == "PUBLISHED"


async def test_stops_renewing_then_reclaimable_after_exactly_lease_timeout() -> None:
    """A worker that STOPS renewing loses the lease once ``locked_at + lease_timeout``
    has fully elapsed — another worker may then reclaim it."""
    outbox = FakeOutboxStore()
    event_id = uuid4()
    outbox.enqueue(event_id)
    record = await _claim(outbox, "worker-long")

    # Renew once at t=30 to prove the window re-arms from the renewal instant.
    outbox.advance(30)
    assert await outbox.renew_lease(
        outbox_id=record.id,
        lease_token=record.lease_token,
        lease_timeout_seconds=60,
        now=outbox.now,
    )
    assert outbox.rows[event_id]["locked_at"] == outbox.now
    # At t=30+59 the lease is NOT yet expired (only 59s since the renewal).
    outbox.advance(59)
    assert not await outbox.claim_batch(
        worker="worker-other", limit=10, available_before=datetime.now(UTC)
    )
    # At t=30+60 the lease IS expired → another worker may reclaim.
    outbox.advance(1)
    reclaimed = await outbox.claim_batch(
        worker="worker-other", limit=10, available_before=datetime.now(UTC)
    )
    assert len(reclaimed) == 1
    assert reclaimed[0].lease_token != record.lease_token
    assert outbox.rows[event_id]["locked_by"] == "worker-other"


async def test_stale_worker_cannot_renew_lost_lease() -> None:
    """After a reclaim, the stale owner's renewal is rejected (0 rows), so it can
    never keep extending a lease it lost."""
    outbox = FakeOutboxStore()
    event_id = uuid4()
    outbox.enqueue(event_id)
    record_a = await _claim(outbox, "worker-a")

    outbox.advance(61)
    await _claim(outbox, "worker-b")

    # A tries to renew the lease it no longer owns → rejected.
    renewed = await outbox.renew_lease(
        outbox_id=record_a.id,
        lease_token=record_a.lease_token,
        lease_timeout_seconds=60,
        now=outbox.now,
    )
    assert renewed is False
    # B's ownership is unchanged.
    assert outbox.rows[event_id]["locked_by"] == "worker-b"
