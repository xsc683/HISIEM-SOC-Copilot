"""Strict grounding invariants (follow-up Fix #2).

- SUPPORTED requires at least one SUPPORTS relation to resolvable evidence in this
  investigation; CONTRADICTED requires at least one CONTRADICTS. CONTEXT (rule
  metadata / unrelated events) is never sufficient, and a CONTRADICTS relation
  cannot ground a SUPPORTED verdict (nor SUPPORTS a CONTRADICTED one). Mismatched
  status → downgraded UNRESOLVED.
- Findings come ONLY from model Finding Candidates whose evidence citations resolve
  to real evidence in this investigation. There is NO generic "supports compromise"
  fallback: rule/CONTEXT evidence never becomes a supporting Finding, and a model
  that emits no valid finding yields ZERO persisted Findings.
- A MALICIOUS/BENIGN verdict with no grounded Finding is deterministically bounded
  to INCONCLUSIVE (never a guessed firm disposition, never FAILED).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from hisiem_soc_copilot.agent.evidence.normalizer import EvidenceNormalizer
from hisiem_soc_copilot.agent.graph.builder import build_investigation_graph, thread_config
from hisiem_soc_copilot.agent.graph.runtime import GraphRuntime
from hisiem_soc_copilot.agent.tools.executor import ToolExecutor
from hisiem_soc_copilot.agent.tools.registry import ToolRegistry
from hisiem_soc_copilot.application.handlers.workflow import InvestigationWorkflowHandler
from hisiem_soc_copilot.application.ports.model_provider import AssessRequest
from hisiem_soc_copilot.contracts.llm.types import (
    AssessmentEvidenceRelation,
    AssessmentSummary,
    FindingCandidate,
    HypothesisAssessmentCandidate,
)
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.enums import (
    HypothesisStatus,
    InvestigationStatus,
)
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from hisiem_soc_copilot.infrastructure.llm.scripted import ScriptedModelProvider
from tests.fixtures.fakes import FakeUnitOfWorkFactory
from tests.fixtures.hisiem_fake import FakeHisiem


def _start() -> tuple[FakeUnitOfWorkFactory, Investigation]:
    uows = FakeUnitOfWorkFactory()
    actor = ActorRef(subject_id="analyst", tenant_id="tenant-a")
    inv = Investigation.create(
        id=uuid4(),
        tenant_id="tenant-a",
        source_alert_ref=ExternalResourceRef(
            provider="hisiem", resource_type="alert", address_id="alert-x"
        ),
        initiated_by=actor,
        budget_limits=BudgetLimits(),
    )
    return uows, inv


async def _boot(uows: FakeUnitOfWorkFactory, inv: Investigation) -> None:
    uow = uows()
    await uow.investigations.add(inv)
    await uow.commit()
    inv.start(actor=inv.initiated_by)
    await uow.investigations.update(inv)
    await uow.commit()


def _script(disposition: str) -> dict[str, Any]:
    return {
        "plan_steps": {"search": "search"},
        "decide": [
            {
                "tool_name": "hisiem.search_events",
                "arguments": {
                    "from": "2026-09-01T09:55:00Z",
                    "to": "2026-09-01T10:05:00Z",
                    "conditions": [
                        {
                            "field": "event.action",
                            "operator": "is",
                            "value": "authentication_success",
                        }
                    ],
                },
            }
        ],
        "findings": [],
        "verdict": {
            "disposition": disposition,
            "summary": "verdict",
            "confidence": 0.8,
            "uncertainty": (
                "no grounded disposition available"
                if disposition == "INCONCLUSIVE"
                else None
            ),
        },
    }


class _StrictAssessModel(ScriptedModelProvider):
    """Drives assess with a caller-supplied candidate + findings fn."""

    def __init__(
        self,
        *,
        script: dict[str, Any] | None = None,
        assess_fn: Any = None,
    ) -> None:
        super().__init__(script=script)
        self._assess_fn = assess_fn

    async def assess(self, request: AssessRequest) -> AssessmentSummary:
        candidates = self._assess_fn(request.hypotheses or [], request.evidence or [])
        return AssessmentSummary(
            decision="FINALIZE",
            assessments=candidates,
            findings=[FindingCandidate(statement=f) for f in (self._findings or [])],
        )


async def _run_graph(
    model: Any,
) -> tuple[FakeUnitOfWorkFactory, Investigation]:
    uows, inv = _start()
    await _boot(uows, inv)
    hisiem = FakeHisiem(alert_id="alert-x")
    runtime = GraphRuntime(
        uow_factory=uows,
        workflow_handler=InvestigationWorkflowHandler(unit_of_work_factory=uows),
        model=model,
        executor=ToolExecutor(hisiem=hisiem),
        normalizer=EvidenceNormalizer(),
        registry=ToolRegistry(),
        hisiem=hisiem,
        tenant_id="tenant-a",
    )
    graph = build_investigation_graph(runtime)
    await graph.ainvoke({"investigation_id": str(inv.id)}, thread_config(str(inv.id)))
    return uows, inv


async def _hypothesis_status(uows: FakeUnitOfWorkFactory, inv: Investigation) -> HypothesisStatus:
    uow = uows()
    hypotheses = await uow.hypotheses.list_by_investigation(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    return hypotheses[0].status


def _event_id(evidence: list[dict[str, Any]]) -> str:
    return str(next(e["id"] for e in evidence if e["operation"] == "search_events"))


async def test_supported_with_only_contradicts_downgrades_to_unresolved() -> None:
    """A SUPPORTED claim backed ONLY by CONTRADICTS relations → UNRESOLVED (never
    SUPPORTED on the wrong-direction relation)."""
    def assess_fn(hypotheses: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[Any]:
        return [
            HypothesisAssessmentCandidate(
                hypothesis_id=str(hypotheses[0]["id"]),
                status="SUPPORTED",
                reason_summary="claims support but cites only CONTRADICTS evidence",
                evidence_relations=[
                    AssessmentEvidenceRelation(
                        evidence_id=_event_id(evidence), relation="CONTRADICTS"
                    )
                ],
            )
        ]

    model = _StrictAssessModel(
        script=_script("INCONCLUSIVE"), assess_fn=assess_fn
    )
    uows, inv = await _run_graph(model)
    assert await _hypothesis_status(uows, inv) == HypothesisStatus.UNRESOLVED


async def test_contradicted_with_only_supports_downgrades_to_unresolved() -> None:
    """A CONTRADICTED claim backed ONLY by SUPPORTS relations → UNRESOLVED."""
    def assess_fn(hypotheses: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[Any]:
        return [
            HypothesisAssessmentCandidate(
                hypothesis_id=str(hypotheses[0]["id"]),
                status="CONTRADICTED",
                reason_summary="claims contradicted but cites only SUPPORTS evidence",
                evidence_relations=[
                    AssessmentEvidenceRelation(
                        evidence_id=_event_id(evidence), relation="SUPPORTS"
                    )
                ],
            )
        ]

    model = _StrictAssessModel(
        script=_script("INCONCLUSIVE"), assess_fn=assess_fn
    )
    uows, inv = await _run_graph(model)
    assert await _hypothesis_status(uows, inv) == HypothesisStatus.UNRESOLVED


async def test_supported_with_mixed_relations_passes_when_supports_present() -> None:
    """Mixed relations are allowed as long as SUPPORTED has a real SUPPORTS relation."""
    def assess_fn(hypotheses: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[Any]:
        return [
            HypothesisAssessmentCandidate(
                hypothesis_id=str(hypotheses[0]["id"]),
                status="SUPPORTED",
                reason_summary="one SUPPORTS + one CONTEXT evidence",
                evidence_relations=[
                    AssessmentEvidenceRelation(
                        evidence_id=_event_id(evidence), relation="SUPPORTS"
                    ),
                    AssessmentEvidenceRelation(
                        evidence_id=_event_id(evidence), relation="CONTEXT"
                    ),
                ],
            )
        ]

    model = _StrictAssessModel(
        script=_script("INCONCLUSIVE"), assess_fn=assess_fn
    )
    uows, inv = await _run_graph(model)
    assert await _hypothesis_status(uows, inv) == HypothesisStatus.SUPPORTED


async def test_context_only_evidence_creates_no_support_finding() -> None:
    """CONTEXT-only evidence (rule metadata / unrelated events) must never become a
    supporting Finding. A model finding citing CONTEXT evidence is still a valid
    Finding (it cites a real evidence id) — but the model emitting NO finding here
    (only the default) must yield ZERO Findings."""
    def assess_fn(hypotheses: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[Any]:
        # The model produces an assessment but NO findings.
        return [
            HypothesisAssessmentCandidate(
                hypothesis_id=str(hypotheses[0]["id"]),
                status="UNRESOLVED",
                reason_summary="only context evidence",
                evidence_relations=[
                    AssessmentEvidenceRelation(
                        evidence_id=_event_id(evidence), relation="CONTEXT"
                    )
                ],
            )
        ]

    # The base model (with script findings []) emits no Finding → 0 persisted.
    model = _StrictAssessModel(script=_script("INCONCLUSIVE"), assess_fn=assess_fn)
    uows, inv = await _run_graph(model)
    uow = uows()
    findings = await uow.findings.list_by_investigation(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert findings == []  # no generic "supports compromise" fallback


async def test_model_no_valid_finding_yields_zero_findings() -> None:
    """A model finding that cites NO evidence (or unresolvable evidence) → ZERO
    Findings persisted (the old generic fallback is gone)."""
    def assess_fn(hypotheses: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[Any]:
        return [
            HypothesisAssessmentCandidate(
                hypothesis_id=str(hypotheses[0]["id"]),
                status="UNRESOLVED",
                reason_summary="no finding produced",
            )
        ]

    script = _script("INCONCLUSIVE")
    script["findings"] = ["no evidence cited here"]  # model finding w/o citation
    model = _StrictAssessModel(script=script, assess_fn=assess_fn)
    uows, inv = await _run_graph(model)
    uow = uows()
    findings = await uow.findings.list_by_investigation(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert findings == []


async def test_malicious_without_grounded_finding_bounded_to_inconclusive() -> None:
    """A MALICIOUS verdict with NO grounded Finding is deterministically bounded to
    INCONCLUSIVE (COMPLETED, never FAILED, never a guessed disposition)."""
    def assess_fn(hypotheses: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[Any]:
        return [
            HypothesisAssessmentCandidate(
                hypothesis_id=str(hypotheses[0]["id"]),
                status="SUPPORTED",
                reason_summary="model claims support but supplies no finding",
                evidence_relations=[
                    AssessmentEvidenceRelation(
                        evidence_id=_event_id(evidence), relation="SUPPORTS"
                    )
                ],
            )
        ]

    # MALICIOUS verdict with empty findings → no grounded Finding → INCONCLUSIVE.
    model = _StrictAssessModel(script=_script("MALICIOUS"), assess_fn=assess_fn)
    uows, inv = await _run_graph(model)
    uow = uows()
    completed = await uow.investigations.get(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert completed is not None
    assert completed.status == InvestigationStatus.COMPLETED
    result = await uow.results.get_by_investigation(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert result is not None
    assert result.verdict.disposition.value == "INCONCLUSIVE"
