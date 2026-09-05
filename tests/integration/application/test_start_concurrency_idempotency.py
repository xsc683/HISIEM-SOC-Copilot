"""Real-Postgres handler-level concurrency + request-idempotency replay.

Fixes #3 and #4 against real rows:

- concurrent ``start_alert_investigation`` (asyncio.gather, 3 callers) converges to
  ONE active investigation: the loser's insert hits the partial unique index, the
  UoW rolls back, the handler re-reads the winner and returns it. Exactly one
  investigation row, one InvestigationCreated event, one dispatch outbox row.
- a stable Idempotency-Key replayed AFTER the original Investigation is terminal
  returns the ORIGINAL investigation (no new row, no new dispatch).
- reusing a key for a DIFFERENT source_alert_ref is a deterministic idempotency
  conflict.
- a NEW key + same alert + terminal previous investigation may create a new run.

Skipped when Postgres is unreachable. HISIEM is a scripted fake (real DB rows only).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.fixtures.hisiem_fake import FakeHisiem

from hisiem_soc_copilot.application.commands.investigation import (
    CompleteInvestigation,
    StartAlertInvestigation,
    StartInvestigation,
)
from hisiem_soc_copilot.application.errors import IdempotencyConflictError
from hisiem_soc_copilot.application.handlers.investigation import InvestigationCommandHandler
from hisiem_soc_copilot.application.handlers.workflow import InvestigationWorkflowHandler
from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.domain.investigation.enums import InvestigationStatus
from hisiem_soc_copilot.domain.investigation.value_objects import (
    BudgetLimits,
    ExternalResourceRef,
)
from hisiem_soc_copilot.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

_TRUNCATE = (
    "tool_invocation",
    "outbox_message",
    "domain_event",
    "command_receipt",
    "orchestration_binding",
    "investigation_result_finding",
    "investigation_result",
    "finding_evidence",
    "finding",
    "evidence",
    "hypothesis_assessment_evidence",
    "hypothesis_assessment",
    "hypothesis",
    "plan_step",
    "plan_revision",
    "investigation",
)


def _settings() -> Settings:
    s = Settings()
    s.database.database_url = (
        "postgresql+psycopg://copilot:copilot@127.0.0.1:5432/copilot"
    )
    s.langgraph.database_url = s.database.database_url
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
async def pg_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if not await _db_reachable():
        pytest.skip("PostgreSQL not reachable — skipping handler concurrency test")
    settings = _settings()
    engine = create_async_engine(settings.database.database_url)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def _clean() -> None:
        async with factory() as session:
            await session.execute(
                text(f"TRUNCATE copilot.{', copilot.'.join(_TRUNCATE)} RESTART IDENTITY CASCADE")
            )
            await session.commit()

    await _clean()
    yield factory
    await _clean()
    await engine.dispose()


def _handler(
    factory: async_sessionmaker[AsyncSession], hisiem: FakeHisiem | None = None
) -> InvestigationCommandHandler:
    return InvestigationCommandHandler(
        unit_of_work_factory=lambda: SqlAlchemyUnitOfWork(factory),
        hisiem=hisiem or FakeHisiem(alert_id="handler-alert-1"),
        budget_limits=BudgetLimits(),
    )


def _workflow(factory: async_sessionmaker[AsyncSession]) -> InvestigationWorkflowHandler:
    return InvestigationWorkflowHandler(
        unit_of_work_factory=lambda: SqlAlchemyUnitOfWork(factory)
    )


async def _drive_to_terminal_completed(
    factory: async_sessionmaker[AsyncSession], investigation_id: UUID
) -> None:
    """CREATED → RUNNING → COMPLETED through the real workflow handler (no graph)."""
    workflow = _workflow(factory)
    iid = investigation_id
    await workflow.start_investigation(
        StartInvestigation(
            tenant_id="tenant-a",
            investigation_id=iid,
            idempotency_key=f"investigation:{iid}:start",
        )
    )
    await workflow.complete(
        CompleteInvestigation(
            tenant_id="tenant-a",
            investigation_id=iid,
            idempotency_key=f"investigation:{iid}:complete",
        )
    )


def _cmd(
    tenant_id: str = "tenant-a",
    alert_id: str = "handler-alert-1",
    *,
    idempotency_key: str | None = None,
    business_id: str | None = None,
) -> StartAlertInvestigation:
    return StartAlertInvestigation(
        tenant_id=tenant_id,
        source_alert_ref=ExternalResourceRef(
            provider="hisiem",
            resource_type="alert",
            address_id=alert_id,
            business_id=business_id,
        ),
        initiated_by_subject="analyst",
        idempotency_key=idempotency_key,
    )


async def _count(
    session: AsyncSession, sql: str, **params: Any
) -> int:
    return int(
        (await session.execute(text(sql), params)).scalar() or 0
    )


async def test_concurrent_starts_converge_to_one_active(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    """asyncio.gather of 3 concurrent starts → same Investigation id, exactly one
    row, one InvestigationCreated event, one dispatch outbox."""
    import asyncio

    hisiem = FakeHisiem(alert_id="handler-alert-1")
    handler = _handler(pg_factory, hisiem=hisiem)

    # Three callers race to start the SAME tenant + alert (no idempotency key: the
    # convergence must come from the DB unique index + re-read, not receipts).
    results = await asyncio.gather(
        handler.start_alert_investigation(_cmd()),
        handler.start_alert_investigation(_cmd()),
        handler.start_alert_investigation(_cmd()),
    )

    ids = {str(r.id) for r in results}
    assert len(ids) == 1  # all returned the SAME logical investigation
    inv_id = ids.pop()

    async with pg_factory() as session:
        investigation_rows = await _count(
            session,
            "SELECT count(*) FROM copilot.investigation WHERE source_address_id='handler-alert-1'",
        )
        created_events = await _count(
            session,
            "SELECT count(*) FROM copilot.domain_event WHERE aggregate_id=:iid "
            "AND event_type='investigation_created'",
            iid=inv_id,
        )
        outbox_rows = await _count(
            session,
            "SELECT count(*) FROM copilot.outbox_message o "
            "JOIN copilot.domain_event e ON e.event_id=o.event_id WHERE e.aggregate_id=:iid",
            iid=inv_id,
        )
    assert investigation_rows == 1
    assert created_events == 1
    assert outbox_rows == 1


async def test_same_key_after_terminal_returns_original(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    """K creates Investigation A → A is completed (terminal) → retry with K returns
    A (no new investigation, no new dispatch)."""
    hisiem = FakeHisiem(alert_id="handler-alert-1")
    handler = _handler(pg_factory, hisiem=hisiem)

    cmd = _cmd(idempotency_key="K-1")
    first = await handler.start_alert_investigation(cmd)
    await _drive_to_terminal_completed(pg_factory, first.id)

    # Replay the SAME Idempotency-Key → returns the ORIGINAL (now COMPLETED) A.
    replayed = await handler.start_alert_investigation(cmd)
    assert replayed.id == first.id
    assert replayed.status == InvestigationStatus.COMPLETED

    async with pg_factory() as session:
        investigation_rows = await _count(
            session,
            "SELECT count(*) FROM copilot.investigation WHERE source_address_id='handler-alert-1'",
        )
        created_outbox_rows = await _count(
            session,
            "SELECT count(*) FROM copilot.outbox_message o "
            "JOIN copilot.domain_event e ON e.event_id=o.event_id "
            "WHERE e.aggregate_id=:iid AND e.event_type='investigation_created'",
            iid=first.id,
        )
    assert investigation_rows == 1  # no second investigation
    assert created_outbox_rows == 1  # no second dispatch


async def test_same_key_same_request_returns_same_result(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    """K + identical request → same logical result both times."""
    hisiem = FakeHisiem(alert_id="handler-alert-1")
    handler = _handler(pg_factory, hisiem=hisiem)
    cmd = _cmd(idempotency_key="K-same")
    first = await handler.start_alert_investigation(cmd)
    second = await handler.start_alert_investigation(cmd)
    assert first.id == second.id


async def test_same_key_different_alert_is_rejected(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    """K used for alert-1 then replayed for alert-2 → deterministic conflict."""
    hisiem = FakeHisiem(alert_id="handler-alert-1")
    handler = _handler(pg_factory, hisiem=hisiem)
    await handler.start_alert_investigation(_cmd(alert_id="handler-alert-1", idempotency_key="K-x"))

    with pytest.raises(IdempotencyConflictError):
        await handler.start_alert_investigation(
            _cmd(alert_id="handler-alert-2", idempotency_key="K-x")
        )


async def test_new_key_same_alert_active_returns_active(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A NEW key + same alert + an active Investigation exists → return the active
    one (no create)."""
    hisiem = FakeHisiem(alert_id="handler-alert-1")
    handler = _handler(pg_factory, hisiem=hisiem)
    first = await handler.start_alert_investigation(_cmd(idempotency_key="K-new-1"))

    second = await handler.start_alert_investigation(_cmd(idempotency_key="K-new-2"))
    assert second.id == first.id  # active exists → returns active


