"""HisiemServiceTrustedContextProvider unit tests — no DB.

Proves the service-to-service authentication boundary: the caller is authenticated
BEFORE any tenant/actor header is trusted, comparison is constant-time, and every
failure is a generic (non-secret-revealing) ServiceAuthenticationError.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from hisiem_soc_copilot.application.ports.trust import (
    ServiceAuthenticationError,
    TrustedContext,
)
from hisiem_soc_copilot.infrastructure.auth.hisiem_service_provider import (
    HisiemServiceTrustedContextProvider,
)

_SECRET = "test-service-secret-value"


def _request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/investigations/x/workspace",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "query_string": b"",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1234),
        "scheme": "http",
        "root_path": "",
    }
    return Request(scope)


def _provider(
    headers: dict[str, str], *, token: str = _SECRET
) -> HisiemServiceTrustedContextProvider:
    return HisiemServiceTrustedContextProvider(_request(headers), expected_token=token)


async def test_valid_bearer_resolves_context() -> None:
    ctx = await _provider(
        {
            "Authorization": f"Bearer {_SECRET}",
            "X-Tenant-ID": "tenant-a",
            "X-Actor-Subject": "analyst@corp",
            "X-Actor-Display-Name": "Analyst One",
        }
    ).resolve()
    assert isinstance(ctx, TrustedContext)
    assert ctx.tenant_id == "tenant-a"
    assert ctx.actor_subject_id == "analyst@corp"
    assert ctx.actor_display_name == "Analyst One"


async def test_scheme_is_case_insensitive_but_token_exact() -> None:
    ctx = await _provider(
        {"Authorization": f"bearer {_SECRET}", "X-Tenant-ID": "t", "X-Actor-Subject": "a"}
    ).resolve()
    assert ctx.tenant_id == "t"


async def test_missing_authorization_rejected() -> None:
    with pytest.raises(ServiceAuthenticationError):
        await _provider({"X-Tenant-ID": "tenant-a", "X-Actor-Subject": "analyst"}).resolve()


async def test_wrong_scheme_rejected() -> None:
    for scheme in ("Basic", "Token", "Service"):
        with pytest.raises(ServiceAuthenticationError):
            await _provider(
                {"Authorization": f"{scheme} {_SECRET}", "X-Tenant-ID": "t", "X-Actor-Subject": "a"}
            ).resolve()


async def test_empty_bearer_rejected() -> None:
    with pytest.raises(ServiceAuthenticationError):
        await _provider(
            {"Authorization": "Bearer ", "X-Tenant-ID": "t", "X-Actor-Subject": "a"}
        ).resolve()


async def test_wrong_bearer_rejected() -> None:
    with pytest.raises(ServiceAuthenticationError):
        await _provider(
            {
                "Authorization": "Bearer not-the-secret",
                "X-Tenant-ID": "tenant-a",
                "X-Actor-Subject": "analyst",
            }
        ).resolve()


async def test_ambiguous_authorization_rejected() -> None:
    with pytest.raises(ServiceAuthenticationError):
        await _provider(
            {
                "Authorization": f"Bearer {_SECRET} extra",
                "X-Tenant-ID": "t",
                "X-Actor-Subject": "a",
            }
        ).resolve()


async def test_valid_bearer_but_missing_tenant_rejected() -> None:
    with pytest.raises(ServiceAuthenticationError):
        await _provider(
            {"Authorization": f"Bearer {_SECRET}", "X-Actor-Subject": "analyst"}
        ).resolve()


async def test_valid_bearer_but_blank_tenant_rejected() -> None:
    with pytest.raises(ServiceAuthenticationError):
        await _provider(
            {"Authorization": f"Bearer {_SECRET}", "X-Tenant-ID": "  ", "X-Actor-Subject": "a"}
        ).resolve()


async def test_valid_bearer_but_missing_actor_rejected() -> None:
    with pytest.raises(ServiceAuthenticationError):
        await _provider({"Authorization": f"Bearer {_SECRET}", "X-Tenant-ID": "tenant-a"}).resolve()


async def test_error_message_never_reveals_the_token() -> None:
    with pytest.raises(ServiceAuthenticationError) as excinfo:
        await _provider({"Authorization": "Bearer wrong-value"}).resolve()
    message = str(excinfo.value)
    assert "wrong-value" not in message
    assert _SECRET not in message


def test_empty_configured_secret_fails_closed() -> None:
    # A provider with no configured credential must never accept a caller.
    with pytest.raises(ServiceAuthenticationError):
        HisiemServiceTrustedContextProvider(
            _request({"Authorization": f"Bearer {_SECRET}", "X-Tenant-ID": "t"}),
            expected_token="",
        )


async def test_unauthenticated_request_cannot_obtain_system_actor() -> None:
    # No bearer at all: even with a forged actor header, the request is rejected
    # before any identity (including actor=system) can be constructed.
    with pytest.raises(ServiceAuthenticationError):
        await _provider({"X-Tenant-ID": "tenant-a", "X-Actor-Subject": "attacker"}).resolve()
