"""E5 end-to-end scenario execution (Stage E / E5 §9, §23, §24).

Every E5-owned XP-01 scenario is driven end to end against a REAL workspace read
model built by the production workspace service over real persisted rows:

```text
persisted rows -> real workspace projection -> REAL frontend authority derivation
        -> E5 measurement adapter (cross_plane_workspace)
        -> E1 typed measurement
        -> E1 deterministic hard gate (evaluate_scenario)
        -> cross-plane-gate-results/v1
```

Both WORKSPACE scenarios declare the ``deterministic`` profile: the presentation
rules are pure functions of the persisted facts and the projection, so no browser is
required to DECIDE them. The real-browser layer (real Vue workspace, real Chromium)
is the separate acceptance recorded in the E5 report.

The negative half (§23) proves each presentation invariant can FAIL, so a green suite
is not a vacuous one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hisiem_soc_copilot.api.app import create_app
from hisiem_soc_copilot.application.commands.response import (
    CreateResponseProposal,
    DecideResponseApproval,
)
from hisiem_soc_copilot.application.ports.soar import SoarExecutionResult
from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.entities import (
    Evidence,
    EvidenceSource,
    Finding,
    InvestigationResult,
    Verdict,
)
from hisiem_soc_copilot.domain.investigation.enums import (
    EvidenceSourceType,
    VerdictDisposition,
)
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from hisiem_soc_copilot.domain.response.enums import ApprovalDecisionKind
from hisiem_soc_copilot.evaluation.cross_plane import (
    GATE_EXPECTED_FACTS_PRESENT,
    GATE_FORBIDDEN_FACTS_ABSENT,
    GATE_IDS,
    GateFamily,
    GateStatus,
    evaluate_gate,
    scenario,
)
from hisiem_soc_copilot.evaluation_harness import read_gate_results
from hisiem_soc_copilot.evaluation_harness.cross_plane_e5_scenarios import (
    E5_SCENARIO_IDS,
    E5Fixture,
    declared_fact_codes,
    e5_inventory,
    evaluate_e5_scenario,
    measured_facts,
    missing_scenarios,
    run_e5_scenario_to_artifact,
    run_e5_suite,
    scenario_ids_for_family,
)
from hisiem_soc_copilot.evaluation_harness.cross_plane_measure import fact_set
from hisiem_soc_copilot.evaluation_harness.cross_plane_workspace import (
    KNOWLEDGE_CONTEXT,
    PLATFORM_FACT,
    PersistedWorkspaceFacts,
    PresentedWorkspace,
    presentation_identity,
)
from tests.fixtures.fakes import FakeSoar
from tests.support import e5_workspace_fixture as fx
from tests.support.db_runtime import SKIP_REASON, apply_settings, server_reachable

TENANT = "tenant-a"
ALERT = "e5-workspace-alert-1"
PLAYBOOK_ID = "11111111-2222-3333-4444-555555555555"
_TOKEN_ENV = "HISIEM_COPILOT_SERVICE_TOKEN"

_TRUNCATE = (
    "response_submission",
    "response_execution_ref",
    "approval_decision",
    "approval_request",
    "response_proposal_evidence",
    "response_proposal_target",
    "response_proposal",
    "tool_invocation",
    "outbox_message",
    "domain_event",
    "command_receipt",
    "orchestration_binding",
    "investigation_result_finding",
    "investigation_result",
    "finding_evidence",
    "finding",
    "evidence",
    "hypothesis_assessment_evidence",
    "hypothesis_assessment",
    "hypothesis",
    "plan_step",
    "plan_revision",
    "investigation",
)


def _settings() -> Settings:
    settings = Settings()
    apply_settings(settings)
    settings.auth.trusted_context_provider = "hisiem_bearer"
    settings.auth.hisiem_service_token_env = _TOKEN_ENV
    settings.app.response_observe_interval_seconds = 0.0
    return settings


@pytest_asyncio.fixture
async def workspace_runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """A real container over the disposable DB with truncated workspace state."""
    if not server_reachable():
        pytest.skip(SKIP_REASON)
    monkeypatch.setenv(_TOKEN_ENV, "integration-service-secret-value")
    settings = _settings()
    engine = create_async_engine(settings.database.database_url)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        await session.execute(
            text(f"TRUNCATE copilot.{', copilot.'.join(_TRUNCATE)} RESTART IDENTITY CASCADE")
        )
        await session.commit()
    app = create_app(settings)
    async with LifespanManager(app):
        yield app.state.container
    await engine.dispose()


async def _seed(container: Any, *, with_knowledge: bool = True) -> tuple[UUID, UUID]:
    """Persist a COMPLETED investigation with PLATFORM and KNOWLEDGE evidence."""
    from hisiem_soc_copilot.domain.shared.identifiers import utc_now

    now = utc_now()
    investigation_id = uuid4()
    uow = container.unit_of_work()
    try:
        investigation = Investigation.create(
            id=investigation_id,
            tenant_id=TENANT,
            source_alert_ref=ExternalResourceRef(
                provider="hisiem",
                resource_type="alert",
                address_id=ALERT,
                business_id="AL-1",
            ),
            initiated_by=ActorRef(subject_id="analyst", tenant_id=TENANT),
            budget_limits=BudgetLimits(),
            now=now,
        )
        investigation.start(actor=ActorRef(subject_id="analyst", tenant_id=TENANT), now=now)
        investigation.complete_without_response()
        await uow.investigations.add(investigation)
        await uow.commit()

        platform = Evidence(
            id=uuid4(),
            investigation_id=investigation_id,
            source=EvidenceSource(
                type=EvidenceSourceType.HISIEM_LOG_SEARCH,
                provider="hisiem",
                operation="search_events",
            ),
            collected_at=now,
            observation={"summary": "6 failed logins in 5 minutes"},
            summary="6 failed logins in 5 minutes",
            dedup_key=f"e5-platform-{uuid4().hex[:8]}",
        )
        await uow.evidence.add(platform)
        knowledge_id: UUID | None = None
        if with_knowledge:
            knowledge = Evidence(
                id=uuid4(),
                investigation_id=investigation_id,
                source=EvidenceSource(
                    type=EvidenceSourceType.KNOWLEDGE,
                    provider="knowledge",
                    operation="retrieve_security_guidance",
                ),
                collected_at=now,
                observation={"summary": "SSH brute-force response guidance"},
                summary="SSH brute-force response guidance",
                dedup_key=f"e5-knowledge-{uuid4().hex[:8]}",
            )
            await uow.evidence.add(knowledge)
            knowledge_id = knowledge.id

        finding = Finding(
            id=uuid4(),
            investigation_id=investigation_id,
            statement="root login followed the brute force",
            evidence_citations=[platform.id],
            created_at=now,
        )
        await uow.findings.add(finding)

        result = InvestigationResult(
            id=uuid4(),
            investigation_id=investigation_id,
            verdict=Verdict(
                disposition=VerdictDisposition.MALICIOUS,
                summary="SSH compromise confirmed",
                confidence=0.9,
            ),
            finding_ids=[finding.id],
            created_at=now,
        )
        await uow.results.add(result)
        await uow.commit()
    finally:
        await uow.close()
    return investigation_id, knowledge_id or platform.id


async def _workspace(container: Any, investigation_id: UUID) -> Any:
    return await container.investigation_workspace_service().get_workspace(
        tenant_id=TENANT, investigation_id=investigation_id
    )


def _fixture(model: Any) -> E5Fixture:
    presented = fx.presented_from_read_model(model)
    persisted = fx.persisted_from_read_model(model)
    return E5Fixture(
        presented=presented,
        persisted=persisted,
        replayed=presented,
        original=presented,
        stale_client=presented,
        server_truth=presented,
    )


# ---------------------------------------------------------------------------
# Inventory / executable-path coverage (E5 §9)
# ---------------------------------------------------------------------------


def test_e5_owns_exactly_the_workspace_family() -> None:
    inventory = e5_inventory()
    assert inventory["families"] == {
        "WORKSPACE": list(scenario_ids_for_family(GateFamily.WORKSPACE))
    }
    assert inventory["total"] == len(E5_SCENARIO_IDS) == 2
    assert scenario_ids_for_family(GateFamily.WORKSPACE) == ("XP-UX-001", "XP-UX-002")
    assert not any(
        scenario(sid).gate_family is not GateFamily.WORKSPACE for sid in E5_SCENARIO_IDS
    )
    assert missing_scenarios({}) == ("XP-UX-001", "XP-UX-002")


def test_no_e5_scenario_requires_a_gate_outside_the_frozen_catalog() -> None:
    for scenario_id in E5_SCENARIO_IDS:
        assert set(scenario(scenario_id).required_gate_ids) <= GATE_IDS


def test_xp01_is_complete_after_e5() -> None:
    """27 scenarios from E1-E4 plus E5's 2 complete the 29-scenario pack."""
    from hisiem_soc_copilot.evaluation.cross_plane import XP01_SCENARIOS

    assert len(XP01_SCENARIOS) == 29
    assert len(E5_SCENARIO_IDS) == 2


