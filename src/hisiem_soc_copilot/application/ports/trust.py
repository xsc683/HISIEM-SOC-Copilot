"""Trusted request-context port.

The API and application depend on a ``TrustedContextProvider`` abstraction — never
on raw request headers or a body. A ``TrustedContext`` is only ever produced by a
provider that has authenticated the caller (currently a development/test-only
adapter in ``infrastructure``); it is never declared by an ordinary client request
(domain-model.md §44: tenant_id / initiated_by come from the authenticated
principal, not from the request body or the model).

Production identity/auth is intentionally NOT implemented in this round; the
provider seam exists so a real authenticator can be wired later without touching
the API/application layers.
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


class TrustedContextProvider(Protocol):
    """Resolves a TrustedContext for the current request, or raises.

    Implementations must obtain tenant/actor from an authenticated source only.
    The header-based implementation is a development/test adapter and must never
    be the default for a production deployment.
    """

    async def resolve(self) -> TrustedContext: ...
