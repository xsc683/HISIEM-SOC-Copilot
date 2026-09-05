"""Outbox lease reclaim + dead-letter — real PostgreSQL (copilot schema).

Proves the store-level crash-recovery contract against real rows:
- claim → worker crash simulation (row left PROCESSING) → lease expires → second
  claim reclaims it and the run completes;
- a live PROCESSING lease is never stolen;
- permanent failure reaches the terminal DEAD_LETTER state and is never re-claimed.

Skipped when Postgres is unreachable (dev-machine safe). Requires the
``4c7a11f2d901`` migration (DEAD_LETTER status + lease-reclaim index).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.infrastructure.persistence.repositories.durable import (
    SqlAlchemyOutboxStore,
)

_TRUNCATE = ("outbox_message", "domain_event", "command_receipt", "investigation")


def _settings() -> Settings:
    s = Settings()
    s.database.database_url = (
        "postgresql+psycopg://copilot:copilot@127.0.0.1:5432/copilot"
    )
    return s


async def _db_reachable() -> bool:
    try:
        import psycopg
        from sqlalchemy.engine import make_url

        url = make_url(_settings().database.database_url)
        conn = psycopg.connect(
            host=url.host,
            port=url.port,
            user=url.username,
            password=url.password,
            dbname=url.database,
            connect_timeout=2,
        )
        conn.execute("SELECT 1")
        conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if not await _db_reachable():
        import pytest

        pytest.skip("PostgreSQL not reachable — skipping outbox lease integration")
    settings = _settings()
    engine = create_async_engine(settings.database.database_url)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        await session.execute(
            text(f"TRUNCATE copilot.{', copilot.'.join(_TRUNCATE)} RESTART IDENTITY CASCADE")
        )
        await session.commit()
    yield factory
    async with factory() as session:
        await session.execute(
            text(f"TRUNCATE copilot.{', copilot.'.join(_TRUNCATE)} RESTART IDENTITY CASCADE")
        )
        await session.commit()
    await engine.dispose()


async def _enqueue(
    factory: async_sessionmaker[AsyncSession],
    event_id: UUID,
    *,
    destination: str = "investigation.graph.run",
    available_at: datetime | None = None,
) -> UUID:
    async with factory() as session:
        row_id = uuid4()
        await session.execute(
            text(
                "INSERT INTO copilot.outbox_message "
                "(id, event_id, destination, status, attempt_count, available_at, created_at) "
                "VALUES (:id, :eid, :dest, 'PENDING', 0, :avail, :now)"
            ),
            {
                "id": row_id,
                "eid": event_id,
                "dest": destination,
                "avail": available_at or datetime.now(UTC),
                "now": datetime.now(UTC),
            },
        )
        await session.commit()
        return row_id


async def _set_row(
    factory: async_sessionmaker[AsyncSession],
    outbox_id: UUID,
    *,
    status: str,
    attempt_count: int,
    locked_at: datetime | None = None,
    locked_by: str | None = None,
    lease_token: str | None = None,
) -> None:
    async with factory() as session:
        await session.execute(
            text(
                "UPDATE copilot.outbox_message SET status=:s, attempt_count=:a, "
                "locked_at=:la, locked_by=:lb, lease_token=:tok WHERE id=:id"
            ),
            {
                "s": status,
                "a": attempt_count,
                "la": locked_at,
                "lb": locked_by,
                "tok": lease_token,
                "id": outbox_id,
            },
        )
        await session.commit()


async def _status_of(
    factory: async_sessionmaker[AsyncSession], outbox_id: UUID
) -> tuple[str, int]:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, attempt_count FROM copilot.outbox_message WHERE id=:id"
                ),
                {"id": outbox_id},
            )
        ).one()
        return row.status, row.attempt_count


async def test_claim_then_crash_then_lease_expiry_then_reclaim_executes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyOutboxStore(session_factory)
    event_id = uuid4()
    outbox_id = await _enqueue(session_factory, event_id)

    # Worker 1 claims the message, then "crashes" before marking it.
    claimed1 = await store.claim_batch(
        worker="worker-1", limit=10, available_before=datetime.now(UTC)
    )
    assert len(claimed1) == 1
    status, attempts = await _status_of(session_factory, outbox_id)
    assert status == "PROCESSING"
    assert attempts == 1

    # A live lease is not reclaimable by another worker.
    claimed_live = await store.claim_batch(
        worker="worker-2", limit=10, available_before=datetime.now(UTC)
    )
    assert claimed_live == []

    # Simulate the lease expiring (rewrite locked_at to a stale timestamp).
    await _set_row(
        session_factory,
        outbox_id,
        status="PROCESSING",
        attempt_count=1,
        locked_at=datetime.now(UTC) - timedelta(seconds=61),
        locked_by="worker-1",
    )

    # Worker 2 reclaims the expired lease and completes the run.
    claimed2 = await store.claim_batch(
        worker="worker-2", limit=10, available_before=datetime.now(UTC)
    )
    assert len(claimed2) == 1
    assert claimed2[0].id == outbox_id
    assert claimed2[0].locked_by == "worker-2"
    status, attempts = await _status_of(session_factory, outbox_id)
    assert status == "PROCESSING"
    assert attempts == 2  # safely incremented on reclaim

    assert await store.mark_published(
        outbox_id=outbox_id,
        lease_token=claimed2[0].lease_token,
        published_at=datetime.now(UTC),
    )
    status, _ = await _status_of(session_factory, outbox_id)
    assert status == "PUBLISHED"


async def test_live_lease_is_not_stolen_by_another_worker(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyOutboxStore(session_factory)
    event_id = uuid4()
    outbox_id = await _enqueue(session_factory, event_id)

    await store.claim_batch(worker="w1", limit=10, available_before=datetime.now(UTC))
    # Locked_at is fresh → the lease is live.
    status, attempts = await _status_of(session_factory, outbox_id)
    assert status == "PROCESSING"

    # A second worker claims nothing (live lease) and cannot touch the row.
    claimed = await store.claim_batch(
        worker="w2", limit=10, available_before=datetime.now(UTC)
    )
    assert claimed == []
    status, attempts = await _status_of(session_factory, outbox_id)
    assert status == "PROCESSING"
    assert attempts == 1  # w2 did not bump the counter


async def test_permanent_failure_dead_letters_and_is_never_reclaimed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyOutboxStore(session_factory)
    event_id = uuid4()
    outbox_id = await _enqueue(session_factory, event_id)

    # Claim once so it is PROCESSING, then move it to a terminal dead letter.
    claimed = await store.claim_batch(worker="w1", limit=10, available_before=datetime.now(UTC))
    assert len(claimed) == 1
    assert await store.mark_dead_letter(
        outbox_id=outbox_id,
        lease_token=claimed[0].lease_token,
        error_code="PERMANENT",
    )

    status, _ = await _status_of(session_factory, outbox_id)
    assert status == "DEAD_LETTER"

    # A DEAD_LETTER is terminal: no subsequent claim ever returns it.
    again = await store.claim_batch(worker="w2", limit=10, available_before=datetime.now(UTC))
    assert again == []
    status, _ = await _status_of(session_factory, outbox_id)
    assert status == "DEAD_LETTER"


async def test_stale_worker_cannot_settle_after_lease_reclaimed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Real Postgres fencing: worker A claims (token-A) → lease expires → worker B
    reclaims (token-B) → A's mark_published(token-A) is REJECTED (0 rows) → B's
    settlement succeeds."""
    store = SqlAlchemyOutboxStore(session_factory)
    event_id = uuid4()
    outbox_id = await _enqueue(session_factory, event_id)

    claimed_a = await store.claim_batch(
        worker="worker-a", limit=10, available_before=datetime.now(UTC)
    )
    assert len(claimed_a) == 1
    token_a = claimed_a[0].lease_token
    assert token_a

    # A's lease expires (only locked_at ages; A's token stays until B reclaims).
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE copilot.outbox_message SET locked_at=:la WHERE id=:id"
            ),
            {
                "la": datetime.now(UTC) - timedelta(seconds=61),
                "id": outbox_id,
            },
        )
        await session.commit()

    # Worker B reclaims and owns the row with a fresh token.
    claimed_b = await store.claim_batch(
        worker="worker-b", limit=10, available_before=datetime.now(UTC)
    )
    assert len(claimed_b) == 1
    assert claimed_b[0].lease_token != token_a

    # A (stale) tries to settle with token-A → rejected.
    stale = await store.mark_published(
        outbox_id=outbox_id, lease_token=token_a, published_at=datetime.now(UTC)
    )
    assert stale is False
    status, _ = await _status_of(session_factory, outbox_id)
    assert status == "PROCESSING"
    assert await _lease_owner(session_factory, outbox_id) == "worker-b"

    # B's settlement succeeds.
    assert await store.mark_published(
        outbox_id=outbox_id,
        lease_token=claimed_b[0].lease_token,
        published_at=datetime.now(UTC),
    )
    status, _ = await _status_of(session_factory, outbox_id)
    assert status == "PUBLISHED"


