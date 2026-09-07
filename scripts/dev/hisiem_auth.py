"""HISIEM dev-only auth bootstrap/login automation (E1-C0).

Automates the OFFICIAL HISIEM HTTP auth flow so a local Agent-Evaluation run
never needs a human to log in, copy a Bearer token, or manually rotate the
bootstrap password. It performs NO database mutation, NO password-hash edit,
and never disables or bypasses HISIEM auth/RBAC.

Flow (all over HISIEM's public API):

    health check  GET /actuator/health            (expect status UP)
        -> normal login  POST /api/auth/login {username, dev_password}
              success -> return fresh token
              fail    -> (first-run/bootstrap condition)
        -> bootstrap login POST /api/auth/login {username, bootstrap_password}
              if passwordChangeRequired -> POST /api/auth/password {current,
                                           new=dev_password} (official rotation)
        -> normal login again -> return fresh token

The returned token is RUNTIME-ONLY. This module never writes it to disk, never
prints it, and callers must inject it into the child Copilot process
environment (never into git / logs / manifests / agent state).

The HTTP transport is injectable so tests can drive every branch against a fake
client without any network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

# Default HISIEM control surface (per local-dev port contract).
DEFAULT_BASE_URL = "http://127.0.0.1:8080"

# Readiness: the ONLY authoritative endpoint (not "/", not "/api/health").
HEALTH_PATH = "/actuator/health"
LOGIN_PATH = "/api/auth/login"
PASSWORD_PATH = "/api/auth/password"
ME_PATH = "/api/auth/me"


class HisiemAuthError(Exception):
    """Base error for HISIEM dev-auth failures (typed, machine-checkable)."""


class HisiemUnavailableError(HisiemAuthError):
    """HISIEM control surface is not reachable / not UP."""


class AuthenticationFailedError(HisiemAuthError):
    """Login failed (bad credentials or account not active)."""


class BootstrapNotConfiguredError(HisiemAuthError):
    """Bootstrap branch required but no bootstrap password was provided."""


class PasswordRotationFailedError(HisiemAuthError):
    """The official password-rotation call was rejected."""


@dataclass(frozen=True)
class HisiemAuthResult:
    """Outcome of the auth bootstrap/login flow.

    ``token`` is runtime-only; never persist or log it.
    ``password_rotated`` is True when this invocation performed the one-time
    bootstrap rotation (normal, already-provisioned environments stay False).
    """

    token: str
    password_rotated: bool = False


class HttpClient(Protocol):
    """Minimal httpx-like surface used by the flow (injectable for tests)."""

    def get(self, url: str) -> httpx.Response: ...

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response: ...


def _http_client(base_url: str, timeout: float) -> HttpClient:
    return httpx.Client(base_url=base_url, timeout=timeout)


def _is_up(response: httpx.Response) -> bool:
    try:
        return response.status_code == 200 and response.json().get("status") == "UP"
    except ValueError:
        return False


def _login(client: HttpClient, username: str, password: str) -> dict[str, Any]:
    """POST /api/auth/login; returns the parsed body on 200, else raises typed."""
    response = client.post(LOGIN_PATH, json={"username": username, "password": password})
    if response.status_code == 200:
        body = response.json()
        # Defensive: a 200 must actually carry a token to be useful.
        if isinstance(body, dict) and body.get("token"):
            return body
    if response.status_code in (401, 403):
        raise AuthenticationFailedError(
            f"HISIEM login rejected for '{username}' (HTTP {response.status_code})"
        )
    raise HisiemAuthError(
        f"HISIEM login returned unexpected HTTP {response.status_code}"
    )


def _rotate_password(
    client: HttpClient, token: str, current_password: str, new_password: str
) -> None:
    """POST /api/auth/password using the official rotation endpoint."""
    response = client.post(
        PASSWORD_PATH,
        json={"currentPassword": current_password, "newPassword": new_password},
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code == 200:
        return
    if response.status_code in (401, 403):
        raise PasswordRotationFailedError(
            f"HISIEM password rotation unauthorized (HTTP {response.status_code})"
        )
    if response.status_code == 428:
        raise PasswordRotationFailedError(
            "HISIEM password rotation refused (428); password policy may reject "
            "the chosen dev password"
        )
    raise PasswordRotationFailedError(
        f"HISIEM password rotation returned unexpected HTTP {response.status_code}"
    )


def obtain_hisiem_token(
    *,
    username: str,
    dev_password: str,
    bootstrap_password: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    client: HttpClient | None = None,
) -> HisiemAuthResult:
    """Run the full bootstrap/login flow and return a fresh runtime Bearer token.

    Args:
        username: HISIEM dev account (e.g. ``admin``).
        dev_password: the dev account's intended password (HISIEM_DEV_PASSWORD).
        bootstrap_password: one-time bootstrap password (HISIEM_BOOTSTRAP_PASSWORD);
            only required when the account is still in the first-run/required-rotation
            state.
        base_url: HISIEM control surface base URL.
        client: injectable HTTP transport (tests supply a fake).

    Returns:
        HisiemAuthResult with the fresh token.

    Raises:
        HisiemUnavailableError: HISIEM not reachable / not UP.
        AuthenticationFailedError: dev login AND bootstrap login both failed.
        BootstrapNotConfiguredError: bootstrap login required but no bootstrap
            password provided.
        PasswordRotationFailedError: the official rotation was rejected.
    """
    own_client = client is None
    http = client if client is not None else _http_client(base_url, timeout=10.0)

    def _health() -> None:
        try:
            response = http.get(HEALTH_PATH)
        except httpx.HTTPError as exc:
            raise HisiemUnavailableError(
                f"HISIEM control surface unreachable at {base_url}: "
                f"{exc.__class__.__name__}"
            ) from exc
        if not _is_up(response):
            raise HisiemUnavailableError(
                f"HISIEM not UP: GET {HEALTH_PATH} -> HTTP {response.status_code}"
            )

    try:
        _health()

        # 1) Normal login with the dev password (already-provisioned envs).
        try:
            body = _login(http, username, dev_password)
            if not body.get("passwordChangeRequired"):
                return HisiemAuthResult(token=body["token"], password_rotated=False)
            # The dev password is correct but rotation is still required.
            current_password = dev_password
        except AuthenticationFailedError:
            # 2) First-run/bootstrap condition: fall back to bootstrap password.
            if not bootstrap_password:
                raise BootstrapNotConfiguredError(
                    "HISIEM login with the dev password failed and no "
                    "HISIEM_BOOTSTRAP_PASSWORD is configured; a first-run bootstrap "
                    "requires it"
                ) from None
            body = _login(http, username, bootstrap_password)
            current_password = bootstrap_password

        # 3) Official rotation: current -> dev password (only when required).
        _rotate_password(
            http, body["token"], current_password=current_password, new_password=dev_password
        )

        # 4) Login again with the dev password to obtain a clean token.
        final = _login(http, username, dev_password)
        return HisiemAuthResult(token=final["token"], password_rotated=True)
    finally:
        if own_client:
            http.close()  # type: ignore[attr-defined]


def check_health(base_url: str = DEFAULT_BASE_URL, client: HttpClient | None = None) -> bool:
    """Return True iff GET /actuator/health reports status UP (never raises)."""
    own_client = client is None
    http = client if client is not None else _http_client(base_url, timeout=10.0)
    try:
        try:
            response = http.get(HEALTH_PATH)
        except httpx.HTTPError:
            return False
        return _is_up(response)
    finally:
        if own_client:
            http.close()  # type: ignore[attr-defined]


if __name__ == "__main__":  # pragma: no cover - thin CLI for up-agent.ps1
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(description="HISIEM dev auth bootstrap")
    parser.add_argument("--username", default=os.environ.get("HISIEM_DEV_USERNAME", "admin"))
    parser.add_argument("--base-url", default=os.environ.get("HISIEM_BASE_URL", DEFAULT_BASE_URL))
    args = parser.parse_args()

    dev_password = os.environ.get("HISIEM_DEV_PASSWORD")
    bootstrap_password = os.environ.get("HISIEM_BOOTSTRAP_PASSWORD")
    if not dev_password:
        print("error: HISIEM_DEV_PASSWORD is not set", file=sys.stderr)
        sys.exit(2)

    try:
        result = obtain_hisiem_token(
            username=args.username,
            dev_password=dev_password,
            bootstrap_password=bootstrap_password,
            base_url=args.base_url,
        )
    except HisiemAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Token is written to stdout for the parent launcher to capture; the launcher
    # must inject it into the child env and must not echo it to logs.
    print(result.token)
