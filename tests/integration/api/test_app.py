"""API tests — health endpoint and (when Postgres is up) start-investigation.

Runs FastAPI over httpx ASGITransport with the real container wired to a running
PostgreSQL (copilot schema migrated). Skipped when COPILOT_DATABASE_URL is not
reachable, so the suite stays green on dev machines without Docker.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager

from hisiem_soc_copilot.api.app import create_app
from hisiem_soc_copilot.config import Settings


def _settings() -> Settings:
    s = Settings()
    s.database.database_url = (
        "postgresql+psycopg://copilot:copilot@127.0.0.1:5432/copilot"
    )
    s.hisiem.base_url = "http://hisiem.test.invalid"
    # API integration tests exercise the header (dev/test) trusted-context
    # provider; production must not select it (config default is ``none``).
    s.auth.trusted_context_provider = "header"
    return s


async def _db_reachable(settings: Settings) -> bool:
    """Probe the DB synchronously — psycopg async cannot run on Windows' default
    ProactorEventLoop, so this fixture avoids async engines entirely."""
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
        cur = conn.execute("SELECT 1")
        cur.fetchone()
        conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    settings = _settings()
    if not await _db_reachable(settings):
        pytest.skip("PostgreSQL not reachable — skipping API integration test")
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            yield c


async def test_healthz(client: httpx.AsyncClient) -> None:
    res = await client.get("/healthz")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


async def test_unknown_alert_start_returns_not_found(client: httpx.AsyncClient) -> None:
    # HISIEM base URL is unreachable (http://hisiem.test.invalid) → adapter maps to
    # a service error; the API should return 502 rather than crash.
    res = await client.post(
        "/api/v1/investigations",
        json={"source_alert_id": "alert-x"},
        headers={"X-Tenant-ID": "tenant-a", "X-Actor-Subject": "analyst"},
    )
    assert res.status_code in (502, 503)


async def test_no_provider_fails_closed() -> None:
    """With the default (``none``) provider, requests cannot be trusted at all."""
    settings = _settings()
    settings.auth.trusted_context_provider = "none"  # explicit production-safe default
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            # Even with headers present, no configured provider → untrusted.
            res = await c.post(
                "/api/v1/investigations",
                json={"source_alert_id": "alert-x"},
                headers={"X-Tenant-ID": "tenant-a", "X-Actor-Subject": "analyst"},
            )
            assert res.status_code == 403
            assert res.json()["code"] == "UNTRUSTED_REQUEST"
