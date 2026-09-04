"""Investigation domain events (append-only audit facts).

Aggregate methods return these when business changes happen. The application
layer maps them to ``domain_event`` + ``outbox_message`` rows in one transaction.
Events are immutable dataclasses built through factory helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from ..shared.identifiers import new_uuid, utc_now
from .enums import (
    InvestigationPhase,
    InvestigationStatus,
    TerminationReason,
)


@dataclass(frozen=True)
class InvestigationEvent:
    """A single append-only investigation business event."""

    event_type: str
    aggregate_id: UUID
    aggregate_type: str = "investigation"
    version: int = 1
    tenant_id: str | None = None
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    actor_subject_id: str | None = None
    occurred_at: datetime = field(default_factory=utc_now)
    event_id: UUID = field(default_factory=new_uuid)
    payload: dict[str, Any] = field(default_factory=dict)


def _ctx(
    *,
    tenant_id: str | None = None,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
    actor_subject_id: str | None = None,
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "actor_subject_id": actor_subject_id,
    }


def investigation_created(
    aggregate_id: UUID, **ctx: Any
) -> InvestigationEvent:
    return InvestigationEvent(
        event_type="investigation_created",
        aggregate_id=aggregate_id,
        payload={},
        **_ctx(**ctx),
    )


def investigation_started(
    aggregate_id: UUID, **ctx: Any
) -> InvestigationEvent:
    return InvestigationEvent(
        event_type="investigation_started",
        aggregate_id=aggregate_id,
        payload={},
        **_ctx(**ctx),
    )


def investigation_phase_changed(
    aggregate_id: UUID, phase: InvestigationPhase, **ctx: Any
) -> InvestigationEvent:
    return InvestigationEvent(
        event_type="investigation_phase_changed",
        aggregate_id=aggregate_id,
        payload={"phase": phase.value},
        **_ctx(**ctx),
    )


def investigation_terminated(
    aggregate_id: UUID,
    status: InvestigationStatus,
    reason: TerminationReason | str,
    **ctx: Any,
) -> InvestigationEvent:
    return InvestigationEvent(
        event_type="investigation_terminated",
        aggregate_id=aggregate_id,
        payload={"status": status.value, "reason": str(reason)},
        **_ctx(**ctx),
    )


# ---------------------------------------------------------------------------
# Investigation workflow events (application-commands...md §10 catalog).
# Factories are pure and append to the aggregate's ``_pending_events`` exactly
# like the lifecycle events above; their ``domain_event``/``outbox_message``
# persistence is a future outbox/dispatcher round.
# ---------------------------------------------------------------------------


def investigation_plan_revised(
    *,
    aggregate_id: UUID,
    plan_revision_id: UUID,
    revision: int,
    tenant_id: str | None = None,
    **ctx: Any,
) -> InvestigationEvent:
    return InvestigationEvent(
        event_type="investigation_plan_revised",
        aggregate_id=aggregate_id,
        payload={"plan_revision_id": str(plan_revision_id), "revision": revision},
        **_ctx(**{**ctx, "tenant_id": tenant_id}),
    )


def hypothesis_registered(
    *,
    aggregate_id: UUID,
    hypothesis_id: UUID,
    statement: str,
    tenant_id: str | None = None,
    **ctx: Any,
) -> InvestigationEvent:
    return InvestigationEvent(
        event_type="hypothesis_registered",
        aggregate_id=aggregate_id,
        payload={"hypothesis_id": str(hypothesis_id), "statement": statement},
        **_ctx(**{**ctx, "tenant_id": tenant_id}),
    )


def evidence_recorded(
    *,
    aggregate_id: UUID,
    evidence_ids: list[UUID],
    source_provider: str,
    source_operation: str,
    tenant_id: str | None = None,
    **ctx: Any,
) -> InvestigationEvent:
    return InvestigationEvent(
        event_type="evidence_recorded",
        aggregate_id=aggregate_id,
        payload={
            "evidence_ids": [str(e) for e in evidence_ids],
            "source_provider": source_provider,
            "source_operation": source_operation,
        },
        **_ctx(**{**ctx, "tenant_id": tenant_id}),
    )


def hypothesis_assessed(
    *,
    aggregate_id: UUID,
    hypothesis_id: UUID,
    assessment_id: UUID,
    revision: int,
    status: str,
    tenant_id: str | None = None,
    **ctx: Any,
) -> InvestigationEvent:
    return InvestigationEvent(
        event_type="hypothesis_assessed",
        aggregate_id=aggregate_id,
        payload={
            "hypothesis_id": str(hypothesis_id),
            "assessment_id": str(assessment_id),
            "revision": revision,
            "status": status,
        },
        **_ctx(**{**ctx, "tenant_id": tenant_id}),
    )


def finding_recorded(
    *,
    aggregate_id: UUID,
    finding_id: UUID,
    statement: str,
    evidence_ids: list[UUID],
    tenant_id: str | None = None,
    **ctx: Any,
) -> InvestigationEvent:
    return InvestigationEvent(
        event_type="finding_recorded",
        aggregate_id=aggregate_id,
        payload={
            "finding_id": str(finding_id),
            "statement": statement,
            "evidence_ids": [str(e) for e in evidence_ids],
        },
        **_ctx(**{**ctx, "tenant_id": tenant_id}),
    )


def investigation_result_finalized(
    *,
    aggregate_id: UUID,
    result_id: UUID,
    verdict_disposition: str,
    confidence: float,
    finding_ids: list[UUID],
    tenant_id: str | None = None,
    **ctx: Any,
) -> InvestigationEvent:
    return InvestigationEvent(
        event_type="investigation_result_finalized",
        aggregate_id=aggregate_id,
        payload={
            "result_id": str(result_id),
            "verdict_disposition": verdict_disposition,
            "confidence": confidence,
            "finding_ids": [str(f) for f in finding_ids],
        },
        **_ctx(**{**ctx, "tenant_id": tenant_id}),
    )


def investigation_completed(
    *,
    aggregate_id: UUID,
    reason: TerminationReason | str,
    tenant_id: str | None = None,
    **ctx: Any,
) -> InvestigationEvent:
    return InvestigationEvent(
        event_type="investigation_completed",
        aggregate_id=aggregate_id,
        payload={"reason": str(reason)},
        **_ctx(**{**ctx, "tenant_id": tenant_id}),
    )
