"""ORM models for events/delivery + application/runtime + operational tables.

- domain_event: append-only event log (sequence IDENTITY handled in migration).
- outbox_message: transactional outbox (delivery).
- orchestration_binding: Domain Investigation ↔ LangGraph thread binding.
- command_receipt: command idempotency.
- tool_invocation: operational audit (never stores full tool results).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
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


class OrchestrationBindingRow(CopilotBase):
    """Domain Investigation ↔ LangGraph thread (they are distinct identities)."""

    __tablename__ = "orchestration_binding"

    investigation_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation.id", ondelete="RESTRICT"), primary_key=True
    )
    thread_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    graph_name: Mapped[str] = mapped_column(Text, nullable=False)
    graph_version: Mapped[str] = mapped_column(Text, nullable=False)
    state_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class CommandReceiptRow(CopilotBase):
    """Command idempotency record.

    The receipt's logical identity is ``(tenant_id, command_type, idempotency_key)``
    — an Idempotency-Key is scoped to a Tenant + Command Type, never a global key
    (persistence-schema.md §25). ``id`` is a surrogate technical primary key;
    ``command_id`` is additionally unique (one receipt per command); the scoped
    unique constraint enforces the idempotency space.
    """

    __tablename__ = "command_receipt"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "command_type",
            "idempotency_key",
            name="uq_command_receipt_tenant_command_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    command_id: Mapped[UUID] = mapped_column(nullable=False, unique=True)
    command_type: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    aggregate_id: Mapped[UUID | None] = mapped_column(nullable=True)
    result_ref_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_ref_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_result: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    # Bounded fingerprint of the business request (e.g. the source_alert_ref) so a
    # replayed Idempotency-Key bound to a DIFFERENT request can be rejected rather
    # than silently returning the original (wrong) logical result.
    request_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime] = mapped_column(nullable=False)


class DomainEventRow(CopilotBase):
    """Append-only domain event (not event sourcing; version/sequence tracked)."""

    __tablename__ = "domain_event"
    __table_args__ = (
        # sequence is GENERATED ALWAYS AS IDENTITY (managed in migration).
        UniqueConstraint("event_id", name="uq_domain_event_event_id"),
        Index("ix_domain_event_tenant_sequence", "tenant_id", "sequence"),
        Index(
            "ix_domain_event_aggregate_sequence",
            "aggregate_type",
            "aggregate_id",
            "sequence",
        ),
        Index("ix_domain_event_type_occurred", "event_type", "occurred_at"),
        Index("ix_domain_event_correlation", "correlation_id"),
    )

    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[UUID] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(nullable=False)
    aggregate_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[UUID | None] = mapped_column(nullable=True)
    causation_id: Mapped[UUID | None] = mapped_column(nullable=True)
    actor_subject_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)


class OutboxMessageRow(CopilotBase):
    __tablename__ = "outbox_message"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "destination", name="uq_outbox_event_destination"
        ),
        Index(
            "ix_outbox_ready",
            "status",
            "available_at",
            # PENDING (never claimed) + FAILED (retry backoff) + PROCESSING with an
            # expired lease (locked_at <= now - lease) are the claimable states.
            # DEAD_LETTER and a live PROCESSING lease are deliberately excluded.
            postgresql_where=("status IN ('PENDING','FAILED')"),
        ),
        Index(
            "ix_outbox_lease_reclaim",
            "status",
            "locked_at",
            postgresql_where="status = 'PROCESSING'",
        ),
        CheckConstraint(
            "status IN ('PENDING','PROCESSING','PUBLISHED','FAILED','DEAD_LETTER')",
            name="outbox_message_status_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    event_id: Mapped[UUID] = mapped_column(nullable=False)
    destination: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    locked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Fencing token: claim writes a fresh token; every settlement (published /
    # failed / dead-letter) and every lease renewal must present the SAME token,
    # so a worker whose lease was lost to a reclaim can never settle the row.
    lease_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class ToolInvocationRow(CopilotBase):
    __tablename__ = "tool_invocation"
    __table_args__ = (
        UniqueConstraint(
            "investigation_id", "idempotency_key", name="uq_tool_invocation_investigation_key"
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','SKIPPED')",
            name="tool_invocation_status_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    investigation_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation.id", ondelete="RESTRICT"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    tool_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    arguments: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    safe_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_metadata: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