# ---------------------------------------------------------------------------
# Real workspace projection
# ---------------------------------------------------------------------------


async def test_the_real_workspace_presents_every_authority_class(
    workspace_runtime: Any,
) -> None:
    container = workspace_runtime
    investigation_id, _knowledge_id = await _seed(container)
    handler = container.response_command_handler()
    uow = container.unit_of_work()
    try:
        evidence = await uow.evidence.list_by_investigation(
            tenant_id=TENANT, investigation_id=investigation_id
        )
    finally:
        await uow.close()
    proposal = await handler.create_response_proposal(
        CreateResponseProposal(
            tenant_id=TENANT,
            investigation_id=investigation_id,
            action_key="START_SOAR_PLAYBOOK",
            evidence_ids=tuple(str(item.id) for item in evidence),
            parameters={"playbook_id": PLAYBOOK_ID},
            reason="contain the intrusion",
            initiated_by_subject="analyst",
        )
    )
    uow = container.unit_of_work()
    try:
        request = await uow.response_approvals.get_request_by_proposal(
            tenant_id=TENANT, proposal_id=proposal.id
        )
    finally:
        await uow.close()
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
    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id="hisiem-exec-e5-1", status="SUCCEEDED")
    )
    submit = container.response_submit_outbox_dispatcher(soar=soar)
    await submit.drain_once()
    await submit.stop()

    model = await _workspace(container, investigation_id)
    fixture = _fixture(model)
    result = evaluate_e5_scenario("XP-UX-001", fixture)
    assert result.overall_gate is GateStatus.PASS, result.gate_failures

    # The classes come from real rows and the real frontend derivation.
    assert fixture.presented.evidence_authority
    assert PLATFORM_FACT in fixture.presented.evidence_authority.values()
    assert KNOWLEDGE_CONTEXT in fixture.presented.evidence_authority.values()
    assert fixture.presented.agent_verdict == "MALICIOUS"
    assert fixture.presented.human_decision == ApprovalDecisionKind.APPROVE.value