async def test_same_key_same_alert_concurrently_converges_to_one(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fix #20 Case A: asyncio.gather of 3 concurrent starts with the SAME
    Idempotency-Key + SAME alert → all succeed with the SAME Investigation id;
    exactly 1 investigation, 1 investigation_created, 1 dispatch outbox,
    1 command_receipt."""
    import asyncio

    hisiem = FakeHisiem(alert_id="handler-alert-1")
    handler = _handler(pg_factory, hisiem=hisiem)

    # Three DISTINCT concurrent requests (each its own command_id / UUID) that share
    # the SAME Idempotency-Key — the real concurrent-retry shape.
    results = await asyncio.gather(
        handler.start_alert_investigation(_cmd(idempotency_key="K-concurrent-same")),
        handler.start_alert_investigation(_cmd(idempotency_key="K-concurrent-same")),
        handler.start_alert_investigation(_cmd(idempotency_key="K-concurrent-same")),
    )
    ids = {str(r.id) for r in results}
    assert len(ids) == 1  # all three converge to ONE logical result
    inv_id = ids.pop()

    async with pg_factory() as session:
        investigation_rows = await _count(
            session,
            "SELECT count(*) FROM copilot.investigation WHERE source_address_id='handler-alert-1'",
        )
        created_events = await _count(
            session,
            "SELECT count(*) FROM copilot.domain_event WHERE aggregate_id=:iid "
            "AND event_type='investigation_created'",
            iid=inv_id,
        )
        outbox_rows = await _count(
            session,
            "SELECT count(*) FROM copilot.outbox_message o "
            "JOIN copilot.domain_event e ON e.event_id=o.event_id WHERE e.aggregate_id=:iid",
            iid=inv_id,
        )
        receipt_rows = await _count(
            session,
            "SELECT count(*) FROM copilot.command_receipt "
            "WHERE idempotency_key='K-concurrent-same'",
        )
    assert investigation_rows == 1
    assert created_events == 1
    assert outbox_rows == 1
    assert receipt_rows == 1


async def test_same_key_different_alert_concurrently_one_wins_one_conflicts(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fix #20 Case B: concurrent start(K, alert-1) + start(K, alert-2) → exactly one
    succeeds; the other raises IdempotencyConflictError (409). Never a raw
    IntegrityError / 500. Exactly one receipt for K in this tenant scope."""
    import asyncio

    from hisiem_soc_copilot.application.ports.hisiem import HisiemAlertData

    class _TwoAlertHisiem(FakeHisiem):
        """Serves both alerts so start(K, alert-1) and start(K, alert-2) can both
        hydrate; only the Idempotency-Key collides."""

        def __init__(self) -> None:
            super().__init__(alert_id="handler-alert-1")

        async def get_alert(self, *, tenant_id: str, alert_id: str):
            self.calls.append(f"get_alert:{alert_id}")
            base = await super().get_alert(tenant_id=tenant_id, alert_id="handler-alert-1")
            if base is None:
                return None
            return HisiemAlertData(
                alert_id=alert_id,
                tenant_id=base.tenant_id,
                rule_id=base.rule_id,
                rule_name=base.rule_name,
                rule_type=base.rule_type,
                severity=base.severity,
                description=base.description,
                status=base.status,
            )

    hisiem = _TwoAlertHisiem()
    handler = _handler(pg_factory, hisiem=hisiem)
    a = handler.start_alert_investigation(_cmd(alert_id="handler-alert-1", idempotency_key="K-x"))
    b = handler.start_alert_investigation(_cmd(alert_id="handler-alert-2", idempotency_key="K-x"))

    outcomes = await asyncio.gather(a, b, return_exceptions=True)
    successes = [o for o in outcomes if not isinstance(o, BaseException)]
    conflicts = [
        o for o in outcomes if isinstance(o, IdempotencyConflictError)
    ]
    assert len(successes) == 1, f"expected exactly one success, got {outcomes}"
    assert len(conflicts) == 1, f"expected exactly one conflict, got {outcomes}"
    winner = successes[0]

    async with pg_factory() as session:
        receipt_rows = await _count(
            session,
            "SELECT count(*) FROM copilot.command_receipt "
            "WHERE tenant_id='tenant-a' AND command_type='StartAlertInvestigation' "
            "AND idempotency_key='K-x'",
        )
        # One logical investigation is associated with that receipt.
        inv_of_receipt = (
            await session.execute(
                text(
                    "SELECT aggregate_id FROM copilot.command_receipt "
                    "WHERE tenant_id='tenant-a' AND command_type='StartAlertInvestigation' "
                    "AND idempotency_key='K-x'"
                )
            )
        ).scalar_one()
        assert str(winner.id) == str(inv_of_receipt)
    assert receipt_rows == 1


async def test_cross_tenant_same_key_both_succeed(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fix #20: the idempotency space is Tenant-scoped. tenant-A/K and tenant-B/K are
    two INDEPENDENT idempotency spaces → both succeed with their own receipt row."""
    import asyncio

    hisiem = FakeHisiem(alert_id="handler-alert-1")
    handler = _handler(pg_factory, hisiem=hisiem)
    outcomes = await asyncio.gather(
        handler.start_alert_investigation(
            _cmd(tenant_id="tenant-a", alert_id="handler-alert-1", idempotency_key="K-shared")
        ),
        handler.start_alert_investigation(
            _cmd(tenant_id="tenant-b", alert_id="handler-alert-1", idempotency_key="K-shared")
        ),
        return_exceptions=True,
    )
    assert all(not isinstance(o, BaseException) for o in outcomes), outcomes

    async with pg_factory() as session:
        receipt_rows = await _count(
            session,
            "SELECT count(*) FROM copilot.command_receipt WHERE idempotency_key='K-shared'",
        )
        tenant_a_rows = await _count(
            session,
            "SELECT count(*) FROM copilot.command_receipt WHERE tenant_id='tenant-a' "
            "AND idempotency_key='K-shared'",
        )
        tenant_b_rows = await _count(
            session,
            "SELECT count(*) FROM copilot.command_receipt WHERE tenant_id='tenant-b' "
            "AND idempotency_key='K-shared'",
        )
    assert receipt_rows == 2  # two independent tenant-scoped idempotency spaces
    assert tenant_a_rows == 1
    assert tenant_b_rows == 1


async def test_same_key_different_command_type_coexist(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The DB identity is (tenant_id, command_type, idempotency_key): a StartAlertInvestigation
    with key K and a different command type with the SAME key K can coexist in one tenant."""
    hisiem = FakeHisiem(alert_id="handler-alert-1")
    handler = _handler(pg_factory, hisiem=hisiem)
    started = await handler.start_alert_investigation(
        _cmd(idempotency_key="K-multi-type")
    )

    # A workflow command (different command_type) with the SAME key in the SAME
    # tenant is an INDEPENDENT idempotency space → coexists.
    workflow = _workflow(pg_factory)
    await workflow.start_investigation(
        StartInvestigation(
            tenant_id="tenant-a",
            investigation_id=started.id,
            idempotency_key="K-multi-type",
        )
    )

    async with pg_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT command_type FROM copilot.command_receipt "
                    "WHERE tenant_id='tenant-a' AND idempotency_key='K-multi-type' "
                    "ORDER BY command_type"
                )
            )
        ).scalars().all()
    assert sorted(str(r) for r in rows) == [
        "StartAlertInvestigation",
        "StartInvestigation",
    ]


