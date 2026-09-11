"""Cross-boundary service-authentication tests over real Postgres.

Models the actual P1 boundary: an attacker who can reach Copilot directly must
NOT obtain any Investigation privilege by asserting ``X-Tenant-ID`` /
``X-Actor-Subject`` alone. Only a request carrying the HISIEM service bearer
credential — which the browser never holds — is authenticated; only then are the
tenant/actor headers trusted, and tenant isolation still applies afterward.

Skipped when Postgres is unreachable.
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

_ALERT = "authz-alert-1"
_SECRET = "integration-service-secret-value"
_ENV_NAME = "HISIEM_COPILOT_SERVICE_TOKEN"

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
        "postgresql+psycopg://copilot:copilot@127.0.0.1:5433/copilot"
    )
    s.langgraph.database_url = s.database.database_url
    s.auth.trusted_context_provider = "hisiem_bearer"
    s.auth.hisiem_service_token_env = _ENV_NAME
    return s


def _headers(
    *, tenant: str = "tenant-a", actor: str = "analyst", bearer: str | None = _SECRET
) -> dict[str, str]:
    headers = {"X-Tenant-ID": tenant, "X-Actor-Subject": actor}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


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
async def boundary_client(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    settings = _settings()
    if not await _db_reachable(settings):
        pytest.skip("PostgreSQL not reachable — skipping service-auth boundary test")

    monkeypatch.setenv(_ENV_NAME, _SECRET)

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
            yield c, container
        await _truncate(factory)
    await engine.dispose()


async def _start_completed(client: httpx.AsyncClient, container: Any) -> str:
    res = await client.post(
        "/api/v1/investigations",
        json={
            "source_alert_ref": {
                "provider": "hisiem",
                "resource_type": "alert",
                "address_id": _ALERT,
            }
        },
        headers=_headers(),
    )
    assert res.status_code == 201, res.text
    investigation_id = res.json()["investigation_id"]
    await container.dispatcher.drain_once()
    return investigation_id


async def test_forged_headers_without_bearer_cannot_start_investigation(
    boundary_client: tuple[httpx.AsyncClient, Any],
) -> None:
    client, _ = boundary_client
    res = await client.post(
        "/api/v1/investigations",
        json={
            "source_alert_ref": {
                "provider": "hisiem",
                "resource_type": "alert",
                "address_id": _ALERT,
            }
        },
        headers={"X-Tenant-ID": "tenant-a", "X-Actor-Subject": "attacker"},
    )
    assert res.status_code == 401, res.text
    assert res.json()["code"] == "SERVICE_AUTHENTICATION_FAILED"


async def test_forged_headers_without_bearer_cannot_read_workspace(
    boundary_client: tuple[httpx.AsyncClient, Any],
) -> None:
    client, container = boundary_client
    investigation_id = await _start_completed(client, container)

    forged = await client.get(
        f"/api/v1/investigations/{investigation_id}/workspace",
        headers={"X-Tenant-ID": "tenant-a", "X-Actor-Subject": "attacker"},
    )
    assert forged.status_code == 401
    assert "MALICIOUS" not in forged.text
    assert investigation_id not in forged.text


async def test_wrong_bearer_is_rejected(
    boundary_client: tuple[httpx.AsyncClient, Any],
) -> None:
    client, container = boundary_client
    investigation_id = await _start_completed(client, container)

    res = await client.get(
        f"/api/v1/investigations/{investigation_id}/workspace",
        headers=_headers(bearer="not-the-service-secret"),
    )
    assert res.status_code == 401
    assert res.json()["code"] == "SERVICE_AUTHENTICATION_FAILED"


async def test_valid_bearer_with_valid_tenant_is_accepted(
    boundary_client: tuple[httpx.AsyncClient, Any],
) -> None:
    client, container = boundary_client
    investigation_id = await _start_completed(client, container)

    res = await client.get(
        f"/api/v1/investigations/{investigation_id}/workspace",
        headers=_headers(),
    )
    assert res.status_code == 200, res.text
    assert res.json()["investigation"]["status"] == "COMPLETED"


async def test_valid_bearer_but_missing_tenant_is_rejected(
    boundary_client: tuple[httpx.AsyncClient, Any],
) -> None:
    client, container = boundary_client
    investigation_id = await _start_completed(client, container)

    res = await client.get(
        f"/api/v1/investigations/{investigation_id}/workspace",
        headers={"Authorization": f"Bearer {_SECRET}", "X-Actor-Subject": "analyst"},
    )
    assert res.status_code == 401


async def test_valid_bearer_does_not_bypass_tenant_isolation(
    boundary_client: tuple[httpx.AsyncClient, Any],
) -> None:
    client, container = boundary_client
    # Investigation lives under tenant-a.
    investigation_id = await _start_completed(client, container)

    # Valid service credential, but a DIFFERENT tenant → still no foreign read.
    foreign = await client.get(
        f"/api/v1/investigations/{investigation_id}/workspace",
        headers=_headers(tenant="tenant-b"),
    )
    assert foreign.status_code == 404
    assert investigation_id not in foreign.text


# §44: EVERY request-time service-auth failure must be externally
# indistinguishable — same status, same code, same message — so the boundary
# leaks no authentication oracle (which factor failed, or whether any did).
_AUTH_FAILURE_VECTORS: dict[str, dict[str, str]] = {
    "no_headers": {},
    "headers_without_bearer": {"X-Tenant-ID": "tenant-a", "X-Actor-Subject": "analyst"},
    "wrong_bearer": {
        "Authorization": "Bearer not-the-service-secret",
        "X-Tenant-ID": "tenant-a",
        "X-Actor-Subject": "analyst",
    },
    "valid_bearer_missing_tenant": {
        "Authorization": f"Bearer {_SECRET}",
        "X-Actor-Subject": "analyst",
    },
    "valid_bearer_missing_actor": {
        "Authorization": f"Bearer {_SECRET}",
        "X-Tenant-ID": "tenant-a",
    },
    "empty_bearer": {
        "Authorization": "Bearer ",
        "X-Tenant-ID": "tenant-a",
        "X-Actor-Subject": "analyst",
    },
}


@pytest.mark.parametrize("vector", list(_AUTH_FAILURE_VECTORS))
async def test_auth_failures_are_indistinguishable(
    boundary_client: tuple[httpx.AsyncClient, Any],
    vector: str,
) -> None:
    client, _ = boundary_client
    res = await client.get(
        "/api/v1/investigations/lookup",
        params={"provider": "hisiem", "resource_type": "alert", "address_id": _ALERT},
        headers=_AUTH_FAILURE_VECTORS[vector],
    )
    assert res.status_code == 401, (vector, res.text)
    body = res.json()
    assert body["code"] == "SERVICE_AUTHENTICATION_FAILED", vector
    assert body["message"] == "service authentication failed", vector
