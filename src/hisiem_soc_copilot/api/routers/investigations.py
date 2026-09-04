"""Investigation HTTP routes (transport only).

Routes never touch the repository, ORM, LangGraph node, or HISIEM client directly.
They resolve the authenticated context + application services, then dispatch an
Application Command/Query.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, status
from pydantic import UUID4

from ...application.commands.investigation import (
    CancelInvestigation,
    StartAlertInvestigation,
)
from ..dependencies import (
    CommandHandlerDep,
    ReadServiceDep,
    TrustedContextDep,
)
from ..schemas.common import (
    InvestigationResponse,
    StartInvestigationRequest,
)

router = APIRouter(prefix="/api/v1/investigations", tags=["investigations"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InvestigationResponse)
async def start_investigation(
    body: StartInvestigationRequest,
    context: TrustedContextDep,
    command_handler: CommandHandlerDep,
    read_service: ReadServiceDep,
) -> InvestigationResponse:
    """Start (or return the existing active) investigation for a HISIEM alert."""
    correlation_id = UUID(body.correlation_id) if body.correlation_id else None
    command = StartAlertInvestigation(
        tenant_id=context.tenant_id,
        source_alert_id=body.source_alert_id,
        initiated_by_subject=context.actor_subject_id,
        initiated_by_display_name=context.actor_display_name,
        correlation_id=correlation_id,
    )
    investigation = await command_handler.start_alert_investigation(command)
    rm = await read_service.get(
        tenant_id=context.tenant_id, investigation_id=investigation.id
    )
    return InvestigationResponse.from_read_model(rm)


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
