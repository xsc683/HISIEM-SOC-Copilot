"""FastAPI shared dependencies.

1. ``trusted_context`` — resolved through the container's configured
   ``TrustedContextProvider`` (an authenticated source), never from an untrusted
   client body. The provider itself is selected by configuration; the header
   adapter is dev/test only and must not be the production default.
2. Service accessors — the application services built by the Composition Root are
   attached to ``app.state.container`` during lifespan; routers depend on these
   accessors, never importing infrastructure.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from ..application.handlers.investigation import InvestigationCommandHandler
from ..application.ports.trust import TrustedContext, TrustedContextProvider
from ..application.services.investigation_service import InvestigationReadService
from ..bootstrap.container import Container


def trusted_context_provider(request: Request) -> TrustedContextProvider:
    """Resolve the container's configured TrustedContextProvider for this request.

    Raises when no provider is configured (the ``none`` default → fail closed).
    """
    container = _container(request)
    provider = container.trusted_context_provider(request)
    if provider is None:
        from ..application.ports.trust import UntrustedRequestError

        raise UntrustedRequestError("no trusted-context provider is configured")
    return provider


async def trusted_context(
    provider: Annotated[TrustedContextProvider, Depends(trusted_context_provider)],
) -> TrustedContext:
    return await provider.resolve()


TrustedContextProviderDep = Annotated[
    TrustedContextProvider, Depends(trusted_context_provider)
]
TrustedContextDep = Annotated[TrustedContext, Depends(trusted_context)]


def _container(request: Request) -> Container:
    container = getattr(request.app.state, "container", None)
    if container is None or not isinstance(container, Container):
        raise RuntimeError("application container is not initialised")
    return container


def command_handler(request: Request) -> InvestigationCommandHandler:
    return _container(request).investigation_command_handler()


def read_service(request: Request) -> InvestigationReadService:
    return _container(request).investigation_read_service()


# Depends() markers: FastAPI must resolve these as dependencies, not treat the
# service classes as request/response fields.
CommandHandlerDep = Annotated[InvestigationCommandHandler, Depends(command_handler)]
ReadServiceDep = Annotated[InvestigationReadService, Depends(read_service)]
