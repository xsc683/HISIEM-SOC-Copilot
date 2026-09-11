"""HISIEM service-to-service TrustedContextProvider (production authenticator).

The P1 trust boundary is:

    Browser → HISIEM (authenticates the user) → HISIEM BFF → Copilot

The browser never authenticates to Copilot and never holds a Copilot credential.
HISIEM authenticates to Copilot with a server-only **service bearer credential**;
only AFTER that credential is verified does Copilot trust the tenant/actor HISIEM
asserts via ``X-Tenant-ID`` / ``X-Actor-Subject``.

Processing order is mandatory (never trust headers first, authenticate later):

    1. authenticate the service bearer credential
    2. reject on missing / malformed / invalid credential
    3. only then read the server-asserted ``X-Tenant-ID`` / ``X-Actor-Subject``
    4. require tenant AND actor to be present
    5. construct the ``TrustedContext``

Credential comparison is constant-time (``hmac.compare_digest``), and every
failure raises the SAME generic :class:`ServiceAuthenticationError` so a caller
cannot distinguish "almost right" / "wrong length" / "wrong prefix".
"""

from __future__ import annotations

import hmac

from starlette.requests import Request

from ...application.ports.trust import (
    ServiceAuthenticationError,
    TrustedContext,
    TrustedContextProvider,
)

TENANT_HEADER = "x-tenant-id"
ACTOR_SUBJECT_HEADER = "x-actor-subject"
ACTOR_DISPLAY_NAME_HEADER = "x-actor-display-name"
_AUTHORIZATION_HEADER = "authorization"
_BEARER_SCHEME = "bearer"


class HisiemServiceTrustedContextProvider(TrustedContextProvider):
    """Authenticates the HISIEM service caller, then trusts its tenant/actor."""

    def __init__(self, request: Request, *, expected_token: str) -> None:
        if not expected_token:
            # Fail closed: a provider with no credential must never accept a caller.
            raise ServiceAuthenticationError("service credential is not configured")
        self._request = request
        self._expected_token = expected_token

    async def resolve(self) -> TrustedContext:
        provided = self._bearer_credential()
        if not hmac.compare_digest(provided, self._expected_token):
            raise ServiceAuthenticationError("invalid service credential")

        tenant = self._request.headers.get(TENANT_HEADER, "").strip()
        if not tenant:
            raise ServiceAuthenticationError("missing tenant identity")
        subject = self._request.headers.get(ACTOR_SUBJECT_HEADER, "").strip()
        if not subject:
            raise ServiceAuthenticationError("missing actor identity")
        display = self._request.headers.get(ACTOR_DISPLAY_NAME_HEADER, "").strip() or None
        return TrustedContext(
            tenant_id=tenant,
            actor_subject_id=subject,
            actor_display_name=display,
        )

    def _bearer_credential(self) -> str:
        """Extract the token from exactly ``Authorization: Bearer <token>``.

        Scheme matching is case-insensitive per HTTP conventions; the token
        contents are compared exactly. Any other scheme (Basic/Token/Service/…),
        a missing header, or an ambiguous/multi-part value is rejected.
        """
        raw = self._request.headers.get(_AUTHORIZATION_HEADER)
        if not raw:
            raise ServiceAuthenticationError("missing service credential")
        parts = raw.split()
        if len(parts) != 2 or parts[0].lower() != _BEARER_SCHEME:
            raise ServiceAuthenticationError("malformed service credential")
        token = parts[1]
        if not token:
            raise ServiceAuthenticationError("missing service credential")
        return token
