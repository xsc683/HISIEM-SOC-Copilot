"""Response workflow API schemas (boundary DTOs).

The browser supplies ONLY bounded decision/contract data. Tenant and actor are
NEVER accepted from the body — they come from the authenticated TrustedContext
(server-derived). No credential, target identity, or approval authority is
representable in a request body.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from ...domain.response.aggregate import ResponseProposal
from ..schemas.workspace import ResponseProposalSchema


class ResponseTargetRequest(BaseModel):
    provider: str
    resource_type: str
    address_id: str
    business_id: str | None = None


class CreateResponseProposalRequest(BaseModel):
    """Derive one typed proposal from a bounded action contract."""

    action_key: str
    target: ResponseTargetRequest
    evidence_ids: list[str]
    parameters: dict[str, str] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=500)


class DecideApprovalRequest(BaseModel):
    """One immutable human decision bound to the exact proposal contract."""

    decision: str  # APPROVE | REJECT
    expected_revision: int
    expected_content_hash: str
    reason: str | None = Field(default=None, max_length=500)


class ResponseProposalResponse(BaseModel):
    proposal: ResponseProposalSchema

    @classmethod
    def from_domain(cls, proposal: ResponseProposal) -> ResponseProposalResponse:
        # A freshly created proposal has no approval/execution projection yet; the
        # authoritative composed view is the Workspace read model.
        return cls(
            proposal=ResponseProposalSchema(
                proposal_id=str(proposal.id),
                revision=proposal.content_revision,
                content_hash=proposal.content_hash,
                status=proposal.status.value,
                action_key=proposal.action_key,
                parameters=dict(proposal.parameters),
                reason=proposal.reason,
                policy_decision=(
                    proposal.policy_decision.value
                    if proposal.policy_decision
                    else None
                ),
                policy_reason=proposal.policy_reason,
                created_at=proposal.created_at,
            )
        )


class ApprovalDecisionResponse(BaseModel):
    """Bounded result of an approve/reject command."""

    proposal_id: str
    status: str
    decision: str
    decided_by: str
    decided_at: datetime
    execution_queued: bool

    @classmethod
    def from_outcome(
        cls,
        *,
        proposal_id: str,
        status: str,
        decision: str,
        decided_by: str,
        decided_at: datetime,
        execution_queued: bool,
    ) -> ApprovalDecisionResponse:
        return cls(
            proposal_id=proposal_id,
            status=status,
            decision=decision,
            decided_by=decided_by,
            decided_at=decided_at,
            execution_queued=execution_queued,
        )
