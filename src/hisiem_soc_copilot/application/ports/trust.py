"""Trusted request-context port.

The API and application depend on a ``TrustedContextProvider`` abstraction — never
on raw request headers or a body. A ``TrustedContext`` is only ever produced by a
provider that has authenticated the caller; it is never declared by an ordinary
client request (domain-model.md §44: tenant_id / initiated_by come from the
authenticated principal, not from the request body or the model).

Trust boundary (P1): the browser authenticates to HISIEM, never to Copilot.
HISIEM authenticates to Copilot with a server-only service credential; ONLY after
that service authentication succeeds may Copilot trust the ``X-Tenant-ID`` /
``X-Actor-Subject`` context HISIEM supplies (see
``infrastructure.auth.hisiem_service_provider.HisiemServiceTrustedContextProvider``).
``HeaderTrustedContextProvider`` reads those headers with no authentication and is
a development/test adapter only — it must never be selected for a production or
integrated runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ...domain.shared.errors import DomainError


@dataclass(frozen=True)
class TrustedContext:
    """Authenticated caller context used to bind domain commands/queries."""

    tenant_id: str
    actor_subject_id: str
    actor_display_name: str | None = None
    # role/authorization snapshots belong here when a real authenticator exists;
    # the application never derives authority from client-declared claims.
    role_snapshot: str | None = None


class UntrustedRequestError(DomainError):
    """Raised when a request cannot be authenticated into a TrustedContext."""

    code = "UNTRUSTED_REQUEST"


class ServiceAuthenticationError(UntrustedRequestError):
    """Raised when the HISIEM service caller cannot be authenticated.

    Distinct from a generic untrusted request: this is a service-to-service
    authentication failure (missing/invalid service credential, or a missing
    server-asserted tenant/actor) and maps to HTTP 401. The message is generic —
    it never reveals whether a credential was close, wrong-length, or which part
    of the identity was missing.
    """

    code = "SERVICE_AUTHENTICATION_FAILED"


class TrustedContextProvider(Protocol):
    """Resolves a TrustedContext for the current request, or raises.

    Implementations must obtain tenant/actor from an authenticated source only.
    The header-based implementation is a development/test adapter and must never
    be the default for a production deployment.
    """

    async def resolve(self) -> TrustedContext: ...
