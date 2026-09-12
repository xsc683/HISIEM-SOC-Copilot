"""Response domain value objects.

ApprovalRequest, ApprovalDecision and ResponseExecutionRef are immutable records
shaped per domain-model.md §26–§28. PolicyDecision mirrors the domain policy enum.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from ..shared.identifiers import utc_now


def submission_key(tenant_id: str, proposal_id: UUID) -> str:
    """Stable idempotency identity for ONE logical provider execution.

    Deterministic across worker retries and crashes — the same proposal always
    presents the same key to HISIEM, so a replay converges on one provider
    execution instead of creating a second one. It is presented as the
    ``Idempotency-Key`` header (and must stay within HISIEM's 128-char bound).
    """
    return f"response:{tenant_id}:{proposal_id}"


@dataclass(frozen=True)
class ApprovalRequest:
    """An approval request bound to an exact proposal content revision/hash."""

    id: UUID
    proposal_id: UUID
    proposal_content_revision: int
    proposal_content_hash: str
    requested_reason: str
    requested_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class ApprovalDecision:
    """An immutable authenticated-human authority fact.

    Invariant (domain-model.md §27): decision exactly once; actor authenticated with
    approval permission; proposal still matches approved revision/hash; the LLM never
    produces an ApprovalDecision.
    """

    id: UUID
    approval_request_id: UUID
    decision: str  # APPROVE / REJECT
    actor_subject_id: str
    actor_tenant_id: str
    reason: str | None = None
    actor_display_name: str | None = None
    decided_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class ResponseExecutionRef:
    """Projection of a HISIEM SOAR execution (never the SOAR source of truth).

    ``status`` is a :class:`ResponseExecutionStatus` value. ``safe_result`` holds
    bounded, non-secret result facts only; ``safe_error_code``/``safe_error_message``
    hold a normalized failure summary — never a raw upstream body.
    """

    proposal_id: UUID
    provider: str
    execution_id: str
    submission_key: str
    status: str
    submitted_at: datetime
    last_observed_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    safe_result: dict[str, object] | None = None
    safe_error_code: str | None = None
    safe_error_message: str | None = None


@dataclass(frozen=True)
class ResponseSubmission:
    """The durable local truth about ONE approved submission attempt.

    Persisted independently of the provider execution projection because the two
    answer different questions: this one answers "did we get the submission to the
    provider?", the other answers "what did the provider do with it?". A definitive
    provider rejection has a submission row and NO execution row — which is exactly
    the fact the workspace has to be able to show.
    """

    proposal_id: UUID
    submission_key: str
    status: str  # ResponseSubmissionStatus
    attempt_count: int = 0
    last_error_code: str | None = None
    safe_error_message: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    submitted_at: datetime | None = None
    failed_at: datetime | None = None
    attention_required_at: datetime | None = None


@dataclass(frozen=True)
class ResponsePolicyDecision:
    decision: str  # DENY / REQUIRE_APPROVAL
    reason: str | None = None
