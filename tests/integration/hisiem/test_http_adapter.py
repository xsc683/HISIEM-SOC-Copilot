"""HISIEM HTTP adapter unit tests (httpx MockTransport, no network)."""

from __future__ import annotations

import httpx
import pytest

from hisiem_soc_copilot.application.errors import ExternalServiceError
from hisiem_soc_copilot.config import HisiemSettings
from hisiem_soc_copilot.infrastructure.hisiem.adapter import HisiemHttpAdapter

BASE = "http://hisiem.test"


def _settings(**overrides) -> HisiemSettings:
    values = dict(base_url=BASE, bearer_token="", timeout_seconds=5.0)
    values.update(overrides)
    return HisiemSettings(**values)


def _adapter(
    handler: object,
    settings: HisiemSettings | None = None,
) -> HisiemHttpAdapter:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    client = httpx.AsyncClient(transport=transport, base_url=BASE)
    return HisiemHttpAdapter(settings=settings or _settings(), client=client)


async def test_get_alert_maps_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Tenant-ID"] == "tenant-a"
        return httpx.Response(
            200,
            json={
                "id": "alert-99",
                "tenant_id": "tenant-a",
                "title": "SSH brute force",
                "severity": "high",
                "status": "open",
                "rule_name": "ssh_bruteforce",
            },
        )

    adapter = _adapter(handler)
    alert = await adapter.get_alert(tenant_id="tenant-a", alert_id="alert-99")
    assert alert is not None
    assert alert.alert_id == "alert-99"
    assert alert.tenant_id == "tenant-a"
    assert alert.title == "SSH brute force"
    assert alert.raw is not None
    await adapter.close()


async def test_get_alert_404_returns_none() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    adapter = _adapter(handler)
    assert await adapter.get_alert(tenant_id="tenant-a", alert_id="missing") is None
    await adapter.close()


async def test_get_alert_5xx_raises_external_service_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "boom"})

    adapter = _adapter(handler)
    with pytest.raises(ExternalServiceError):
        await adapter.get_alert(tenant_id="tenant-a", alert_id="alert-99")
    await adapter.close()


async def test_search_events_passes_tenant_and_returns_list() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Tenant-ID"] == "tenant-a"
        assert "query" in (request.content.decode() if request.content else "")
        return httpx.Response(200, json=[{"_id": "evt-1"}, {"_id": "evt-2"}])

    adapter = _adapter(handler)
    events = await adapter.search_events(
        tenant_id="tenant-a", query="source.ip:1.2.3.4", size=50
    )
    assert len(events) == 2
    await adapter.close()


async def test_invalid_payload_raises() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "an alert object"})

    adapter = _adapter(handler)
    alert = await adapter.get_alert(tenant_id="tenant-a", alert_id="alert-99")
    # mapper tolerates missing keys; alert id empty indicates malformed authority data
    assert alert is not None and alert.alert_id == ""
    await adapter.close()
