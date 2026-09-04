"""ORM models for response_proposal + targets/evidence links + approval + execution ref.

Proposal content (action_key, parameters, targets) is immutable after creation;
approval binds the exact content_revision + content_hash.
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import CopilotBase


class ResponseProposalRow(CopilotBase):
    __tablename__ = "response_proposal"
    __table_args__ = (
        UniqueConstraint(
            "investigation_id", name="uq_response_proposal_investigation"
        ),
        UniqueConstraint("result_id", name="uq_response_proposal_result"),
        CheckConstraint(
            "status IN ('CREATED','DENIED','WAITING_APPROVAL','APPROVED',"
            "'REJECTED','SUBMITTED')",
            name="response_proposal_status_valid",
        ),
        CheckConstraint(
            "policy_decision IS NULL OR policy_decision IN ('DENY','REQUIRE_APPROVAL')",
            name="response_proposal_policy_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation.id", ondelete="RESTRICT"), nullable=False
    )
    result_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_result.id", ondelete="RESTRICT"), nullable=False
    )

    status: Mapped[str] = mapped_column(Text, nullable=False, default="CREATED")
    action_key: Mapped[str] = mapped_column(Text, nullable=False)
    parameters: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    policy_decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    content_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    lock_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class ResponseProposalTargetRow(CopilotBase):
    __tablename__ = "response_proposal_target"
    __table_args__ = (
        UniqueConstraint(
            "proposal_id",
            "provider",
            "resource_type",
            "address_id",
            name="uq_response_proposal_target_ref",
        ),
    )

    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("response_proposal.id", ondelete="RESTRICT"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    address_id: Mapped[str] = mapped_column(Text, nullable=False)
    business_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class ResponseProposalEvidenceRow(CopilotBase):
    __tablename__ = "response_proposal_evidence"

    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("response_proposal.id", ondelete="RESTRICT"), primary_key=True
    )
    evidence_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence.id", ondelete="RESTRICT"), primary_key=True
    )


class ApprovalRequestRow(CopilotBase):
    __tablename__ = "approval_request"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("response_proposal.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    proposal_content_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    proposal_content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    requested_reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(nullable=False)


class ApprovalDecisionRow(CopilotBase):
    __tablename__ = "approval_decision"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('APPROVE','REJECT')", name="approval_decision_decision_valid"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    approval_request_id: Mapped[UUID] = mapped_column(
        ForeignKey("approval_request.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    actor_subject_id: Mapped[str] = mapped_column(Text, nullable=False)
    actor_tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    actor_display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(nullable=False)


class ResponseExecutionRefRow(CopilotBase):
    __tablename__ = "response_execution_ref"
    __table_args__ = (
        UniqueConstraint("provider", "execution_id", name="uq_response_execution_provider_id"),
        UniqueConstraint("submission_key", name="uq_response_execution_submission_key"),
    )

    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("response_proposal.id", ondelete="RESTRICT"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    execution_id: Mapped[str] = mapped_column(Text, nullable=False)
    submission_key: Mapped[str] = mapped_column(Text, nullable=False)
    last_observed_status: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(nullable=False)
