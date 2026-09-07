"""Tests for the HISIEM dev-auth bootstrap/login automation (E1-C0 §8, §21).

Offline: drives every branch of the auth flow against a FAKE HTTP transport —
no network, no real HISIEM. Verifies the happy path (normal login), the
first-run bootstrap + password rotation path, bad credentials, HISIEM
unavailable, and that the token is never written to any output the test can
observe (the flow only returns it in-memory).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from scripts.dev.hisiem_auth import (
    AuthenticationFailedError,
    BootstrapNotConfiguredError,
    HisiemUnavailableError,
    PasswordRotationFailedError,
    check_health,
    obtain_hisiem_token,
)

_ADMIN = "admin"
_DEV_PW = "DevPassword123!"  # >= 12 chars per HISIEM policy
_BOOTSTRAP_PW = "BootstrapOneTime99"


class FakeHttp:
    """Scripted httpx-like transport (implements the auth flow's client surface)."""

    def __init__(self) -> None:
        self.health_status = 200
        self.health_body: dict[str, Any] = {"status": "UP"}
        # (username, password) -> (http_status, body)
        self.login_handler: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
        self.login_sequence: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
        self.password_requests: list[dict[str, Any]] = []
        self.default_login: tuple[int, dict[str, Any]] = (401, {})
        self.reject_password = False
        self.close_called = False

    def get(self, url: str) -> httpx.Response:
        assert url == "/actuator/health"
        return httpx.Response(
            self.health_status, json=self.health_body, request=httpx.Request("GET", url)
        )

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        if url == "/api/auth/login":
            assert json is not None
            key = (json.get("username", ""), json.get("password", ""))
            if key in self.login_sequence:
                seq = self.login_sequence[key]
                status, body = seq.pop(0) if seq else self.default_login
                if not seq:
                    del self.login_sequence[key]
                return httpx.Response(
                    status, json=body, request=httpx.Request("POST", url)
                )
            status, body = self.login_handler.get(key, self.default_login)
            return httpx.Response(
                status, json=body, request=httpx.Request("POST", url)
            )
        if url == "/api/auth/password":
            self.password_requests.append(
                {"json": json, "auth": (headers or {}).get("Authorization")}
            )
            if self.reject_password:
                return httpx.Response(
                    400, json={}, request=httpx.Request("POST", url)
                )
            current = (json or {}).get("currentPassword")
            new = (json or {}).get("newPassword")
            if current in (_BOOTSTRAP_PW, _DEV_PW):
                if new != current and len(new or "") >= 12:
                    return httpx.Response(
                        200,
                        json={"username": _ADMIN, "passwordChangeRequired": False},
                        request=httpx.Request("POST", url),
                    )
                return httpx.Response(
                    400, json={}, request=httpx.Request("POST", url)
                )
            return httpx.Response(
                401, json={}, request=httpx.Request("POST", url)
            )
        raise AssertionError(f"unexpected POST {url}")


def _login_body(username: str, password: str, change_required: bool = False) -> dict[str, Any]:
    return {
        "token": f"tok-{username}-{password}",
        "username": username,
        "role": "admin",
        "expiresAt": "2026-09-08T00:00:00Z",
        "passwordChangeRequired": change_required,
    }


# -- health ----------------------------------------------------------------


def test_health_up_when_actuator_reports_up() -> None:
    fake = FakeHttp()
    assert check_health(client=fake) is True


def test_health_down_when_http_error() -> None:
    fake = FakeHttp()
    fake.health_status = 503
    assert check_health(client=fake) is False


# -- happy path: normal login, no rotation ---------------------------------


def test_normal_login_returns_token_without_rotation() -> None:
    fake = FakeHttp()
    fake.login_handler[(_ADMIN, _DEV_PW)] = (200, _login_body(_ADMIN, _DEV_PW))
    result = obtain_hisiem_token(
        username=_ADMIN, dev_password=_DEV_PW, client=fake
    )
    assert result.token == f"tok-{_ADMIN}-{_DEV_PW}"
    assert result.password_rotated is False
    assert fake.password_requests == []  # no rotation on the happy path


# -- health gating ---------------------------------------------------------


def test_unavailable_when_hisiem_not_up() -> None:
    fake = FakeHttp()
    fake.health_status = 503
    with pytest.raises(HisiemUnavailableError):
        obtain_hisiem_token(username=_ADMIN, dev_password=_DEV_PW, client=fake)


def test_health_http_error_surfaces_unavailable() -> None:
    class _ConnRefused(FakeHttp):
        def get(self, url: str) -> httpx.Response:
            raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    with pytest.raises(HisiemUnavailableError):
        obtain_hisiem_token(
            username=_ADMIN, dev_password=_DEV_PW, client=_ConnRefused()
        )


# -- first-run bootstrap + rotation ----------------------------------------


def test_bootstrap_login_then_official_rotation_then_second_login() -> None:
    fake = FakeHttp()
    # Dev password not yet set (first run): dev login fails on the first attempt,
    # then succeeds AFTER rotation.
    fake.login_sequence[(_ADMIN, _DEV_PW)] = [
        (401, {}),
        (200, _login_body(_ADMIN, _DEV_PW)),
    ]
    # Bootstrap password logs in with passwordChangeRequired=true.
    fake.login_handler[(_ADMIN, _BOOTSTRAP_PW)] = (
        200,
        _login_body(_ADMIN, _BOOTSTRAP_PW, change_required=True),
    )

    result = obtain_hisiem_token(
        username=_ADMIN,
        dev_password=_DEV_PW,
        bootstrap_password=_BOOTSTRAP_PW,
        client=fake,
    )
    assert result.password_rotated is True
    assert result.token == f"tok-{_ADMIN}-{_DEV_PW}"
    # Exactly one official rotation call was made with bootstrap -> dev.
    assert len(fake.password_requests) == 1
    req = fake.password_requests[0]
    assert req["json"] == {
        "currentPassword": _BOOTSTRAP_PW,
        "newPassword": _DEV_PW,
    }
    assert req["auth"] == f"Bearer tok-{_ADMIN}-{_BOOTSTRAP_PW}"


def test_dev_password_already_set_but_rotation_required_fails_closed() -> None:
    # Edge: the dev password is correct but passwordChangeRequired is still true
    # (e.g. a fresh legacy import). HISIEM policy forbids rotating to the SAME
    # password ("新密码不能与当前密码相同"), so with no distinct target the module
    # must FAIL CLOSED (typed error), not silently return a still-required token.
    fake = FakeHttp()
    fake.login_handler[(_ADMIN, _DEV_PW)] = (
        200,
        _login_body(_ADMIN, _DEV_PW, change_required=True),
    )
    with pytest.raises(PasswordRotationFailedError):
        obtain_hisiem_token(username=_ADMIN, dev_password=_DEV_PW, client=fake)


# -- failure paths ---------------------------------------------------------


def test_dev_login_fails_and_no_bootstrap_configured() -> None:
    fake = FakeHttp()
    fake.default_login = (401, {})
    with pytest.raises(BootstrapNotConfiguredError):
        obtain_hisiem_token(username=_ADMIN, dev_password=_DEV_PW, client=fake)


def test_bad_credentials_401_typed() -> None:
    fake = FakeHttp()
    fake.default_login = (401, {})
    with pytest.raises(AuthenticationFailedError):
        obtain_hisiem_token(
            username=_ADMIN,
            dev_password=_DEV_PW,
            bootstrap_password="wrong-bootstrap-pw",
            client=fake,
        )


def test_rotation_rejected_surfaces_typed_error() -> None:
    fake = FakeHttp()
    # Dev password not yet set; bootstrap password works but requires rotation.
    fake.login_sequence[(_ADMIN, _DEV_PW)] = [(401, {})]
    fake.login_handler[(_ADMIN, _BOOTSTRAP_PW)] = (
        200,
        _login_body(_ADMIN, _BOOTSTRAP_PW, change_required=True),
    )
    # Force the official rotation endpoint to reject (simulate policy rejection).
    fake.reject_password = True

    with pytest.raises(PasswordRotationFailedError):
        obtain_hisiem_token(
            username=_ADMIN,
            dev_password=_DEV_PW,
            bootstrap_password=_BOOTSTRAP_PW,
            client=fake,
        )


# -- token hygiene ---------------------------------------------------------


def test_token_never_appears_in_any_sent_payload() -> None:
    """The obtained token must never be echoed back to HISIEM or captured in a
    request body (only returned in-memory to the caller)."""
    fake = FakeHttp()
    fake.login_handler[(_ADMIN, _DEV_PW)] = (200, _login_body(_ADMIN, _DEV_PW))
    result = obtain_hisiem_token(username=_ADMIN, dev_password=_DEV_PW, client=fake)
    token = result.token
    # The token only exists in the result object; the fake recorded no request that
    # contains it as a body/json value.
    for req in fake.password_requests:
        assert token not in str(req["json"])
    # Nothing was printed: capture stdout to prove the flow is silent.
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        obtain_hisiem_token(username=_ADMIN, dev_password=_DEV_PW, client=fake)
    assert token not in buf.getvalue()
