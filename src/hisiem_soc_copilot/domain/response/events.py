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
    aggregate_id: UUID,
    *,
    status: ResponseProposalStatus,
    actor_subject_id: str,
    actor_display_name: str | None = None,
    tenant_id: str | None = None,
) -> ResponseEvent:
    """Proposal creation, carrying the PROPOSER as the event actor.

    ``actor_subject_id`` is the server-derived proposer (spec §1 provenance): the
    audit trail must be able to answer "who proposed this response" from the event
    ledger alone, without re-reading a mutable row.
    """
    return ResponseEvent(
        event_type="response_proposal_created",
        aggregate_id=aggregate_id,
        tenant_id=tenant_id,
        actor_subject_id=actor_subject_id,
        payload={
            "status": status.value,
            "created_by_subject": actor_subject_id,
            "created_by_display_name": actor_display_name,
        },
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


def response_execution_queued(
    aggregate_id: UUID,
    *,
    submission_key: str,
    tenant_id: str | None = None,
) -> ResponseEvent:
    """Durable local SUBMISSION INTENT — enqueues the submit delivery.

    This is deliberately NOT a provider execution reference: at approval time no
    provider execution exists yet, and fabricating one (``execution_id=""`` or a
    ``pending-...`` sentinel) would both lie about provider identity and collide on
    the ``(provider, execution_id)`` uniqueness. The local submission intent is
    fully represented by the ApprovalDecision + APPROVED proposal + this event +
    its outbox row, keyed by the stable ``submission_key``.
    """
    return ResponseEvent(
        event_type="response_execution_queued",
        aggregate_id=aggregate_id,
        tenant_id=tenant_id,
        payload={"submission_key": submission_key},
    )


def response_execution_submitted(
    aggregate_id: UUID,
    *,
    external_execution_id: str,
    tenant_id: str | None = None,
) -> ResponseEvent:
    """HISIEM accepted the submission and a REAL execution id is durably known.

    Enqueues the first OBSERVE delivery — reconciliation is a durable, resumable
    responsibility, never an in-process polling loop.
    """
    return ResponseEvent(
        event_type="response_execution_submitted",
        aggregate_id=aggregate_id,
        tenant_id=tenant_id,
        payload={"external_execution_id": external_execution_id},
    )


def response_execution_observed(
    aggregate_id: UUID,
    *,
    external_execution_id: str,
    status: str,
    tenant_id: str | None = None,
) -> ResponseEvent:
    """A non-terminal provider observation that durably re-schedules the next one.

    A legitimately RUNNING/WAITING/WAITING_HUMAN execution is NOT a failure: it is
    persisted as an observation and re-enqueued with a future ``available_at``
    instead of raising a retry exception that would eventually dead-letter it.
    """
    return ResponseEvent(
        event_type="response_execution_observed",
        aggregate_id=aggregate_id,
        tenant_id=tenant_id,
        payload={"external_execution_id": external_execution_id, "status": status},
    )


def response_submission_retrying(
    aggregate_id: UUID,
    *,
    submission_key: str,
    attempt_count: int,
    error_code: str,
    tenant_id: str | None = None,
) -> ResponseEvent:
    """A TRANSIENT submission failure: still local-only, still retryable.

    No provider execution exists and none is claimed. The submit delivery stays in
    the outbox and is retried under the SAME ``submission_key``.
    """
    return ResponseEvent(
        event_type="response_submission_retrying",
        aggregate_id=aggregate_id,
        tenant_id=tenant_id,
        payload={
            "submission_key": submission_key,
            "attempt_count": attempt_count,
            "error_code": error_code,
        },
    )


def response_submission_failed(
    aggregate_id: UUID,
    *,
    submission_key: str,
    attempt_count: int,
    error_code: str,
    safe_error_message: str | None = None,
    tenant_id: str | None = None,
) -> ResponseEvent:
    """The provider DEFINITIVELY refused this submission (4xx contract rejection).

    This is a fact about the SUBMISSION, not about an execution: no provider
    execution was created, so this must never be recorded as an execution failure
    and must never produce a ResponseExecutionRef.
    """
    return ResponseEvent(
        event_type="response_submission_failed",
        aggregate_id=aggregate_id,
        tenant_id=tenant_id,
        actor_subject_id=None,
        payload={
            "submission_key": submission_key,
            "attempt_count": attempt_count,
            "error_code": error_code,
            "safe_error_message": safe_error_message,
        },
    )


def response_execution_started(
    aggregate_id: UUID, *, tenant_id: str | None = None
) -> ResponseEvent:
    return ResponseEvent(
        event_type="response_execution_started",
        aggregate_id=aggregate_id,
        tenant_id=tenant_id,
        payload={},
    )


def response_execution_succeeded(
    aggregate_id: UUID,
    *,
    external_execution_id: str,
    tenant_id: str | None = None,
) -> ResponseEvent:
    return ResponseEvent(
        event_type="response_execution_succeeded",
        aggregate_id=aggregate_id,
        tenant_id=tenant_id,
        payload={"external_execution_id": external_execution_id},
    )


def response_execution_failed(
    aggregate_id: UUID,
    *,
    error_code: str,
    tenant_id: str | None = None,
) -> ResponseEvent:
    return ResponseEvent(
        event_type="response_execution_failed",
        aggregate_id=aggregate_id,
        tenant_id=tenant_id,
        payload={"error_code": error_code},
    )
