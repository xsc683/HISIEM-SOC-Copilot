"""Header TrustedContextProvider (dev/test adapter) unit tests — no DB."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from hisiem_soc_copilot.application.ports.trust import UntrustedRequestError
from hisiem_soc_copilot.infrastructure.auth.header_provider import (
    HeaderTrustedContextProvider,
)


def _request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/investigations",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "query_string": b"",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1234),
        "scheme": "http",
        "root_path": "",
    }
    return Request(scope)


async def test_resolves_tenant_and_actor() -> None:
    req = _request(
        {
            "X-Tenant-ID": "tenant-a",
            "X-Actor-Subject": "analyst@corp",
            "X-Actor-Display-Name": "Analyst One",
        }
    )
    provider = HeaderTrustedContextProvider(req)
    ctx = await provider.resolve()
    assert ctx.tenant_id == "tenant-a"
    assert ctx.actor_subject_id == "analyst@corp"
    assert ctx.actor_display_name == "Analyst One"


async def test_missing_tenant_raises_untrusted() -> None:
    provider = HeaderTrustedContextProvider(_request({"X-Actor-Subject": "analyst"}))
    with pytest.raises(UntrustedRequestError):
        await provider.resolve()
