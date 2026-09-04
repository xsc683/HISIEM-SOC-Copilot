"""ORM model for the ``investigation`` table.

Persistence concern only. The Domain aggregate (plain dataclass) is the business
model; mappers translate between them. No other layer imports these models
(architecture test enforces this).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import CopilotBase

ACTIVE_INVESTIGATION_STATUSES = (
    "CREATED",
    "RUNNING",
    "WAITING_APPROVAL",
    "EXECUTING_RESPONSE",
)
_ALL_INVESTIGATION_STATUSES = ACTIVE_INVESTIGATION_STATUSES + (
    "COMPLETED",
    "FAILED",
    "CANCELLED",
)


class InvestigationRow(CopilotBase):
    __tablename__ = "investigation"
    __table_args__ = (
        # One Active Investigation per Tenant + Alert (partial unique index).
        Index(
            "uq_investigation_active_alert",
            "tenant_id",
            "source_provider",
            "source_resource_type",
            "source_address_id",
            unique=True,
            postgresql_where=text(
                "status IN ('CREATED','RUNNING','WAITING_APPROVAL','EXECUTING_RESPONSE')"
            ),
        ),
        Index(
            "ix_investigation_tenant_status_created",
            "tenant_id",
            "status",
            "created_at",
        ),
        Index("ix_investigation_tenant_source", "tenant_id", "source_address_id"),
        CheckConstraint(
            f"status IN ({','.join(repr(s) for s in _ALL_INVESTIGATION_STATUSES)})",
            name="investigation_status_valid",
        ),
        CheckConstraint(
            "phase IS NULL OR phase IN ('HYDRATING','PLANNING','INVESTIGATING',"
            "'VERIFYING','FINALIZING')",
            name="investigation_phase_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_provider: Mapped[str] = mapped_column(Text, nullable=False)
    source_resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_address_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_business_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    initiated_by_subject: Mapped[str] = mapped_column(Text, nullable=False)
    initiated_by_display_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(Text, nullable=False, default="CREATED")
    phase: Mapped[str | None] = mapped_column(Text, nullable=True)

    current_plan_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    budget_limits: Mapped[dict[str, int]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    termination_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    lock_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_id: Mapped[UUID | None] = mapped_column(nullable=True)
    response_proposal_id: Mapped[UUID | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(nullable=True)
