"""Durable response-execution worker tests (P2).

The worker consumes a ``response_execution_queued`` delivery and performs the ONE
approved side effect through the SoarPort. These tests prove: it reloads the
persisted approved contract (never a queue payload), it is idempotent, it maps
provider outcomes correctly, it retries transient failures, and — structurally —
it can never call the LLM.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from hisiem_soc_copilot.application.commands.response import (
    CreateResponseProposal,
    DecideResponseApproval,
)
from hisiem_soc_copilot.application.errors import ExternalServiceError
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
from hisiem_soc_copilot.domain.response.enums import (
    ApprovalDecisionKind,
    ResponseProposalStatus,
)
from hisiem_soc_copilot.domain.response.value_objects import ResponseExecutionRef
from hisiem_soc_copilot.infrastructure.durable.investigation_runner import (
    NonRetryableRunError,
)
from hisiem_soc_copilot.infrastructure.durable.response_runner import (
    ResponseExecutionRunner,
)
from tests.fixtures.fakes import FakeSoar, FakeUnitOfWorkFactory

TENANT = "tenant-a"
ALERT = "alert-0001"
T0 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)
PLAYBOOK_ID = "11111111-2222-3333-4444-555555555555"


def _target() -> ExternalResourceRef:
    return ExternalResourceRef(
        provider="hisiem", resource_type="alert", address_id=ALERT, business_id="AL-1"
    )


async def _seed_investigation(factory: FakeUnitOfWorkFactory) -> UUID:
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
        verdict=Verdict(disposition=VerdictDisposition.MALICIOUS, summary="s", confidence=0.8),
        finding_ids=[],
        created_at=T0,
    )
    uow = factory()
    await uow.investigations.add(inv)
    await uow.results.add(result)
    return investigation_id


async def _queued_approved(
    factory: FakeUnitOfWorkFactory,
) -> tuple[UUID, UUID]:
    """Drive create→approve; returns (proposal_id, approval_request_id)."""
    investigation_id = await _seed_investigation(factory)
    handler = ResponseCommandHandler(unit_of_work_factory=factory)
    proposal = await handler.create_response_proposal(
        CreateResponseProposal(
            tenant_id=TENANT,
            investigation_id=investigation_id,
            action_key="START_SOAR_PLAYBOOK",
            target_ref=_target(),
            evidence_ids=(uuid4(),),
            parameters={"playbook_id": PLAYBOOK_ID},
            reason="contain",
            initiated_by_subject="analyst",
        )
    )
    request = await factory().response_approvals.get_request_by_proposal(
        tenant_id=TENANT, proposal_id=proposal.id
    )
    assert request is not None
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
    return proposal.id, request.id


def _runner(factory: FakeUnitOfWorkFactory, soar: FakeSoar) -> ResponseExecutionRunner:
    return ResponseExecutionRunner(
        unit_of_work_factory=factory, soar=soar, poll_interval_seconds=0.0
    )


async def test_runner_submits_persisted_approved_contract_and_settles() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal_id, _ = await _queued_approved(factory)
    soar = FakeSoar(submit_result=None)  # defaults to SUCCEEDED

    await _runner(factory, soar).run(
        aggregate_id=str(proposal_id), tenant_id=TENANT
    )

    assert len(soar.submitted) == 1
    call = soar.submitted[0]
    assert call["action_key"] == "START_SOAR_PLAYBOOK"
    assert call["parameters"] == {"playbook_id": PLAYBOOK_ID}
    assert call["submission_key"] == f"response:{TENANT}:{proposal_id}"

    execution = await factory().response_executions.get_by_proposal(
        tenant_id=TENANT, proposal_id=proposal_id
    )
    assert execution is not None and execution.status == "SUCCEEDED"
    assert execution.finished_at is not None
    kinds = [e.event_type for e in factory.events.events]
    assert "response_execution_succeeded" in kinds


async def test_runner_missing_proposal_is_a_noop() -> None:
    factory = FakeUnitOfWorkFactory()
    soar = FakeSoar()
    await _runner(factory, soar).run(aggregate_id=str(uuid4()), tenant_id=TENANT)
    assert soar.submitted == []


async def test_runner_terminal_execution_is_a_noop() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal_id, _ = await _queued_approved(factory)
    # Mark the execution terminal as if a prior delivery already settled it.
    uow = factory()
    execu = await uow.response_executions.get_by_proposal(
        tenant_id=TENANT, proposal_id=proposal_id
    )
    assert execu is not None
    await uow.response_executions.update(
        ResponseExecutionRef(
            proposal_id=execu.proposal_id,
            provider=execu.provider,
            execution_id="hisiem-exec-done",
            submission_key=execu.submission_key,
            status="SUCCEEDED",
            submitted_at=execu.submitted_at,
            last_observed_at=execu.last_observed_at,
            finished_at=execu.last_observed_at,
        )
    )
    soar = FakeSoar()
    await _runner(factory, soar).run(aggregate_id=str(proposal_id), tenant_id=TENANT)
    assert soar.submitted == []  # never re-submits a settled execution


async def test_runner_non_approved_proposal_is_nonretryable() -> None:
    factory = FakeUnitOfWorkFactory()
    # A proposal still WAITING_APPROVAL with an execution ref must never execute.
    investigation_id = await _seed_investigation(factory)
    handler = ResponseCommandHandler(unit_of_work_factory=factory)
    proposal = await handler.create_response_proposal(
        CreateResponseProposal(
            tenant_id=TENANT,
            investigation_id=investigation_id,
            action_key="START_SOAR_PLAYBOOK",
            target_ref=_target(),
            evidence_ids=(uuid4(),),
            parameters={"playbook_id": PLAYBOOK_ID},
            reason="contain",
            initiated_by_subject="analyst",
        )
    )
    assert proposal.status == ResponseProposalStatus.WAITING_APPROVAL
    await factory().response_executions.add(
        ResponseExecutionRef(
            proposal_id=proposal.id,
            provider="hisiem",
            execution_id="",
            submission_key="response:x:y",
            status="QUEUED",
            submitted_at=T0,
            last_observed_at=T0,
        )
    )
    soar = FakeSoar()
    with pytest.raises(NonRetryableRunError):
        await _runner(factory, soar).run(
            aggregate_id=str(proposal.id), tenant_id=TENANT
        )
    assert soar.submitted == []


async def test_runner_definitive_provider_error_marks_failed() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal_id, _ = await _queued_approved(factory)
    soar = FakeSoar(
        raise_on_submit=ExternalServiceError(
            "bad target", service="hisiem", code="HTTP_422"
        )
    )
    await _runner(factory, soar).run(aggregate_id=str(proposal_id), tenant_id=TENANT)

    execution = await factory().response_executions.get_by_proposal(
        tenant_id=TENANT, proposal_id=proposal_id
    )
    assert execution is not None and execution.status == "FAILED"
    assert execution.safe_error_code == "HTTP_422"
    assert "response_execution_failed" in {e.event_type for e in factory.events.events}


async def test_runner_transient_provider_error_is_reraised_for_retry() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal_id, _ = await _queued_approved(factory)
    soar = FakeSoar(
        raise_on_submit=ExternalServiceError(
            "unavailable", service="hisiem", code="HTTP_503"
        )
    )
    with pytest.raises(ExternalServiceError):
        await _runner(factory, soar).run(
            aggregate_id=str(proposal_id), tenant_id=TENANT
        )
    # Not settled as FAILED — the dispatcher will retry with backoff.
    execution = await factory().response_executions.get_by_proposal(
        tenant_id=TENANT, proposal_id=proposal_id
    )
    assert execution is not None and execution.status == "QUEUED"


async def test_runner_polls_until_terminal() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal_id, _ = await _queued_approved(factory)
    from hisiem_soc_copilot.application.ports.soar import SoarExecutionResult

    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id="hisiem-exec-1", status="RUNNING"),
        status_sequence=["RUNNING", "SUCCEEDED"],
    )
    await _runner(factory, soar).run(aggregate_id=str(proposal_id), tenant_id=TENANT)

    assert soar.status_calls == ["hisiem-exec-1", "hisiem-exec-1"]
    execution = await factory().response_executions.get_by_proposal(
        tenant_id=TENANT, proposal_id=proposal_id
    )
    assert execution is not None and execution.status == "SUCCEEDED"


async def test_runner_has_no_model_dependency() -> None:
    """Structural proof the worker cannot call the LLM (spec §16)."""
    params = set(inspect.signature(ResponseExecutionRunner.__init__).parameters)
    assert params == {
        "self",
        "unit_of_work_factory",
        "soar",
        "max_poll_attempts",
        "poll_interval_seconds",
    }
