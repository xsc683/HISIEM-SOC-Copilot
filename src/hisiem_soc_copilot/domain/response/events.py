"""Response domain events (append-only, mapped to domain_event + outbox rows)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from ..shared.identifiers import new_uuid, utc_now
from .enums import PolicyDecision, ResponseProposalStatus


@dataclass(frozen=True)
class ResponseEvent:
    event_type: str
    aggregate_id: UUID
    aggregate_type: str = "response_proposal"
    version: int = 1
    tenant_id: str | None = None
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    actor_subject_id: str | None = None
    occurred_at: datetime = field(default_factory=utc_now)
    event_id: UUID = field(default_factory=new_uuid)
    payload: dict[str, Any] = field(default_factory=dict)


def response_proposal_created(
    aggregate_id: UUID, *, status: ResponseProposalStatus, tenant_id: str | None = None
) -> ResponseEvent:
    return ResponseEvent(
        event_type="response_proposal_created",
        aggregate_id=aggregate_id,
        tenant_id=tenant_id,
        payload={"status": status.value},
    )


def response_policy_decided(
    aggregate_id: UUID,
    *,
    decision: PolicyDecision,
    reason: str | None,
    tenant_id: str | None = None,
) -> ResponseEvent:
    return ResponseEvent(
        event_type="response_policy_decided",
        aggregate_id=aggregate_id,
        tenant_id=tenant_id,
        payload={"decision": decision.value, "reason": reason},
    )


def response_approval_requested(
    aggregate_id: UUID, *, request_id: UUID, tenant_id: str | None = None
) -> ResponseEvent:
    return ResponseEvent(
        event_type="response_approval_requested",
        aggregate_id=aggregate_id,
        tenant_id=tenant_id,
        payload={"request_id": str(request_id)},
    )


def response_approval_decided(
    aggregate_id: UUID,
    *,
    request_id: UUID,
    decision: str,
    actor_subject_id: str | None,
    tenant_id: str | None = None,
) -> ResponseEvent:
    return ResponseEvent(
        event_type="response_approval_decided",
        aggregate_id=aggregate_id,
        tenant_id=tenant_id,
        actor_subject_id=actor_subject_id,
        payload={"request_id": str(request_id), "decision": decision},
    )
