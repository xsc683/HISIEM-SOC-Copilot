"""Explicit mappers for the ResponseProposal aggregate and response value objects.

ORM rows are translated to/from pure domain dataclasses here — no
``from_attributes`` magic (python-package-boundary.md §18). Proposal targets and
evidence links live in their own rows; the repository loads them and hands them to
:func:`to_domain`.
"""

from __future__ import annotations

from uuid import UUID

from ....domain.investigation.value_objects import ExternalResourceRef
from ....domain.response.aggregate import ResponseProposal
from ....domain.response.enums import PolicyDecision, ResponseProposalStatus
from ....domain.response.value_objects import (
    ApprovalDecision,
    ApprovalRequest,
    ResponseExecutionRef,
    ResponseSubmission,
)
from ..orm.response import (
    ApprovalDecisionRow,
    ApprovalRequestRow,
    ResponseExecutionRefRow,
    ResponseProposalRow,
    ResponseProposalTargetRow,
    ResponseSubmissionRow,
)


def proposal_to_domain(
    row: ResponseProposalRow,
    *,
    targets: list[ResponseProposalTargetRow],
    evidence_ids: list[UUID],
) -> ResponseProposal:
    """Rebuild the aggregate from its row + target/evidence rows (hash recomputed)."""
    proposal = ResponseProposal(
        id=row.id,
        investigation_id=row.investigation_id,
        result_id=row.result_id,
        action_key=row.action_key,
        parameters=dict(row.parameters or {}),
        reason=row.reason,
        target_refs=[
            ExternalResourceRef(
                provider=t.provider,
                resource_type=t.resource_type,
                address_id=t.address_id,
                business_id=t.business_id,
            )
            for t in sorted(targets, key=lambda t: t.ordinal)
        ],
        evidence_ids=[e for e in evidence_ids],
        status=ResponseProposalStatus(row.status),
        policy_decision=(
            PolicyDecision(row.policy_decision) if row.policy_decision else None
        ),
        policy_reason=row.policy_reason,
        content_revision=row.content_revision,
        content_hash=row.content_hash,
        lock_version=row.lock_version,
        approval_request_id=None,
        created_by_subject=row.created_by_subject,
        created_by_display_name=row.created_by_display_name,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
    # ``content_hash`` is recomputed in __post_init__ from the same canonical form;
    # it must equal the persisted value (a mismatch means schema/contract drift).
    return proposal


def proposal_to_row(proposal: ResponseProposal) -> ResponseProposalRow:
    return ResponseProposalRow(
        id=proposal.id,
        investigation_id=proposal.investigation_id,
        result_id=proposal.result_id,
        status=proposal.status.value,
        action_key=proposal.action_key,
        parameters=proposal.parameters,
        reason=proposal.reason,
        policy_decision=(
            proposal.policy_decision.value if proposal.policy_decision else None
        ),
        policy_reason=proposal.policy_reason,
        content_revision=proposal.content_revision,
        content_hash=proposal.content_hash,
        lock_version=proposal.lock_version,
        created_by_subject=proposal.created_by_subject,
        created_by_display_name=proposal.created_by_display_name,
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
    )


def targets_to_rows(proposal: ResponseProposal) -> list[ResponseProposalTargetRow]:
    return [
        ResponseProposalTargetRow(
            proposal_id=proposal.id,
            ordinal=index,
            provider=t.provider,
            resource_type=t.resource_type,
            address_id=t.address_id,
            business_id=t.business_id,
        )
        for index, t in enumerate(proposal.target_refs)
    ]


def approval_request_to_domain(row: ApprovalRequestRow) -> ApprovalRequest:
    return ApprovalRequest(
        id=row.id,
        proposal_id=row.proposal_id,
        proposal_content_revision=row.proposal_content_revision,
        proposal_content_hash=row.proposal_content_hash,
        requested_reason=row.requested_reason,
        requested_at=row.requested_at,
    )


def approval_request_to_row(request: ApprovalRequest) -> ApprovalRequestRow:
    return ApprovalRequestRow(
        id=request.id,
        proposal_id=request.proposal_id,
        proposal_content_revision=request.proposal_content_revision,
        proposal_content_hash=request.proposal_content_hash,
        requested_reason=request.requested_reason,
        requested_at=request.requested_at,
    )


def approval_decision_to_domain(row: ApprovalDecisionRow) -> ApprovalDecision:
    return ApprovalDecision(
        id=row.id,
        approval_request_id=row.approval_request_id,
        decision=row.decision,
        actor_subject_id=row.actor_subject_id,
        actor_tenant_id=row.actor_tenant_id,
        reason=row.reason,
        actor_display_name=row.actor_display_name,
        decided_at=row.decided_at,
    )


def approval_decision_to_row(decision: ApprovalDecision) -> ApprovalDecisionRow:
    return ApprovalDecisionRow(
        id=decision.id,
        approval_request_id=decision.approval_request_id,
        decision=decision.decision,
        actor_subject_id=decision.actor_subject_id,
        actor_tenant_id=decision.actor_tenant_id,
        actor_display_name=decision.actor_display_name,
        reason=decision.reason,
        decided_at=decision.decided_at,
    )


def execution_to_domain(row: ResponseExecutionRefRow) -> ResponseExecutionRef:
    return ResponseExecutionRef(
        proposal_id=row.proposal_id,
        provider=row.provider,
        execution_id=row.execution_id,
        submission_key=row.submission_key,
        status=row.status,
        submitted_at=row.submitted_at,
        last_observed_at=row.last_observed_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        safe_result=dict(row.safe_result) if row.safe_result else None,
        safe_error_code=row.safe_error_code,
        safe_error_message=row.safe_error_message,
    )


def submission_to_domain(row: ResponseSubmissionRow) -> ResponseSubmission:
    return ResponseSubmission(
        proposal_id=row.proposal_id,
        submission_key=row.submission_key,
        status=row.status,
        attempt_count=row.attempt_count,
        last_error_code=row.last_error_code,
        safe_error_message=row.safe_error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
        submitted_at=row.submitted_at,
        failed_at=row.failed_at,
        attention_required_at=row.attention_required_at,
    )


def submission_to_row(submission: ResponseSubmission) -> ResponseSubmissionRow:
    return ResponseSubmissionRow(
        proposal_id=submission.proposal_id,
        submission_key=submission.submission_key,
        status=submission.status,
        attempt_count=submission.attempt_count,
        last_error_code=submission.last_error_code,
        safe_error_message=submission.safe_error_message,
        created_at=submission.created_at,
        updated_at=submission.updated_at,
        submitted_at=submission.submitted_at,
        failed_at=submission.failed_at,
        attention_required_at=submission.attention_required_at,
    )
