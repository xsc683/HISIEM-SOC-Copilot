"""Response workflow commands (human-authored and orchestration).

Command shape mirrors the Investigation commands: user-triggered commands carry
the server-authenticated ``initiated_by_*`` identity (never a body field), an
optional stable ``idempotency_key`` for retry-safe replay, and a per-call
``command_id`` for the audit receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from ...domain.investigation.value_objects import ExternalResourceRef
from ...domain.response.enums import ApprovalDecisionKind


@dataclass(frozen=True)
class CreateResponseProposal:
    """Derive + policy-validate + persist a typed response proposal.

    Deterministic: the caller supplies the bounded action contract directly (no
    model round). ``target_ref`` and ``evidence_ids`` must already be resolved and
    belong to the investigation's tenant.
    """

    tenant_id: str
    investigation_id: UUID
    action_key: str
    target_ref: ExternalResourceRef
    evidence_ids: tuple[UUID, ...]
    parameters: dict[str, object]
    reason: str
    initiated_by_subject: str
    initiated_by_display_name: str | None = None
    command_id: UUID = field(default_factory=uuid4)
    idempotency_key: str | None = None
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class DecideResponseApproval:
    """Record one immutable human approval decision against an exact contract.

    ``expected_revision``/``expected_content_hash`` bind the decision to the exact
    proposal content the approver saw; a mismatch is a deterministic
    ``APPROVAL_CONTRACT_MISMATCH`` (never a best-effort carry-forward).
    """

    tenant_id: str
    approval_request_id: UUID
    decision: ApprovalDecisionKind
    expected_revision: int
    expected_content_hash: str
    initiated_by_subject: str
    reason: str | None = None
    initiated_by_display_name: str | None = None
    command_id: UUID = field(default_factory=uuid4)
    idempotency_key: str | None = None
    correlation_id: UUID | None = None
