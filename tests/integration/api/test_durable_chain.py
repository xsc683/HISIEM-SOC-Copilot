"""API → outbox → dispatcher → OrchestrationBinding → LangGraph checkpoint → graph.

The required real-Postgres chain: a POST /api/v1/investigations atomically commits
the investigation + InvestigationCreated + outbox row + receipt, returns quickly,
and a subsequent dispatcher ``drain_once()`` starts the investigation graph which
reaches COMPLETED (MALICIOUS). A second duplicate POST returns the SAME active
investigation and does not re-dispatch.

Skipped when Postgres is unreachable. The HISIEM alert hydration + the graph reads
run against a scripted fake (real DB rows only).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hisiem_soc_copilot.api.app import create_app
from hisiem_soc_copilot.config import Settings
from tests.fixtures.hisiem_fake import FakeHisiem
from tests.fixtures.ssh_models import GroundedSshModel

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
    s.auth.trusted_context_provider = "header"
    return s


async def _db_reachable(settings: Settings) -> bool:
    try:
        import psycopg
        from sqlalchemy.engine import make_url

        url = make_url(settings.database.database_url)
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
async def chain_client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    settings = _settings()
    if not await _db_reachable(settings):
        pytest.skip("PostgreSQL not reachable — skipping durable API chain test")

    async def _truncate(session_factory: Any) -> None:
        async with session_factory() as session:
            await session.execute(
                text(
                    f"TRUNCATE copilot.{', copilot.'.join(_TRUNCATE)} "
                    "RESTART IDENTITY CASCADE"
                )
            )
            await session.commit()

    engine = create_async_engine(settings.database.database_url)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await _truncate(factory)

    app = create_app(settings)
    async with LifespanManager(app):
        container = app.state.container
        # Drive the whole chain with a scripted HISIEM + scripted model so no real
        # HISIEM is needed; the container is otherwise real (Postgres).
        hisiem = FakeHisiem(alert_id="chain-alert-1")
        container.hisiem_adapter = hisiem  # type: ignore[assignment]
        container.dispatcher = container.outbox_dispatcher(
            hisiem=hisiem,
            model=GroundedSshModel(
                script={
                    "decide": [
                        {
                            "tool_name": "hisiem.get_detection_rule",
                            "arguments": {"rule_id": "ssh_brute_force"},
                        },
                        {
                            "tool_name": "hisiem.search_events",
                            "arguments": {
                                "from": "2026-09-01T09:55:00Z",
                                "to": "2026-09-01T10:05:00Z",
                                "conditions": [
                                    {
                                        "field": "event.action",
                                        "operator": "is",
                                        "value": "authentication_success",
                                    }
                                ],
                            },
                        },
                    ],
                    "findings": ["root login after brute force"],
                    "verdict": {
                        "disposition": "MALICIOUS",
                        "summary": "SSH compromise confirmed",
                        "confidence": 0.9,
                    },
                }
            ),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, container
        # Clean up rows for idempotent re-runs.
        await _truncate(factory)
    await engine.dispose()


async def _read_status(session: Any, investigation_id: str) -> str:
    row = (
        await session.execute(
            text(
                "SELECT status FROM copilot.investigation WHERE id=:iid"
            ),
            {"iid": investigation_id},
        )
    ).scalar()
    return str(row)


async def test_post_then_dispatch_reaches_completed(
    chain_client: tuple[httpx.AsyncClient, Any],
) -> None:
    client, container = chain_client
    # POST commits + returns fast with the investigation still CREATED.
    res = await client.post(
        "/api/v1/investigations",
        json={
            "source_alert_ref": {
                "provider": "hisiem",
                "resource_type": "alert",
                "address_id": "chain-alert-1",
            },
        },
        headers={"X-Tenant-ID": "tenant-a", "X-Actor-Subject": "analyst"},
    )
    assert res.status_code == 201
    body = res.json()
    inv_id = body["investigation_id"]
    assert body["status"] == "CREATED"

    # The dispatcher then picks up the outbox row and runs the graph to COMPLETED.
    dispatcher = container.dispatcher
    await dispatcher.drain_once()

    # Read the aggregate back from Postgres through the app's session factory.
    sessions = container.session_factory()
    async with sessions() as session:
        status = await _read_status(session, inv_id)
        outbox_status = (
            await session.execute(
                text(
                    "SELECT o.status FROM copilot.outbox_message o "
                    "JOIN copilot.domain_event e ON e.event_id=o.event_id "
                    "JOIN copilot.investigation i ON i.id=e.aggregate_id "
                    "WHERE i.id=:iid"
                ),
                {"iid": inv_id},
            )
        ).scalars().all()
        binding_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM copilot.orchestration_binding "
                    "WHERE investigation_id=:iid"
                ),
                {"iid": inv_id},
            )
        ).scalar()
        result_disposition = (
            await session.execute(
                text(
                    "SELECT verdict_disposition FROM copilot.investigation_result "
                    "WHERE investigation_id=:iid"
                ),
                {"iid": inv_id},
            )
        ).scalar()

    assert status == "COMPLETED"
    assert "PUBLISHED" in outbox_status  # dispatch delivered + published
    assert binding_count == 1
    assert result_disposition == "MALICIOUS"


async def test_duplicate_post_returns_existing_and_does_not_re_dispatch(
    chain_client: tuple[httpx.AsyncClient, Any],
) -> None:
    client, container = chain_client
    first = await client.post(
        "/api/v1/investigations",
        json={
            "source_alert_ref": {
                "provider": "hisiem",
                "resource_type": "alert",
                "address_id": "chain-alert-1",
            },
        },
        headers={"X-Tenant-ID": "tenant-a", "X-Actor-Subject": "analyst"},
    )
    assert first.status_code == 201
    first_id = first.json()["investigation_id"]

    # Second POST for the same tenant+alert returns the SAME active investigation
    # (no second outbox row, so a subsequent drain is a no-op for it).
    second = await client.post(
        "/api/v1/investigations",
        json={
            "source_alert_ref": {
                "provider": "hisiem",
                "resource_type": "alert",
                "address_id": "chain-alert-1",
            },
        },
        headers={"X-Tenant-ID": "tenant-a", "X-Actor-Subject": "analyst"},
    )
    assert second.status_code == 201
    assert second.json()["investigation_id"] == first_id

    # Drain: only ONE investigation_created outbox row existed (from the first
    # POST), so the graph runs once; the duplicate created no second dispatch.
    await container.dispatcher.drain_once()

    sessions = container.session_factory()
    async with sessions() as session:
        investigation_rows = (
            await session.execute(
                text(
                    "SELECT count(*) FROM copilot.investigation "
                    "WHERE source_address_id='chain-alert-1'"
                )
            )
        ).scalar()
        outbox_rows = (
            await session.execute(text("SELECT count(*) FROM copilot.outbox_message"))
        ).scalar()
        status = await _read_status(session, first_id)
    assert investigation_rows == 1
    assert outbox_rows == 1
    assert status == "COMPLETED"


async def test_same_idempotency_key_different_alert_api_409(
    chain_client: tuple[httpx.AsyncClient, Any],
) -> None:
    """A stable Idempotency-Key bound to alert-1, replayed for alert-2 through the
    API → a deterministic 409 IDEMPOTENCY_CONFLICT (never 500 / raw IntegrityError)."""
    import uuid

    from hisiem_soc_copilot.application.ports.hisiem import HisiemAlertData

    client, container = chain_client

    class _TwoAlertHisiem(FakeHisiem):
        """Serves both chain alerts so each POST can hydrate."""

        def __init__(self) -> None:
            super().__init__(alert_id="chain-alert-1")
            self._base = super()

        async def get_alert(self, *, tenant_id: str, alert_id: str):
            self.calls.append(f"get_alert:{alert_id}")
            base = await self._base.get_alert(
                tenant_id=tenant_id, alert_id="chain-alert-1"
            )
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

    container.hisiem_adapter = _TwoAlertHisiem()  # type: ignore[assignment]

    key = f"api-key-{uuid.uuid4()}"
    ok = await client.post(
        "/api/v1/investigations",
        json={
            "source_alert_ref": {
                "provider": "hisiem",
                "resource_type": "alert",
                "address_id": "chain-alert-1",
            },
        },
        headers={
            "X-Tenant-ID": "tenant-a",
            "X-Actor-Subject": "analyst",
            "Idempotency-Key": key,
        },
    )
    assert ok.status_code == 201

    # Replaying the SAME key for a DIFFERENT alert is a deterministic conflict.
    conflict = await client.post(
        "/api/v1/investigations",
        json={
            "source_alert_ref": {
                "provider": "hisiem",
                "resource_type": "alert",
                "address_id": "chain-alert-2",
            },
        },
        headers={
            "X-Tenant-ID": "tenant-a",
            "X-Actor-Subject": "analyst",
            "Idempotency-Key": key,
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"

    # Exactly ONE receipt for this key/tenant/command scope, pointing at alert-1's
    # investigation — no second row, no second investigation leaked.
    sessions = container.session_factory()
    async with sessions() as session:
        receipt_rows = (
            await session.execute(
                text(
                    "SELECT count(*) FROM copilot.command_receipt "
                    "WHERE tenant_id='tenant-a' AND command_type='StartAlertInvestigation' "
                    "AND idempotency_key=:key"
                ),
                {"key": key},
            )
        ).scalar()
        alert2_investigations = (
            await session.execute(
                text(
                    "SELECT count(*) FROM copilot.investigation "
                    "WHERE source_address_id='chain-alert-2'"
                )
            )
        ).scalar()
    assert receipt_rows == 1
    assert alert2_investigations == 0
