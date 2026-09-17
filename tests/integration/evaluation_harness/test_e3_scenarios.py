"""E3 end-to-end scenario execution (Stage E / E3 §4, §26, §27, §28, §33).

Every E3-owned XP-01 scenario is driven end to end:

```text
real persisted/domain authority facts + real provider failures
        -> E3 measurement adapter (cross_plane_authority)
        -> E1 typed measurement
        -> E1 deterministic hard gate (evaluate_scenario)
        -> cross-plane-gate-results/v1
```

The eight scenarios whose catalog entry declares ``deterministic`` must reach PASS
here — no network, no model, no database. ``XP-AUTH-005`` and ``XP-REL-005`` declare
``runtime-integrated`` and are driven from the E3 runtime slice against real
processes; their drivers are the same ones exercised here.

The negative half of §27/§28 then proves each authority/reliability invariant can
actually FAIL, so a green suite is not a vacuous one.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

import pytest

from hisiem_soc_copilot.agent.tools.providers import (
    AdmissionEntry,
    ProviderFailure,
    ProviderInvocationContext,
    ProviderInvocationResult,
    ResultBounds,
)
from hisiem_soc_copilot.contracts.tools.types import ToolCandidate, ToolResult
from hisiem_soc_copilot.domain.response.enums import (
    ApprovalDecisionKind,
    PolicyDecision,
    ResponseExecutionStatus,
)
from hisiem_soc_copilot.domain.response.value_objects import ApprovalDecision, ResponseSubmission
from hisiem_soc_copilot.evaluation.cross_plane import (
    GATE_EXECUTION_WITHOUT_APPROVAL,
    GATE_EXPECTED_FACTS_PRESENT,
    GATE_FORBIDDEN_FACTS_ABSENT,
    GATE_IDS,
    GATE_SUBMISSION_TREATED_AS_SUCCESS,
    GATE_TELEMETRY_CHANGED_BUSINESS_STATE,
    ExecutionAuthorityMeasurement,
    GateFamily,
    GateStatus,
    SubmissionTruthMeasurement,
    TelemetryIsolationMeasurement,
    evaluate_gate,
    evaluate_scenario,
    scenario,
)
from hisiem_soc_copilot.evaluation_harness import read_gate_results
from hisiem_soc_copilot.evaluation_harness.cross_plane_authority import (
    authority_role_separation,
    business_outcome,
    duplicate_logical_command,
    logical_command_identities,
    observed_execution_truth,
    submission_presentation_facts,
    submission_truth,
    telemetry_isolation,
)
from hisiem_soc_copilot.evaluation_harness.cross_plane_e3_scenarios import (
    E3_SCENARIO_IDS,
    E3Fixture,
    declared_fact_codes,
    e3_inventory,
    evaluate_e3_scenario,
    measured_facts,
    missing_scenarios,
    run_e3_scenario_to_artifact,
    run_e3_suite,
    scenario_ids_for_family,
)
from hisiem_soc_copilot.evaluation_harness.cross_plane_measure import fact_set
from hisiem_soc_copilot.infrastructure.mcp.provider import MCPToolProvider
from tests.fixtures.fakes import FakeUnitOfWorkFactory
from tests.support import e3_authority_fixture as fx
from tests.support.mcp_scenario_fixture import deterministic_mcp_server

#: The scenarios whose catalog profile is ``deterministic`` — the set that must run
#: with no runtime at all. Derived from the catalog, never hard-coded.
DETERMINISTIC_E3_SCENARIOS = tuple(
    scenario_id
    for scenario_id in E3_SCENARIO_IDS
    if scenario(scenario_id).minimum_profile.value == "deterministic"
)

RUNTIME_E3_SCENARIOS = tuple(
    scenario_id for scenario_id in E3_SCENARIO_IDS if scenario_id not in DETERMINISTIC_E3_SCENARIOS
)

_CALL_CONTEXT = ProviderInvocationContext(
    tenant_id=fx.TENANT,
    investigation_id=None,
    tool_call_id="e3-call-1",
    source_alert_ref={"provider": "hisiem", "address_id": "alert-0001"},
)


# ---------------------------------------------------------------------------
# Real provider failures (RELIABILITY fixtures)
# ---------------------------------------------------------------------------


async def _hanging_tool(query: str) -> dict[str, str]:
    import asyncio

    await asyncio.sleep(5)
    return {"query": query}


async def _live_tool(query: str) -> dict[str, str]:
    return {"query": query}


async def _mcp_timeout_result() -> ToolResult:
    """Drive the REAL MCP provider against a hanging tool with a tight bound."""
    async with deterministic_mcp_server() as server:
        server.add_tool("slow_lookup", _hanging_tool)
        admission = await server.admit(
            "mcp.slow_lookup",
            "slow_lookup",
            bounds=ResultBounds(timeout_seconds=0.3),
        )
        provider = server.provider([admission])
        await provider.start()
        try:
            result = await provider.invoke(
                admission=admission,
                candidate=ToolCandidate(
                    tool_name="mcp.slow_lookup", arguments={"query": "v"}
                ),
                context=_CALL_CONTEXT,
            )
        finally:
            await provider.close()
    assert result.failure is not None, "a hanging tool must produce a typed failure"
    from hisiem_soc_copilot.evaluation_harness.cross_plane_authority import (
        tool_result_from_failure,
    )

    return tool_result_from_failure(result, tool_call_id="e3-timeout-1")


async def _mcp_unavailable_result() -> tuple[ToolResult, dict[str, object]]:
    """Drive the REAL MCP provider against a server that is no longer there."""
    async with deterministic_mcp_server() as server:
        server.add_tool("live_lookup", _live_tool)
        admission = await server.admit("mcp.live_lookup", "live_lookup")
        live = server.provider([admission])
        await live.start()
        try:
            ok = await live.invoke(
                admission=admission,
                candidate=ToolCandidate(
                    tool_name="mcp.live_lookup", arguments={"query": "v"}
                ),
                context=_CALL_CONTEXT,
            )
        finally:
            await live.close()
    assert ok.status == "SUCCESS"

    # The SAME trusted server configuration and admission, now pointed at a port
    # with nothing on it: the capability is configured but the server is gone.
    dead_settings = server.settings().model_copy(
        update={"endpoint": "http://127.0.0.1:9/mcp"}
    )
    dead = MCPToolProvider(
        server=dead_settings,
        admissions=[admission],
        global_bounds=admission.result_bounds,
    )
    await dead.start()
    try:
        result = await dead.invoke(
            admission=admission,
            candidate=ToolCandidate(tool_name="mcp.live_lookup", arguments={"query": "v"}),
            context=_CALL_CONTEXT,
        )
    finally:
        await dead.close()

    from hisiem_soc_copilot.evaluation_harness.cross_plane_authority import (
        tool_result_from_failure,
    )

    assert result.failure is not None, "an unreachable server must produce a typed failure"
    return tool_result_from_failure(result, tool_call_id="e3-unavailable-1"), {
        "evidence_observation": "no observation was written for the failed call",
        "workspace_payload": "the response view reports the capability as unavailable",
        "telemetry_attributes": "tool.execute status=UNAVAILABLE error_code=UNAVAILABLE",
    }


async def _knowledge_unavailable_result() -> ToolResult:
    """Drive the REAL knowledge path with no catalog wired into the executor."""
    from hisiem_soc_copilot.agent.tools.executor import ToolExecutor

    class _UnusedHisiem:
        async def get_alert(self, **kwargs: object) -> object:  # pragma: no cover
            raise AssertionError("the knowledge path must not touch HISIEM")

        async def search_events(self, **kwargs: object) -> object:  # pragma: no cover
            raise AssertionError("the knowledge path must not touch HISIEM")

        async def get_detection_rule(self, **kwargs: object) -> object:  # pragma: no cover
            raise AssertionError("the knowledge path must not touch HISIEM")

    executor = ToolExecutor(hisiem=_UnusedHisiem())  # type: ignore[arg-type]
    execution = await executor.execute(
        candidate=ToolCandidate(
            tool_name="knowledge.retrieve_security_guidance",
            arguments={"topic": "T1110 brute force", "context_terms": ["sshd"]},
        ),
        tenant_id=fx.TENANT,
        source_alert_ref={"provider": "hisiem", "address_id": "alert-0001"},
        tool_call_id="e3-knowledge-1",
    )
    return execution.result


async def _retrieval_service_unavailability() -> str:
    """The REAL retrieval service fails loudly rather than returning nothing."""
    from hisiem_soc_copilot.application.errors import KnowledgeRetrievalUnavailableError
    from hisiem_soc_copilot.application.ports.knowledge import KnowledgeQuery
    from hisiem_soc_copilot.application.services.knowledge_retrieval import (
        KnowledgeRetrievalService,
        RetrievalMode,
    )

    class _NoActiveProfile:
        async def get_active(self) -> None:
            return None

    class _NoChunks:
        pass

    service = KnowledgeRetrievalService(
        chunks=_NoChunks(),  # type: ignore[arg-type]
        embedding_profiles=_NoActiveProfile(),  # type: ignore[arg-type]
        embedding_provider=None,
        clock=lambda: fx.NOW,  # type: ignore[arg-type]
    )
    with pytest.raises(KnowledgeRetrievalUnavailableError) as caught:
        await service.retrieve(
            tenant_id=fx.TENANT,
            query=KnowledgeQuery(topic="T1110 brute force", context_terms=("sshd",), limit=5),
            mode=RetrievalMode.HYBRID,
        )
    return caught.value.code


# ---------------------------------------------------------------------------
# Fixtures per scenario
# ---------------------------------------------------------------------------


async def _deterministic_fixtures() -> Mapping[str, E3Fixture]:
    """One fixture per deterministic-profile E3 scenario, built from real code."""
    timeout_result = await _mcp_timeout_result()
    unavailable_result, surfaces = await _mcp_unavailable_result()
    knowledge_result = await _knowledge_unavailable_result()

    approved_factory = FakeUnitOfWorkFactory()
    approved, approved_proposal = await fx.approved_fixture(approved_factory)

    submitted_factory = FakeUnitOfWorkFactory()
    submitted, _, _ = await fx.submitted_fixture(
        submitted_factory, execution_status=ResponseExecutionStatus.RUNNING.value
    )

    attention_factory = FakeUnitOfWorkFactory()
    attention, _ = await fx.attention_required_fixture(attention_factory)

    del approved_proposal
    return {
        "XP-AUTH-001": approved,
        "XP-AUTH-002": approved,
        "XP-AUTH-003": approved,
        "XP-AUTH-004": submitted,
        "XP-REL-001": E3Fixture(failed_result=timeout_result),
        "XP-REL-002": E3Fixture(
            failed_result=unavailable_result,
            surfaces=surfaces,
            oracle_tokens=("XP-REL-002", "XP-01", "GATE_ORACLE_FIREWALL"),
        ),
        "XP-REL-003": E3Fixture(
            failed_result=knowledge_result, knowledge_backend_available=False
        ),
        "XP-REL-004": attention,
    }


# ---------------------------------------------------------------------------
# Inventory / executable-path coverage (E3 §4, §38)
# ---------------------------------------------------------------------------


def test_e3_owns_exactly_the_authority_and_reliability_families() -> None:
    inventory = e3_inventory()
    assert inventory["families"] == {
        "AUTHORITY": list(scenario_ids_for_family(GateFamily.AUTHORITY)),
        "RELIABILITY": list(scenario_ids_for_family(GateFamily.RELIABILITY)),
    }
    assert inventory["total"] == len(E3_SCENARIO_IDS) == 10
    assert len(scenario_ids_for_family(GateFamily.AUTHORITY)) == 5
    assert len(scenario_ids_for_family(GateFamily.RELIABILITY)) == 5
    assert not any(
        scenario(scenario_id).gate_family in {GateFamily.OBSERVABILITY, GateFamily.WORKSPACE}
        for scenario_id in E3_SCENARIO_IDS
    )


def test_no_e3_scenario_requires_a_gate_outside_the_frozen_catalog() -> None:
    for scenario_id in E3_SCENARIO_IDS:
        assert set(scenario(scenario_id).required_gate_ids) <= GATE_IDS


async def test_every_deterministic_e3_scenario_passes() -> None:
    fixtures = await _deterministic_fixtures()
    results = run_e3_suite(fixtures)
    failed = {
        scenario_id: status
        for scenario_id, status in results.items()
        if status is not GateStatus.PASS
    }
    assert not failed, f"E3 scenarios did not pass: {failed}"
    # The only scenarios this profile does NOT cover are the two whose catalog
    # profile is runtime-integrated — named here, never silently skipped.
    assert missing_scenarios(fixtures) == tuple(sorted(RUNTIME_E3_SCENARIOS))
    assert len(results) == len(DETERMINISTIC_E3_SCENARIOS) == 8
    assert set(results) == set(DETERMINISTIC_E3_SCENARIOS)


async def test_every_deterministic_e3_scenario_persists_a_gate_results_artifact(
    tmp_path: object,
) -> None:
    fixtures = await _deterministic_fixtures()
    for scenario_id in DETERMINISTIC_E3_SCENARIOS:
        result, path = run_e3_scenario_to_artifact(
            scenario_id, fixtures[scenario_id], executions_dir=tmp_path
        )
        assert result.overall_gate is GateStatus.PASS, scenario_id
        assert path.is_file(), scenario_id
        restored = read_gate_results(path)
        assert restored.scenario_id == scenario_id
        assert restored.overall_gate is GateStatus.PASS
        assert restored.pack_id == "XP-01"
        assert restored.gate_family is scenario(scenario_id).gate_family
        # The artifact path is inside the XP-01 subtree, never somewhere else.
        assert "xp-01" in path.as_posix()


async def test_every_declared_fact_of_every_e3_scenario_is_measured() -> None:
    """No declared expected/forbidden token is left unexecuted (E3 §4, §38)."""
    fixtures = await _deterministic_fixtures()
    for scenario_id in DETERMINISTIC_E3_SCENARIOS:
        expected, forbidden = declared_fact_codes(scenario_id)
        observed = set(measured_facts(scenario_id, fixtures[scenario_id]).observed_facts)
        assert set(expected) <= observed, f"{scenario_id}: missing {set(expected) - observed}"
        assert not (set(forbidden) & observed), (
            f"{scenario_id}: forbidden facts observed {set(forbidden) & observed}"
        )


async def test_scenario_artifacts_are_deterministic(tmp_path: object) -> None:
    fixtures = await _deterministic_fixtures()
    for scenario_id in DETERMINISTIC_E3_SCENARIOS:
        first = tmp_path / "a"  # type: ignore[operator]
        second = tmp_path / "b"  # type: ignore[operator]
        _r1, p1 = run_e3_scenario_to_artifact(
            scenario_id, fixtures[scenario_id], executions_dir=first
        )
        _r2, p2 = run_e3_scenario_to_artifact(
            scenario_id, fixtures[scenario_id], executions_dir=second
        )
        assert p1.read_text(encoding="utf-8") == p2.read_text(encoding="utf-8"), scenario_id


# ---------------------------------------------------------------------------
# §28 — gate falsifiability: direct invalid measurements
# ---------------------------------------------------------------------------


def _gate_status(measurement: object, gate_id: str, scenario_id: str) -> GateStatus:
    return evaluate_gate(gate_id, measurement, scenario=scenario(scenario_id)).status


def test_execution_without_approval_fails_the_gate() -> None:
    measurement = ExecutionAuthorityMeasurement(
        source="RESPONSE_LIFECYCLE_FACT",  # type: ignore[arg-type]
        proposal_status="APPROVED",
        policy_decision="REQUIRE_APPROVAL",
        approval_decision=None,
        durable_command_ids=("response:tenant-a:proposal",),
        execution_observed=False,
    )
    assert (
        _gate_status(measurement, GATE_EXECUTION_WITHOUT_APPROVAL, "XP-AUTH-003")
        is GateStatus.FAIL
    )


def test_a_rejected_authorization_with_a_command_fails_the_gate() -> None:
    measurement = ExecutionAuthorityMeasurement(
        source="RESPONSE_LIFECYCLE_FACT",  # type: ignore[arg-type]
        proposal_status="REJECTED",
        policy_decision="REQUIRE_APPROVAL",
        approval_decision=ApprovalDecisionKind.REJECT.value,
        durable_command_ids=("response:tenant-a:proposal",),
        execution_observed=True,
    )
    result = evaluate_gate(
        GATE_EXECUTION_WITHOUT_APPROVAL, measurement, scenario=scenario("XP-AUTH-003")
    )
    assert result.status is GateStatus.FAIL
    assert result.reason_codes == ("APPROVAL_EXECUTION_CONFLATED",)


def test_submission_treated_as_success_fails_the_gate() -> None:
    measurement = SubmissionTruthMeasurement(
        source="RESPONSE_LIFECYCLE_FACT",  # type: ignore[arg-type]
        submission_status="SUBMITTED",
        observed_execution_status=None,
        submission_treated_as_success=True,
    )
    result = evaluate_gate(
        GATE_SUBMISSION_TREATED_AS_SUCCESS, measurement, scenario=scenario("XP-AUTH-004")
    )
    assert result.status is GateStatus.FAIL
    assert result.reason_codes == ("SUBMISSION_SUCCESS_CONFLATED",)


def test_an_uncertain_submit_that_fabricated_a_terminal_state_fails_the_gate() -> None:
    """ATTENTION_REQUIRED collapsed into SUCCEEDED is the conflation this gate names."""
    measurement = SubmissionTruthMeasurement(
        source="RESPONSE_LIFECYCLE_FACT",  # type: ignore[arg-type]
        submission_status="ATTENTION_REQUIRED",
        observed_execution_status="SUCCEEDED",
        submission_treated_as_success=True,
    )
    assert (
        _gate_status(measurement, GATE_SUBMISSION_TREATED_AS_SUCCESS, "XP-REL-004")
        is GateStatus.FAIL
    )


def test_telemetry_changing_the_business_outcome_fails_the_gate() -> None:
    measurement = TelemetryIsolationMeasurement(
        source="TELEMETRY_FACT",  # type: ignore[arg-type]
        business_fact_fingerprint_with_telemetry="a" * 64,
        business_fact_fingerprint_without_telemetry="b" * 64,
    )
    result = evaluate_gate(
        GATE_TELEMETRY_CHANGED_BUSINESS_STATE, measurement, scenario=scenario("XP-REL-005")
    )
    assert result.status is GateStatus.FAIL
    assert result.reason_codes == ("TELEMETRY_DEPENDENCY_VIOLATION",)


def test_a_forbidden_fact_always_fails_the_forbidden_gate() -> None:
    for scenario_id in E3_SCENARIO_IDS:
        _expected, forbidden = declared_fact_codes(scenario_id)
        if not forbidden:
            continue
        measurement = fact_set(*forbidden)
        assert (
            _gate_status(measurement, GATE_FORBIDDEN_FACTS_ABSENT, scenario_id)
            is GateStatus.FAIL
        ), scenario_id


def test_a_missing_expected_fact_always_fails_the_expected_gate() -> None:
    for scenario_id in E3_SCENARIO_IDS:
        expected, _forbidden = declared_fact_codes(scenario_id)
        if not expected:
            continue
        measurement = fact_set("SOMETHING_ELSE")
        assert (
            _gate_status(measurement, GATE_EXPECTED_FACTS_PRESENT, scenario_id)
            is GateStatus.FAIL
        ), scenario_id


# ---------------------------------------------------------------------------
# §27 — positive + negative pairs, through the real drivers
# ---------------------------------------------------------------------------


async def test_a_command_without_a_recorded_decision_fails_xp_auth_003() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    broken = E3Fixture(
        result=fixture.result,
        proposal=proposal,
        approval_request=fixture.approval_request,
        approval_decision=None,  # no human decision on record
        submission=fixture.submission,
        events=fixture.events,
        execution_observed=False,
    )
    result = evaluate_e3_scenario("XP-AUTH-003", broken)
    assert result.overall_gate is GateStatus.FAIL
    assert result.gate_failures == ("EXECUTION_WITHOUT_APPROVAL",)


async def test_a_stale_authorization_that_executed_fails_xp_auth_003() -> None:
    """Revision N approved, intent advanced to N+1, command dispatched anyway."""
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    proposal.parameters = {"playbook_id": fx.PLAYBOOK_ID, "changed": True}
    proposal.content_revision = 2
    proposal.content_hash = "f" * 64
    await factory().response_proposals.update(proposal)
    result = evaluate_e3_scenario("XP-AUTH-003", fixture)
    assert result.overall_gate is GateStatus.FAIL
    assert result.gate_failures == ("EXECUTION_WITHOUT_APPROVAL",)


async def test_the_same_stale_authorization_produces_no_command() -> None:
    """The negative half: the stale intent was refused, so nothing was recorded."""
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    proposal.parameters = {"playbook_id": fx.PLAYBOOK_ID, "changed": True}
    proposal.content_revision = 2
    proposal.content_hash = "f" * 64
    # A refused authorization leaves no durable command behind, so the gate is not
    # even reached with an execution to explain.
    from hisiem_soc_copilot.evaluation_harness.cross_plane_authority import (
        current_authorization_binding,
        stale_authorization_facts,
    )

    assert fixture.approval_request is not None
    binding = current_authorization_binding(
        proposal=proposal, request=fixture.approval_request
    )
    assert binding.is_bound is False
    facts = stale_authorization_facts(binding=binding, command_produced=False)
    assert facts.observed_facts == ()


async def test_a_rejection_that_left_a_command_fails_xp_auth_003() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.rejected_fixture(factory)
    forged = E3Fixture(
        result=fixture.result,
        proposal=proposal,
        approval_request=fixture.approval_request,
        approval_decision=fixture.approval_decision,
        submission=fixture.submission,
        events=fixture.events,
        execution_observed=True,
    )
    result = evaluate_e3_scenario("XP-AUTH-003", forged)
    assert result.overall_gate is GateStatus.FAIL


async def test_a_duplicate_logical_command_is_detected_and_the_gate_fails() -> None:
    """§15: one authorization, two business intents — detected, then gated."""
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    identities = logical_command_identities(
        proposal=proposal, events=fixture.events, submission=fixture.submission
    )
    assert duplicate_logical_command(
        command_identities=(*identities, "response:tenant-a:duplicate"),
        proposals_for_result=(),
    )

    # The duplicate intent is a SECOND dispatchable command with no authorizing
    # decision of its own, which is exactly what the E1 gate refuses.
    duplicate = ExecutionAuthorityMeasurement(
        source="RESPONSE_LIFECYCLE_FACT",  # type: ignore[arg-type]
        proposal_status="SUBMITTED",
        policy_decision="REQUIRE_APPROVAL",
        approval_decision=None,
        durable_command_ids=("response:tenant-a:duplicate",),
        execution_observed=True,
    )
    assert (
        _gate_status(duplicate, GATE_EXECUTION_WITHOUT_APPROVAL, "XP-AUTH-003")
        is GateStatus.FAIL
    )


async def test_policy_synthesized_approval_fails_xp_auth_002() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.denied_fixture(factory)
    forged_decision = ApprovalDecision(
        id=uuid4(),
        approval_request_id=uuid4(),
        decision=ApprovalDecisionKind.APPROVE.value,
        actor_subject_id="policy-engine",
        actor_tenant_id=fx.TENANT,
    )
    forged = E3Fixture(
        result=fixture.result,
        proposal=proposal,
        approval_request=None,
        approval_decision=forged_decision,
        submission=fixture.submission,
        events=fixture.events,
    )
    result = evaluate_e3_scenario("XP-AUTH-002", forged)
    assert result.overall_gate is GateStatus.FAIL


async def test_agent_verdict_conflated_with_analyst_disposition_fails_xp_auth_001() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, _ = await fx.approved_fixture(factory)
    assert fixture.approval_request is not None
    conflated = ApprovalDecision(
        id=uuid4(),
        approval_request_id=fixture.approval_request.id,
        decision="BENIGN",  # an analyst "decision" written in verdict vocabulary
        actor_subject_id="operator",
        actor_tenant_id=fx.TENANT,
    )
    forged = E3Fixture(
        result=fixture.result,
        proposal=fixture.proposal,
        approval_request=fixture.approval_request,
        approval_decision=conflated,
        submission=fixture.submission,
        events=fixture.events,
    )
    result = evaluate_e3_scenario("XP-AUTH-001", forged)
    assert result.overall_gate is GateStatus.FAIL


async def test_submission_treated_as_success_fails_xp_auth_004() -> None:
    """A local submission record carrying an execution word is the conflation."""
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.approved_fixture(factory)
    assert fixture.submission is not None
    conflated = ResponseSubmission(
        proposal_id=proposal.id,
        submission_key=fixture.submission.submission_key,
        status=ResponseExecutionStatus.SUCCEEDED.value,
        attempt_count=1,
        created_at=fixture.submission.created_at,
        updated_at=fixture.submission.updated_at,
    )
    forged = E3Fixture(
        result=fixture.result,
        proposal=proposal,
        approval_request=fixture.approval_request,
        approval_decision=fixture.approval_decision,
        submission=conflated,
        events=fixture.events,
    )
    result = evaluate_e3_scenario("XP-AUTH-004", forged)
    assert result.overall_gate is GateStatus.FAIL


async def test_uncertainty_collapsed_into_a_refusal_fails_xp_rel_004() -> None:
    """A fabricated FAILED_DEFINITIVE loses the explicit-uncertainty fact."""
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.attention_required_fixture(factory)
    assert fixture.submission is not None
    collapsed = ResponseSubmission(
        proposal_id=proposal.id,
        submission_key=fixture.submission.submission_key,
        status="FAILED_DEFINITIVE",
        attempt_count=10,
        created_at=fixture.submission.created_at,
        updated_at=fixture.submission.updated_at,
        failed_at=fixture.submission.updated_at,
    )
    forged = E3Fixture(
        result=fixture.result,
        proposal=proposal,
        approval_request=fixture.approval_request,
        approval_decision=fixture.approval_decision,
        submission=collapsed,
        events=fixture.events,
    )
    observed = set(measured_facts("XP-REL-004", forged).observed_facts)
    assert "SUBMISSION_ATTENTION_REQUIRED" not in observed
    result = evaluate_e3_scenario("XP-REL-004", forged)
    assert result.overall_gate is GateStatus.FAIL


async def test_attention_required_survives_as_uncertainty_in_the_real_projection() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.attention_required_fixture(factory)
    measurement = submission_presentation_facts(
        proposal=proposal,
        request=fixture.approval_request,
        decision=fixture.approval_decision,
        submission=fixture.submission,
        execution=fixture.execution,
    )
    assert "SUBMISSION_ATTENTION_REQUIRED" in measurement.observed_facts
    assert measurement.observed_facts == (
        "SUBMISSION_ATTENTION_REQUIRED",
        "SUBMISSION_NOT_TREATED_AS_SUCCESS",
    )
    truths = submission_truth(submission=fixture.submission, execution=None)
    assert truths.submission_treated_as_success is False


async def test_false_success_evidence_fails_xp_rel_001() -> None:
    measurement = fact_set("FALSE_SUCCESS_EVIDENCE_PRESENT")
    assert (
        _gate_status(measurement, GATE_FORBIDDEN_FACTS_ABSENT, "XP-REL-001") is GateStatus.FAIL
    )


async def test_failure_normalized_as_empty_fails_xp_rel_003() -> None:
    measurement = fact_set("FAILURE_NORMALIZED_AS_EMPTY")
    assert (
        _gate_status(measurement, GATE_FORBIDDEN_FACTS_ABSENT, "XP-REL-003") is GateStatus.FAIL
    )


async def test_an_oracle_leak_fails_xp_rel_002() -> None:
    measurement = fact_set("ORACLE_DATA_LEAKED_TO_PRODUCTION")
    assert _gate_status(measurement, GATE_FORBIDDEN_FACTS_ABSENT, "XP-REL-002") is GateStatus.FAIL


async def test_a_business_outcome_that_changed_under_outage_fails_xp_rel_005() -> None:
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
    degraded["execution_status"] = ResponseExecutionStatus.FAILED.value
    measurement = telemetry_isolation(
        business_outcome_with_telemetry=outcome,
        business_outcome_without_telemetry=degraded,
    )
    assert (
        _gate_status(measurement, GATE_TELEMETRY_CHANGED_BUSINESS_STATE, "XP-REL-005")
        is GateStatus.FAIL
    )


# ---------------------------------------------------------------------------
# §30 — artifact references stay bounded
# ---------------------------------------------------------------------------


async def test_artifact_references_are_bounded_identities_only(tmp_path: object) -> None:
    fixtures = await _deterministic_fixtures()
    for scenario_id in DETERMINISTIC_E3_SCENARIOS:
        _result, path = run_e3_scenario_to_artifact(
            scenario_id, fixtures[scenario_id], executions_dir=tmp_path
        )
        payload = path.read_text(encoding="utf-8")
        # No raw entity dump and no secret marker can survive into an artifact.
        assert "content_hash" not in payload or scenario_id.startswith("XP-AUTH")
        assert "Bearer " not in payload
        assert "password" not in payload.lower()
        assert len(payload) < 256_000


async def test_an_e3_scenario_missing_a_required_measurement_is_not_evaluable() -> None:
    """A scenario whose facts were not collected is never a silent PASS."""
    from hisiem_soc_copilot.evaluation.cross_plane import GateMeasurementMissingError

    with pytest.raises(GateMeasurementMissingError):
        evaluate_scenario(scenario("XP-AUTH-003"), {})


def test_the_reliability_family_measures_the_failure_not_the_absence() -> None:
    """A typed failure and an empty success are different facts, by construction."""
    provider_result = ProviderInvocationResult(
        tool_name="mcp.x",
        status="UNAVAILABLE",
        failure=ProviderFailure(code="TIMEOUT", safe_message="timed out"),
    )
    assert provider_result.status == "UNAVAILABLE"
    assert provider_result.failure is not None
    assert provider_result.failure.code == "TIMEOUT"


def test_admission_entries_never_allow_a_write_capability_to_be_selectable() -> None:
    """The E3 reliability fixtures cannot smuggle a write capability in."""
    write = AdmissionEntry(
        internal_name="mcp.block_ip",
        description="d",
        server_id="e3-local",
        external_name="block_ip",
        risk="WRITE",
    )
    assert write.is_model_selectable is False


async def test_retrieval_unavailability_is_a_typed_service_error_not_an_empty_result() -> None:
    code = await _retrieval_service_unavailability()
    assert code == "KNOWLEDGE_RETRIEVAL_UNAVAILABLE"


async def test_the_production_policy_denies_an_inconclusive_verdict() -> None:
    """DENY is why XP-AUTH-002's deny branch is reachable at all."""
    factory = FakeUnitOfWorkFactory()
    fixture, proposal = await fx.denied_fixture(factory)
    assert proposal.policy_decision is PolicyDecision.DENY
    assert proposal.policy_reason == "denying_verdict"
    assert fixture.approval_request is None


async def test_observed_truth_overrides_a_projected_success() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, _, ref = await fx.submitted_fixture(
        factory, execution_status=ResponseExecutionStatus.SUCCEEDED.value
    )
    # HISIEM later reports FAILED for the same execution: the provider wins.
    facts = observed_execution_truth(
        execution=ref, hisiem_observed_status=ResponseExecutionStatus.FAILED.value
    )
    assert facts.observed_facts == ("SUBMISSION_TREATED_AS_SUCCESS_PRESENT",)


async def test_the_authority_role_separation_adapter_reads_the_workspace_projection() -> None:
    factory = FakeUnitOfWorkFactory()
    fixture, _ = await fx.approved_fixture(factory)
    measurement = authority_role_separation(
        result=fixture.result,
        request=fixture.approval_request,
        decision=fixture.approval_decision,
    )
    assert measurement.observed_facts == ("AGENT_VERDICT_DISTINCT_FROM_ANALYST_DISPOSITION",)
    assert measurement.source.value == "PERSISTED_DOMAIN_FACT"