async def test_the_real_frontend_derivation_separates_knowledge_from_platform() -> None:
    labels = fx.frontend_authority_labels()
    assert labels["PLATFORM_FACT"] != labels["KNOWLEDGE_CONTEXT"]
    by_type = fx.frontend_evidence_authority(
        ["HISIEM_LOG_SEARCH", "HISIEM_ALERT", "KNOWLEDGE", "SYSTEM"]
    )
    assert by_type["HISIEM_LOG_SEARCH"] == PLATFORM_FACT
    assert by_type["HISIEM_ALERT"] == PLATFORM_FACT
    assert by_type["KNOWLEDGE"] == KNOWLEDGE_CONTEXT
    assert by_type["SYSTEM"] != PLATFORM_FACT


async def test_a_fresh_projection_reconstructs_the_same_presentation(
    workspace_runtime: Any,
) -> None:
    """XP-UX-002: the SAME persisted state produces the SAME presentation."""
    container = workspace_runtime
    investigation_id, _ = await _seed(container)
    first = await _workspace(container, investigation_id)
    second = await _workspace(container, investigation_id)
    assert presentation_identity(
        fx.presented_from_read_model(first)
    ) == presentation_identity(fx.presented_from_read_model(second))

    fixture = E5Fixture(
        presented=fx.presented_from_read_model(first),
        persisted=fx.persisted_from_read_model(first),
        replayed=fx.presented_from_read_model(second),
        original=fx.presented_from_read_model(first),
        stale_client=PresentedWorkspace(proposal_status="WAITING_APPROVAL"),
        server_truth=fx.presented_from_read_model(second),
    )
    result = evaluate_e5_scenario("XP-UX-002", fixture)
    assert result.overall_gate is GateStatus.PASS, result.gate_failures


