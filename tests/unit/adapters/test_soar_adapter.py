"""HISIEM SOAR adapter tests over an httpx MockTransport (P2).

Proves the wire contract: the internal server-to-server path, X-Tenant-ID and
bearer headers, the stable Idempotency-Key, the bounded payload, status mapping,
and fail-closed construction when no service credential is configured.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from hisiem_soc_copilot.application.errors import ExternalServiceError
from hisiem_soc_copilot.config import SoarSettings
from hisiem_soc_copilot.domain.investigation.value_objects import ExternalResourceRef
from hisiem_soc_copilot.infrastructure.soar.adapter import HisiemSoarAdapter

TENANT = "tenant-a"
PLAYBOOK_ID = "11111111-2222-3333-4444-555555555555"


def _target() -> ExternalResourceRef:
    return ExternalResourceRef(
        provider="hisiem", resource_type="alert", address_id="alert-1", business_id="AL-1"
    )


def _settings(**kw: object) -> SoarSettings:
    data = {
        "base_url": "http://hisiem.internal:8080",
        "bearer_token": "s3cr3t-service-token",
        "timeout_seconds": 5.0,
    }
    data.update(kw)
    return SoarSettings(**data)  # type: ignore[arg-type]


def _adapter(handler) -> HisiemSoarAdapter:  # type: ignore[no-untyped-def]
    client = httpx.AsyncClient(
        base_url="http://hisiem.internal:8080", transport=httpx.MockTransport(handler)
    )
    return HisiemSoarAdapter(settings=_settings(), client=client)


def test_construction_fails_closed_without_credential() -> None:
    with pytest.raises(ExternalServiceError) as exc:
        HisiemSoarAdapter(settings=_settings(bearer_token="   "))
    assert exc.value.upstream_code == "SOAR_CONFIGURATION"


async def test_submit_execution_sends_expected_contract() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["tenant"] = request.headers.get("X-Tenant-ID")
        captured["auth"] = request.headers.get("Authorization")
        captured["idem"] = request.headers.get("Idempotency-Key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "execution_id": "exec-77",
                "status": "running",
                "result": {"step": "isolated", "count": 3},
            },
        )

    adapter = _adapter(handler)
    result = await adapter.submit_execution(
        tenant_id=TENANT,
        proposal_id=uuid4(),
        submission_key="response:tenant-a:prop-1",
        action_key="START_SOAR_PLAYBOOK",
        parameters={"playbook_id": PLAYBOOK_ID},
        target_ref=_target(),
    )

    assert captured["path"] == "/api/internal/soar/executions"
    assert captured["tenant"] == TENANT
    assert captured["auth"] == "Bearer s3cr3t-service-token"
    assert captured["idem"] == "response:tenant-a:prop-1"
    assert captured["body"] == {
        "action_key": "START_SOAR_PLAYBOOK",
        "playbook_id": PLAYBOOK_ID,
        "target": {"provider": "hisiem", "resource_type": "alert", "address_id": "alert-1"},
    }
    assert result.execution_id == "exec-77"
    assert result.status == "RUNNING"
    assert result.safe_result == {"step": "isolated", "count": 3}
    await adapter.close()


@pytest.mark.parametrize(
    ("hisiem_status", "expected"),
    [
        ("pending", "QUEUED"),
        ("running", "RUNNING"),
        ("waiting", "RUNNING"),
        ("waiting_human", "RUNNING"),
        ("success", "SUCCEEDED"),
        ("failed", "FAILED"),
        ("cancelled", "FAILED"),
    ],
)
async def test_status_mapping(hisiem_status: str, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"execution_id": "exec-1", "status": hisiem_status}
        )

    adapter = _adapter(handler)
    result = await adapter.get_execution_status(tenant_id=TENANT, execution_id="exec-1")
    assert result.status == expected
    await adapter.close()


async def test_not_found_maps_to_soar_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "nope"})

    adapter = _adapter(handler)
    with pytest.raises(ExternalServiceError) as exc:
        await adapter.get_execution_status(tenant_id=TENANT, execution_id="missing")
    assert exc.value.upstream_code == "SOAR_NOT_FOUND"
    await adapter.close()


async def test_http_error_maps_without_leaking_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"secret": "do-not-leak"})

    adapter = _adapter(handler)
    with pytest.raises(ExternalServiceError) as exc:
        await adapter.submit_execution(
            tenant_id=TENANT,
            proposal_id=uuid4(),
            submission_key="k",
            action_key="START_SOAR_PLAYBOOK",
            parameters={"playbook_id": PLAYBOOK_ID},
            target_ref=_target(),
        )
    assert exc.value.upstream_code == "HTTP_422"
    assert "do-not-leak" not in str(exc.value)
    await adapter.close()


async def test_missing_execution_id_is_invalid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success"})

    adapter = _adapter(handler)
    with pytest.raises(ExternalServiceError) as exc:
        await adapter.get_execution_status(tenant_id=TENANT, execution_id="x")
    assert exc.value.upstream_code == "INVALID_RESPONSE"
    await adapter.close()


async def test_transport_error_maps_to_external_service_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    adapter = _adapter(handler)
    with pytest.raises(ExternalServiceError) as exc:
        await adapter.get_execution_status(tenant_id=TENANT, execution_id="x")
    assert exc.value.service == "hisiem"
    await adapter.close()
