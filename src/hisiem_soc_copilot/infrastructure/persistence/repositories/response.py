"""SQLAlchemy response repositories (proposal / approval / execution).

All reads are tenant-scoped by joining through ``investigation.tenant_id`` — the
single owner of a proposal's tenant. Proposal updates use optimistic locking; the
approval decision insert relies on the UNIQUE(approval_request_id) constraint so a
second decision can never be committed (spec §13/§51).
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ....application.ports.repositories import (
    ResponseApprovalRepository,
    ResponseExecutionRepository,
    ResponseProposalRepository,
    ResponseSubmissionRepository,
)
from ....domain.response.aggregate import ResponseProposal
from ....domain.response.errors import ResponseProposalConflictError
from ....domain.response.value_objects import (
    ApprovalDecision,
    ApprovalRequest,
    ResponseExecutionRef,
    ResponseSubmission,
)
from ....domain.shared.errors import OptimisticConcurrencyError
from ..mappers.response import (
    approval_decision_to_domain,
    approval_decision_to_row,
    approval_request_to_domain,
    approval_request_to_row,
    execution_to_domain,
    proposal_to_domain,
    proposal_to_row,
    submission_to_domain,
    submission_to_row,
    targets_to_rows,
)
from ..orm.investigation import InvestigationRow
from ..orm.response import (
    ApprovalDecisionRow,
    ApprovalRequestRow,
    ResponseExecutionRefRow,
    ResponseProposalEvidenceRow,
    ResponseProposalRow,
    ResponseProposalTargetRow,
    ResponseSubmissionRow,
)

# V1 allows exactly ONE response proposal per investigation (and per result).
# Both unique constraints mean the same thing to a caller: someone else created
# this investigation's proposal first. Shared with the unit of work, which keeps a
# commit-time translation as a second net.
_PROPOSAL_UNIQUE_CONSTRAINTS = (
    "uq_response_proposal_investigation",
    "uq_response_proposal_result",
)


def _is_proposal_unique_conflict(exc: IntegrityError) -> bool:
    orig = exc.orig
    name = getattr(orig, "constraint_name", None)
    if name is None:
        diag = getattr(orig, "diag", None)
        name = getattr(diag, "constraint_name", None)
    return name in _PROPOSAL_UNIQUE_CONSTRAINTS


class SqlAlchemyResponseProposalRepository(ResponseProposalRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, proposal: ResponseProposal) -> None:
        self._session.add(proposal_to_row(proposal))
        # Flush the proposal row BEFORE its FK children so the parent exists for
        # the child INSERTs (and for an approval_request added later in the same
        # transaction). Without an ORM relationship, insert order is otherwise
        # not guaranteed.
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # The INSERT is what actually violates UNIQUE(investigation_id) /
            # UNIQUE(result_id), so the concurrent-first-create race surfaces HERE
            # rather than at commit(). Translate it at the point it fires so the
            # handler can converge instead of leaking a raw IntegrityError as 500.
            await self._session.rollback()
            if _is_proposal_unique_conflict(exc):
                raise ResponseProposalConflictError(
                    investigation_id=proposal.investigation_id
                ) from exc
            raise
        for target in targets_to_rows(proposal):
            self._session.add(target)
        for evidence_id in proposal.evidence_ids:
            self._session.add(
                ResponseProposalEvidenceRow(
                    proposal_id=proposal.id, evidence_id=evidence_id
                )
            )

    async def update(self, proposal: ResponseProposal) -> None:
        """Persist mutable proposal columns with an optimistic-lock UPDATE.

        The action contract (targets/evidence) is immutable after creation, so only
        status/policy/approval linkage columns are written here.
        """
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                update(ResponseProposalRow)
                .where(
                    ResponseProposalRow.id == proposal.id,
                    ResponseProposalRow.lock_version == proposal.lock_version,
                )
                .values(
                    status=proposal.status.value,
                    policy_decision=(
                        proposal.policy_decision.value
                        if proposal.policy_decision
                        else None
                    ),
                    policy_reason=proposal.policy_reason,
                    updated_at=proposal.updated_at,
                    lock_version=proposal.lock_version + 1,
                )
            ),
        )
        if result.rowcount == 0:
            raise OptimisticConcurrencyError(
                aggregate_type="response_proposal",
                aggregate_id=str(proposal.id),
            )
        proposal.lock_version += 1

    async def get(
        self, *, tenant_id: str, proposal_id: UUID
    ) -> ResponseProposal | None:
        row = await self._load_row(tenant_id=tenant_id, proposal_id=proposal_id)
        return await self._hydrate(row) if row is not None else None

    async def get_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> ResponseProposal | None:
        row = (
            await self._session.execute(
                select(ResponseProposalRow)
                .join(
                    InvestigationRow,
                    InvestigationRow.id == ResponseProposalRow.investigation_id,
                )
                .where(
                    InvestigationRow.tenant_id == tenant_id,
                    ResponseProposalRow.investigation_id == investigation_id,
                )
            )
        ).scalar_one_or_none()
        return await self._hydrate(row) if row is not None else None

    async def get_by_result(
        self, *, tenant_id: str, result_id: UUID
    ) -> ResponseProposal | None:
        row = (
            await self._session.execute(
                select(ResponseProposalRow)
                .join(
                    InvestigationRow,
                    InvestigationRow.id == ResponseProposalRow.investigation_id,
                )
                .where(
                    InvestigationRow.tenant_id == tenant_id,
                    ResponseProposalRow.result_id == result_id,
                )
            )
        ).scalar_one_or_none()
        return await self._hydrate(row) if row is not None else None

    async def _load_row(
        self, *, tenant_id: str, proposal_id: UUID
    ) -> ResponseProposalRow | None:
        return (
            await self._session.execute(
                select(ResponseProposalRow)
                .join(
                    InvestigationRow,
                    InvestigationRow.id == ResponseProposalRow.investigation_id,
                )
                .where(
                    InvestigationRow.tenant_id == tenant_id,
                    ResponseProposalRow.id == proposal_id,
                )
            )
        ).scalar_one_or_none()

    async def _hydrate(self, row: ResponseProposalRow) -> ResponseProposal:
        targets = list(
            (
                await self._session.execute(
                    select(ResponseProposalTargetRow).where(
                        ResponseProposalTargetRow.proposal_id == row.id
                    )
                )
            )
            .scalars()
            .all()
        )
        evidence_ids = list(
            (
                await self._session.execute(
                    select(ResponseProposalEvidenceRow.evidence_id).where(
                        ResponseProposalEvidenceRow.proposal_id == row.id
                    )
                )
            )
            .scalars()
            .all()
        )
        return proposal_to_domain(row, targets=targets, evidence_ids=evidence_ids)


class SqlAlchemyResponseApprovalRepository(ResponseApprovalRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_request(self, request: ApprovalRequest) -> None:
        self._session.add(approval_request_to_row(request))

    async def get_request(
        self, *, tenant_id: str, approval_request_id: UUID
    ) -> ApprovalRequest | None:
        row = (
            await self._session.execute(
                select(ApprovalRequestRow)
                .join(
                    ResponseProposalRow,
                    ResponseProposalRow.id == ApprovalRequestRow.proposal_id,
                )
                .join(
                    InvestigationRow,
                    InvestigationRow.id == ResponseProposalRow.investigation_id,
                )
                .where(
                    InvestigationRow.tenant_id == tenant_id,
                    ApprovalRequestRow.id == approval_request_id,
                )
            )
        ).scalar_one_or_none()
        return approval_request_to_domain(row) if row is not None else None

    async def get_request_by_proposal(
        self, *, tenant_id: str, proposal_id: UUID
    ) -> ApprovalRequest | None:
        row = (
            await self._session.execute(
                select(ApprovalRequestRow)
                .join(
                    ResponseProposalRow,
                    ResponseProposalRow.id == ApprovalRequestRow.proposal_id,
                )
                .join(
                    InvestigationRow,
                    InvestigationRow.id == ResponseProposalRow.investigation_id,
                )
                .where(
                    InvestigationRow.tenant_id == tenant_id,
                    ApprovalRequestRow.proposal_id == proposal_id,
                )
            )
        ).scalar_one_or_none()
        return approval_request_to_domain(row) if row is not None else None

    async def add_decision(self, decision: ApprovalDecision) -> None:
        self._session.add(approval_decision_to_row(decision))

    async def get_decision(
        self, *, tenant_id: str, approval_request_id: UUID
    ) -> ApprovalDecision | None:
        row = (
            await self._session.execute(
                select(ApprovalDecisionRow)
                .join(
                    ApprovalRequestRow,
                    ApprovalRequestRow.id == ApprovalDecisionRow.approval_request_id,
                )
                .join(
                    ResponseProposalRow,
                    ResponseProposalRow.id == ApprovalRequestRow.proposal_id,
                )
                .join(
                    InvestigationRow,
                    InvestigationRow.id == ResponseProposalRow.investigation_id,
                )
                .where(
                    InvestigationRow.tenant_id == tenant_id,
                    ApprovalDecisionRow.approval_request_id == approval_request_id,
                )
            )
        ).scalar_one_or_none()
        return approval_decision_to_domain(row) if row is not None else None


class SqlAlchemyResponseExecutionRepository(ResponseExecutionRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, execution: ResponseExecutionRef) -> None:
        self._session.add(
            ResponseExecutionRefRow(
                proposal_id=execution.proposal_id,
                provider=execution.provider,
                execution_id=execution.execution_id,
                submission_key=execution.submission_key,
                status=execution.status,
                submitted_at=execution.submitted_at,
                last_observed_at=execution.last_observed_at,
                started_at=execution.started_at,
                finished_at=execution.finished_at,
                safe_result=execution.safe_result,
                safe_error_code=execution.safe_error_code,
                safe_error_message=execution.safe_error_message,
            )
        )

    async def update(self, execution: ResponseExecutionRef) -> None:
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                update(ResponseExecutionRefRow)
                .where(ResponseExecutionRefRow.proposal_id == execution.proposal_id)
                .values(
                    provider=execution.provider,
                    execution_id=execution.execution_id,
                    status=execution.status,
                    last_observed_at=execution.last_observed_at,
                    started_at=execution.started_at,
                    finished_at=execution.finished_at,
                    safe_result=execution.safe_result,
                    safe_error_code=execution.safe_error_code,
                    safe_error_message=execution.safe_error_message,
                )
            ),
        )
        if result.rowcount == 0:
            raise KeyError(f"response execution {execution.proposal_id} not found")

    async def get_by_proposal(
        self, *, tenant_id: str, proposal_id: UUID
    ) -> ResponseExecutionRef | None:
        row = (
            await self._session.execute(
                select(ResponseExecutionRefRow)
                .join(
                    ResponseProposalRow,
                    ResponseProposalRow.id == ResponseExecutionRefRow.proposal_id,
                )
                .join(
                    InvestigationRow,
                    InvestigationRow.id == ResponseProposalRow.investigation_id,
                )
                .where(
                    InvestigationRow.tenant_id == tenant_id,
                    ResponseExecutionRefRow.proposal_id == proposal_id,
                )
            )
        ).scalar_one_or_none()
        return execution_to_domain(row) if row is not None else None

    async def get_by_execution_id(
        self, *, tenant_id: str, execution_id: str
    ) -> ResponseExecutionRef | None:
        row = (
            await self._session.execute(
                select(ResponseExecutionRefRow)
                .join(
                    ResponseProposalRow,
                    ResponseProposalRow.id == ResponseExecutionRefRow.proposal_id,
                )
                .join(
                    InvestigationRow,
                    InvestigationRow.id == ResponseProposalRow.investigation_id,
                )
                .where(
                    InvestigationRow.tenant_id == tenant_id,
                    ResponseExecutionRefRow.execution_id == execution_id,
                )
            )
        ).scalar_one_or_none()
        return execution_to_domain(row) if row is not None else None


class SqlAlchemyResponseSubmissionRepository(ResponseSubmissionRepository):
    """Local submission lifecycle; tenant-scoped through the owning investigation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, submission: ResponseSubmission) -> None:
        self._session.add(submission_to_row(submission))

    async def update(self, submission: ResponseSubmission) -> None:
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                update(ResponseSubmissionRow)
                .where(ResponseSubmissionRow.proposal_id == submission.proposal_id)
                .values(
                    status=submission.status,
                    attempt_count=submission.attempt_count,
                    last_error_code=submission.last_error_code,
                    safe_error_message=submission.safe_error_message,
                    updated_at=submission.updated_at,
                    submitted_at=submission.submitted_at,
                    failed_at=submission.failed_at,
                )
            ),
        )
        if result.rowcount == 0:
            raise KeyError(f"response submission {submission.proposal_id} not found")

    async def get_by_proposal(
        self, *, tenant_id: str, proposal_id: UUID
    ) -> ResponseSubmission | None:
        row = (
            await self._session.execute(
                select(ResponseSubmissionRow)
                .join(
                    ResponseProposalRow,
                    ResponseProposalRow.id == ResponseSubmissionRow.proposal_id,
                )
                .join(
                    InvestigationRow,
                    InvestigationRow.id == ResponseProposalRow.investigation_id,
                )
                .where(
                    InvestigationRow.tenant_id == tenant_id,
                    ResponseSubmissionRow.proposal_id == proposal_id,
                )
            )
        ).scalar_one_or_none()
        return submission_to_domain(row) if row is not None else None


__all__ = [
    "SqlAlchemyResponseProposalRepository",
    "SqlAlchemyResponseSubmissionRepository",
    "SqlAlchemyResponseApprovalRepository",
    "SqlAlchemyResponseExecutionRepository",
]
