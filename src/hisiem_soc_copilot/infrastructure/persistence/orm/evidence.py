"""ORM models for evidence, hypothesis, hypothesis_assessment + links, finding.

Evidence/Finding/Assessment are immutable append-only ledgers. Only
``hypothesis`` (current_status/assessment_revision) is mutable.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import CopilotBase


class EvidenceRow(CopilotBase):
    __tablename__ = "evidence"
    __table_args__ = (
        # Deduplication per investigation + dedup_key.
        UniqueConstraint(
            "investigation_id", "dedup_key", name="uq_evidence_investigation_dedup"
        ),
        CheckConstraint(
            "source_type IN ('HISIEM_ALERT','HISIEM_EVENT','HISIEM_LOG_SEARCH',"
            "'HISIEM_ENTITY','THREAT_INTEL','KNOWLEDGE','SYSTEM')",
            name="evidence_source_type_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation.id", ondelete="RESTRICT"), nullable=False
    )

    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_provider: Mapped[str] = mapped_column(Text, nullable=False)
    source_operation: Mapped[str] = mapped_column(Text, nullable=False)

    source_resource_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_resource_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_resource_address_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_resource_business_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_tool_invocation_id: Mapped[UUID | None] = mapped_column(nullable=True)

    observed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    collected_at: Mapped[datetime] = mapped_column(nullable=False)

    observation: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_reference: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    entity_refs: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    provenance_authority: Mapped[str] = mapped_column(
        Text, nullable=False, default="EXTERNAL_EVIDENCE"
    )

    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False)


class HypothesisRow(CopilotBase):
    __tablename__ = "hypothesis"
    __table_args__ = (
        Index(
            "ix_hypothesis_investigation_status",
            "investigation_id",
            "current_status",
        ),
        CheckConstraint(
            "current_status IN ('OPEN','SUPPORTED','CONTRADICTED','UNRESOLVED')",
            name="hypothesis_status_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation.id", ondelete="RESTRICT"), nullable=False
    )
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    current_status: Mapped[str] = mapped_column(Text, nullable=False, default="OPEN")
    assessment_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class HypothesisAssessmentRow(CopilotBase):
    __tablename__ = "hypothesis_assessment"
    __table_args__ = (
        UniqueConstraint(
            "hypothesis_id", "revision", name="uq_hypothesis_assessment_hypothesis_revision"
        ),
        CheckConstraint(
            "status IN ('OPEN','SUPPORTED','CONTRADICTED','UNRESOLVED')",
            name="hypothesis_assessment_status_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation.id", ondelete="RESTRICT"), nullable=False
    )
    hypothesis_id: Mapped[UUID] = mapped_column(
        ForeignKey("hypothesis.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reason_summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class HypothesisAssessmentEvidenceRow(CopilotBase):
    __tablename__ = "hypothesis_assessment_evidence"
    __table_args__ = (
        CheckConstraint(
            "relation IN ('SUPPORTS','CONTRADICTS','CONTEXT')",
            name="hae_relation_valid",
        ),
    )

    assessment_id: Mapped[UUID] = mapped_column(
        ForeignKey("hypothesis_assessment.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    evidence_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence.id", ondelete="RESTRICT"), primary_key=True
    )
    relation: Mapped[str] = mapped_column(Text, primary_key=True)


class FindingRow(CopilotBase):
    __tablename__ = "finding"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation.id", ondelete="RESTRICT"), nullable=False
    )
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class FindingEvidenceRow(CopilotBase):
    __tablename__ = "finding_evidence"

    finding_id: Mapped[UUID] = mapped_column(
        ForeignKey("finding.id", ondelete="RESTRICT"), primary_key=True
    )
    evidence_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence.id", ondelete="RESTRICT"), primary_key=True
    )
