"""Reusable E3 authority/reliability fixture builders (Stage E / E3).

E3's ten scenarios need real production authority objects (a persisted proposal, an
approval request, an immutable human decision, a submission record, a provider
execution projection, and the durable event ledger) and real provider failures.
Four of the families' scenarios need them repeatedly, so this module builds them
through the REAL production code paths rather than hand-constructing domain objects:
the proposal and the approval go through ``ResponseCommandHandler``, and the
exhausted-uncertain submission goes through ``ResponseSubmitExhaustionHandler``.

Test-support only: nothing here is production code, and nothing in ``src`` imports it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from hisiem_soc_copilot.application.commands.response import (
    DecideResponseApproval,
)
from hisiem_soc_copilot.application.handlers.response import ResponseCommandHandler
from hisiem_soc_copilot.application.ports.soar import SoarExecutionResult
from hisiem_soc_copilot.domain.investigation.enums import VerdictDisposition
from hisiem_soc_copilot.domain.response.aggregate import ResponseProposal
from hisiem_soc_copilot.domain.response.enums import (
    ApprovalDecisionKind,
    ResponseExecutionStatus,
    ResponseProposalStatus,
    ResponseSubmissionStatus,
)
from hisiem_soc_copilot.domain.response.value_objects import (
    ResponseExecutionRef,
    ResponseSubmission,
    submission_key,
)
from hisiem_soc_copilot.evaluation_harness.cross_plane_e3_scenarios import E3Fixture
from hisiem_soc_copilot.infrastructure.durable.response_runner import (
    ResponseSubmitExhaustionHandler,
)
from tests.fixtures import response_flow
from tests.fixtures.fakes import FakeUnitOfWorkFactory

TENANT = response_flow.TENANT
PLAYBOOK_ID = response_flow.PLAYBOOK_ID
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


async def create_proposal(
    factory: FakeUnitOfWorkFactory,
    *,
    disposition: VerdictDisposition = VerdictDisposition.MALICIOUS,
    action_key: str = "START_SOAR_PLAYBOOK",
) -> tuple[UUID, UUID, ResponseProposal]:
    """Seed a COMPLETED investigation + result and run the REAL create handler.

    Returns ``(investigation_id, result_id, proposal)``. For a MALICIOUS verdict the
    real policy returns REQUIRE_APPROVAL and the handler records one approval
    request; for an INCONCLUSIVE verdict policy returns DENY.
    """
    investigation_id, _ = response_flow.seed_investigation(
        factory, disposition=disposition
    )
    proposal = await response_flow.create_proposal(
        factory, investigation_id, action_key=action_key
    )
    result = await factory().results.get_by_investigation(
        tenant_id=TENANT, investigation_id=investigation_id
    )
    assert result is not None
    return investigation_id, result.id, proposal


async def decide(
    factory: FakeUnitOfWorkFactory,
    proposal: ResponseProposal,
    *,
    decision: ApprovalDecisionKind,
    actor: str = "operator",
):
    """Run the REAL approval-decision handler against the exact proposal contract."""
    request = await factory().response_approvals.get_request_by_proposal(
        tenant_id=TENANT, proposal_id=proposal.id
    )
    assert request is not None, "proposal has no approval request"
    return await ResponseCommandHandler(
        unit_of_work_factory=factory
    ).decide_response_approval(
        DecideResponseApproval(
            tenant_id=TENANT,
            approval_request_id=request.id,
            decision=decision,
            expected_revision=proposal.content_revision,
            expected_content_hash=proposal.content_hash,
            initiated_by_subject=actor,
        )
    )


async def ledger_events(factory: FakeUnitOfWorkFactory, proposal_id: UUID):
    """The persisted durable events for one proposal (the real event ledger)."""
    return tuple(
        event for event in factory.events.events if event.aggregate_id == proposal_id
    )


async def reload(factory: FakeUnitOfWorkFactory, proposal_id: UUID) -> ResponseProposal:
    proposal = await factory().response_proposals.get(
        tenant_id=TENANT, proposal_id=proposal_id
    )
    assert proposal is not None
    return proposal


async def approved_fixture(
    factory: FakeUnitOfWorkFactory,
) -> tuple[E3Fixture, ResponseProposal]:
    """An APPROVED proposal with one durable submission intent and no execution.

    This is the state right after a human approval: authorization is recorded, the
    durable command is queued, and NO provider execution exists yet (E3 §6: this is
    a valid intermediate state, not an error).
    """
    investigation_id, result_id, proposal = await create_proposal(factory)
    outcome = await decide(factory, proposal, decision=ApprovalDecisionKind.APPROVE)
    current = await reload(factory, proposal.id)
    submission = await factory().response_submissions.get_by_proposal(
        tenant_id=TENANT, proposal_id=proposal.id
    )
    events = await ledger_events(factory, proposal.id)
    result = await factory().results.get_by_investigation(
        tenant_id=TENANT, investigation_id=investigation_id
    )
    fixture = E3Fixture(
        result=result,
        proposal=current,
        approval_request=await factory().response_approvals.get_request_by_proposal(
            tenant_id=TENANT, proposal_id=proposal.id
        ),
        approval_decision=outcome.decision,
        submission=submission,
        execution=None,
        events=events,
        execution_observed=False,
    )
    del result_id
    return fixture, current


async def rejected_fixture(
    factory: FakeUnitOfWorkFactory,
) -> tuple[E3Fixture, ResponseProposal]:
    """A REJECTED proposal: authorization refused, so NO durable command exists."""
    investigation_id, _, proposal = await create_proposal(factory)
    outcome = await decide(factory, proposal, decision=ApprovalDecisionKind.REJECT)
    current = await reload(factory, proposal.id)
    result = await factory().results.get_by_investigation(
        tenant_id=TENANT, investigation_id=investigation_id
    )
    return (
        E3Fixture(
            result=result,
            proposal=current,
            approval_request=await factory().response_approvals.get_request_by_proposal(
                tenant_id=TENANT, proposal_id=proposal.id
            ),
            approval_decision=outcome.decision,
            submission=await factory().response_submissions.get_by_proposal(
                tenant_id=TENANT, proposal_id=proposal.id
            ),
            execution=None,
            events=await ledger_events(factory, proposal.id),
            execution_observed=False,
        ),
        current,
    )


async def denied_fixture(
    factory: FakeUnitOfWorkFactory,
) -> tuple[E3Fixture, ResponseProposal]:
    """A policy-DENIED proposal: no approval request, no decision, no command."""
    investigation_id, _, proposal = await create_proposal(
        factory, disposition=VerdictDisposition.INCONCLUSIVE
    )
    current = await reload(factory, proposal.id)
    result = await factory().results.get_by_investigation(
        tenant_id=TENANT, investigation_id=investigation_id
    )
    return (
        E3Fixture(
            result=result,
            proposal=current,
            approval_request=await factory().response_approvals.get_request_by_proposal(
                tenant_id=TENANT, proposal_id=proposal.id
            ),
            approval_decision=None,
            submission=await factory().response_submissions.get_by_proposal(
                tenant_id=TENANT, proposal_id=proposal.id
            ),
            execution=None,
            events=await ledger_events(factory, proposal.id),
            execution_observed=False,
        ),
        current,
    )


async def exhaust_submission(
    factory: FakeUnitOfWorkFactory, proposal: ResponseProposal
) -> None:
    """Drive the REAL exhaustion handler for a still-RETRYING submission.

    ``_MAX_ATTEMPTS`` transport attempts have already failed uncertainly; production
    records the business meaning of that (explicit uncertainty, human attention
    required) rather than a success or a refusal.
    """
    for attempt in range(1, 11):
        await factory._response_submissions.add(  # noqa: SLF001 - test seeding
            ResponseSubmission(
                proposal_id=proposal.id,
                submission_key=submission_key(TENANT, proposal.id),
                status=ResponseSubmissionStatus.RETRYING.value,
                attempt_count=attempt,
                last_error_code="SOAR_UNAVAILABLE",
                safe_error_message="SOAR is unavailable",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    await ResponseSubmitExhaustionHandler(
        unit_of_work_factory=factory
    ).on_retry_exhausted(
        aggregate_id=str(proposal.id), tenant_id=TENANT, error_code="SOAR_UNAVAILABLE"
    )


async def attention_required_fixture(
    factory: FakeUnitOfWorkFactory,
) -> tuple[E3Fixture, ResponseProposal]:
    """An approved submission whose automatic retry budget ran out, still uncertain."""
    fixture, proposal = await approved_fixture(factory)
    await exhaust_submission(factory, proposal)
    return (
        E3Fixture(
            result=fixture.result,
            proposal=await reload(factory, proposal.id),
            approval_request=fixture.approval_request,
            approval_decision=fixture.approval_decision,
            submission=await factory().response_submissions.get_by_proposal(
                tenant_id=TENANT, proposal_id=proposal.id
            ),
            execution=None,
            events=fixture.events,
            execution_observed=False,
        ),
        proposal,
    )


def execution_ref(
    proposal: ResponseProposal,
    *,
    status: str = ResponseExecutionStatus.QUEUED.value,
    execution_id: str = "exec-00000000-0000-0000-0000-000000000001",
    finished_at: datetime | None = None,
) -> ResponseExecutionRef:
    """A provider execution projection, exactly as the durable runner persists it."""
    return ResponseExecutionRef(
        proposal_id=proposal.id,
        provider="hisiem",
        execution_id=execution_id,
        submission_key=submission_key(TENANT, proposal.id),
        status=status,
        submitted_at=NOW,
        last_observed_at=NOW + timedelta(seconds=15),
        started_at=NOW,
        finished_at=finished_at,
        safe_result=None,
        safe_error_code=None,
        safe_error_message=None,
    )


async def submitted_fixture(
    factory: FakeUnitOfWorkFactory,
    *,
    execution_status: str = ResponseExecutionStatus.QUEUED.value,
) -> tuple[E3Fixture, ResponseProposal, ResponseExecutionRef]:
    """An approved + SUBMITTED submission with a real provider execution ref.

    ``execution_status`` is what HISIEM last reported; it is deliberately not
    SUCCEEDED unless the caller asks for it, so the submission/execution distinction
    is exercised rather than assumed.
    """
    fixture, proposal = await approved_fixture(factory)
    await factory._response_submissions.update(  # noqa: SLF001 - test seeding
        ResponseSubmission(
            proposal_id=proposal.id,
            submission_key=submission_key(TENANT, proposal.id),
            status=ResponseSubmissionStatus.SUBMITTED.value,
            attempt_count=3,
            created_at=NOW,
            updated_at=NOW,
            submitted_at=NOW,
        )
    )
    ref = execution_ref(proposal, status=execution_status)
    await factory().response_executions.add(ref)
    current = await reload(factory, proposal.id)
    current.status = ResponseProposalStatus.SUBMITTED
    await factory().response_proposals.update(current)
    return (
        E3Fixture(
            result=fixture.result,
            proposal=current,
            approval_request=fixture.approval_request,
            approval_decision=fixture.approval_decision,
            submission=await factory().response_submissions.get_by_proposal(
                tenant_id=TENANT, proposal_id=proposal.id
            ),
            execution=ref,
            events=fixture.events,
            execution_observed=True,
            hisiem_observed_status=execution_status,
        ),
        current,
        ref,
    )


def soar_result(
    proposal_id: UUID,
    *,
    status: str = "SUCCEEDED",
    execution_id: str = "exec-00000000-0000-0000-0000-0000000000aa",
) -> SoarExecutionResult:
    """A real ``SoarExecutionResult`` as the SOAR port returns it."""
    return SoarExecutionResult(
        execution_id=execution_id,
        status=status,
        safe_result={"proposal_id": str(proposal_id)},
        safe_error_code=None,
        safe_error_message=None,
    )


def new_uuid() -> UUID:
    return uuid4()


__all__ = [
    "NOW",
    "PLAYBOOK_ID",
    "TENANT",
    "approved_fixture",
    "attention_required_fixture",
    "create_proposal",
    "decide",
    "denied_fixture",
    "execution_ref",
    "exhaust_submission",
    "ledger_events",
    "new_uuid",
    "rejected_fixture",
    "reload",
    "soar_result",
    "submitted_fixture",
]
