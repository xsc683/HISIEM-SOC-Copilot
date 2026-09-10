"""Workspace API over real Postgres (docs §32).

Drives a real POST → dispatcher drain → COMPLETED investigation over a live Copilot
Postgres (schema ``copilot`` on :5433), then exercises the read-only Workspace
endpoints: the composed projection, Finding→Evidence citation integrity, tenant
isolation (404, never a foreign read), the Alert re-entry lookup, and the response
secret boundary. Reads must not mutate rows.

Skipped when Postgres is unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

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

_ALERT = "ws-alert-1"
_HEADERS = {"X-Tenant-ID": "tenant-a", "X-Actor-Subject": "analyst"}

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

_FORBIDDEN = (
    "CMD_API_KEY",
    "Authorization",
    "Bearer ",
    "password",
    "chain_of_thought",
    "system_prompt",
    "checkpoint",
)


def _settings() -> Settings:
    s = Settings()
    s.database.database_url = (
        "postgresql+psycopg://copilot:copilot@127.0.0.1:5433/copilot"
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


def _script() -> dict[str, Any]:
    return {
        "decide": [
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


@pytest_asyncio.fixture
async def ws_client() -> AsyncIterator[tuple[httpx.AsyncClient, Any, Any]]:
    settings = _settings()
    if not await _db_reachable(settings):
        pytest.skip("PostgreSQL not reachable — skipping Workspace API test")

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
        hisiem = FakeHisiem(alert_id=_ALERT)
        container.hisiem_adapter = hisiem  # type: ignore[assignment]
        container.dispatcher = container.outbox_dispatcher(
            hisiem=hisiem, model=GroundedSshModel(script=_script())
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, container, factory
        await _truncate(factory)
    await engine.dispose()


async def _completed_investigation(
    client: httpx.AsyncClient, container: Any
) -> str:
    res = await client.post(
        "/api/v1/investigations",
        json={
            "source_alert_ref": {
                "provider": "hisiem",
                "resource_type": "alert",
                "address_id": _ALERT,
            }
        },
        headers=_HEADERS,
    )
    assert res.status_code == 201
    investigation_id = res.json()["investigation_id"]
    await container.dispatcher.drain_once()
    return investigation_id


async def test_workspace_composes_completed_investigation(
    ws_client: tuple[httpx.AsyncClient, Any, Any],
) -> None:
    client, container, _factory = ws_client
    investigation_id = await _completed_investigation(client, container)

    res = await client.get(
        f"/api/v1/investigations/{investigation_id}/workspace", headers=_HEADERS
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["investigation"]["status"] == "COMPLETED"
    assert body["source_alert_ref"]["address_id"] == _ALERT

    assert body["plan_revisions"], "expected a persisted plan"
    assert [p["revision"] for p in body["plan_revisions"]] == sorted(
        p["revision"] for p in body["plan_revisions"]
    )

    evidence_ids = {e["evidence_id"] for e in body["evidence"]}
    assert evidence_ids, "expected persisted evidence"
    # grounded evidence carries real HISIEM provenance
    grounded = [e for e in body["evidence"] if e["source"]["operation"] == "search_events"]
    assert grounded, "expected a search_events evidence"
    assert grounded[0]["source"]["provider"] == "hisiem"

    # Finding → Evidence citations resolve to real evidence IDs of THIS investigation
    finding_ids = {f["finding_id"] for f in body["findings"]}
    assert finding_ids, "expected persisted findings"
    for finding in body["findings"]:
        for citation in finding["evidence_citations"]:
            assert citation in evidence_ids, "citation must resolve to real evidence"

    result = body["result"]
    assert result is not None
    assert result["verdict"]["disposition"] == "MALICIOUS"
    assert result["finding_ids"], "result must reference findings"
    assert set(result["finding_ids"]) <= finding_ids

    # tool activity + ordered timeline
    assert body["tool_activity"], "expected tool activity"
    times = [e["occurred_at"] for e in body["timeline"]]
    assert times == sorted(times)
    assert any(e["kind"] == "INVESTIGATION_COMPLETED" for e in body["timeline"])


async def test_workspace_response_has_no_secret_boundary_leak(
    ws_client: tuple[httpx.AsyncClient, Any, Any],
) -> None:
    client, container, _factory = ws_client
    investigation_id = await _completed_investigation(client, container)
    res = await client.get(
        f"/api/v1/investigations/{investigation_id}/workspace", headers=_HEADERS
    )
    assert res.status_code == 200
    text_body = res.text
    for token in _FORBIDDEN:
        assert token not in text_body, f"forbidden token leaked: {token!r}"


async def test_workspace_wrong_tenant_and_unknown_are_404(
    ws_client: tuple[httpx.AsyncClient, Any, Any],
) -> None:
    client, container, _factory = ws_client
    investigation_id = await _completed_investigation(client, container)

    foreign = await client.get(
        f"/api/v1/investigations/{investigation_id}/workspace",
        headers={"X-Tenant-ID": "tenant-b", "X-Actor-Subject": "analyst"},
    )
    assert foreign.status_code == 404

    unknown = await client.get(
        f"/api/v1/investigations/{uuid4()}/workspace",
        headers=_HEADERS,
    )
    assert unknown.status_code == 404


async def test_workspace_read_does_not_mutate(
    ws_client: tuple[httpx.AsyncClient, Any, Any],
) -> None:
    client, container, _factory = ws_client
    investigation_id = await _completed_investigation(client, container)

    sessions = container.session_factory()
    async with sessions() as session:
        before = (
            await session.execute(
                text(
                    "SELECT status, lock_version, revision FROM copilot.investigation "
                    "WHERE id=:iid"
                ),
                {"iid": investigation_id},
            )
        ).one()
        evidence_before = (
            await session.execute(
                text("SELECT count(*) FROM copilot.evidence WHERE investigation_id=:iid"),
                {"iid": investigation_id},
            )
        ).scalar()

    for _ in range(2):
        res = await client.get(
            f"/api/v1/investigations/{investigation_id}/workspace", headers=_HEADERS
        )
        assert res.status_code == 200

    async with sessions() as session:
        after = (
            await session.execute(
                text(
                    "SELECT status, lock_version, revision FROM copilot.investigation "
                    "WHERE id=:iid"
                ),
                {"iid": investigation_id},
            )
        ).one()
        evidence_after = (
            await session.execute(
                text("SELECT count(*) FROM copilot.evidence WHERE investigation_id=:iid"),
                {"iid": investigation_id},
            )
        ).scalar()

    assert tuple(before) == tuple(after)
    assert evidence_before == evidence_after


async def test_alert_lookup_reports_latest(
    ws_client: tuple[httpx.AsyncClient, Any, Any],
) -> None:
    client, container, _factory = ws_client
    investigation_id = await _completed_investigation(client, container)

    res = await client.get(
        "/api/v1/investigations/lookup",
        params={
            "provider": "hisiem",
            "resource_type": "alert",
            "address_id": _ALERT,
        },
        headers=_HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # The investigation is COMPLETED → not active, but it is the latest.
    assert body["active"] is None
    assert body["latest"] is not None
    assert body["latest"]["investigation_id"] == investigation_id

    # A foreign tenant sees nothing for the same alert.
    foreign = await client.get(
        "/api/v1/investigations/lookup",
        params={
            "provider": "hisiem",
            "resource_type": "alert",
            "address_id": _ALERT,
        },
        headers={"X-Tenant-ID": "tenant-b", "X-Actor-Subject": "analyst"},
    )
    assert foreign.status_code == 200
    assert foreign.json() == {"active": None, "latest": None}
