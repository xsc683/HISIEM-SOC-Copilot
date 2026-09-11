"""Response workflow command handlers.

Thin orchestration over one UnitOfWork per command: load the aggregate, apply the
domain method, persist the new child rows + domain events, commit. No HTTP, no
ORM, no infrastructure imports (python-package-boundary.md).

Transaction discipline (persistence-schema.md §3): NO network I/O runs inside a
transaction. Approval therefore never calls SOAR inline — it records the immutable
decision, creates a QUEUED execution ref, and appends a ``response_execution_queued``
event whose OUTBOX row a separate durable worker consumes (spec §15/§16).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from ...domain.response.actions import is_executable, validate_action_parameters
from ...domain.response.aggregate import ResponseProposal
from ...domain.response.enums import (
    ApprovalDecisionKind,
    PolicyDecision,
)
from ...domain.response.errors import (
    ApprovalContractError,
    ApprovalDecisionAlreadyExistsError,
)
from ...domain.response.events import (
    response_approval_decided,
    response_approval_requested,
    response_execution_queued,
    response_policy_decided,
    response_proposal_created,
)
from ...domain.response.policy import evaluate_response_policy
from ...domain.response.value_objects import (
    ApprovalDecision,
    ApprovalRequest,
    ResponseExecutionRef,
)
from ...domain.shared.errors import DomainError
from ...domain.shared.identifiers import utc_now
from ..commands.response import CreateResponseProposal, DecideResponseApproval
from ..errors import NotFoundError
from ..ports.unit_of_work import UnitOfWork

_PROVIDER = "hisiem"


@dataclass(frozen=True)
class ApprovalOutcome:
    """Result of one approval decision: the proposal + the persisted decision."""

    proposal: ResponseProposal
    decision: ApprovalDecision
    execution_queued: bool


class ResponseCommandHandler:
    """Coordinates response proposal + approval commands against short UoWs."""

    def __init__(self, *, unit_of_work_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = unit_of_work_factory

    # ------------------------------------------------------------------
    async def create_response_proposal(
        self, command: CreateResponseProposal
    ) -> ResponseProposal:
        """Derive + policy-validate + persist one typed proposal (idempotent)."""
        uow = self._uow_factory()
        try:
            investigation = await uow.investigations.get(
                tenant_id=command.tenant_id, investigation_id=command.investigation_id
            )
            if investigation is None:
                raise NotFoundError(
                    "investigation not found",
                    resource_type="investigation",
                    resource_id=str(command.investigation_id),
                )
            # One proposal per investigation in V1: re-creating returns the existing
            # one (deterministic, never a duplicate).
            existing = await uow.response_proposals.get_by_investigation(
                tenant_id=command.tenant_id,
                investigation_id=command.investigation_id,
            )
            if existing is not None:
                return existing

            result = await uow.results.get_by_investigation(
                tenant_id=command.tenant_id,
                investigation_id=command.investigation_id,
            )
            if result is None:
                raise DomainError(
                    "a response proposal requires a finalized investigation result"
                )

            # Bounded, typed action contract — no arbitrary action, no arbitrary body.
            validate_action_parameters(command.action_key, command.parameters)
            if not is_executable(command.action_key):
                raise DomainError(f"response action {command.action_key!r} is not executable")

            policy = evaluate_response_policy(
                action_key=command.action_key,
                verdict_disposition=result.verdict.disposition,
                target_refs=[command.target_ref],
                evidence_ids=list(command.evidence_ids),
                parameters=command.parameters,
                tenant_id=command.tenant_id,
            )

            proposal = ResponseProposal(
                id=uuid4(),
                investigation_id=command.investigation_id,
                result_id=result.id,
                action_key=command.action_key,
                parameters=dict(command.parameters),
                reason=command.reason,
                target_refs=[command.target_ref],
                evidence_ids=list(command.evidence_ids),
                policy_decision=policy.decision,
                policy_reason=policy.reason if isinstance(policy.reason, str) else (
                    policy.reason.value if policy.reason is not None else None
                ),
            )
            events = [
                response_proposal_created(
                    proposal.id,
                    status=proposal.status,
                    tenant_id=command.tenant_id,
                ),
                response_policy_decided(
                    proposal.id,
                    decision=policy.decision,
                    reason=proposal.policy_reason,
                    tenant_id=command.tenant_id,
                ),
            ]

            if policy.decision == PolicyDecision.DENY:
                proposal.deny(proposal.policy_reason)
                await uow.response_proposals.add(proposal)
                await uow.investigations.update(investigation)
            else:
                proposal.request_approval()
                request = ApprovalRequest(
                    id=uuid4(),
                    proposal_id=proposal.id,
                    proposal_content_revision=proposal.content_revision,
                    proposal_content_hash=proposal.content_hash,
                    requested_reason=command.reason,
                )
                proposal.approval_request_id = request.id
                await uow.response_proposals.add(proposal)
                await uow.response_approvals.add_request(request)
                events.append(
                    response_approval_requested(
                        proposal.id, request_id=request.id, tenant_id=command.tenant_id
                    )
                )

            investigation.response_proposal_id = proposal.id
            for event in events:
                await uow.events.append(event, aggregate_revision=proposal.lock_version)
            await uow.commit()
            return proposal
        finally:
            await uow.close()

    # ------------------------------------------------------------------
    async def decide_response_approval(
        self, command: DecideResponseApproval
    ) -> ApprovalOutcome:
        """Record one immutable human decision bound to the exact proposal contract."""
        uow = self._uow_factory()
        try:
            request = await uow.response_approvals.get_request(
                tenant_id=command.tenant_id,
                approval_request_id=command.approval_request_id,
            )
            if request is None:
                raise NotFoundError(
                    "approval request not found",
                    resource_type="response_approval_request",
                    resource_id=str(command.approval_request_id),
                )
            proposal = await uow.response_proposals.get(
                tenant_id=command.tenant_id, proposal_id=request.proposal_id
            )
            if proposal is None:
                raise NotFoundError(
                    "response proposal not found",
                    resource_type="response_proposal",
                    resource_id=str(request.proposal_id),
                )

            existing = await uow.response_approvals.get_decision(
                tenant_id=command.tenant_id,
                approval_request_id=request.id,
            )
            if existing is not None:
                # A decision is immutable: a REPLAY of the same decision converges;
                # a DIFFERENT decision is a deterministic conflict (spec §13).
                if existing.decision == command.decision.value:
                    return ApprovalOutcome(
                        proposal=proposal,
                        decision=existing,
                        execution_queued=(
                            existing.decision == ApprovalDecisionKind.APPROVE.value
                        ),
                    )
                raise ApprovalDecisionAlreadyExistsError(
                    approval_request_id=request.id
                )

            # The approval contract must bind the EXACT revision+hash requested.
            if not proposal.content_hash_matches(
                command.expected_revision, command.expected_content_hash
            ):
                raise ApprovalContractError(
                    approval_request_id=request.id, proposal_id=proposal.id
                )

            decision = ApprovalDecision(
                id=uuid4(),
                approval_request_id=request.id,
                decision=command.decision.value,
                actor_subject_id=command.initiated_by_subject,
                actor_tenant_id=command.tenant_id,
                reason=command.reason,
                actor_display_name=command.initiated_by_display_name,
            )
            await uow.response_approvals.add_decision(decision)

            if command.decision == ApprovalDecisionKind.REJECT:
                proposal.reject()
                await uow.response_proposals.update(proposal)
                await uow.events.append(
                    response_approval_decided(
                        proposal.id,
                        request_id=request.id,
                        decision=decision.decision,
                        actor_subject_id=decision.actor_subject_id,
                        tenant_id=command.tenant_id,
                    ),
                    aggregate_revision=proposal.lock_version,
                )
                await uow.commit()
                return ApprovalOutcome(
                    proposal=proposal, decision=decision, execution_queued=False
                )

            # APPROVE → durable queue only; NO SOAR call inside this transaction.
            proposal.approve(approval_request_id=request.id)
            await uow.response_proposals.update(proposal)

            now = utc_now()
            submission_key = _submission_key(command.tenant_id, proposal.id)
            execution = ResponseExecutionRef(
                proposal_id=proposal.id,
                provider=_PROVIDER,
                execution_id="",
                submission_key=submission_key,
                status="QUEUED",
                submitted_at=now,
                last_observed_at=now,
            )
            await uow.response_executions.add(execution)

            await uow.events.append(
                response_approval_decided(
                    proposal.id,
                    request_id=request.id,
                    decision=decision.decision,
                    actor_subject_id=decision.actor_subject_id,
                    tenant_id=command.tenant_id,
                ),
                aggregate_revision=proposal.lock_version,
            )
            # This event is the ONLY response event that enqueues an outbox delivery.
            await uow.events.append(
                response_execution_queued(
                    proposal.id, execution_id=proposal.id, tenant_id=command.tenant_id
                ),
                aggregate_revision=proposal.lock_version,
            )
            await uow.commit()
            return ApprovalOutcome(
                proposal=proposal, decision=decision, execution_queued=True
            )
        finally:
            await uow.close()


def _submission_key(tenant_id: str, proposal_id: UUID) -> str:
    """Stable idempotency identity for one logical execution (spec §25).

    Deterministic across retries/crashes — a re-approval or a worker retry always
    presents the same key, so HISIEM can converge on one execution identity.
    """
    return f"response:{tenant_id}:{proposal_id}"
