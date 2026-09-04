"""Workflow handler tests (orchestrator/system commands) with in-memory fakes.

Covers the read-only Investigation loop's application commands and their domain
invariants:
- plan revision + hypotheses + phase changes;
- evidence batch with deterministic dedup (retry never duplicates);
- hypothesis assessment requires a SUPPORTED/CONTRADICTED EvidenceRelation;
- findings require grounded evidence (finding-without-evidence rejected);
- result invariants (MALICIOUS/BENIGN needs a Finding; INCONCLUSIVE needs an
  Uncertainty) and idempotent re-finalize.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from hisiem_soc_copilot.application.commands.investigation import (
    AssessHypotheses,
    ChangeInvestigationPhase,
    CompleteInvestigation,
    EvidenceObservation,
    FinalizeInvestigationResult,
    FindingCandidate,
    HypothesisAssessmentCandidate,
    HypothesisCandidate,
    PlanStepCandidate,
    RecordEvidenceBatch,
    RecordFindings,
    RegisterHypotheses,
    ResultVerdictCandidate,
    ReviseInvestigationPlan,
)
from hisiem_soc_copilot.application.handlers.workflow import InvestigationWorkflowHandler
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.enums import (
    HypothesisStatus,
    InvestigationPhase,
    InvestigationStatus,
    VerdictDisposition,
)
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from tests.fixtures.fakes import FakeUnitOfWorkFactory


def _investigation(
    tenant_id: str = "tenant-a", alert_id: str = "alert-ssh-1"
) -> Investigation:
    return Investigation.create(
        id=uuid4(),
        tenant_id=tenant_id,
        source_alert_ref=ExternalResourceRef(
            provider="hisiem", resource_type="alert", address_id=alert_id
        ),
        initiated_by=ActorRef(subject_id="analyst", tenant_id=tenant_id),
        budget_limits=BudgetLimits(),
    )


async def _run_investigation(uows: FakeUnitOfWorkFactory) -> Investigation:
    """Create + start an investigation and return the aggregate for workflow use."""
    uow = uows()
    inv = _investigation()
    await uow.investigations.add(inv)
    await uow.commit()
    inv.start(actor=inv.initiated_by)
    await uow.investigations.update(inv)
    await uow.commit()
    return inv


def _workflow(uows: FakeUnitOfWorkFactory) -> InvestigationWorkflowHandler:
    return InvestigationWorkflowHandler(unit_of_work_factory=uows)


async def test_plan_revise_and_register_hypotheses() -> None:
    uows = FakeUnitOfWorkFactory()
    inv = await _run_investigation(uows)
    handler = _workflow(uows)

    await handler.change_phase(
        ChangeInvestigationPhase(
            tenant_id=inv.tenant_id, investigation_id=inv.id, phase=InvestigationPhase.PLANNING
        )
    )
    _, plan = await handler.revise_plan(
        ReviseInvestigationPlan(
            tenant_id=inv.tenant_id,
            investigation_id=inv.id,
            revision=1,
            goal="SSH compromise check",
            steps=[
                PlanStepCandidate(step_key="s1", objective="search failures", ordinal=0),
                PlanStepCandidate(step_key="s2", objective="search success", ordinal=1),
            ],
        )
    )
    _, hyps = await handler.register_hypotheses(
        RegisterHypotheses(
            tenant_id=inv.tenant_id,
            investigation_id=inv.id,
            hypotheses=[HypothesisCandidate(statement="An attacker got in via SSH")],
        )
    )
    assert plan.revision == 1
    assert len(plan.steps) == 2
    assert hyps[0].status == HypothesisStatus.OPEN

    uow = uows.instances[-1]
    reloaded = await uow.investigations.get(
        tenant_id=inv.tenant_id, investigation_id=inv.id
    )
    assert reloaded is not None
    assert reloaded.phase == InvestigationPhase.PLANNING
    assert reloaded.current_plan_revision == 1


async def test_record_evidence_batch_dedup_no_duplicates() -> None:
    uows = FakeUnitOfWorkFactory()
    inv = await _run_investigation(uows)
    handler = _workflow(uows)

    def obs(doc: str) -> EvidenceObservation:
        return EvidenceObservation(
            source_type="HISIEM_LOG_SEARCH",
            source_provider="hisiem",
            source_operation="search_events",
            observation={"document_id": doc, "event.action": "authentication_success"},
            raw_reference={"document_id": doc, "query_fingerprint": "q1", "index": "i"},
        )

    cmd = RecordEvidenceBatch(
        tenant_id=inv.tenant_id,
        investigation_id=inv.id,
        observations=[obs("evt-a"), obs("evt-a"), obs("evt-b")],
    )
    _, recorded = await handler.record_evidence_batch(cmd)
    assert len(recorded) == 2  # evt-a deduped, evt-b recorded

    # Retry of the same batch must not create further rows.
    _, recorded_again = await handler.record_evidence_batch(cmd)
    assert recorded_again == []


async def test_assessment_requires_evidence_relation_for_supported() -> None:
    uows = FakeUnitOfWorkFactory()
    inv = await _run_investigation(uows)
    handler = _workflow(uows)
    _, hyps = await handler.register_hypotheses(
        RegisterHypotheses(
            tenant_id=inv.tenant_id,
            investigation_id=inv.id,
            hypotheses=[HypothesisCandidate(statement="SSH compromise")],
        )
    )
    with pytest.raises(ValueError):
        await handler.assess_hypotheses(
            AssessHypotheses(
                tenant_id=inv.tenant_id,
                investigation_id=inv.id,
                assessments=[
                    HypothesisAssessmentCandidate(
                        hypothesis_id=hyps[0].id,
                        status="SUPPORTED",
                        reason_summary="no evidence cited",
                        evidence_relations=[],
                    )
                ],
            )
        )


async def test_finding_without_evidence_rejected() -> None:
    uows = FakeUnitOfWorkFactory()
    inv = await _run_investigation(uows)
    handler = _workflow(uows)
    with pytest.raises(ValueError):
        await handler.record_findings(
            RecordFindings(
                tenant_id=inv.tenant_id,
                investigation_id=inv.id,
                findings=[FindingCandidate(statement="ungrounded", evidence_citations=[])],
            )
        )


async def test_finding_citing_unknown_evidence_rejected() -> None:
    uows = FakeUnitOfWorkFactory()
    inv = await _run_investigation(uows)
    handler = _workflow(uows)
    with pytest.raises(ValueError):
        await handler.record_findings(
            RecordFindings(
                tenant_id=inv.tenant_id,
                investigation_id=inv.id,
                findings=[
                    FindingCandidate(statement="x", evidence_citations=[uuid4()])
                ],
            )
        )


async def test_malicious_verdict_requires_grounded_finding() -> None:
    uows = FakeUnitOfWorkFactory()
    inv = await _run_investigation(uows)
    handler = _workflow(uows)

    _, evidence = await handler.record_evidence_batch(
        RecordEvidenceBatch(
            tenant_id=inv.tenant_id,
            investigation_id=inv.id,
            observations=[
                EvidenceObservation(
                    source_type="HISIEM_EVENT",
                    source_provider="hisiem",
                    source_operation="event",
                    observation={"event.action": "authentication_success"},
                    raw_reference={"document_id": "evt-a"},
                )
            ],
        )
    )
    _, findings = await handler.record_findings(
        RecordFindings(
            tenant_id=inv.tenant_id,
            investigation_id=inv.id,
            findings=[
                FindingCandidate(
                    statement="Root login succeeded from the brute-force source",
                    evidence_citations=[evidence[0].id],
                )
            ],
        )
    )
    _, result = await handler.finalize_result(
        FinalizeInvestigationResult(
            tenant_id=inv.tenant_id,
            investigation_id=inv.id,
            verdict=ResultVerdictCandidate(
                disposition=VerdictDisposition.MALICIOUS,
                summary="Confirmed SSH compromise",
                confidence=0.9,
            ),
            finding_ids=[f.id for f in findings],
        )
    )
    assert result.verdict.disposition == VerdictDisposition.MALICIOUS
    assert result.finding_ids == [f.id for f in findings]

    # Re-finalize is idempotent: the same immutable result is returned.
    _, again = await handler.finalize_result(
        FinalizeInvestigationResult(
            tenant_id=inv.tenant_id,
            investigation_id=inv.id,
            verdict=ResultVerdictCandidate(
                disposition=VerdictDisposition.BENIGN,
                summary="changed",
                confidence=0.1,
            ),
            finding_ids=[],
        )
    )
    assert again.id == result.id


async def test_verdict_without_findings_rejected() -> None:
    uows = FakeUnitOfWorkFactory()
    inv = await _run_investigation(uows)
    handler = _workflow(uows)
    with pytest.raises(ValueError):
        await handler.finalize_result(
            FinalizeInvestigationResult(
                tenant_id=inv.tenant_id,
                investigation_id=inv.id,
                verdict=ResultVerdictCandidate(
                    disposition=VerdictDisposition.BENIGN,
                    summary="no grounds",
                    confidence=0.1,
                ),
                finding_ids=[],
            )
        )


async def test_inconclusive_verdict_requires_uncertainty() -> None:
    uows = FakeUnitOfWorkFactory()
    inv = await _run_investigation(uows)
    handler = _workflow(uows)
    with pytest.raises(ValueError):
        await handler.finalize_result(
            FinalizeInvestigationResult(
                tenant_id=inv.tenant_id,
                investigation_id=inv.id,
                verdict=ResultVerdictCandidate(
                    disposition=VerdictDisposition.INCONCLUSIVE,
                    summary="no signal",
                    confidence=0.2,
                ),
                finding_ids=[],
            )
        )


async def test_complete_transitions_to_completed() -> None:
    uows = FakeUnitOfWorkFactory()
    inv = await _run_investigation(uows)
    handler = _workflow(uows)
    completed = await handler.complete(
        CompleteInvestigation(tenant_id=inv.tenant_id, investigation_id=inv.id)
    )
    assert completed.status == InvestigationStatus.COMPLETED
    assert completed.termination_reason is not None
