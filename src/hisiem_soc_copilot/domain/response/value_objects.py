"""Response domain value objects.

ApprovalRequest, ApprovalDecision and ResponseExecutionRef are immutable records
shaped per domain-model.md §26–§28. PolicyDecision mirrors the domain policy enum.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from ..shared.identifiers import utc_now


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
    """Projection of a HISIEM SOAR execution (never the SOAR source of truth)."""

    proposal_id: UUID
    provider: str
    execution_id: str
    submission_key: str
    last_observed_status: str
    submitted_at: datetime
    last_observed_at: datetime


@dataclass(frozen=True)
class ResponsePolicyDecision:
    decision: str  # DENY / REQUIRE_APPROVAL
    reason: str | None = None