async def test_server_truth_wins_over_a_stale_client_snapshot(workspace_runtime: Any) -> None:
    """The stale client saw WAITING_APPROVAL; the server has since advanced."""
    container = workspace_runtime
    investigation_id, _ = await _seed(container)
    stale = fx.presented_from_read_model(await _workspace(container, investigation_id))

    handler = container.response_command_handler()
    uow = container.unit_of_work()
    try:
        evidence = await uow.evidence.list_by_investigation(
            tenant_id=TENANT, investigation_id=investigation_id
        )
    finally:
        await uow.close()
    proposal = await handler.create_response_proposal(
        CreateResponseProposal(
            tenant_id=TENANT,
            investigation_id=investigation_id,
            action_key="START_SOAR_PLAYBOOK",
            evidence_ids=tuple(str(item.id) for item in evidence),
            parameters={"playbook_id": PLAYBOOK_ID},
            reason="contain the intrusion",
            initiated_by_subject="analyst",
        )
    )
    uow = container.unit_of_work()
    try:
        request = await uow.response_approvals.get_request_by_proposal(
            tenant_id=TENANT, proposal_id=proposal.id
        )
    finally:
        await uow.close()
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
    server = fx.presented_from_read_model(await _workspace(container, investigation_id))
    assert presentation_identity(stale) != presentation_identity(server)
    assert stale.human_decision is None and server.human_decision == "APPROVE"

    fixture = E5Fixture(
        presented=server,
        persisted=fx.persisted_from_read_model(await _workspace(container, investigation_id)),
        replayed=server,
        original=stale,
        stale_client=stale,
        server_truth=server,
    )
    result = evaluate_e5_scenario("XP-UX-002", fixture)
    assert result.overall_gate is GateStatus.PASS, result.gate_failures


async def test_attention_required_is_presented_as_explicit_uncertainty(
    workspace_runtime: Any,
) -> None:
    """Approval APPROVED + submission ATTENTION_REQUIRED stay two distinct facts."""
    from datetime import UTC, datetime

    from hisiem_soc_copilot.domain.response.enums import ResponseSubmissionStatus
    from hisiem_soc_copilot.domain.response.value_objects import (
        ResponseSubmission,
        submission_key,
    )
    from hisiem_soc_copilot.infrastructure.durable.response_runner import (
        ResponseSubmitExhaustionHandler,
    )

    container = workspace_runtime
    investigation_id, _ = await _seed(container)
    handler = container.response_command_handler()
    uow = container.unit_of_work()
    try:
        evidence = await uow.evidence.list_by_investigation(
            tenant_id=TENANT, investigation_id=investigation_id
        )
    finally:
        await uow.close()
    proposal = await handler.create_response_proposal(
        CreateResponseProposal(
            tenant_id=TENANT,
            investigation_id=investigation_id,
            action_key="START_SOAR_PLAYBOOK",
            evidence_ids=tuple(str(item.id) for item in evidence),
            parameters={"playbook_id": PLAYBOOK_ID},
            reason="contain the intrusion",
            initiated_by_subject="analyst",
        )
    )
    uow = container.unit_of_work()
    try:
        request = await uow.response_approvals.get_request_by_proposal(
            tenant_id=TENANT, proposal_id=proposal.id
        )
    finally:
        await uow.close()
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
    now = datetime.now(UTC)
    uow = container.unit_of_work()
    try:
        # The approval already created the PENDING submission row; move it to the
        # exhausted-uncertain shape the handler would have produced.
        await uow.response_submissions.update(
            ResponseSubmission(
                proposal_id=proposal.id,
                submission_key=submission_key(TENANT, proposal.id),
                status=ResponseSubmissionStatus.RETRYING.value,
                attempt_count=10,
                last_error_code="SOAR_UNAVAILABLE",
                created_at=now,
                updated_at=now,
            )
        )
        await uow.commit()
    finally:
        await uow.close()
    await ResponseSubmitExhaustionHandler(
        unit_of_work_factory=container.unit_of_work_factory()
    ).on_retry_exhausted(
        aggregate_id=str(proposal.id), tenant_id=TENANT, error_code="SOAR_UNAVAILABLE"
    )

    model = await _workspace(container, investigation_id)
    fixture = _fixture(model)
    assert fixture.presented.human_decision == "APPROVE"
    assert fixture.presented.submission_status == "ATTENTION_REQUIRED"
    assert fixture.presented.execution_status is None
    assert evaluate_e5_scenario("XP-UX-001", fixture).overall_gate is GateStatus.PASS
    statuses = set(fixture.presented.timeline_statuses)
    assert "SUBMISSION_ATTENTION_REQUIRED" in statuses
    assert not (statuses & {"SUCCEEDED", "FAILED"})


