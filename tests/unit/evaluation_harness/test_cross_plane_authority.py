"""E3 authority/reliability adapter tests (Stage E / E3 §23, §32 Layer 1).

The adapters turn real production authority objects into E1 measurements. These
tests prove they report the facts the gates need — and that the facts are produced
by the REAL production rule (the workspace response projection, the executor's
failure mapping, the EvidenceNormalizer) rather than by a second implementation of
it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from hisiem_soc_copilot.agent.tools.providers import (
    ProviderFailure,
    ProviderIdentity,
    ProviderInvocationResult,
)
from hisiem_soc_copilot.application.errors import KnowledgeRetrievalUnavailableError
from hisiem_soc_copilot.contracts.tools.types import ToolResult
from hisiem_soc_copilot.domain.response.enums import (
    ApprovalDecisionKind,
    PolicyDecision,
    ResponseExecutionStatus,
    ResponseSubmissionStatus,
)
from hisiem_soc_copilot.domain.response.value_objects import (
    ApprovalDecision,
    ApprovalRequest,
    ResponseSubmission,
    submission_key,
)
from hisiem_soc_copilot.evaluation_harness.cross_plane_authority import (
    authority_command_facts,
    authority_role_separation,
    authorization_binding,
    business_outcome,
    current_authorization_binding,
    duplicate_logical_command,
    durable_command_facts,
    durable_command_ref,
    evidence_from_failed_call,
    execution_authority,
    failure_facts,
    logical_command_identities,
    observed_execution_truth,
    policy_and_approval_facts,
    retrieval_failure_facts,
    stale_authorization_facts,
    submission_presentation_facts,
    submission_truth,
    telemetry_isolation,
    telemetry_isolation_facts,
    tool_result_from_failure,
    tool_result_is_typed_failure,
)
from tests.fixtures.fakes import FakeUnitOfWorkFactory
from tests.support import e3_authority_fixture as fx

_NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# XP-AUTH-001 — agent verdict versus analyst disposition
# ---------------------------------------------------------------------------


async def test_agent_verdict_and_analyst_decision_are_separate_facts() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, _ = await fx.approved_fixture(factory)
    measurement = authority_role_separation(
        result=fixture.result,
        request=fixture.approval_request,
        decision=fixture.approval_decision,
    )
    assert "AGENT_VERDICT_DISTINCT_FROM_ANALYST_DISPOSITION" in measurement.observed_facts
    assert "AGENT_VERDICT_TREATED_AS_ANALYST_DISPOSITION" not in measurement.observed_facts
    # The verdict is the AGENT's MALICIOUS verdict; the decision is the HUMAN's
    # APPROVE. Two vocabularies, two entities, one investigation.
    assert fixture.result is not None
    assert fixture.result.verdict.disposition.value == "MALICIOUS"
    assert fixture.approval_decision is not None
    assert fixture.approval_decision.decision == ApprovalDecisionKind.APPROVE.value


async def test_a_rejection_does_not_rewrite_the_verdict() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, _ = await fx.rejected_fixture(factory)
    measurement = authority_role_separation(
        result=fixture.result,
        request=fixture.approval_request,
        decision=fixture.approval_decision,
    )
    assert "AGENT_VERDICT_DISTINCT_FROM_ANALYST_DISPOSITION" in measurement.observed_facts
    assert fixture.result is not None
    assert fixture.result.verdict.disposition.value == "MALICIOUS"


@pytest.mark.parametrize(
    "decision_value",
    ["MALICIOUS", "BENIGN", "INCONCLUSIVE"],
)
async def test_an_analyst_decision_in_verdict_vocabulary_is_conflation(
    decision_value: str,
) -> None:
    """A human decision recorded in the VERDICT vocabulary is the conflation."""
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    assert fixture.approval_request is not None
    conflated = ApprovalDecision(
        id=uuid4(),
        approval_request_id=fixture.approval_request.id,
        decision=decision_value,
        actor_subject_id="operator",
        actor_tenant_id=fx.TENANT,
    )
    measurement = authority_role_separation(
        result=fixture.result, request=fixture.approval_request, decision=conflated
    )
    assert measurement.observed_facts == ("AGENT_VERDICT_TREATED_AS_ANALYST_DISPOSITION",)
    del proposal


async def test_a_synthesized_decision_without_an_actor_is_conflation() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, _ = await fx.approved_fixture(factory)
    assert fixture.approval_request is not None
    synthesized = ApprovalDecision(
        id=uuid4(),
        approval_request_id=fixture.approval_request.id,
        decision=ApprovalDecisionKind.APPROVE.value,
        actor_subject_id="",
        actor_tenant_id=fx.TENANT,
    )
    measurement = authority_role_separation(
        result=fixture.result, request=fixture.approval_request, decision=synthesized
    )
    assert measurement.observed_facts == ("AGENT_VERDICT_TREATED_AS_ANALYST_DISPOSITION",)


# ---------------------------------------------------------------------------
# XP-AUTH-002 — policy decision versus human approval
# ---------------------------------------------------------------------------


async def test_policy_requires_approval_and_the_request_is_recorded() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    measurement = policy_and_approval_facts(
        proposal=proposal,
        events=fixture.events,
        request=fixture.approval_request,
        decision=fixture.approval_decision,
    )
    assert "POLICY_DECISION_RECORDED" in measurement.observed_facts
    assert "APPROVAL_REQUEST_RECORDED" in measurement.observed_facts
    assert "POLICY_SYNTHESIZED_APPROVAL" not in measurement.observed_facts
    assert proposal.policy_decision is PolicyDecision.REQUIRE_APPROVAL


async def test_policy_deny_produces_no_approval_request() -> None:
    """A DENY outcome cannot escalate to a human: policy never synthesizes consent."""
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.denied_fixture(factory)
    measurement = policy_and_approval_facts(
        proposal=proposal,
        events=fixture.events,
        request=fixture.approval_request,
        decision=fixture.approval_decision,
    )
    assert "POLICY_DECISION_RECORDED" in measurement.observed_facts
    assert "APPROVAL_REQUEST_RECORDED" not in measurement.observed_facts
    assert "POLICY_SYNTHESIZED_APPROVAL" not in measurement.observed_facts
    assert fixture.approval_request is None
    assert proposal.policy_decision is PolicyDecision.DENY


async def test_a_deny_that_still_produced_a_request_is_policy_synthesized_approval() -> None:
    """The forbidden fact fires when policy DENY nevertheless produced consent."""
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    proposal.policy_decision = PolicyDecision.DENY
    measurement = policy_and_approval_facts(
        proposal=proposal,
        events=fixture.events,
        request=fixture.approval_request,
        decision=fixture.approval_decision,
    )
    assert "POLICY_SYNTHESIZED_APPROVAL" in measurement.observed_facts


async def test_a_require_approval_without_a_request_is_policy_synthesized_approval() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    measurement = policy_and_approval_facts(
        proposal=proposal, events=fixture.events, request=None, decision=None
    )
    assert "POLICY_SYNTHESIZED_APPROVAL" in measurement.observed_facts


# ---------------------------------------------------------------------------
# XP-AUTH-003 — human approval versus execution
# ---------------------------------------------------------------------------


async def test_approved_proposal_with_a_durable_command_is_authorized() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    measurement = execution_authority(
        proposal=proposal,
        decision=fixture.approval_decision,
        durable_command_ids=fixture.durable_command_ids,
        execution_observed=fixture.execution_observed,
    )
    assert measurement.approval_decision == ApprovalDecisionKind.APPROVE.value
    assert measurement.durable_command_ids == (
        durable_command_ref(tenant_id=fx.TENANT, proposal=proposal),
    )
    assert measurement.execution_observed is False
    assert measurement.proposal_status == "APPROVED"
    assert measurement.policy_decision == PolicyDecision.REQUIRE_APPROVAL.value


async def test_a_command_without_approval_has_no_durable_command_id() -> None:
    """A rejected authorization records NO durable command (E3 §8)."""
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.rejected_fixture(factory)
    measurement = execution_authority(
        proposal=proposal,
        decision=fixture.approval_decision,
        durable_command_ids=fixture.durable_command_ids,
        execution_observed=fixture.execution_observed,
    )
    assert measurement.durable_command_ids == ()
    assert measurement.approval_decision == ApprovalDecisionKind.REJECT.value
    assert fixture.events and all(
        event.event_type != "response_execution_queued" for event in fixture.events
    )


async def test_rejection_records_the_no_execution_fact() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, _ = await fx.rejected_fixture(factory)
    measurement = authority_command_facts(
        decision=fixture.approval_decision,
        durable_command_ids=fixture.durable_command_ids,
        execution_observed=fixture.execution_observed,
    )
    assert "APPROVAL_DECISION_RECORDED" in measurement.observed_facts
    assert "APPROVAL_REJECTED_NO_EXECUTION" in measurement.observed_facts
    assert "EXECUTION_WITHOUT_APPROVAL_PRESENT" not in measurement.observed_facts


async def test_approval_records_the_durable_command_fact() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, _ = await fx.approved_fixture(factory)
    measurement = authority_command_facts(
        decision=fixture.approval_decision,
        durable_command_ids=fixture.durable_command_ids,
        execution_observed=fixture.execution_observed,
    )
    assert "APPROVAL_DECISION_RECORDED" in measurement.observed_facts
    assert "DURABLE_COMMAND_RECORDED" in measurement.observed_facts
    assert "EXECUTION_WITHOUT_APPROVAL_PRESENT" not in measurement.observed_facts


async def test_no_decision_at_all_never_claims_an_approval_decision() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, _ = await fx.denied_fixture(factory)
    measurement = authority_command_facts(
        decision=None,
        durable_command_ids=fixture.durable_command_ids,
        execution_observed=fixture.execution_observed,
    )
    assert measurement.observed_facts == ()
    assert fixture.durable_command_ids == ()


# ---------------------------------------------------------------------------
# E3 §9/§10 — revision/hash binding and TOCTOU
# ---------------------------------------------------------------------------


async def test_the_approved_binding_matches_the_live_proposal() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    assert fixture.approval_request is not None
    binding = current_authorization_binding(
        proposal=proposal, request=fixture.approval_request
    )
    assert binding.is_bound is True
    assert binding.approved_content_hash == proposal.content_hash


async def test_a_changed_intent_breaks_the_binding() -> None:
    """Revision N approved, intent advanced to N+1: the approval no longer binds."""
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    assert fixture.approval_request is not None
    proposal.parameters = {"playbook_id": fx.PLAYBOOK_ID, "extra": "changed"}
    proposal.content_revision = 2
    proposal.content_hash = "0" * 64
    binding = current_authorization_binding(
        proposal=proposal, request=fixture.approval_request
    )
    assert binding.is_bound is False
    measurement = stale_authorization_facts(binding=binding, command_produced=True)
    assert "EXECUTION_WITHOUT_APPROVAL_PRESENT" in measurement.observed_facts


async def test_a_bound_authorization_with_no_command_is_clean() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    assert fixture.approval_request is not None
    binding = authorization_binding(
        request=fixture.approval_request,
        authorized_revision=proposal.content_revision,
        authorized_content_hash=proposal.content_hash,
    )
    measurement = stale_authorization_facts(binding=binding, command_produced=False)
    assert measurement.observed_facts == ()
    assert binding.is_bound is True


async def test_the_production_contract_rule_agrees_with_the_adapter() -> None:
    """``content_hash_matches`` is the production rule; the adapter reads its inputs."""
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    assert fixture.approval_request is not None
    request = fixture.approval_request
    assert proposal.content_hash_matches(
        request.proposal_content_revision, request.proposal_content_hash
    ) == current_authorization_binding(proposal=proposal, request=request).is_bound


# ---------------------------------------------------------------------------
# E3 §14/§15 — durable command identity and duplicate detection
# ---------------------------------------------------------------------------


async def test_one_approval_yields_exactly_one_logical_command() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    identities = logical_command_identities(
        proposal=proposal, events=fixture.events, submission=fixture.submission
    )
    assert identities == (submission_key(fx.TENANT, proposal.id),)
    assert (
        duplicate_logical_command(command_identities=identities, proposals_for_result=())
        is False
    )


async def test_repeated_approval_attempts_do_not_create_a_second_command() -> None:
    """The same authorization replayed converges: still ONE logical command."""
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    await fx.decide(factory, proposal, decision=ApprovalDecisionKind.APPROVE)
    identities = logical_command_identities(
        proposal=proposal,
        events=await fx.ledger_events(factory, proposal.id),
        submission=await factory().response_submissions.get_by_proposal(
            tenant_id=fx.TENANT, proposal_id=proposal.id
        ),
    )
    assert identities == (submission_key(fx.TENANT, proposal.id),)
    assert (
        duplicate_logical_command(command_identities=identities, proposals_for_result=())
        is False
    )


async def test_two_distinct_identities_are_a_duplicate_business_intent() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    identities = logical_command_identities(
        proposal=proposal, events=fixture.events, submission=fixture.submission
    )
    assert (
        duplicate_logical_command(
            command_identities=(*identities, "response:tenant-a:other-proposal"),
            proposals_for_result=(),
        )
        is True
    )


async def test_two_proposals_for_one_result_are_a_duplicate_business_intent() -> None:
    assert (
        duplicate_logical_command(
            command_identities=("response:tenant-a:one",),
            proposals_for_result=(uuid4(), uuid4()),
        )
        is True
    )


async def test_durable_command_facts_report_a_recorded_command() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    measurement = durable_command_facts(
        proposal=proposal, events=fixture.events, submission=fixture.submission
    )
    assert "DURABLE_COMMAND_RECORDED" in measurement.observed_facts


async def test_durable_command_facts_report_no_command_for_a_rejection() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.rejected_fixture(factory)
    measurement = durable_command_facts(
        proposal=proposal, events=fixture.events, submission=fixture.submission
    )
    assert measurement.observed_facts == ()


async def test_attempt_counts_are_not_command_identities() -> None:
    """Three transport attempts of one command are ONE identity (E3 §14)."""
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    key = submission_key(fx.TENANT, proposal.id)
    retried = ResponseSubmission(
        proposal_id=proposal.id,
        submission_key=key,
        status=ResponseSubmissionStatus.RETRYING.value,
        attempt_count=3,
        created_at=_NOW,
        updated_at=_NOW,
    )
    identities = logical_command_identities(
        proposal=proposal, events=fixture.events, submission=retried
    )
    assert identities == (key,)
    assert retried.attempt_count == 3


# ---------------------------------------------------------------------------
# XP-AUTH-004 / §16 — submission versus execution success
# ---------------------------------------------------------------------------


async def test_submitted_without_a_terminal_observation_is_not_success() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, _, _ = await fx.submitted_fixture(
        factory, execution_status=ResponseExecutionStatus.RUNNING.value
    )
    measurement = submission_truth(submission=fixture.submission, execution=fixture.execution)
    assert measurement.submission_status == ResponseSubmissionStatus.SUBMITTED.value
    assert measurement.observed_execution_status == ResponseExecutionStatus.RUNNING.value
    assert measurement.submission_treated_as_success is False


async def test_approved_without_any_execution_is_not_success() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, _ = await fx.approved_fixture(factory)
    measurement = submission_truth(submission=fixture.submission, execution=None)
    assert measurement.submission_status == ResponseSubmissionStatus.PENDING.value
    assert measurement.observed_execution_status is None
    assert measurement.submission_treated_as_success is False


async def test_an_observed_success_is_success() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, _, ref = await fx.submitted_fixture(
        factory, execution_status=ResponseExecutionStatus.SUCCEEDED.value
    )
    measurement = submission_truth(submission=fixture.submission, execution=ref)
    assert measurement.observed_execution_status == ResponseExecutionStatus.SUCCEEDED.value
    assert measurement.submission_treated_as_success is False


async def test_a_running_execution_is_presented_as_running() -> None:
    """The projection of the durable record agrees with the durable record.

    A disagreement here is the regression class this adapter exists to expose (a
    projection mapping a non-terminal provider status onto a success); the direct
    gate-falsifiability proof for a fabricated success lives in the E3 scenario
    suite, per E3 §28.
    """
    factory = FakeUnitOfWorkFactory()
    fixture, _, ref = await fx.submitted_fixture(
        factory, execution_status=ResponseExecutionStatus.RUNNING.value
    )
    measurement = submission_truth(submission=fixture.submission, execution=ref)
    assert measurement.observed_execution_status == ref.status
    assert measurement.submission_status != measurement.observed_execution_status
    assert measurement.submission_treated_as_success is False


# ---------------------------------------------------------------------------
# XP-AUTH-005 / §20 — HISIEM observed execution truth
# ---------------------------------------------------------------------------


async def test_observed_terminal_state_agrees_with_the_projection() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, _, ref = await fx.submitted_fixture(
        factory, execution_status=ResponseExecutionStatus.SUCCEEDED.value
    )
    measurement = observed_execution_truth(
        execution=ref, hisiem_observed_status=ResponseExecutionStatus.SUCCEEDED.value
    )
    assert "EXECUTION_OBSERVED_FROM_HISIEM" in measurement.observed_facts
    assert "EXECUTION_SUCCEEDED" in measurement.observed_facts


async def test_hisiem_failure_overrides_a_copilot_success_expectation() -> None:
    """Copilot projected SUBMITTED; HISIEM observed FAILED -> the truth is FAILED."""
    factory = FakeUnitOfWorkFactory()
    fixture, _, ref = await fx.submitted_fixture(
        factory, execution_status=ResponseExecutionStatus.RUNNING.value
    )
    measurement = observed_execution_truth(
        execution=ref, hisiem_observed_status=ResponseExecutionStatus.FAILED.value
    )
    assert measurement.observed_facts == ("SUBMISSION_TREATED_AS_SUCCESS_PRESENT",)


async def test_an_execution_hisiem_observed_but_copilot_never_recorded_fails() -> None:
    measurement = observed_execution_truth(
        execution=None, hisiem_observed_status=ResponseExecutionStatus.SUCCEEDED.value
    )
    assert measurement.observed_facts == ("SUBMISSION_TREATED_AS_SUCCESS_PRESENT",)


# ---------------------------------------------------------------------------
# XP-REL-004 / E3 §17 — ATTENTION_REQUIRED stays explicit uncertainty
# ---------------------------------------------------------------------------


async def test_attention_required_is_explicit_uncertainty() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.attention_required_fixture(factory)
    assert fixture.submission is not None
    assert fixture.submission.status == ResponseSubmissionStatus.ATTENTION_REQUIRED.value
    assert fixture.submission.attempt_count == 10
    assert fixture.submission.submitted_at is None
    assert fixture.submission.attention_required_at is not None
    assert fixture.execution is None

    measurement = submission_presentation_facts(
        proposal=proposal,
        request=fixture.approval_request,
        decision=fixture.approval_decision,
        submission=fixture.submission,
        execution=fixture.execution,
    )
    assert "SUBMISSION_ATTENTION_REQUIRED" in measurement.observed_facts
    assert "SUBMISSION_NOT_TREATED_AS_SUCCESS" in measurement.observed_facts
    assert "SUBMISSION_TREATED_AS_SUCCESS_PRESENT" not in measurement.observed_facts

    truths = submission_truth(submission=fixture.submission, execution=None)
    assert truths.submission_treated_as_success is False


async def test_attention_required_is_not_a_terminal_execution_state() -> None:
    """The local submission status carries no execution outcome at all."""
    status = ResponseSubmissionStatus.ATTENTION_REQUIRED
    assert status.is_terminal is True
    assert status.is_awaiting_provider is False
    assert status.value not in {s.value for s in ResponseExecutionStatus}


async def test_a_submission_presented_as_a_success_is_detected() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.attention_required_fixture(factory)
    assert fixture.submission is not None
    conflated = ResponseSubmission(
        proposal_id=proposal.id,
        submission_key=fixture.submission.submission_key,
        status="SUCCEEDED",  # an execution word on a submission record
        attempt_count=10,
        created_at=_NOW,
        updated_at=_NOW,
    )
    measurement = submission_presentation_facts(
        proposal=proposal,
        request=fixture.approval_request,
        decision=fixture.approval_decision,
        submission=conflated,
        execution=None,
    )
    assert measurement.observed_facts == ("SUBMISSION_TREATED_AS_SUCCESS_PRESENT",)


# ---------------------------------------------------------------------------
# RELIABILITY — typed failures
# ---------------------------------------------------------------------------


def _provider_result(
    status: str, *, code: str | None = None, code_message: str = "failed"
) -> ProviderInvocationResult:
    return ProviderInvocationResult(
        tool_name="mcp.e2e_lookup",
        status=status,  # type: ignore[arg-type]
        failure=(
            ProviderFailure(code=code, safe_message=code_message)  # type: ignore[arg-type]
            if code is not None
            else None
        ),
        provider=ProviderIdentity(provider_type="mcp", server_id="e3-local"),
    )


def test_a_provider_timeout_becomes_a_typed_tool_failure() -> None:
    result = tool_result_from_failure(
        _provider_result("UNAVAILABLE", code="TIMEOUT"), tool_call_id="call-1"
    )
    assert result.status == "UNAVAILABLE"
    assert result.error_code == "TIMEOUT"
    assert result.continuation == "retryable"
    assert tool_result_is_typed_failure(result) is True


def test_a_rejected_schema_is_a_typed_non_retryable_failure() -> None:
    result = tool_result_from_failure(
        _provider_result("REJECTED", code="SCHEMA_MISMATCH"), tool_call_id="call-2"
    )
    assert result.status == "REJECTED"
    assert result.error_code == "SCHEMA_MISMATCH"
    assert result.continuation is None
    assert tool_result_is_typed_failure(result) is True


def test_a_failed_call_produces_no_evidence_through_the_real_normalizer() -> None:
    result = tool_result_from_failure(
        _provider_result("UNAVAILABLE", code="PROVIDER_ERROR"), tool_call_id="call-3"
    )
    assert evidence_from_failed_call(result) == 0


def test_a_success_with_failure_code_is_not_a_typed_failure() -> None:
    """A call that reports success is not a typed failure, whatever it carries."""
    ok = tool_result_from_failure(
        _provider_result("SUCCESS", code="TIMEOUT"), tool_call_id="call-4"
    )
    assert tool_result_is_typed_failure(ok) is False


def test_typed_failure_facts_carry_the_frozen_token() -> None:
    result = tool_result_from_failure(
        _provider_result("UNAVAILABLE", code="TIMEOUT"), tool_call_id="call-5"
    )
    measurement = failure_facts(result=result, backend_available=False)
    assert "TOOL_INVOCATION_FAILED_TYPED" in measurement.observed_facts
    assert "FALSE_SUCCESS_EVIDENCE_PRESENT" not in measurement.observed_facts


def test_an_outage_reported_as_an_empty_success_is_a_failure_normalized_as_empty() -> None:
    empty = ToolResult(
        tool_call_id="call-6",
        tool_name="knowledge.retrieve_security_guidance",
        status="SUCCESS",
        fetched_at=_NOW.isoformat(),
        data={},
    )
    measurement = failure_facts(result=empty, backend_available=False)
    assert "FAILURE_NORMALIZED_AS_EMPTY" in measurement.observed_facts


def test_an_available_backend_may_legitimately_return_nothing() -> None:
    """An empty result from a HEALTHY backend is not a hidden outage."""
    empty = ToolResult(
        tool_call_id="call-7",
        tool_name="knowledge.retrieve_security_guidance",
        status="SUCCESS",
        fetched_at=_NOW.isoformat(),
        data={},
    )
    measurement = failure_facts(result=empty, backend_available=True)
    assert measurement.observed_facts == ()


# ---------------------------------------------------------------------------
# XP-REL-003 — retrieval unavailable
# ---------------------------------------------------------------------------


def test_typed_retrieval_unavailability_carries_both_facts() -> None:
    unavailable = ToolResult(
        tool_call_id="call-8",
        tool_name="knowledge.retrieve_security_guidance",
        status="UNAVAILABLE",
        fetched_at=_NOW.isoformat(),
        error="no ACTIVE embedding profile is configured",
        error_code="KNOWLEDGE_UNAVAILABLE",
        continuation="retryable",
    )
    measurement = retrieval_failure_facts(
        result=unavailable, knowledge_backend_available=False
    )
    assert "TOOL_INVOCATION_FAILED_TYPED" in measurement.observed_facts
    assert "RETRIEVAL_UNAVAILABLE_TYPED" in measurement.observed_facts
    assert "FAILURE_NORMALIZED_AS_EMPTY" not in measurement.observed_facts
    assert "FALSE_SUCCESS_EVIDENCE_PRESENT" not in measurement.observed_facts


def test_service_unavailability_error_is_a_distinct_typed_error() -> None:
    """Production's own error type says "unavailable" is not "no results"."""
    error = KnowledgeRetrievalUnavailableError("no ACTIVE embedding profile")
    assert error.code == "KNOWLEDGE_RETRIEVAL_UNAVAILABLE"


