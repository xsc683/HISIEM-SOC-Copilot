"""Investigation queries — read-only facts (no domain mutation)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class InvestigationReadModel:
    """Read projection of an Investigation for the API/workspace.

    Built from the aggregate + persisted domain rows; never from ORM entities
    directly at the API boundary.
    """

    investigation_id: UUID
    tenant_id: str
    source_provider: str
    source_resource_type: str
    source_address_id: str
    status: str
    phase: str | None
    initiated_by: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    current_plan_revision: int = 0
    termination_reason: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status in {
            "CREATED",
            "RUNNING",
            "WAITING_APPROVAL",
            "EXECUTING_RESPONSE",
        }


@dataclass(frozen=True)
class GetInvestigation:
    tenant_id: str
    investigation_id: UUID


@dataclass(frozen=True)
class ListInvestigationSummaries:
    tenant_id: str
    status: str | None = None
    limit: int = 50
