"""ORM model for ``investigation_result`` + result-finding link (immutable)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import CopilotBase


class InvestigationResultRow(CopilotBase):
    __tablename__ = "investigation_result"
    __table_args__ = (
        CheckConstraint(
            "verdict_disposition IN ('MALICIOUS','BENIGN','INCONCLUSIVE')",
            name="investigation_result_verdict_valid",
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="investigation_result_confidence_range",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )

    verdict_disposition: Mapped[str] = mapped_column(Text, nullable=False)
    verdict_summary: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)

    uncertainties: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    attack_mappings: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    response_recommendations: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list
    )

    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class InvestigationResultFindingRow(CopilotBase):
    __tablename__ = "investigation_result_finding"

    result_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_result.id", ondelete="RESTRICT"), primary_key=True
    )
    finding_id: Mapped[UUID] = mapped_column(
        ForeignKey("finding.id", ondelete="RESTRICT"), primary_key=True
    )