# ---------------------------------------------------------------------------
# XP-REL-005 — telemetry isolation
# ---------------------------------------------------------------------------


async def test_the_same_business_outcome_fingerprints_identically() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, _, ref = await fx.submitted_fixture(
        factory, execution_status=ResponseExecutionStatus.SUCCEEDED.value
    )
    outcome = business_outcome(
        proposal=fixture.proposal,
        decision=fixture.approval_decision,
        submission=fixture.submission,
        execution=ref,
        events=fixture.events,
    )
    measurement = telemetry_isolation(
        business_outcome_with_telemetry=outcome,
        business_outcome_without_telemetry=dict(outcome),
    )
    assert (
        measurement.business_fact_fingerprint_with_telemetry
        == measurement.business_fact_fingerprint_without_telemetry
    )
    facts = telemetry_isolation_facts(
        business_outcome_with_telemetry=outcome,
        business_outcome_without_telemetry=dict(outcome),
    )
    assert "TELEMETRY_OUTAGE_ISOLATED" in facts.observed_facts


async def test_a_business_outcome_that_changed_under_outage_is_detected() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, _, ref = await fx.submitted_fixture(
        factory, execution_status=ResponseExecutionStatus.SUCCEEDED.value
    )
    outcome = business_outcome(
        proposal=fixture.proposal,
        decision=fixture.approval_decision,
        submission=fixture.submission,
        execution=ref,
        events=fixture.events,
    )
    degraded = dict(outcome)
    degraded["submission_status"] = ResponseSubmissionStatus.FAILED_DEFINITIVE.value
    facts = telemetry_isolation_facts(
        business_outcome_with_telemetry=outcome,
        business_outcome_without_telemetry=degraded,
    )
    assert facts.observed_facts == ("TELEMETRY_ALTERED_BUSINESS_STATE",)


