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


async def test_get_alert_maps_flat_alert_payload() -> None:
    """The real HISIEM alert payload flattens fields under ``alert.*`` + ``_id``."""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Tenant-ID"] == "tenant-a"
        return httpx.Response(
            200,
            json={
                "_id": "es_alert_sha1",
                "alert": {
                    "id": "alert-uuid-1",
                    "rule_id": "rule-100",
                    "rule_name": "ssh_bruteforce",
                    "type": "bruteforce",
                    "severity": "high",
                    "description": "SSH brute force",
                    "status": "open",
                    "created_at": "2026-09-01T10:00:00Z",
                    "risk_score": 85,
                },
                "rule": {"tags": ["brute-force", "ssh"]},
                "source.ip": "203.0.113.9",
                "user.name": "root",
                "host.name": "web-01",
            },
        )

    adapter = _adapter(handler)
    alert = await adapter.get_alert(tenant_id="tenant-a", alert_id="es_alert_sha1")
    assert alert is not None
    assert alert.alert_id == "es_alert_sha1"
    assert alert.rule_id == "rule-100"
    assert alert.rule_name == "ssh_bruteforce"
    assert alert.rule_type == "bruteforce"
    assert alert.severity == "high"
    assert alert.description == "SSH brute force"
    assert alert.risk_score == 85
    assert alert.source_ip == "203.0.113.9"
    assert alert.user_name == "root"
    assert alert.rule_tags == ["brute-force", "ssh"]
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


async def test_search_events_posts_bounded_log_search() -> None:
    import json as _json

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Tenant-ID"] == "tenant-a"
        body = _json.loads(request.content or b"{}")
        assert body["logic"] == "AND"
        assert body["conditions"] == [
            {"field": "user.name", "operator": "is", "value": "root"}
        ]
        assert body["size"] == 100
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "_id": "evt-1",
                        "_index": "siem-events-2026.09.01",
                        "@timestamp": "2026-09-01T10:00:01Z",
                        "event": {"action": "authentication_success"},
                        "user.name": "root",
                        "source.ip": "203.0.113.9",
                    }
                ],
                "page": 0,
                "size": 100,
                "total": 1,
                "tookMs": 12,
                "from": "2026-09-01T09:00:00Z",
                "to": "2026-09-01T10:00:00Z",
            },
        )

    adapter = _adapter(handler)
    result = await adapter.search_events(
        tenant_id="tenant-a",
        from_="2026-09-01T09:00:00Z",
        to="2026-09-01T10:00:00Z",
        conditions=[{"field": "user.name", "operator": "is", "value": "root"}],
        limit=100,
    )
    assert len(result.items) == 1
    assert result.total == 1
    assert result.items[0].document_id == "evt-1"
    assert result.items[0].source_ip == "203.0.113.9"
    await adapter.close()


async def test_get_detection_rule_maps_yaml_map() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Tenant-ID"] == "tenant-a"
        return httpx.Response(
            200,
            json={
                "id": "rule-100",
                "name": "SSH Brute Force",
                "category": "credential-access",
                "type": "threshold",
                "severity": "high",
                "enabled": True,
                "version": "1.0",
                "tags": ["ssh", "brute-force"],
                "condition": {"field": "event.action", "value": "failed-login"},
            },
        )

    adapter = _adapter(handler)
    rule = await adapter.get_detection_rule(tenant_id="tenant-a", rule_id="rule-100")
    assert rule is not None
    assert rule.rule_id == "rule-100"
    assert rule.name == "SSH Brute Force"
    assert rule.category == "credential-access"
    assert rule.enabled is True
    assert rule.logic_summary is not None
    await adapter.close()


async def test_invalid_payload_raises() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "an alert object"})

    adapter = _adapter(handler)
    # A provider response WITHOUT the addressing ``_id`` is invalid — it must never
    # be silently mapped (e.g. by falling back to ``alert.id``); it raises instead.
    with pytest.raises(ExternalServiceError):
        await adapter.get_alert(tenant_id="tenant-a", alert_id="alert-99")
    await adapter.close()


async def test_alert_id_never_falls_back_to_alert_id_business_field() -> None:
    """Fix #17: the addressing alert_id comes ONLY from ``_id``.

    A payload that has ``alert.id`` but NO ``_id`` is an invalid provider response
    (never silently addressed by the business id); and when ``_id`` IS present, the
    ``alert.id`` value only ever populates the optional ``business_id`` display field
    — never the addressing alert_id.
    """
    from hisiem_soc_copilot.infrastructure.hisiem.mapper import map_alert_detail

    # 1) _id present + alert.id present → alert_id = _id; business_id = alert.id.
    mapped = map_alert_detail(
        {"_id": "es_alert_sha1", "alert": {"id": "alert-uuid-1"}},
        tenant_id="tenant-a",
    )
    assert mapped.alert_id == "es_alert_sha1"
    assert mapped.business_id == "alert-uuid-1"

    # 2) alert.id present but _id MISSING → invalid (raises, no fallback).
    with pytest.raises(ExternalServiceError):
        map_alert_detail({"alert": {"id": "alert-uuid-1"}}, tenant_id="tenant-a")
