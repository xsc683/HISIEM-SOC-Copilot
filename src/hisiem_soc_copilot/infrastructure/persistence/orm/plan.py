"""ORM models: plan_revision, plan_step, plan_step_state.

PlanRevision/PlanStep are immutable definitions; PlanStepState is a mutable
progress projection. Status lives in plan_step_state, not the definition.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import CopilotBase


class PlanRevisionRow(CopilotBase):
    __tablename__ = "plan_revision"
    __table_args__ = (
        UniqueConstraint(
            "investigation_id",
            "revision",
            name="uq_plan_revision_investigation_revision",
        ),
        CheckConstraint(
            "generator_kind IN ('system','llm','human')",
            name="plan_revision_generator_kind_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    generator_kind: Mapped[str] = mapped_column(Text, nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class PlanStepRow(CopilotBase):
    __tablename__ = "plan_step"
    __table_args__ = (
        UniqueConstraint("plan_revision_id", "step_key", name="uq_plan_step_revision_key"),
        UniqueConstraint("plan_revision_id", "ordinal", name="uq_plan_step_revision_ordinal"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    plan_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("plan_revision.id", ondelete="RESTRICT"), nullable=False
    )
    step_key: Mapped[str] = mapped_column(Text, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)


class PlanStepStateRow(CopilotBase):
    __tablename__ = "plan_step_state"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','ACTIVE','COMPLETED','SKIPPED')",
            name="plan_step_state_status_valid",
        ),
    )

    plan_step_id: Mapped[UUID] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING")
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    lock_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