async def test_new_key_same_alert_terminal_may_create_new(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A NEW key + same alert + the previous Investigation is terminal → a new run
    may be created (active-alert uniqueness no longer blocks it)."""
    hisiem = FakeHisiem(alert_id="handler-alert-1")
    handler = _handler(pg_factory, hisiem=hisiem)

    first = await handler.start_alert_investigation(_cmd(idempotency_key="K-a1"))
    await _drive_to_terminal_completed(pg_factory, first.id)

    second = await handler.start_alert_investigation(_cmd(idempotency_key="K-a2"))
    assert second.id != first.id  # terminal prior → a new investigation is allowed

    async with pg_factory() as session:
        investigation_rows = await _count(
            session,
            "SELECT count(*) FROM copilot.investigation WHERE source_address_id='handler-alert-1'",
        )
    assert investigation_rows == 2


async def test_new_key_bound_to_active_returns_same_after_terminal(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Test A: K1 creates Investigation A; a NEW key K2 issued while A is ACTIVE must
    return A AND bind receipt(K2).aggregate_id → A. Once A is COMPLETED, retrying K2
    still returns A — the active-return bind must not be lost, so no Investigation B
    is ever created for K2."""
    hisiem = FakeHisiem(alert_id="handler-alert-1")
    handler = _handler(pg_factory, hisiem=hisiem)

    first = await handler.start_alert_investigation(_cmd(idempotency_key="K1"))

    # K2 (a NEW key) + same alert + A active → returns A and binds K2 → A.
    second = await handler.start_alert_investigation(_cmd(idempotency_key="K2"))
    assert second.id == first.id
    async with pg_factory() as session:
        k2_aggregate = (
            await session.execute(
                text(
                    "SELECT aggregate_id FROM copilot.command_receipt "
                    "WHERE tenant_id='tenant-a' AND command_type='StartAlertInvestigation' "
                    "AND idempotency_key='K2'"
                )
            )
        ).scalar_one()
        assert str(k2_aggregate) == str(first.id)

    # A completes → terminal.
    await _drive_to_terminal_completed(pg_factory, first.id)

    # Retry K2 → still returns A (COMPLETED) via the K2 → A binding; no B is created.
    retried = await handler.start_alert_investigation(_cmd(idempotency_key="K2"))
    assert retried.id == first.id
    assert retried.status == InvestigationStatus.COMPLETED

    async with pg_factory() as session:
        investigation_rows = await _count(
            session,
            "SELECT count(*) FROM copilot.investigation WHERE source_address_id='handler-alert-1'",
        )
        created_events = await _count(
            session,
            "SELECT count(*) FROM copilot.domain_event WHERE event_type='investigation_created'",
        )
    assert investigation_rows == 1  # never a second investigation for K2
    assert created_events == 1


async def test_same_key_same_address_different_business_concurrent_one_conflicts(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Test B: two CONCURRENT requests share key K + address X but carry DIFFERENT
    business_ids. The active-alert uniqueness (address-scoped, NOT business-scoped)
    races, but the winner's committed receipt(K) fingerprints the winner's request —
    so exactly ONE succeeds and the loser raises IdempotencyConflictError. Never a
    raw IntegrityError / 500, and never both succeeding."""
    import asyncio

    hisiem = FakeHisiem(alert_id="handler-alert-1")
    handler = _handler(pg_factory, hisiem=hisiem)
    a = handler.start_alert_investigation(
        _cmd(idempotency_key="K-biz", business_id="biz-A")
    )
    b = handler.start_alert_investigation(
        _cmd(idempotency_key="K-biz", business_id="biz-B")
    )

    outcomes = await asyncio.gather(a, b, return_exceptions=True)
    successes = [o for o in outcomes if not isinstance(o, BaseException)]
    conflicts = [o for o in outcomes if isinstance(o, IdempotencyConflictError)]
    raw_errors = [
        o
        for o in outcomes
        if isinstance(o, Exception) and not isinstance(o, IdempotencyConflictError)
    ]
    assert len(successes) == 1, f"expected exactly one success, got {outcomes}"
    assert len(conflicts) == 1, f"expected exactly one conflict, got {outcomes}"
    assert raw_errors == [], f"expected no raw errors, got {raw_errors}"
    winner = successes[0]

    async with pg_factory() as session:
        receipt_rows = await _count(
            session,
            "SELECT count(*) FROM copilot.command_receipt "
            "WHERE tenant_id='tenant-a' AND command_type='StartAlertInvestigation' "
            "AND idempotency_key='K-biz'",
        )
        # The single committed receipt must point at the winner's investigation.
        winner_aggregate = (
            await session.execute(
                text(
                    "SELECT aggregate_id FROM copilot.command_receipt "
                    "WHERE tenant_id='tenant-a' AND command_type='StartAlertInvestigation' "
                    "AND idempotency_key='K-biz'"
                )
            )
        ).scalar_one()
        investigation_rows = await _count(
            session,
            "SELECT count(*) FROM copilot.investigation WHERE source_address_id='handler-alert-1'",
        )
    assert receipt_rows == 1
    assert str(winner_aggregate) == str(winner.id)
    assert investigation_rows == 1
