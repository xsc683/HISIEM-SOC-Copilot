"""Persistence integration tests against a real PostgreSQL (copilot schema).

These exercise the invariants the schema must enforce (persistence-schema.md §38).
They are skipped when PostgreSQL is not reachable, so the suite stays green on dev
machines without Docker. Requires: ``alembic upgrade head`` applied.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from hisiem_soc_copilot.infrastructure.persistence.repositories.investigation import (
    SqlAlchemyInvestigationRepository,
)
from hisiem_soc_copilot.infrastructure.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
)


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
        cur = conn.execute("SELECT 1")
        cur.fetchone()
        conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if not await _db_reachable():
        import pytest

        pytest.skip("PostgreSQL not reachable — skipping persistence integration")
    settings = _settings()
    engine = create_async_engine(settings.database.database_url)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    # Clear investigation rows so tests are idempotent.
    async with factory() as session:
        await session.execute(text("DELETE FROM copilot.investigation"))
        await session.commit()
    yield factory
    async with factory() as session:
        await session.execute(text("DELETE FROM copilot.investigation"))
        await session.commit()
    await engine.dispose()


def _inv(tenant_id: str = "tenant-a", alert_id: str = "alert-1") -> Investigation:
    return Investigation.create(
        id=uuid4(),
        tenant_id=tenant_id,
        source_alert_ref=ExternalResourceRef(
            provider="hisiem", resource_type="alert", address_id=alert_id
        ),
        initiated_by=ActorRef(subject_id="analyst", tenant_id=tenant_id),
        budget_limits=BudgetLimits(),
    )


async def test_repository_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        repo = SqlAlchemyInvestigationRepository(session)
        inv = _inv()
        await repo.add(inv)
        await session.commit()

        loaded = await repo.get(tenant_id="tenant-a", investigation_id=inv.id)
        assert loaded is not None
        assert loaded.status.value == "CREATED"
        assert loaded.tenant_id == "tenant-a"


async def test_optimistic_lock_conflict_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Two UoWs each load the same aggregate; both try to update; second must fail.
    async with session_factory() as s1, session_factory() as s2:
        r1 = SqlAlchemyInvestigationRepository(s1)
        r2 = SqlAlchemyInvestigationRepository(s2)
        inv = _inv()
        await r1.add(inv)
        await s1.commit()

        # Load into two aggregates
        a1 = await r1.get(tenant_id="tenant-a", investigation_id=inv.id)
        a2 = await r2.get(tenant_id="tenant-a", investigation_id=inv.id)
        assert a1 is not None and a2 is not None

        # First update succeeds
        a1.start(actor=a1.initiated_by)
        await r1.update(a1)
        await s1.commit()

        # Second update with stale lock_version raises OptimisticConcurrencyError
        import pytest

        from hisiem_soc_copilot.domain.shared.errors import OptimisticConcurrencyError

        a2.start(actor=a2.initiated_by)
        with pytest.raises(OptimisticConcurrencyError):
            await r2.update(a2)


async def test_only_one_active_investigation_per_alert(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two concurrent starts on the same tenant+alert converge to exactly one."""
    import asyncio

    from sqlalchemy import text as _text

    async def attempt(alert_id: str) -> bool:
        # each attempt runs its own transaction with a fresh investigation id
        uow = SqlAlchemyUnitOfWork(session_factory)
        inv = _inv(tenant_id="tenant-a", alert_id=alert_id)
        try:
            existing = await uow.investigations.find_active_by_alert(
                tenant_id="tenant-a",
                source_alert_ref=inv.source_alert_ref,
            )
            if existing is not None:
                await uow.close()
                return True
            await uow.investigations.add(inv)
            await uow.commit()
            await uow.close()
            return True
        except Exception:
            await uow.rollback()
            await uow.close()
            return False

    alert_id = "concurrent-alert-1"
    results = await asyncio.gather(
        attempt(alert_id), attempt(alert_id), attempt(alert_id)
    )
    assert all(results)

    async with session_factory() as session:
        rows = (
            await session.execute(
                _text(
                    "SELECT count(*) FROM copilot.investigation "
                    "WHERE tenant_id='tenant-a' AND source_address_id=:aid"
                ),
                {"aid": alert_id},
            )
        ).scalar()
        assert rows == 1