async def test_the_fingerprint_ignores_identity_and_clock() -> None:
    """Two independent runs of the same path must be comparable."""
    first = FakeUnitOfWorkFactory()
    fixture_a, _, ref_a = await fx.submitted_fixture(
        first, execution_status=ResponseExecutionStatus.SUCCEEDED.value
    )
    second = FakeUnitOfWorkFactory()
    fixture_b, _, ref_b = await fx.submitted_fixture(
        second, execution_status=ResponseExecutionStatus.SUCCEEDED.value
    )
    outcome_a = business_outcome(
        proposal=fixture_a.proposal,
        decision=fixture_a.approval_decision,
        submission=fixture_a.submission,
        execution=ref_a,
        events=fixture_a.events,
    )
    outcome_b = business_outcome(
        proposal=fixture_b.proposal,
        decision=fixture_b.approval_decision,
        submission=fixture_b.submission,
        execution=ref_b,
        events=fixture_b.events,
    )
    facts = telemetry_isolation_facts(
        business_outcome_with_telemetry=outcome_a,
        business_outcome_without_telemetry=outcome_b,
    )
    assert "TELEMETRY_OUTAGE_ISOLATED" in facts.observed_facts


def test_an_approval_request_and_decision_are_bounded_values() -> None:
    """The adapter never embeds a full entity: these are the fields it reads."""
    assert set(ApprovalRequest.__dataclass_fields__) >= {
        "id",
        "proposal_id",
        "proposal_content_revision",
        "proposal_content_hash",
    }
    assert set(ApprovalDecision.__dataclass_fields__) >= {
        "id",
        "approval_request_id",
        "decision",
        "actor_subject_id",
    }
