"""Header-based TrustedContextProvider (development/test adapter).

Reads tenant/actor from ``X-Tenant-ID`` / ``X-Actor-Subject`` request headers.

This is NOT a production authenticator: an ordinary client can forge these
headers. It exists so local development and integration tests can exercise the
API/application paths without standing up a real IdP. The Composition Root must
not select this adapter for a production deployment — see ``TrustProviderSettings``
in the container and the "no default trusted provider in production" invariant.

In a real deployment the edge (HISIEM) authenticates the caller and injects the
established principal; a production ``TrustedContextProvider`` will read that
authenticated principal, not untrusted headers.
"""

from __future__ import annotations

from starlette.requests import Request

from ...application.ports.trust import (
    TrustedContext,
    TrustedContextProvider,
    UntrustedRequestError,
)

TENANT_HEADER = "x-tenant-id"
ACTOR_SUBJECT_HEADER = "x-actor-subject"
ACTOR_DISPLAY_NAME_HEADER = "x-actor-display-name"


class HeaderTrustedContextProvider(TrustedContextProvider):
    """Dev/test provider resolving TrustedContext from request headers."""

    def __init__(self, request: Request) -> None:
        self._request = request

    async def resolve(self) -> TrustedContext:
        tenant = self._request.headers.get(TENANT_HEADER, "").strip()
        if not tenant:
            raise UntrustedRequestError("missing tenant identity")
        subject = self._request.headers.get(ACTOR_SUBJECT_HEADER, "").strip() or "system"
        display = self._request.headers.get(ACTOR_DISPLAY_NAME_HEADER, "").strip() or None
        return TrustedContext(
            tenant_id=tenant,
            actor_subject_id=subject,
            actor_display_name=display,
        )
