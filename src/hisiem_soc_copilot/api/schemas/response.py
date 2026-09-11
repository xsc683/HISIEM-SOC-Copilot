"""Response workflow API schemas (boundary DTOs).

The browser supplies ONLY bounded decision/contract data. Tenant and actor are
NEVER accepted from the body — they come from the authenticated TrustedContext
(server-derived). No credential, target identity, or approval authority is
representable in a request body.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ...domain.response.aggregate import ResponseProposal
from ..schemas.workspace import ResponseProposalSchema


class CreateResponseProposalRequest(BaseModel):
    """Bounded action contract for deriving one typed proposal.

    There is deliberately NO ``target`` field. The action target is DERIVED
    server-side from the persisted Investigation's authoritative
    ``source_alert_ref``; a browser cannot name, forge, or influence the resource
    an approved action would hit. ``model_config`` forbids extra properties so a
    caller that still sends ``target`` / ``tenant_id`` / ``actor`` **fails loudly**
    with a 422 instead of having the field silently ignored (spec §1.1).

    Likewise there is no tenant, actor, or approval-authority field: all three are
    server-derived from the authenticated TrustedContext.
    """

    model_config = ConfigDict(extra="forbid")

    action_key: str = Field(min_length=1, max_length=64)
    evidence_ids: list[str] = Field(min_length=1)
    parameters: dict[str, str] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=500)


class DecideApprovalRequest(BaseModel):
    """One immutable human decision bound to the exact proposal contract."""

    model_config = ConfigDict(extra="forbid")

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
