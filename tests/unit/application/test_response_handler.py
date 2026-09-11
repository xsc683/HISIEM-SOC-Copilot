"""Response workflow command-handler tests over in-memory fakes (P2).

Covers the invariant "Agent recommends, human authorizes": every write requires an
approval, approval binds the exact proposal revision+content_hash, reject produces
zero execution, and a duplicate decision can never create a second logical
execution.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from hisiem_soc_copilot.application.commands.response import (
    CreateResponseProposal,
    DecideResponseApproval,
)
from hisiem_soc_copilot.application.handlers.response import ResponseCommandHandler
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.entities import (
    InvestigationResult,
    Verdict,
)
from hisiem_soc_copilot.domain.investigation.enums import VerdictDisposition
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from hisiem_soc_copilot.domain.response.actions import ResponseActionUnsupportedError
from hisiem_soc_copilot.domain.response.enums import (
    ApprovalDecisionKind,
    ResponseProposalStatus,
)
from hisiem_soc_copilot.domain.response.errors import (
    ApprovalContractError,
    ApprovalDecisionAlreadyExistsError,
)
from hisiem_soc_copilot.domain.shared.errors import DomainError
from tests.fixtures.fakes import FakeUnitOfWorkFactory

TENANT = "tenant-a"
ALERT = "alert-0001"
T0 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)
PLAYBOOK_ID = "11111111-2222-3333-4444-555555555555"


def _target() -> ExternalResourceRef:
    return ExternalResourceRef(
        provider="hisiem", resource_type="alert", address_id=ALERT, business_id="AL-1"
    )


async def _seed(
    factory: FakeUnitOfWorkFactory,
    *,
    disposition: VerdictDisposition = VerdictDisposition.MALICIOUS,
) -> UUID:
    investigation_id = uuid4()
    inv = Investigation.create(
        id=investigation_id,
        tenant_id=TENANT,
        source_alert_ref=_target(),
        initiated_by=ActorRef(subject_id="analyst", tenant_id=TENANT),
        budget_limits=BudgetLimits(),
        now=T0,
    )
    inv.start(actor=ActorRef(subject_id="analyst", tenant_id=TENANT), now=T0)
    inv.complete_without_response()
    result = InvestigationResult(
        id=uuid4(),
        investigation_id=investigation_id,
        verdict=Verdict(
            disposition=disposition, summary="s", confidence=0.8
        ),
        finding_ids=[],
        created_at=T0,
    )
    uow = factory()
    await uow.investigations.add(inv)
    await uow.results.add(result)
    return investigation_id


def _handler(factory: FakeUnitOfWorkFactory) -> ResponseCommandHandler:
    return ResponseCommandHandler(unit_of_work_factory=factory)


async def _create(factory: FakeUnitOfWorkFactory, investigation_id: UUID):
    return await _handler(factory).create_response_proposal(
        CreateResponseProposal(
            tenant_id=TENANT,
            investigation_id=investigation_id,
            action_key="START_SOAR_PLAYBOOK",
            target_ref=_target(),
            evidence_ids=(uuid4(),),
            parameters={"playbook_id": PLAYBOOK_ID},
            reason="contain the intrusion",
            initiated_by_subject="analyst",
        )
    )


async def test_create_proposal_requires_approval() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id = await _seed(factory)

    proposal = await _create(factory, investigation_id)

    assert proposal.status == ResponseProposalStatus.WAITING_APPROVAL
    assert proposal.policy_decision is not None
    assert proposal.policy_decision.value == "REQUIRE_APPROVAL"
    assert proposal.approval_request_id is not None
    # investigation links the proposal (no status mutation — COMPLETED is terminal)
    inv = await factory().investigations.get(
        tenant_id=TENANT, investigation_id=investigation_id
    )
    assert inv is not None and inv.response_proposal_id == proposal.id
    request = await factory().response_approvals.get_request_by_proposal(
        tenant_id=TENANT, proposal_id=proposal.id
    )
    assert request is not None
    assert request.proposal_content_revision == proposal.content_revision
    assert request.proposal_content_hash == proposal.content_hash
    kinds = {e.event_type for e in factory.events.events}
    assert {
        "response_proposal_created",
        "response_policy_decided",
        "response_approval_requested",
    } <= kinds


async def test_create_proposal_is_idempotent_per_investigation() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id = await _seed(factory)
    first = await _create(factory, investigation_id)
    second = await _create(factory, investigation_id)
    assert first.id == second.id


async def test_unsupported_action_is_rejected() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id = await _seed(factory)
    with pytest.raises(ResponseActionUnsupportedError):
        await _handler(factory).create_response_proposal(
            CreateResponseProposal(
                tenant_id=TENANT,
                investigation_id=investigation_id,
                action_key="BLOCK_SOURCE_IP",
                target_ref=_target(),
                evidence_ids=(uuid4(),),
                parameters={"ip": "203.0.113.9"},
                reason="block attacker",
                initiated_by_subject="analyst",
            )
        )


async def test_missing_required_parameter_is_rejected() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id = await _seed(factory)
    with pytest.raises(DomainError):
        await _handler(factory).create_response_proposal(
            CreateResponseProposal(
                tenant_id=TENANT,
                investigation_id=investigation_id,
                action_key="START_SOAR_PLAYBOOK",
                target_ref=_target(),
                evidence_ids=(uuid4(),),
                parameters={},
                reason="missing playbook id",
                initiated_by_subject="analyst",
            )
        )


async def test_inconclusive_verdict_is_policy_denied_without_approval() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id = await _seed(
        factory, disposition=VerdictDisposition.INCONCLUSIVE
    )
    proposal = await _create(factory, investigation_id)
    assert proposal.status == ResponseProposalStatus.DENIED
    request = await factory().response_approvals.get_request_by_proposal(
        tenant_id=TENANT, proposal_id=proposal.id
    )
    assert request is None


async def test_approve_binds_exact_contract_and_queues_one_execution() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id = await _seed(factory)
    proposal = await _create(factory, investigation_id)
    request = await factory().response_approvals.get_request_by_proposal(
        tenant_id=TENANT, proposal_id=proposal.id
    )
    assert request is not None

    outcome = await _handler(factory).decide_response_approval(
        DecideResponseApproval(
            tenant_id=TENANT,
            approval_request_id=request.id,
            decision=ApprovalDecisionKind.APPROVE,
            expected_revision=proposal.content_revision,
            expected_content_hash=proposal.content_hash,
            initiated_by_subject="operator",
        )
    )
    assert outcome.execution_queued is True
    assert outcome.proposal.status == ResponseProposalStatus.APPROVED

    execution = await factory().response_executions.get_by_proposal(
        tenant_id=TENANT, proposal_id=proposal.id
    )
    assert execution is not None and execution.status == "QUEUED"
    queued = [
        e for e in factory.events.events if e.event_type == "response_execution_queued"
    ]
    assert len(queued) == 1
    decisions = await factory().response_approvals.get_decision(
        tenant_id=TENANT, approval_request_id=request.id
    )
    assert decisions is not None and decisions.actor_subject_id == "operator"


async def test_stale_revision_is_contract_mismatch() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id = await _seed(factory)
    proposal = await _create(factory, investigation_id)
    request = await factory().response_approvals.get_request_by_proposal(
        tenant_id=TENANT, proposal_id=proposal.id
    )
    assert request is not None
    with pytest.raises(ApprovalContractError):
        await _handler(factory).decide_response_approval(
            DecideResponseApproval(
                tenant_id=TENANT,
                approval_request_id=request.id,
                decision=ApprovalDecisionKind.APPROVE,
                expected_revision=proposal.content_revision + 1,
                expected_content_hash=proposal.content_hash,
                initiated_by_subject="operator",
            )
        )


async def test_stale_hash_is_contract_mismatch() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id = await _seed(factory)
    proposal = await _create(factory, investigation_id)
    request = await factory().response_approvals.get_request_by_proposal(
        tenant_id=TENANT, proposal_id=proposal.id
    )
    assert request is not None
    with pytest.raises(ApprovalContractError):
        await _handler(factory).decide_response_approval(
            DecideResponseApproval(
                tenant_id=TENANT,
                approval_request_id=request.id,
                decision=ApprovalDecisionKind.APPROVE,
                expected_revision=proposal.content_revision,
                expected_content_hash="deadbeef",
                initiated_by_subject="operator",
            )
        )


async def test_reject_records_decision_and_zero_execution() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id = await _seed(factory)
    proposal = await _create(factory, investigation_id)
    request = await factory().response_approvals.get_request_by_proposal(
        tenant_id=TENANT, proposal_id=proposal.id
    )
    assert request is not None

    outcome = await _handler(factory).decide_response_approval(
        DecideResponseApproval(
            tenant_id=TENANT,
            approval_request_id=request.id,
            decision=ApprovalDecisionKind.REJECT,
            expected_revision=proposal.content_revision,
            expected_content_hash=proposal.content_hash,
            initiated_by_subject="operator",
            reason="insufficient evidence",
        )
    )
    assert outcome.proposal.status == ResponseProposalStatus.REJECTED
    assert outcome.execution_queued is False
    execution = await factory().response_executions.get_by_proposal(
        tenant_id=TENANT, proposal_id=proposal.id
    )
    assert execution is None
    assert not [
        e for e in factory.events.events if e.event_type == "response_execution_queued"
    ]


async def test_conflicting_second_decision_is_rejected() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id = await _seed(factory)
    proposal = await _create(factory, investigation_id)
    request = await factory().response_approvals.get_request_by_proposal(
        tenant_id=TENANT, proposal_id=proposal.id
    )
    assert request is not None
    handler = _handler(factory)
    await handler.decide_response_approval(
        DecideResponseApproval(
            tenant_id=TENANT,
            approval_request_id=request.id,
            decision=ApprovalDecisionKind.APPROVE,
            expected_revision=proposal.content_revision,
            expected_content_hash=proposal.content_hash,
            initiated_by_subject="operator",
        )
    )
    with pytest.raises(ApprovalDecisionAlreadyExistsError):
        await handler.decide_response_approval(
            DecideResponseApproval(
                tenant_id=TENANT,
                approval_request_id=request.id,
                decision=ApprovalDecisionKind.REJECT,
                expected_revision=proposal.content_revision,
                expected_content_hash=proposal.content_hash,
                initiated_by_subject="operator-2",
            )
        )


async def test_duplicate_approve_is_idempotent_and_creates_one_execution() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id = await _seed(factory)
    proposal = await _create(factory, investigation_id)
    request = await factory().response_approvals.get_request_by_proposal(
        tenant_id=TENANT, proposal_id=proposal.id
    )
    assert request is not None
    handler = _handler(factory)
    command = DecideResponseApproval(
        tenant_id=TENANT,
        approval_request_id=request.id,
        decision=ApprovalDecisionKind.APPROVE,
        expected_revision=proposal.content_revision,
        expected_content_hash=proposal.content_hash,
        initiated_by_subject="operator",
    )
    await handler.decide_response_approval(command)
    outcome = await handler.decide_response_approval(command)  # exact retry
    assert outcome.execution_queued is True
    queued = [
        e for e in factory.events.events if e.event_type == "response_execution_queued"
    ]
    assert len(queued) == 1  # no second logical execution