# ---------------------------------------------------------------------------
# Declared facts / artifacts
# ---------------------------------------------------------------------------


def _deterministic_fixture() -> E5Fixture:
    presented = PresentedWorkspace(
        evidence_authority={"e1": PLATFORM_FACT, "e2": KNOWLEDGE_CONTEXT},
        finding_ids=("f1",),
        agent_verdict="MALICIOUS",
        policy_decision="REQUIRE_APPROVAL",
        human_decision="APPROVE",
        submission_status="SUBMITTED",
        execution_status="SUCCEEDED",
        proposal_status="SUBMITTED",
        timeline_statuses=("RESPONSE_EXECUTION_SUCCEEDED",),
    )
    persisted = PersistedWorkspaceFacts(
        evidence_source_types={"e1": "HISIEM_LOG_SEARCH", "e2": "KNOWLEDGE"},
        evidence_ids=("e1", "e2"),
        finding_ids=("f1",),
        agent_verdict="MALICIOUS",
        policy_decision="REQUIRE_APPROVAL",
        human_decision="APPROVE",
        submission_status="SUBMITTED",
        execution_status="SUCCEEDED",
        proposal_status="SUBMITTED",
    )
    return E5Fixture(
        presented=presented,
        persisted=persisted,
        replayed=presented,
        original=presented,
        stale_client=PresentedWorkspace(proposal_status="WAITING_APPROVAL"),
        server_truth=presented,
    )


def test_every_declared_fact_of_every_e5_scenario_is_measured() -> None:
    fixture = _deterministic_fixture()
    for scenario_id in E5_SCENARIO_IDS:
        expected, forbidden = declared_fact_codes(scenario_id)
        observed = set(measured_facts(scenario_id, fixture).observed_facts)
        assert set(expected) <= observed, f"{scenario_id}: missing {set(expected) - observed}"
        assert not (set(forbidden) & observed)


def test_both_e5_scenarios_pass_on_the_deterministic_fixture() -> None:
    fixtures = {scenario_id: _deterministic_fixture() for scenario_id in E5_SCENARIO_IDS}
    results = run_e5_suite(fixtures)
    failed = {sid: status for sid, status in results.items() if status is not GateStatus.PASS}
    assert not failed, failed


def test_e5_scenarios_persist_and_reload_artifacts(tmp_path: Path) -> None:
    fixture = _deterministic_fixture()
    for scenario_id in E5_SCENARIO_IDS:
        result, path = run_e5_scenario_to_artifact(
            scenario_id, fixture, executions_dir=tmp_path
        )
        assert result.overall_gate is GateStatus.PASS, scenario_id
        restored = read_gate_results(path)
        assert restored.scenario_id == scenario_id
        assert restored.gate_family is GateFamily.WORKSPACE
        assert restored.pack_id == "XP-01"
        assert "xp-01" in path.as_posix()


def test_e5_artifacts_are_deterministic(tmp_path: Path) -> None:
    fixture = _deterministic_fixture()
    for scenario_id in E5_SCENARIO_IDS:
        _r1, first = run_e5_scenario_to_artifact(
            scenario_id, fixture, executions_dir=tmp_path / "a"
        )
        _r2, second = run_e5_scenario_to_artifact(
            scenario_id, fixture, executions_dir=tmp_path / "b"
        )
        assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# §23 falsifiability — direct invalid measurements
