"""Pydantic API schemas (boundary DTOs).

API depends on the application read models (allowed direction); it never depends
on infrastructure/ORM. Schemas are pure transport serializers.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from ...application.queries.investigation import InvestigationReadModel


class StartInvestigationRequest(BaseModel):
    """Start-investigation request body.

    Deliberately minimal and alert-scoped: no tenant, no actor, no alert content.
    Tenant and actor come from the authenticated request context; the alert id is
    the only client-declared reference, and Copilot hydrates authoritative data.
    """

    source_alert_id: str = Field(..., min_length=1, description="HISIEM alert id")
    correlation_id: str | None = None


class InvestigationResponse(BaseModel):
    investigation_id: str
    status: str
    phase: str | None = None
    tenant_id: str
    source_provider: str
    source_resource_type: str
    source_address_id: str
    initiated_by: str
    current_plan_revision: int = 0
    termination_reason: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def from_read_model(cls, rm: InvestigationReadModel) -> InvestigationResponse:
        return cls(
            investigation_id=str(rm.investigation_id),
            status=rm.status,
            phase=rm.phase,
            tenant_id=rm.tenant_id,
            source_provider=rm.source_provider,
            source_resource_type=rm.source_resource_type,
            source_address_id=rm.source_address_id,
            initiated_by=rm.initiated_by,
            current_plan_revision=rm.current_plan_revision,
            termination_reason=rm.termination_reason,
            created_at=rm.created_at,
            started_at=rm.started_at,
            finished_at=rm.finished_at,
        )


class HealthResponse(BaseModel):
    status: str
    service: str
    database: str = "not-checked"