async def test_claim_sets_locked_at_to_now(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``locked_at`` is the claim timestamp — never ``now + lease_timeout``."""
    store = SqlAlchemyOutboxStore(session_factory)
    event_id = uuid4()
    outbox_id = await _enqueue(session_factory, event_id)

    claim_time = datetime.now(UTC)
    claimed = await store.claim_batch(
        worker="worker-1", limit=10, available_before=datetime.now(UTC)
    )
    assert len(claimed) == 1
    locked_at = await _locked_at_of(session_factory, outbox_id)
    assert locked_at is not None
    # locked_at == the claim instant (the fake clock used for the claim). The column
    # is naive UTC; re-attach UTC so the comparison is aware-vs-aware.
    assert abs((locked_at.replace(tzinfo=UTC) - claim_time).total_seconds()) < 2.0


async def test_long_running_worker_renews_lease_and_is_not_stolen(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A legitimate long run renews its lease (token-fenced) across the fixed 60s
    window; ``locked_at`` is reset to the RENEWAL time (never into the future), so
    another worker cannot reclaim while the owner keeps renewing."""
    store = SqlAlchemyOutboxStore(session_factory)
    event_id = uuid4()
    outbox_id = await _enqueue(session_factory, event_id)

    claimed = await store.claim_batch(
        worker="worker-long", limit=10, available_before=datetime.now(UTC)
    )
    assert len(claimed) == 1

    # Renew twice, advancing the effective clock between renewals. Each renewal sets
    # locked_at = the renewal time (NOT renewal_time + lease_timeout).
    for offset in (30, 90):
        renew_time = datetime.now(UTC) + timedelta(seconds=offset)
        renewed = await store.renew_lease(
            outbox_id=outbox_id,
            lease_token=claimed[0].lease_token,
            lease_timeout_seconds=60,
            now=renew_time,
        )
        assert renewed is True
        locked_at = await _locked_at_of(session_factory, outbox_id)
        assert locked_at is not None
        # locked_at == the renewal instant, NOT renewal_instant + 60s (naive col).
        assert abs((locked_at.replace(tzinfo=UTC) - renew_time).total_seconds()) < 2.0
        # The lease is now live (fresh locked_at) → not claimable by another worker.
        status, attempts = await _status_of(session_factory, outbox_id)
        assert status == "PROCESSING"
        assert attempts == 1  # nobody else bumped it

    # The long run completes with its own token.
    assert await store.mark_published(
        outbox_id=outbox_id,
        lease_token=claimed[0].lease_token,
        published_at=datetime.now(UTC),
    )
    status, _ = await _status_of(session_factory, outbox_id)
    assert status == "PUBLISHED"


async def test_worker_stops_renewing_then_reclaimable_after_lease_timeout(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When the owner stops renewing, the lease expires exactly one lease_timeout
    after the LAST renewal: another worker may then reclaim it."""
    store = SqlAlchemyOutboxStore(session_factory)
    event_id = uuid4()
    outbox_id = await _enqueue(session_factory, event_id)

    claimed = await store.claim_batch(
        worker="worker-long", limit=10, available_before=datetime.now(UTC)
    )
    assert len(claimed) == 1

    # Renew once to prove the window re-arms from the renewal instant.
    renewal_base = datetime.now(UTC) + timedelta(seconds=30)
    assert await store.renew_lease(
        outbox_id=outbox_id,
        lease_token=claimed[0].lease_token,
        lease_timeout_seconds=60,
        now=renewal_base,
    )
    locked_at = await _locked_at_of(session_factory, outbox_id)
    assert locked_at is not None
    assert abs((locked_at.replace(tzinfo=UTC) - renewal_base).total_seconds()) < 2.0

    # Rewrite locked_at so "now" is 59s after the renewal: NOT yet expired. The
    # column is naive UTC, so write the naive equivalent of the renewal instant.
    async with session_factory() as session:
        await session.execute(
            text("UPDATE copilot.outbox_message SET locked_at=:la WHERE id=:id"),
            {
                "la": renewal_base.replace(tzinfo=None),
                "id": outbox_id,
            },
        )
        await session.commit()
    live_before = await store.claim_batch(
        worker="worker-other", limit=10, available_before=renewal_base + timedelta(seconds=59)
    )
    assert live_before == []

    # At exactly locked_at + 60s (no further renewal) the lease is reclaimable.
    reclaimed = await store.claim_batch(
        worker="worker-other",
        limit=10,
        available_before=renewal_base + timedelta(seconds=60),
    )
    assert len(reclaimed) == 1
    assert reclaimed[0].lease_token != claimed[0].lease_token
    assert await _lease_owner(session_factory, outbox_id) == "worker-other"


async def _locked_at_of(
    factory: async_sessionmaker[AsyncSession], outbox_id: UUID
) -> datetime | None:
    async with factory() as session:
        value = (
            await session.execute(
                text("SELECT locked_at FROM copilot.outbox_message WHERE id=:id"),
                {"id": outbox_id},
            )
        ).scalar()
        return value


async def _lease_owner(
    factory: async_sessionmaker[AsyncSession], outbox_id: UUID
) -> str:
    async with factory() as session:
        return str(
            (
                await session.execute(
                    text(
                        "SELECT locked_by FROM copilot.outbox_message WHERE id=:id"
                    ),
                    {"id": outbox_id},
                )
            ).scalar()
        )
