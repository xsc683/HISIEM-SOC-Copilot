"""Workspace read service tests over in-memory fakes (docs §8/§10/§32).

Covers composition from persisted facts, deterministic ordering, Finding→Evidence
citation preservation, latest-assessment selection, partial states, tenant
isolation, and the read-only invariant (no commit, no domain mutation).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from hisiem_soc_copilot.application.errors import NotFoundError
from hisiem_soc_copilot.application.ports.durable import ToolInvocationRecord
from hisiem_soc_copilot.application.services.workspace_service import (
    TL_EVIDENCE_RECORDED,
    TL_FINDING_RECORDED,
    TL_INVESTIGATION_CANCELLED,
    TL_INVESTIGATION_COMPLETED,
    TL_INVESTIGATION_CREATED,
    TL_INVESTIGATION_STARTED,
    TL_PLAN_CREATED,
    TL_PLAN_REVISED,
    TL_RESULT_FINALIZED,
    TL_TOOL_STARTED,
    TL_TOOL_SUCCEEDED,
    InvestigationWorkspaceService,
)
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.entities import (
    AttackMapping,
    EntityRef,
    Evidence,
    EvidenceSource,
    Finding,
    Hypothesis,
    HypothesisAssessment,
    HypothesisAssessmentEvidence,
    InvestigationResult,
    PlanRevision,
    PlanStep,
    ResponseRecommendation,
    Uncertainty,
    Verdict,
)
from hisiem_soc_copilot.domain.investigation.enums import (
    EvidenceRelation,
    EvidenceSourceType,
    HypothesisStatus,
    VerdictDisposition,
)
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from tests.fixtures.fakes import FakeUnitOfWorkFactory

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"
ALERT = "alert-0001"
T0 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)


def _t(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _ref() -> ExternalResourceRef:
    return ExternalResourceRef(
        provider="hisiem", resource_type="alert", address_id=ALERT, business_id="AL-1"
    )


def _running(investigation_id: UUID) -> Investigation:
    inv = Investigation.create(
        id=investigation_id,
        tenant_id=TENANT,
        source_alert_ref=_ref(),
        initiated_by=ActorRef(subject_id="analyst", tenant_id=TENANT),
        budget_limits=BudgetLimits(),
        now=T0,
    )
    inv.start(actor=ActorRef(subject_id="analyst", tenant_id=TENANT), now=_t(1))
    return inv


def _evidence(investigation_id: UUID) -> Evidence:
    evidence = Evidence(
        id=uuid4(),
        investigation_id=investigation_id,
        source=EvidenceSource(
            type=EvidenceSourceType.HISIEM_LOG_SEARCH,
            provider="hisiem",
            operation="search_events",
        ),
        collected_at=_t(4),
        observation={"event.action": "authentication_success", "user.name": "root"},
        observed_at=_t(3),
        summary="Successful SSH auth from 10.0.0.9",
        raw_reference={
            "index": "siem-events-2026.09.07",
            "document_id": "doc-1",
            "query_fingerprint": "fp-1",
        },
        entity_refs=[EntityRef(kind="user", value="root")],
        content_hash="hash-1",
        dedup_key="dedup-1",
    )
    return evidence


async def _seed(factory: FakeUnitOfWorkFactory) -> dict[str, object]:
    """Seed a COMPLETED investigation with a full child set + a tool audit row."""
    investigation_id = uuid4()
    inv = _running(investigation_id)
    inv.complete_without_response()
    inv.current_plan_revision = 2

    evidence = _evidence(investigation_id)
    finding_id = uuid4()
    hypothesis_id = uuid4()
    assessment_1 = HypothesisAssessment(
        id=uuid4(),
        hypothesis_id=hypothesis_id,
        investigation_id=investigation_id,
        revision=1,
        status=HypothesisStatus.OPEN,
        evidence_relations=[],
        reason_summary="initial",
        created_at=_t(5),
    )
    assessment_2 = HypothesisAssessment(
        id=uuid4(),
        hypothesis_id=hypothesis_id,
        investigation_id=investigation_id,
        revision=2,
        status=HypothesisStatus.SUPPORTED,
        evidence_relations=[
            HypothesisAssessmentEvidence(
                evidence_id=evidence.id, relation=EvidenceRelation.SUPPORTS
            )
        ],
        reason_summary="supported by S1",
        created_at=_t(6),
    )

    uow = factory()
    await uow.investigations.add(inv)
    await uow.plan_revisions.add(
        PlanRevision(
            id=uuid4(),
            investigation_id=investigation_id,
            revision=1,
            goal="triage",
            steps=[PlanStep(step_id="s1", objective="collect auth logs", ordinal=1)],
            created_at=_t(2),
        )
    )
    await uow.plan_revisions.add(
        PlanRevision(
            id=uuid4(),
            investigation_id=investigation_id,
            revision=2,
            goal="triage",
            steps=[PlanStep(step_id="s1", objective="collect auth logs", ordinal=1)],
            created_at=_t(3),
        )
    )
    await uow.evidence.add(evidence)
    await uow.hypotheses.add(
        Hypothesis(
            id=hypothesis_id,
            investigation_id=investigation_id,
            statement="root login is malicious",
            status=HypothesisStatus.SUPPORTED,
            assessment_revision=2,
            created_at=_t(4),
            updated_at=_t(6),
        )
    )
    await uow.hypothesis_assessments.add(assessment_1)
    await uow.hypothesis_assessments.add(assessment_2)
    await uow.findings.add(
        Finding(
            id=finding_id,
            investigation_id=investigation_id,
            statement="root authenticated successfully",
            evidence_citations=[evidence.id],
            created_at=_t(7),
        )
    )
    await uow.results.add(
        InvestigationResult(
            id=uuid4(),
            investigation_id=investigation_id,
            verdict=Verdict(
                disposition=VerdictDisposition.MALICIOUS,
                summary="confirmed intrusion",
                confidence=0.8,
            ),
            finding_ids=[finding_id],
            uncertainties=[
                Uncertainty(
                    description="lateral movement not confirmed",
                    missing_information="edr telemetry",
                )
            ],
            attack_mappings=[
                AttackMapping(technique_id="T1078", name="Valid Accounts")
            ],
            response_recommendations=[
                ResponseRecommendation(description="disable user", reason="T1078")
            ],
            created_at=_t(8),
        )
    )
    await factory.tool_invocations.add_started(
        tenant_id=TENANT,
        record=ToolInvocationRecord(
            id=uuid4(),
            investigation_id=investigation_id,
            tool_name="search_events",
            idempotency_key="k1",
            status="RUNNING",
            started_at=_t(4),
        ),
    )
    await factory.tool_invocations.finish(
        tenant_id=TENANT,
        investigation_id=investigation_id,
        idempotency_key="k1",
        status="SUCCEEDED",
        finished_at=_t(4) + timedelta(milliseconds=250),
    )
    return {
        "investigation_id": investigation_id,
        "evidence_id": evidence.id,
        "finding_id": finding_id,
        "hypothesis_id": hypothesis_id,
    }


async def test_full_workspace_composition() -> None:
    factory = FakeUnitOfWorkFactory()
    ids = await _seed(factory)
    uow = factory()
    service = InvestigationWorkspaceService(unit_of_work=uow)

    ws = await service.get_workspace(
        tenant_id=TENANT, investigation_id=ids["investigation_id"]  # type: ignore[arg-type]
    )

    assert ws.investigation.status == "COMPLETED"
    assert ws.investigation.current_plan_revision == 2
    assert ws.investigation.termination_reason == "COMPLETED_WITHOUT_RESPONSE"
    assert ws.source_alert_ref.address_id == ALERT
    assert ws.source_alert_ref.business_id == "AL-1"

    assert [p.revision for p in ws.plan_revisions] == [1, 2]
    assert ws.plan_revisions[0].steps[0].step_id == "s1"

    assert len(ws.evidence) == 1
    ev = ws.evidence[0]
    assert ev.observed_at == _t(3)
    assert ev.source.provider == "hisiem"
    assert ev.source.operation == "search_events"
    assert ev.raw_reference == {
        "index": "siem-events-2026.09.07",
        "document_id": "doc-1",
        "query_fingerprint": "fp-1",
    }
    assert ev.content_hash == "hash-1"
    assert ev.entity_refs[0].kind == "user"

    # latest assessment is revision 2 (by revision, not insertion order)
    assert len(ws.hypotheses) == 1
    assert ws.hypotheses[0].latest_assessment is not None
    assert ws.hypotheses[0].latest_assessment.revision == 2  # type: ignore[union-attr]
    assert ws.hypotheses[0].latest_assessment.status == "SUPPORTED"  # type: ignore[union-attr]
    assert (
        ws.hypotheses[0].latest_assessment.evidence_relations[0].evidence_id  # type: ignore[union-attr]
        == ids["evidence_id"]
    )

    # Finding citations preserved as IDs; in_result reflects the finalized result
    assert len(ws.findings) == 1
    assert ws.findings[0].evidence_citations == (ids["evidence_id"],)
    assert ws.findings[0].in_result is True

    assert ws.result is not None
    assert ws.result.verdict.disposition == "MALICIOUS"
    assert ws.result.finding_ids == (ids["finding_id"],)
    assert ws.result.uncertainties[0].missing_information == "edr telemetry"
    assert ws.result.attack_mappings[0].technique_id == "T1078"
    assert ws.result.response_recommendations[0].description == "disable user"

    assert len(ws.tool_activity) == 1
    assert ws.tool_activity[0].tool_name == "search_events"
    assert ws.tool_activity[0].status == "SUCCEEDED"
    assert ws.tool_activity[0].duration_ms == 250


async def test_timeline_is_deterministic_and_ordered() -> None:
    factory = FakeUnitOfWorkFactory()
    ids = await _seed(factory)

    first = await InvestigationWorkspaceService(
        unit_of_work=factory()
    ).get_workspace(tenant_id=TENANT, investigation_id=ids["investigation_id"])  # type: ignore[arg-type]
    second = await InvestigationWorkspaceService(
        unit_of_work=factory()
    ).get_workspace(tenant_id=TENANT, investigation_id=ids["investigation_id"])  # type: ignore[arg-type]

    assert first.timeline == second.timeline

    times = [e.occurred_at for e in first.timeline]
    assert times == sorted(times), "timeline must be ordered by occurred_at ASC"

    kinds = {e.kind for e in first.timeline}
    assert TL_INVESTIGATION_CREATED in kinds
    assert TL_INVESTIGATION_STARTED in kinds
    assert TL_PLAN_CREATED in kinds
    assert TL_PLAN_REVISED in kinds
    assert TL_TOOL_STARTED in kinds
    assert TL_TOOL_SUCCEEDED in kinds
    assert TL_EVIDENCE_RECORDED in kinds
    assert TL_FINDING_RECORDED in kinds
    assert TL_RESULT_FINALIZED in kinds
    assert TL_INVESTIGATION_COMPLETED in kinds


async def test_read_performs_no_mutation_and_no_commit() -> None:
    factory = FakeUnitOfWorkFactory()
    ids = await _seed(factory)
    uow = factory()
    before = await uow.investigations.get(
        tenant_id=TENANT, investigation_id=ids["investigation_id"]  # type: ignore[arg-type]
    )
    assert before is not None
    snapshot = (before.status, before.revision, before.lock_version)

    service = InvestigationWorkspaceService(unit_of_work=uow)
    await service.get_workspace(
        tenant_id=TENANT, investigation_id=ids["investigation_id"]  # type: ignore[arg-type]
    )

    after = await uow.investigations.get(
        tenant_id=TENANT, investigation_id=ids["investigation_id"]  # type: ignore[arg-type]
    )
    assert after is not None
    assert (after.status, after.revision, after.lock_version) == snapshot
    assert uow.commits == 0


async def test_partial_running_state_renders_without_result_or_findings() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id = uuid4()
    uow = factory()
    await uow.investigations.add(_running(investigation_id))

    ws = await InvestigationWorkspaceService(unit_of_work=factory()).get_workspace(
        tenant_id=TENANT, investigation_id=investigation_id
    )

    assert ws.investigation.status == "RUNNING"
    assert ws.result is None
    assert ws.findings == ()
    assert ws.evidence == ()
    # created + started + phase settled: no terminal entry
    kinds = {e.kind for e in ws.timeline}
    assert TL_INVESTIGATION_CREATED in kinds
    assert TL_INVESTIGATION_COMPLETED not in kinds


async def test_cancelled_state_has_cancelled_timeline_kind() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id = uuid4()
    inv = Investigation.create(
        id=investigation_id,
        tenant_id=TENANT,
        source_alert_ref=_ref(),
        initiated_by=ActorRef(subject_id="analyst", tenant_id=TENANT),
        budget_limits=BudgetLimits(),
        now=T0,
    )
    inv.cancel(actor=ActorRef(subject_id="analyst", tenant_id=TENANT))
    await factory().investigations.add(inv)

    ws = await InvestigationWorkspaceService(unit_of_work=factory()).get_workspace(
        tenant_id=TENANT, investigation_id=investigation_id
    )
    assert ws.investigation.status == "CANCELLED"
    assert TL_INVESTIGATION_CANCELLED in {e.kind for e in ws.timeline}


async def test_wrong_tenant_is_not_found() -> None:
    factory = FakeUnitOfWorkFactory()
    ids = await _seed(factory)
    service = InvestigationWorkspaceService(unit_of_work=factory())
    with pytest.raises(NotFoundError):
        await service.get_workspace(
            tenant_id=OTHER_TENANT, investigation_id=ids["investigation_id"]  # type: ignore[arg-type]
        )


async def test_unknown_investigation_is_not_found() -> None:
    factory = FakeUnitOfWorkFactory()
    service = InvestigationWorkspaceService(unit_of_work=factory())
    with pytest.raises(NotFoundError):
        await service.get_workspace(tenant_id=TENANT, investigation_id=uuid4())


async def test_alert_lookup_returns_active_and_latest() -> None:
    factory = FakeUnitOfWorkFactory()
    active_id = uuid4()
    await factory().investigations.add(_running(active_id))

    service = InvestigationWorkspaceService(unit_of_work=factory())
    lookup = await service.lookup_alert_investigation(
        tenant_id=TENANT,
        provider="hisiem",
        resource_type="alert",
        address_id=ALERT,
    )
    assert lookup.active is not None
    assert lookup.active.investigation_id == active_id
    assert lookup.latest is not None
    assert lookup.latest.investigation_id == active_id


async def test_alert_lookup_isolated_by_tenant() -> None:
    factory = FakeUnitOfWorkFactory()
    await factory().investigations.add(_running(uuid4()))

    service = InvestigationWorkspaceService(unit_of_work=factory())
    lookup = await service.lookup_alert_investigation(
        tenant_id=OTHER_TENANT,
        provider="hisiem",
        resource_type="alert",
        address_id=ALERT,
    )
    assert lookup.active is None
    assert lookup.latest is None
