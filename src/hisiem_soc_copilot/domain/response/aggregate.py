"""ResponseProposal Aggregate Root.

Encodes the intent to perform a deterministic, HISIEM-resolved security action,
subject to policy validation and human approval. The aggregate binds approval to
an exact content revision + hash, per domain-model.md §21/§24.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from ..investigation.value_objects import ExternalResourceRef
from ..shared.errors import DomainError, StateTransitionError
from ..shared.identifiers import utc_now
from .enums import PolicyDecision, ResponseProposalStatus

_TRANSITIONS: dict[ResponseProposalStatus, dict[str, ResponseProposalStatus]] = {
    ResponseProposalStatus.CREATED: {
        "deny": ResponseProposalStatus.DENIED,
        "request_approval": ResponseProposalStatus.WAITING_APPROVAL,
    },
    ResponseProposalStatus.WAITING_APPROVAL: {
        "approve": ResponseProposalStatus.APPROVED,
        "reject": ResponseProposalStatus.REJECTED,
        "deny": ResponseProposalStatus.DENIED,
    },
    ResponseProposalStatus.APPROVED: {
        "submit": ResponseProposalStatus.SUBMITTED,
    },
}


@dataclass
class ResponseProposal:
    """Aggregate root for a validated, approval-bound response intent."""

    id: UUID
    investigation_id: UUID
    result_id: UUID
    action_key: str
    parameters: dict[str, Any]
    reason: str
    target_refs: list[ExternalResourceRef] = field(default_factory=list)
    evidence_ids: list[UUID] = field(default_factory=list)
    status: ResponseProposalStatus = ResponseProposalStatus.CREATED
    policy_decision: PolicyDecision | None = None
    policy_reason: str | None = None
    content_revision: int = 1
    content_hash: str = ""
    lock_version: int = 0
    approval_request_id: UUID | None = None
    execution_ref: Any = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.content_hash = self._compute_content_hash()

    # ------------------------------------------------------------------
    @staticmethod
    def action_allowed(action_key: str) -> bool:
        """Actions must come from the system allowlist (domain-model.md §22)."""
        from .enums import ResponseActionKey

        return any(action_key == a.value for a in ResponseActionKey)

    def validate_for_approval(self) -> None:
        """V1 invariant gate before entering approval (domain-model.md §24)."""
        errors: list[str] = []
        if not self.action_allowed(self.action_key):
            errors.append("action is not registered")
        if not self.target_refs:
            errors.append("target must be resolved")
        if not self.evidence_ids:
            errors.append("supporting evidence is required")
        if self.policy_decision is None:
            errors.append("policy validation must be completed")
        if self.policy_decision == PolicyDecision.DENY:
            errors.append("policy decision is DENY")
        if errors:
            raise DomainError("ResponseProposal cannot enter approval: " + "; ".join(errors))

    # ------------------------------------------------------------------
    def _compute_content_hash(self) -> str:
        canonical = {
            "action_key": self.action_key,
            "target_refs": [
                {
                    "provider": t.provider,
                    "resource_type": t.resource_type,
                    "address_id": t.address_id,
                    "business_id": t.business_id,
                }
                for t in self.target_refs
            ],
            "parameters": self.parameters,
        }
        encoded = json.dumps(canonical, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def content_hash_matches(self, revision: int, content_hash: str) -> bool:
        """Approval contract must bind the exact revision+hash that was requested."""
        return self.content_revision == revision and self.content_hash == content_hash

    def request_approval(self) -> None:
        self._transition("request_approval")
        self.status = ResponseProposalStatus.WAITING_APPROVAL

    def deny(self, reason: str | None = None) -> None:
        self._transition("deny")
        self.policy_decision = PolicyDecision.DENY
        self.policy_reason = reason

    def approve(self, approval_request_id: UUID | None = None) -> None:
        self._transition("approve")
        self.approval_request_id = approval_request_id

    def reject(self) -> None:
        self._transition("reject")

    def mark_submitted(self, execution_ref: Any) -> None:
        self._transition("submit")
        self.execution_ref = execution_ref

    def _transition(self, command: str) -> None:
        allowed = _TRANSITIONS.get(self.status, {})
        if command not in allowed:
            raise StateTransitionError(
                aggregate_type="response_proposal",
                current_status=self.status.value,
                command=command,
            )
        self.status = allowed[command]
        self.updated_at = utc_now()
        self.lock_version += 1
