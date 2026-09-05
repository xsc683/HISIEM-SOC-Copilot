"""Grounded hypothesis assessment semantics.

The assess node must NOT auto-SUPPORT a hypothesis just because evidence exists,
and must not blanket-mark every evidence SUPPORTS. The model returns a structured
per-hypothesis assessment that cites specific evidence ids with a semantic
relation; the node resolves every citation strictly against evidence in the SAME
investigation. Rule metadata / unrelated context evidence can never, on its own,
make the account-compromise hypothesis SUPPORTED. A hypothesis the model cannot
ground is assessed UNRESOLVED.

These run the real graph with a model that grounds deterministically on the actual
evidence ids the assess node supplies (as a real provider would).
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


class _StructuredAssessModel(ScriptedModelProvider):
    """Drives the assess step with a caller-supplied per-hypothesis script.

    ``assess_script(hypotheses, evidence) -> list[HypothesisAssessmentCandidate]``
    lets a test express grounded (or ungrounded) candidates against the real ids
    the node passes.
    """

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
    model: Any, hisiem: FakeHisiem | None = None
) -> tuple[FakeUnitOfWorkFactory, Investigation]:
    uows, inv = _start()
    await _boot(uows, inv)
    hisiem = hisiem or FakeHisiem(alert_id="alert-x")
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


def _script(decide: list[dict[str, Any]]) -> dict[str, Any]:
    """decide steps: rule read first (metadata), then an event search."""
    return {
        "plan_steps": {"search": "search"},
        "decide": decide,
        "findings": [],
        "verdict": {
            "disposition": "INCONCLUSIVE",
            "summary": "assessment result",
            "confidence": 0.5,
            "uncertainty": "assessment outcome not grounded enough for a firm disposition",
        },
    }


async def _hypothesis_status(uows: FakeUnitOfWorkFactory, inv: Investigation) -> HypothesisStatus:
    uow = uows()
    hypotheses = await uow.hypotheses.list_by_investigation(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    return hypotheses[0].status


async def test_rule_metadata_only_never_auto_supports() -> None:
    """Only detection-rule metadata evidence exists → hypothesis is NOT auto-SUPPORTED.

    The assess node must refuse to let rule metadata alone support the
    account-compromise hypothesis. The structured candidate cites only the rule
    metadata (CONTEXT), which is not semantic support → the node downgrades the
    SUPPORTED claim to UNRESOLVED.
    """
    rule_search = {
        "tool_name": "hisiem.get_detection_rule",
        "arguments": {"rule_id": "ssh_brute_force"},
    }

    def assess_fn(hypotheses: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[Any]:
        rule_id = next(e["id"] for e in evidence if e["operation"] == "get_detection_rule")
        # Model claims SUPPORTED but only cites CONTEXT rule metadata → must be
        # downgraded because there is no semantic supporting evidence.
        return [
            HypothesisAssessmentCandidate(
                hypothesis_id=hypotheses[0]["id"],
                status="SUPPORTED",
                reason_summary="rule mentions brute force",
                evidence_relations=[
                    AssessmentEvidenceRelation(
                        evidence_id=rule_id, relation="CONTEXT"
                    )
                ],
            )
        ]

    model = _StructuredAssessModel(
        script=_script(decide=[rule_search, {"decision": "FINALIZE"}]),
        assess_fn=assess_fn,
    )
    uows, inv = await _run_graph(model)
    status = await _hypothesis_status(uows, inv)
    # CONTEXT-only "support" is not semantic → never SUPPORTED.
    assert status != HypothesisStatus.SUPPORTED
    assert status in (HypothesisStatus.UNRESOLVED, HypothesisStatus.CONTRADICTED)

    uow = uows()
    completed = await uow.investigations.get(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert completed is not None
    assert completed.status == InvestigationStatus.COMPLETED


async def test_irrelevant_event_is_context_not_support() -> None:
    """An unrelated event (e.g. a benign network log) is CONTEXT / UNRESOLVED, never
    grounds SUPPORT for the account-compromise hypothesis."""
    search = {
        "tool_name": "hisiem.search_events",
        "arguments": {
            "from": "2026-09-01T09:55:00Z",
            "to": "2026-09-01T10:05:00Z",
            "conditions": [
                {
                    "field": "event.action",
                    "operator": "is",
                    "value": "network_session",
                }
            ],
        },
    }

    def assess_fn(hypotheses: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[Any]:
        event_id = next(e["id"] for e in evidence if e["operation"] == "search_events")
        return [
            HypothesisAssessmentCandidate(
                hypothesis_id=hypotheses[0]["id"],
                status="UNRESOLVED",
                reason_summary="event is unrelated to account compromise",
                evidence_relations=[
                    AssessmentEvidenceRelation(
                        evidence_id=event_id, relation="CONTEXT"
                    )
                ],
            )
        ]

    model = _StructuredAssessModel(
        script=_script(decide=[search, {"decision": "FINALIZE"}]),
        assess_fn=assess_fn,
    )
    uows, inv = await _run_graph(model)
    assert await _hypothesis_status(uows, inv) == HypothesisStatus.UNRESOLVED


async def test_failed_then_successful_login_may_support() -> None:
    """Failed logins + a matching successful login → the hypothesis MAY be SUPPORTED
    (grounded on the successful login that follows the brute force)."""
    success_search = {
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

    def assess_fn(hypotheses: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[Any]:
        # FakeHisiem returns a successful login for authentication_success.
        success_id = next(
            e["id"]
            for e in evidence
            if e["operation"] == "search_events"
            and "authentication_success" in str(e.get("summary", ""))
        )
        return [
            HypothesisAssessmentCandidate(
                hypothesis_id=hypotheses[0]["id"],
                status="SUPPORTED",
                reason_summary="successful login followed the brute force",
                evidence_relations=[
                    AssessmentEvidenceRelation(
                        evidence_id=success_id, relation="SUPPORTS"
                    )
                ],
            )
        ]

    model = _StructuredAssessModel(
        script=_script(decide=[success_search, {"decision": "FINALIZE"}]),
        assess_fn=assess_fn,
    )
    uows, inv = await _run_graph(model)
    assert await _hypothesis_status(uows, inv) == HypothesisStatus.SUPPORTED


async def test_contradicting_evidence_supported_by_model_relation() -> None:
    """A model that sees contradicting evidence and returns a CONTRADICTED verdict on
    the specific evidence relation persists CONTRADICTED (mixed/contradicting as the
    candidate defines)."""
    success_search = {
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

    def assess_fn(hypotheses: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[Any]:
        # A success event exists, but the model judges it does NOT belong to the same
        # session (contradicts the compromise hypothesis), so it returns CONTRADICTED
        # with a CONTRADICTS relation on that evidence.
        event_id = next(
            e["id"]
            for e in evidence
            if e["operation"] == "search_events"
            and "authentication_success" in str(e.get("summary", ""))
        )
        return [
            HypothesisAssessmentCandidate(
                hypothesis_id=hypotheses[0]["id"],
                status="CONTRADICTED",
                reason_summary="the successful login is unrelated to the brute-force source",
                evidence_relations=[
                    AssessmentEvidenceRelation(
                        evidence_id=event_id, relation="CONTRADICTS"
                    )
                ],
            )
        ]

    model = _StructuredAssessModel(
        script=_script(decide=[success_search, {"decision": "FINALIZE"}]),
        assess_fn=assess_fn,
    )
    uows, inv = await _run_graph(model)
    assert await _hypothesis_status(uows, inv) == HypothesisStatus.CONTRADICTED


async def test_model_citing_unknown_evidence_id_is_rejected_not_supported() -> None:
    """The model cannot ground SUPPORTED on an evidence id that does not exist in this
    investigation — the citation is dropped and the hypothesis stays UNRESOLVED."""
    search = {
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

    def assess_fn(hypotheses: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[Any]:
        # Cites an evidence id from a DIFFERENT investigation that does not exist here.
        return [
            HypothesisAssessmentCandidate(
                hypothesis_id=hypotheses[0]["id"],
                status="SUPPORTED",
                reason_summary="grounded on evidence that does not exist here",
                evidence_relations=[
                    AssessmentEvidenceRelation(
                        evidence_id=str(uuid4()), relation="SUPPORTS"
                    )
                ],
            )
        ]

    model = _StructuredAssessModel(
        script=_script(decide=[search, {"decision": "FINALIZE"}]),
        assess_fn=assess_fn,
    )
    uows, inv = await _run_graph(model)
    # The unresolvable citation is dropped → not SUPPORTED.
    assert await _hypothesis_status(uows, inv) != HypothesisStatus.SUPPORTED
