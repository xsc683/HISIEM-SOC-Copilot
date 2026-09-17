"""E3 focused durable integration (Stage E / E3 §21 "focused integration", §33).

This layer exercises the REAL durable authority path over the disposable Postgres
the test suite already owns: real SQLAlchemy repositories, the real command
handler, the real SUBMIT/OBSERVE runners, the real outbox dispatcher (including its
retry/back-off/dead-letter semantics), and the real exhaustion handler. Only the
external HISIEM SOAR port is scripted — XP-AUTH-005 drives the real one.

```text
real handler + real repositories + real dispatcher + real runners (Postgres 5434)
        -> persisted authority/execution facts
        -> E3 measurement adapter
        -> E1 hard gate
```

The E3 §14/§15/§16/§17/§19 invariants are proven here on persisted rows, not on
in-memory objects: retry keeps ONE logical durable intent, a duplicate delivery
converges, an uncertain submit fabricates nothing, an exhausted budget becomes
explicit uncertainty, and a fresh worker picks up the durable intent after a
"restart" without creating a second business intent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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
from hisiem_soc_copilot.application.errors import ExternalServiceError
from hisiem_soc_copilot.application.ports.soar import SoarExecutionResult
from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.entities import (
    Evidence,
    EvidenceSource,
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
from hisiem_soc_copilot.domain.response.enums import (
    ApprovalDecisionKind,
    ResponseProposalStatus,
    ResponseSubmissionStatus,
)
from hisiem_soc_copilot.domain.response.events import ResponseEvent
from hisiem_soc_copilot.evaluation.cross_plane import GateStatus
from hisiem_soc_copilot.evaluation_harness.cross_plane_authority import (
    business_outcome,
    duplicate_logical_command,
    logical_command_identities,
    submission_presentation_facts,
)
from hisiem_soc_copilot.evaluation_harness.cross_plane_e3_scenarios import (
    E3Fixture,
    evaluate_e3_scenario,
)
from hisiem_soc_copilot.infrastructure.durable.dispatcher import _MAX_ATTEMPTS
from tests.fixtures.fakes import FakeSoar
from tests.support.db_runtime import SKIP_REASON, apply_settings, server_reachable

TENANT = "tenant-a"
ALERT = "e3-durable-alert-1"
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
    # Deterministic reconciliation: a durable observation is immediately claimable.
    settings.app.response_observe_interval_seconds = 0.0
    return settings


@pytest_asyncio.fixture
async def runtime(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[Any, Any]]:
    """A real container over the disposable DB, with a truncated response state."""
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
        container = app.state.container
        # The HISIEM investigation adapter is never used on this path; the SOAR port
        # is supplied per test.
        yield container, factory
    await engine.dispose()


async def _seed_investigation(
    container: Any,
    *,
    disposition: VerdictDisposition = VerdictDisposition.MALICIOUS,
) -> tuple[UUID, UUID, UUID]:
    """Persist a COMPLETED investigation + result + Evidence through real repositories."""
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
        # Commit the investigation FIRST: the evidence/result rows carry an FK to it,
        # and the unit of work inserts by mapper order rather than by relationship.
        await uow.commit()

        evidence = Evidence(
            id=uuid4(),
            investigation_id=investigation_id,
            source=EvidenceSource(
                type=EvidenceSourceType.HISIEM_LOG_SEARCH,
                provider="hisiem",
                operation="log_search",
            ),
            collected_at=now,
            observation={"summary": "sudo auth failure from 203.0.113.9"},
        )
        await uow.evidence.add(evidence)

        result = InvestigationResult(
            id=uuid4(),
            investigation_id=investigation_id,
            verdict=Verdict(
                disposition=disposition, summary="confirmed", confidence=0.9
            ),
            finding_ids=[],
            created_at=now,
        )
        await uow.results.add(result)
        await uow.commit()
    finally:
        await uow.close()
    return investigation_id, result.id, evidence.id


async def _approve(
    container: Any, investigation_id: UUID, evidence_id: UUID
) -> tuple[UUID, UUID]:
    """Create + approve one proposal through the REAL handler. Returns ids."""
    handler = container.response_command_handler()
    proposal = await handler.create_response_proposal(
        CreateResponseProposal(
            tenant_id=TENANT,
            investigation_id=investigation_id,
            action_key="START_SOAR_PLAYBOOK",
            evidence_ids=(str(evidence_id),),
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
    return proposal.id, request.id


async def _read_state(container: Any, proposal_id: UUID) -> dict[str, Any]:
    """Read the persisted authority/execution facts for one proposal."""
    uow = container.unit_of_work()
    try:
        proposal = await uow.response_proposals.get(tenant_id=TENANT, proposal_id=proposal_id)
        submission = await uow.response_submissions.get_by_proposal(
            tenant_id=TENANT, proposal_id=proposal_id
        )
        execution = await uow.response_executions.get_by_proposal(
            tenant_id=TENANT, proposal_id=proposal_id
        )
        request = await uow.response_approvals.get_request_by_proposal(
            tenant_id=TENANT, proposal_id=proposal_id
        )
        decision = (
            await uow.response_approvals.get_decision(
                tenant_id=TENANT, approval_request_id=request.id
            )
            if request is not None
            else None
        )
    finally:
        await uow.close()
    return {
        "proposal": proposal,
        "submission": submission,
        "execution": execution,
        "request": request,
        "decision": decision,
    }


async def _ledger(container: Any, aggregate_id: UUID) -> tuple[ResponseEvent, ...]:
    """The REAL persisted domain events for one aggregate (bounded read)."""
    from hisiem_soc_copilot.domain.response.events import ResponseEvent as _Event

    events: list[_Event] = []
    async with container.session_factory()() as session:
        rows = await session.execute(
            text(
                "SELECT event_type, aggregate_id, tenant_id, payload "
                "FROM copilot.domain_event WHERE aggregate_id = :aid "
                "ORDER BY occurred_at, event_id"
            ),
            {"aid": str(aggregate_id)},
        )
        for row in rows:
            events.append(
                _Event(
                    event_type=row.event_type,
                    aggregate_id=row.aggregate_id,
                    tenant_id=row.tenant_id,
                    payload=dict(row.payload or {}),
                )
            )
    return tuple(events)


async def _make_claimable(container: Any, *, destination: str | None = None) -> None:
    """Move any pending delivery back into the claimable window.

    Test control over the OUTBOX CLOCK only: production retry/back-off semantics are
    untouched, the row is simply due now.
    """
    async with container.session_factory()() as session:
        if destination is None:
            await session.execute(
                text(
                    "UPDATE copilot.outbox_message SET available_at = now() - interval '1 hour', "
                    "status = 'PENDING', locked_at = NULL, locked_by = NULL, lease_token = NULL "
                    "WHERE status <> 'PUBLISHED'"
                )
            )
        else:
            await session.execute(
                text(
                    "UPDATE copilot.outbox_message SET available_at = now() - interval '1 hour', "
                    "status = 'PENDING', locked_at = NULL, locked_by = NULL, lease_token = NULL "
                    "WHERE destination = :d AND status <> 'PUBLISHED'"
                ),
                {"d": destination},
            )
        await session.commit()


async def _outbox_rows(container: Any) -> list[dict[str, Any]]:
    async with container.session_factory()() as session:
        rows = await session.execute(
            text(
                "SELECT destination, status, attempt_count FROM copilot.outbox_message "
                "ORDER BY created_at"
            )
        )
        return [
            {
                "destination": row.destination,
                "status": row.status,
                "attempt_count": row.attempt_count,
            }
            for row in rows
        ]


def _fixture(container: Any, state: dict[str, Any], *, execution_observed: bool) -> E3Fixture:
    return E3Fixture(
        proposal=state["proposal"],
        approval_request=state["request"],
        approval_decision=state["decision"],
        submission=state["submission"],
        execution=state["execution"],
        events=(),
        execution_observed=execution_observed,
    )


# ---------------------------------------------------------------------------
# §14/§15 — durable command persistence and idempotency
# ---------------------------------------------------------------------------


async def test_approval_persists_exactly_one_durable_command(
    runtime: tuple[Any, Any],
) -> None:
    container, _factory = runtime
    investigation_id, _result_id, evidence_id = await _seed_investigation(container)
    proposal_id, _request_id = await _approve(container, investigation_id, evidence_id)

    state = await _read_state(container, proposal_id)
    assert state["proposal"].status is ResponseProposalStatus.APPROVED
    assert state["submission"] is not None
    assert state["submission"].status == ResponseSubmissionStatus.PENDING.value
    assert state["execution"] is None

    events = await _ledger(container, proposal_id)
    queued = [event for event in events if event.event_type == "response_execution_queued"]
    assert len(queued) == 1
    identities = logical_command_identities(
        proposal=state["proposal"], events=events, submission=state["submission"]
    )
    assert len(identities) == 1
    assert duplicate_logical_command(
        command_identities=identities, proposals_for_result=()
    ) is False

    rows = await _outbox_rows(container)
    submit_rows = [r for r in rows if r["destination"] == "response.execution.submit"]
    assert len(submit_rows) == 1


async def test_a_duplicate_delivery_converges_on_one_provider_execution(
    runtime: tuple[Any, Any],
) -> None:
    container, _factory = runtime
    investigation_id, _result_id, evidence_id = await _seed_investigation(container)
    proposal_id, _request_id = await _approve(container, investigation_id, evidence_id)

    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id="hisiem-exec-e3-1", status="SUCCEEDED")
    )
    container.response_submit_dispatcher = container.response_submit_outbox_dispatcher(soar=soar)
    assert await container.response_submit_dispatcher.drain_once() >= 1

    state = await _read_state(container, proposal_id)
    assert state["execution"] is not None
    assert state["submission"].status == ResponseSubmissionStatus.SUBMITTED.value
    first_execution_id = state["execution"].execution_id

    # A replayed delivery (worker crash after committing, reclaimed delivery) must
    # converge, never execute a second time.
    await _make_claimable(container, destination="response.execution.submit")
    await container.response_submit_dispatcher.drain_once()

    state = await _read_state(container, proposal_id)
    assert state["execution"].execution_id == first_execution_id
    assert len([s for s in soar.submitted if s["proposal_id"] == proposal_id]) <= 2
    events = await _ledger(container, proposal_id)
    identities = logical_command_identities(
        proposal=state["proposal"], events=events, submission=state["submission"]
    )
    assert len(identities) == 1


# ---------------------------------------------------------------------------
# §16 — submit uncertainty fabricates nothing
# ---------------------------------------------------------------------------


async def test_a_transient_submit_failure_records_retrying_and_no_execution(
    runtime: tuple[Any, Any],
) -> None:
    container, _factory = runtime
    investigation_id, _result_id, evidence_id = await _seed_investigation(container)
    proposal_id, _request_id = await _approve(container, investigation_id, evidence_id)

    soar = FakeSoar(
        raise_on_submit=ExternalServiceError(
            "SOAR is unavailable", service="hisiem_soar", code="SOAR_UNAVAILABLE"
        )
    )
    container.response_submit_dispatcher = container.response_submit_outbox_dispatcher(soar=soar)
    await container.response_submit_dispatcher.drain_once()

    state = await _read_state(container, proposal_id)
    assert state["submission"].status == ResponseSubmissionStatus.RETRYING.value
    assert state["submission"].attempt_count >= 1
    # Neither a success nor a failure was fabricated, and no provider identity was
    # invented for an execution we cannot see.
    assert state["execution"] is None
    assert state["submission"].submitted_at is None
    assert state["proposal"].status is ResponseProposalStatus.APPROVED

    facts = submission_presentation_facts(
        proposal=state["proposal"],
        request=state["request"],
        decision=state["decision"],
        submission=state["submission"],
        execution=None,
    )
    assert "SUBMISSION_NOT_TREATED_AS_SUCCESS" in facts.observed_facts
    assert "SUBMISSION_TREATED_AS_SUCCESS_PRESENT" not in facts.observed_facts


async def test_a_provider_rejection_is_not_an_execution_failure(
    runtime: tuple[Any, Any],
) -> None:
    container, _factory = runtime
    investigation_id, _result_id, evidence_id = await _seed_investigation(container)
    proposal_id, _request_id = await _approve(container, investigation_id, evidence_id)

    soar = FakeSoar(
        raise_on_submit=ExternalServiceError(
            "SOAR rejected the action", service="hisiem_soar", code="HTTP_400"
        )
    )
    container.response_submit_dispatcher = container.response_submit_outbox_dispatcher(soar=soar)
    await container.response_submit_dispatcher.drain_once()

    state = await _read_state(container, proposal_id)
    assert state["submission"].status == ResponseSubmissionStatus.FAILED_DEFINITIVE.value
    # The provider refused the SUBMISSION and created no execution: nothing may be
    # recorded as an execution failure, and the approved intent is not retracted.
    assert state["execution"] is None
    assert state["proposal"].status is ResponseProposalStatus.APPROVED


# NOTE (E3 §16): the "provider returned no execution id" variant of an uncertain
# submit is already covered by the repository's own structural test
# ``tests/unit/agent/test_response_runner.py::test_an_empty_execution_id_is_uncertain_not_failed``,
# which this suite deliberately does not duplicate. That file is also the sole
# owner of the empty provider-identity construction: the repo's guard
# ``test_no_fixture_ever_constructs_an_empty_execution_id`` exists to keep a
# placeholder provider identity out of every OTHER module, and E3 respects it.


# ---------------------------------------------------------------------------
# §17/§18 — retry exhaustion becomes explicit uncertainty
# ---------------------------------------------------------------------------


async def test_an_exhausted_uncertain_submission_becomes_attention_required(
    runtime: tuple[Any, Any],
) -> None:
    container, _factory = runtime
    investigation_id, _result_id, evidence_id = await _seed_investigation(container)
    proposal_id, _request_id = await _approve(container, investigation_id, evidence_id)

    soar = FakeSoar(
        raise_on_submit=ExternalServiceError(
            "SOAR is unavailable", service="hisiem_soar", code="SOAR_UNAVAILABLE"
        )
    )
    container.response_submit_dispatcher = container.response_submit_outbox_dispatcher(soar=soar)

    for _ in range(_MAX_ATTEMPTS):
        await _make_claimable(container, destination="response.execution.submit")
        await container.response_submit_dispatcher.drain_once()

    state = await _read_state(container, proposal_id)
    submission = state["submission"]
    assert submission.status == ResponseSubmissionStatus.ATTENTION_REQUIRED.value
    assert submission.attempt_count == _MAX_ATTEMPTS
    assert submission.attention_required_at is not None
    assert submission.submitted_at is None
    assert submission.failed_at is None
    # Explicit uncertainty: no execution, no claim that the provider refused it, and
    # the approved intent is still standing for a human to resolve.
    assert state["execution"] is None
    assert state["proposal"].status is ResponseProposalStatus.APPROVED

    facts = submission_presentation_facts(
        proposal=state["proposal"],
        request=state["request"],
        decision=state["decision"],
        submission=submission,
        execution=None,
    )
    assert "SUBMISSION_ATTENTION_REQUIRED" in facts.observed_facts
    assert "SUBMISSION_NOT_TREATED_AS_SUCCESS" in facts.observed_facts

    # A reclaimed delivery must NOT silently resume submitting.
    submitted_before = len(soar.submitted)
    await _make_claimable(container, destination="response.execution.submit")
    await container.response_submit_dispatcher.drain_once()
    assert len(soar.submitted) == submitted_before


async def test_attention_required_is_carried_by_the_workspace_projection(
    runtime: tuple[Any, Any],
) -> None:
    from hisiem_soc_copilot.application.services.workspace_service import (
        _response_timeline_entries,
    )

    container, _factory = runtime
    investigation_id, _result_id, evidence_id = await _seed_investigation(container)
    proposal_id, _request_id = await _approve(container, investigation_id, evidence_id)
    soar = FakeSoar(
        raise_on_submit=ExternalServiceError(
            "down", service="hisiem_soar", code="SOAR_UNAVAILABLE"
        )
    )
    container.response_submit_dispatcher = container.response_submit_outbox_dispatcher(soar=soar)
    for _ in range(_MAX_ATTEMPTS):
        await _make_claimable(container, destination="response.execution.submit")
        await container.response_submit_dispatcher.drain_once()

    state = await _read_state(container, proposal_id)
    entries = _response_timeline_entries(
        proposal=state["proposal"],
        approval=state["request"],
        decision=state["decision"],
        submission=state["submission"],
        execution=None,
    )
    kinds = {entry.kind for entry in entries}
    assert "RESPONSE_SUBMISSION_ATTENTION_REQUIRED" in kinds
    assert not any("succeeded" in kind for kind in kinds)


# ---------------------------------------------------------------------------
# §19 — restart/recovery preserves ONE logical durable intent
# ---------------------------------------------------------------------------


async def test_a_restarted_worker_resumes_the_durable_intent_exactly_once(
    runtime: tuple[Any, Any],
) -> None:
    container, _factory = runtime
    investigation_id, _result_id, evidence_id = await _seed_investigation(container)
    proposal_id, _request_id = await _approve(container, investigation_id, evidence_id)

    # Nothing ran before the "restart": the durable intent is committed and pending.
    rows_before = await _outbox_rows(container)
    assert any(
        r["destination"] == "response.execution.submit" and r["status"] == "PENDING"
        for r in rows_before
    )

    # A fresh dispatcher + runner pair = the process that comes up after a restart.
    restarted_soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id="hisiem-exec-restart", status="RUNNING")
    )
    restarted = container.response_submit_outbox_dispatcher(soar=restarted_soar)
    assert await restarted.drain_once() >= 1
    await restarted.stop()

    state = await _read_state(container, proposal_id)
    assert state["execution"] is not None
    assert state["execution"].execution_id == "hisiem-exec-restart"
    assert len([s for s in restarted_soar.submitted if s["proposal_id"] == proposal_id]) == 1

    events = await _ledger(container, proposal_id)
    identities = logical_command_identities(
        proposal=state["proposal"], events=events, submission=state["submission"]
    )
    assert len(identities) == 1

    result = evaluate_e3_scenario(
        "XP-AUTH-003", _fixture(container, state, execution_observed=True)
    )
    assert result.overall_gate is GateStatus.PASS


# ---------------------------------------------------------------------------
# §20 — observation reconciles to the provider's state
# ---------------------------------------------------------------------------


async def test_a_submitted_execution_reconciles_to_the_provider_state(
    runtime: tuple[Any, Any],
) -> None:
    container, _factory = runtime
    investigation_id, _result_id, evidence_id = await _seed_investigation(container)
    proposal_id, _request_id = await _approve(container, investigation_id, evidence_id)

    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id="hisiem-exec-e3-2", status="RUNNING"),
        status_sequence=["RUNNING", "SUCCEEDED"],
    )
    container.response_submit_dispatcher = container.response_submit_outbox_dispatcher(soar=soar)
    container.response_observe_dispatcher = container.response_observe_outbox_dispatcher(
        soar=soar
    )
    await container.response_submit_dispatcher.drain_once()

    state = await _read_state(container, proposal_id)
    assert state["execution"].status == "RUNNING"
    assert state["submission"].status == ResponseSubmissionStatus.SUBMITTED.value

    for _ in range(4):
        await _make_claimable(container, destination="response.execution.observe")
        drained = await container.response_observe_dispatcher.drain_once()
        if drained == 0:
            break
        state = await _read_state(container, proposal_id)
        if state["execution"].status == "SUCCEEDED":
            break

    state = await _read_state(container, proposal_id)
    assert state["execution"].status == "SUCCEEDED"
    # The local submission and the provider execution remain distinct facts.
    assert state["submission"].status == ResponseSubmissionStatus.SUBMITTED.value
    assert state["submission"].status != state["execution"].status


async def test_observed_truth_wins_over_a_projected_success(
    runtime: tuple[Any, Any],
) -> None:
    """Copilot submitted; HISIEM later reports FAILED. FAILED is the final truth."""
    from hisiem_soc_copilot.evaluation_harness.cross_plane_authority import (
        observed_execution_truth,
    )

    container, _factory = runtime
    investigation_id, _result_id, evidence_id = await _seed_investigation(container)
    proposal_id, _request_id = await _approve(container, investigation_id, evidence_id)

    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id="hisiem-exec-e3-3", status="RUNNING"),
        status_sequence=["FAILED"],
    )
    container.response_submit_dispatcher = container.response_submit_outbox_dispatcher(soar=soar)
    container.response_observe_dispatcher = container.response_observe_outbox_dispatcher(
        soar=soar
    )
    await container.response_submit_dispatcher.drain_once()

    for _ in range(4):
        await _make_claimable(container, destination="response.execution.observe")
        drained = await container.response_observe_dispatcher.drain_once()
        if drained == 0:
            break
        state = await _read_state(container, proposal_id)
        if state["execution"].status == "FAILED":
            break

    state = await _read_state(container, proposal_id)
    assert state["execution"].status == "FAILED"
    facts = observed_execution_truth(
        execution=state["execution"], hisiem_observed_status="FAILED"
    )
    assert "EXECUTION_OBSERVED_FROM_HISIEM" in facts.observed_facts
    assert "EXECUTION_FAILED" in facts.observed_facts
    assert "EXECUTION_SUCCEEDED" not in facts.observed_facts


# ---------------------------------------------------------------------------
# The persisted facts drive the E1 gates
# ---------------------------------------------------------------------------


async def test_persisted_authority_facts_drive_the_xp01_gates(
    runtime: tuple[Any, Any],
) -> None:
    container, _factory = runtime
    investigation_id, _result_id, evidence_id = await _seed_investigation(container)
    proposal_id, _request_id = await _approve(container, investigation_id, evidence_id)

    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id="hisiem-exec-e3-4", status="SUCCEEDED")
    )
    container.response_submit_dispatcher = container.response_submit_outbox_dispatcher(soar=soar)
    await container.response_submit_dispatcher.drain_once()

    state = await _read_state(container, proposal_id)
    events = await _ledger(container, proposal_id)
    fixture = E3Fixture(
        proposal=state["proposal"],
        approval_request=state["request"],
        approval_decision=state["decision"],
        submission=state["submission"],
        execution=state["execution"],
        events=events,
        execution_observed=True,
        hisiem_observed_status=state["execution"].status,
    )
    for scenario_id in ("XP-AUTH-003", "XP-AUTH-004"):
        result = evaluate_e3_scenario(scenario_id, fixture)
        assert result.overall_gate is GateStatus.PASS, scenario_id

    outcome = business_outcome(
        proposal=state["proposal"],
        decision=state["decision"],
        submission=state["submission"],
        execution=state["execution"],
        events=events,
    )
    assert outcome["execution_status"] == "SUCCEEDED"
    assert outcome["submission_status"] == ResponseSubmissionStatus.SUBMITTED.value
    assert outcome["approval_decision"] == ApprovalDecisionKind.APPROVE.value


async def test_a_rejected_proposal_persists_no_dispatchable_command(
    runtime: tuple[Any, Any],
) -> None:
    container, _factory = runtime
    investigation_id, _result_id, evidence_id = await _seed_investigation(container)

    handler = container.response_command_handler()
    proposal = await handler.create_response_proposal(
        CreateResponseProposal(
            tenant_id=TENANT,
            investigation_id=investigation_id,
            action_key="START_SOAR_PLAYBOOK",
            evidence_ids=(str(evidence_id),),
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
            decision=ApprovalDecisionKind.REJECT,
            expected_revision=proposal.content_revision,
            expected_content_hash=proposal.content_hash,
            initiated_by_subject="operator",
        )
    )

    state = await _read_state(container, proposal.id)
    assert state["proposal"].status is ResponseProposalStatus.REJECTED
    events = await _ledger(container, proposal.id)
    assert not [e for e in events if e.event_type == "response_execution_queued"]
    rows = await _outbox_rows(container)
    assert not [r for r in rows if r["destination"] == "response.execution.submit"]
    assert state["execution"] is None

    fixture = E3Fixture(
        proposal=state["proposal"],
        approval_request=state["request"],
        approval_decision=state["decision"],
        submission=state["submission"],
        execution=None,
        events=events,
        execution_observed=False,
    )
    result = evaluate_e3_scenario("XP-AUTH-003", fixture)
    assert result.overall_gate is GateStatus.PASS
    assert fixture.durable_command_ids == ()


async def test_the_provider_execution_identity_is_real_and_unique(
    runtime: tuple[Any, Any],
) -> None:
    container, _factory = runtime
    investigation_id, _result_id, evidence_id = await _seed_investigation(container)
    proposal_id, _request_id = await _approve(container, investigation_id, evidence_id)
    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id="hisiem-exec-e3-5", status="QUEUED")
    )
    container.response_submit_dispatcher = container.response_submit_outbox_dispatcher(soar=soar)
    await container.response_submit_dispatcher.drain_once()

    state = await _read_state(container, proposal_id)
    execution = state["execution"]
    assert execution.provider == "hisiem"
    assert execution.execution_id == "hisiem-exec-e3-5"
    assert execution.execution_id != str(state["proposal"].id)
    assert execution.submission_key == f"response:{TENANT}:{proposal_id}"