# ---------------------------------------------------------------------------


def _status(gate_id: str, measurement: object, scenario_id: str) -> GateStatus:
    return evaluate_gate(gate_id, measurement, scenario=scenario(scenario_id)).status


def _presented(**overrides: object) -> PresentedWorkspace:
    base = {
        "evidence_authority": {"e1": PLATFORM_FACT, "e2": KNOWLEDGE_CONTEXT},
        "finding_ids": ("f1",),
        "agent_verdict": "MALICIOUS",
        "policy_decision": "REQUIRE_APPROVAL",
        "human_decision": "APPROVE",
        "submission_status": "SUBMITTED",
        "execution_status": "SUCCEEDED",
        "proposal_status": "SUBMITTED",
    }
    base.update(overrides)
    return PresentedWorkspace(**base)  # type: ignore[arg-type]


def _persisted(**overrides: object) -> PersistedWorkspaceFacts:
    base = {
        "evidence_source_types": {"e1": "HISIEM_LOG_SEARCH", "e2": "KNOWLEDGE"},
        "evidence_ids": ("e1", "e2"),
        "finding_ids": ("f1",),
        "agent_verdict": "MALICIOUS",
        "policy_decision": "REQUIRE_APPROVAL",
        "human_decision": "APPROVE",
        "submission_status": "SUBMITTED",
        "execution_status": "SUCCEEDED",
        "proposal_status": "SUBMITTED",
    }
    base.update(overrides)
    return PersistedWorkspaceFacts(**base)  # type: ignore[arg-type]


def test_agent_verdict_mislabeled_as_human_decision_fails() -> None:
    """A workspace presenting the verdict where the human decision belongs."""
    fixture = E5Fixture(
        presented=_presented(human_decision="MALICIOUS"),
        persisted=_persisted(),
    )
    result = evaluate_e5_scenario("XP-UX-001", fixture)
    assert result.overall_gate is GateStatus.FAIL


def test_policy_decision_mislabeled_as_approval_fails() -> None:
    """Policy REQUIRE_APPROVAL presented as if a human had already approved."""
    fixture = E5Fixture(
        presented=_presented(human_decision="REQUIRE_APPROVAL"),
        persisted=_persisted(human_decision=None),
    )
    result = evaluate_e5_scenario("XP-UX-001", fixture)
    assert result.overall_gate is GateStatus.FAIL


def test_submitted_presented_as_success_fails() -> None:
    """No provider execution exists, yet the workspace shows SUCCEEDED."""
    fixture = E5Fixture(
        presented=_presented(execution_status="SUCCEEDED"),
        persisted=_persisted(execution_status=None, submission_status="SUBMITTED"),
    )
    result = evaluate_e5_scenario("XP-UX-001", fixture)
    assert result.overall_gate is GateStatus.FAIL
    assert GATE_FORBIDDEN_FACTS_ABSENT in result.gate_failures


def test_attention_required_presented_as_terminal_success_fails() -> None:
    presented = _presented(
        submission_status="ATTENTION_REQUIRED", execution_status="SUCCEEDED"
    )
    persisted = _persisted(submission_status="ATTENTION_REQUIRED", execution_status=None)
    result = evaluate_e5_scenario(
        "XP-UX-001", E5Fixture(presented=presented, persisted=persisted)
    )
    assert result.overall_gate is GateStatus.FAIL


def test_attention_required_presented_as_rejection_fails() -> None:
    presented = _presented(submission_status="FAILED_DEFINITIVE", execution_status=None)
    persisted = _persisted(submission_status="ATTENTION_REQUIRED", execution_status=None)
    result = evaluate_e5_scenario(
        "XP-UX-001", E5Fixture(presented=presented, persisted=persisted)
    )
    assert result.overall_gate is GateStatus.FAIL


def test_stale_client_state_winning_over_newer_server_truth_fails() -> None:
    stale = _presented(human_decision=None, proposal_status="WAITING_APPROVAL")
    server = _presented()
    fixture = E5Fixture(
        presented=server,
        persisted=_persisted(),
        replayed=stale,
        original=server,
        stale_client=stale,
        server_truth=server,
    )
    result = evaluate_e5_scenario("XP-UX-002", fixture)
    assert result.overall_gate is GateStatus.FAIL


