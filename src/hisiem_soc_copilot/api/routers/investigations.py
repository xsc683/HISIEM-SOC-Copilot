"""Investigation HTTP routes (transport only).

Routes never touch the repository, ORM, LangGraph node, or HISIEM client directly.
They resolve the authenticated context + application services, then dispatch an
Application Command/Query.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Path, Query, status
from pydantic import UUID4

from ...application.commands.investigation import (
    CancelInvestigation,
    StartAlertInvestigation,
)
from ...domain.investigation.value_objects import ExternalResourceRef
from ..dependencies import (
    CommandHandlerDep,
    ReadServiceDep,
    TrustedContextDep,
    WorkspaceServiceDep,
)
from ..schemas.common import (
    InvestigationResponse,
    StartInvestigationRequest,
)
from ..schemas.workspace import (
    AlertInvestigationLookupResponse,
    InvestigationWorkspaceResponse,
)

router = APIRouter(prefix="/api/v1/investigations", tags=["investigations"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InvestigationResponse)
async def start_investigation(
    body: StartInvestigationRequest,
    context: TrustedContextDep,
    command_handler: CommandHandlerDep,
    read_service: ReadServiceDep,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> InvestigationResponse:
    """Start (or return the existing active) investigation for a HISIEM alert.

    The request body carries only the alert ExternalResourceRef; tenant/actor come
    from the trusted context. ``Idempotency-Key`` is propagated as the command's
    idempotency metadata so a HISIEM network retry reuses the same logical result.
    """
    ref = body.source_alert_ref
    correlation_id = UUID(body.correlation_id) if body.correlation_id else None
    command = StartAlertInvestigation(
        tenant_id=context.tenant_id,
        source_alert_ref=ExternalResourceRef(
            provider=ref.provider,
            resource_type=ref.resource_type,
            address_id=ref.address_id,
            business_id=ref.business_id,
        ),
        initiated_by_subject=context.actor_subject_id,
        initiated_by_display_name=context.actor_display_name,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
    )
    investigation = await command_handler.start_alert_investigation(command)
    rm = await read_service.get(
        tenant_id=context.tenant_id, investigation_id=investigation.id
    )
    return InvestigationResponse.from_read_model(rm)


@router.get("/lookup", response_model=AlertInvestigationLookupResponse)
async def lookup_alert_investigation(
    context: TrustedContextDep,
    workspace_service: WorkspaceServiceDep,
    provider: Annotated[str, Query(min_length=1)],
    resource_type: Annotated[str, Query(min_length=1)],
    address_id: Annotated[str, Query(min_length=1)],
) -> AlertInvestigationLookupResponse:
    """Server-to-server Alert re-entry lookup (docs §22).

    Returns the at-most-one ACTIVE Investigation for the source alert plus the most
    recent Investigation of any status, so HISIEM can render the correct Alert
    action (start / continue / view). Tenant is derived from the trusted context —
    never declared by the caller — so a foreign alert can never be resolved.
    """
    lookup = await workspace_service.lookup_alert_investigation(
        tenant_id=context.tenant_id,
        provider=provider,
        resource_type=resource_type,
        address_id=address_id,
    )
    return AlertInvestigationLookupResponse.from_read_model(lookup)


@router.get("/{investigation_id}", response_model=InvestigationResponse)
async def get_investigation(
    investigation_id: Annotated[UUID4, Path()],
    context: TrustedContextDep,
    read_service: ReadServiceDep,
) -> InvestigationResponse:
    """Read an investigation overview (tenant-scoped)."""
    rm = await read_service.get(
        tenant_id=context.tenant_id, investigation_id=UUID(str(investigation_id))
    )
    return InvestigationResponse.from_read_model(rm)


@router.get(
    "/{investigation_id}/workspace", response_model=InvestigationWorkspaceResponse
)
async def get_investigation_workspace(
    investigation_id: Annotated[UUID4, Path()],
    context: TrustedContextDep,
    workspace_service: WorkspaceServiceDep,
) -> InvestigationWorkspaceResponse:
    """Compose the Analyst Workspace projection for one investigation (docs §7/§8).

    Read-only: it mutates no domain state and runs no Agent/Graph/model/tool. The
    whole projection is tenant-scoped; a foreign or unknown investigation is a 404.
    """
    workspace = await workspace_service.get_workspace(
        tenant_id=context.tenant_id,
        investigation_id=UUID(str(investigation_id)),
    )
    return InvestigationWorkspaceResponse.from_read_model(workspace)


@router.post("/{investigation_id}/cancel", response_model=InvestigationResponse)
async def cancel_investigation(
    investigation_id: Annotated[UUID4, Path()],
    context: TrustedContextDep,
    command_handler: CommandHandlerDep,
    read_service: ReadServiceDep,
) -> InvestigationResponse:
    """Cancel an investigation that is still in a cancellable status."""
    command = CancelInvestigation(
        tenant_id=context.tenant_id,
        investigation_id=UUID(str(investigation_id)),
        initiated_by_subject=context.actor_subject_id,
    )
    investigation = await command_handler.cancel_investigation(command)
    rm = await read_service.get(
        tenant_id=context.tenant_id, investigation_id=investigation.id
    )
    return InvestigationResponse.from_read_model(rm)