def test_knowledge_context_mislabeled_as_platform_fact_fails() -> None:
    fixture = E5Fixture(
        presented=_presented(evidence_authority={"e1": PLATFORM_FACT, "e2": PLATFORM_FACT}),
        persisted=_persisted(),
    )
    result = evaluate_e5_scenario("XP-UX-001", fixture)
    assert result.overall_gate is GateStatus.FAIL
    assert GATE_FORBIDDEN_FACTS_ABSENT in result.gate_failures


def test_platform_evidence_mislabeled_as_supporting_context_fails() -> None:
    fixture = E5Fixture(
        presented=_presented(
            evidence_authority={"e1": KNOWLEDGE_CONTEXT, "e2": KNOWLEDGE_CONTEXT}
        ),
        persisted=_persisted(),
    )
    result = evaluate_e5_scenario("XP-UX-001", fixture)
    assert result.overall_gate is GateStatus.FAIL


def test_a_finding_without_a_persisted_row_fails() -> None:
    fixture = E5Fixture(
        presented=_presented(finding_ids=("f1", "f-invented")),
        persisted=_persisted(),
    )
    result = evaluate_e5_scenario("XP-UX-001", fixture)
    assert result.overall_gate is GateStatus.FAIL


def test_a_missing_authority_class_is_not_a_pass() -> None:
    """A workspace hiding the execution plane cannot present authority semantics."""
    fixture = E5Fixture(
        presented=_presented(execution_status=None, submission_status=None),
        persisted=_persisted(),
    )
    measurement = measured_facts("XP-UX-001", fixture)
    assert (
        _status(GATE_EXPECTED_FACTS_PRESENT, measurement, "XP-UX-001") is GateStatus.FAIL
    )


def test_a_forbidden_fact_always_fails_the_forbidden_gate() -> None:
    for scenario_id in E5_SCENARIO_IDS:
        _expected, forbidden = declared_fact_codes(scenario_id)
        if not forbidden:
            continue
        assert (
            _status(GATE_FORBIDDEN_FACTS_ABSENT, fact_set(*forbidden), scenario_id)
            is GateStatus.FAIL
        ), scenario_id


# ---------------------------------------------------------------------------
# Positive/negative pairs through the drivers
# ---------------------------------------------------------------------------


def test_reconstruction_pass_and_fail_pair() -> None:
    server = _presented()
    stale = _presented(human_decision=None, proposal_status="WAITING_APPROVAL")
    good = E5Fixture(
        presented=server,
        persisted=_persisted(),
        replayed=server,
        original=server,
        stale_client=stale,
        server_truth=server,
    )
    assert evaluate_e5_scenario("XP-UX-002", good).overall_gate is GateStatus.PASS
    bad = E5Fixture(
        presented=server,
        persisted=_persisted(),
        replayed=stale,
        original=server,
        stale_client=stale,
        server_truth=server,
    )
    assert evaluate_e5_scenario("XP-UX-002", bad).overall_gate is GateStatus.FAIL


def test_authority_pass_and_fail_pair() -> None:
    good = E5Fixture(presented=_presented(), persisted=_persisted())
    assert evaluate_e5_scenario("XP-UX-001", good).overall_gate is GateStatus.PASS
    bad = E5Fixture(
        presented=_presented(evidence_authority={"e1": "UNKNOWN"}),
        persisted=_persisted(),
    )
    assert evaluate_e5_scenario("XP-UX-001", bad).overall_gate is GateStatus.FAIL


def test_the_artifact_never_embeds_a_raw_workspace_payload(tmp_path: Path) -> None:
    from hisiem_soc_copilot.evaluation_harness.cross_plane_adapter import evaluate_and_build
    from hisiem_soc_copilot.evaluation_harness.cross_plane_e5_scenarios import (
        E5_SCENARIO_DRIVERS,
    )

    fixture = _deterministic_fixture()
    _result, payload = evaluate_and_build(
        "XP-UX-001", E5_SCENARIO_DRIVERS["XP-UX-001"](fixture)
    )
    text = repr(payload)
    assert "observation" not in text
    assert "payload" not in text
    assert len(text) < 8_000
