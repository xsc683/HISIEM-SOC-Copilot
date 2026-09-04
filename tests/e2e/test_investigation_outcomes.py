"""Alternate investigation outcomes: BENIGN / INCONCLUSIVE / budget-exhausted /
tool-unavailable. All must reach COMPLETED without FAILED, per the budget and
tool-failure rules (application-commands...md §26, investigation-tool-contract §10).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from hisiem_soc_copilot.agent.evidence.normalizer import EvidenceNormalizer
from hisiem_soc_copilot.agent.graph.builder import build_investigation_graph, thread_config
from hisiem_soc_copilot.agent.graph.runtime import GraphRuntime
from hisiem_soc_copilot.agent.tools.executor import ToolExecutor
from hisiem_soc_copilot.agent.tools.registry import ToolRegistry
from hisiem_soc_copilot.application.errors import ExternalServiceError
from hisiem_soc_copilot.application.handlers.workflow import InvestigationWorkflowHandler
from hisiem_soc_copilot.application.ports.hisiem import (
    DetectionRuleContext,
    EventSearchResult,
)
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.entities import InvestigationResult
from hisiem_soc_copilot.domain.investigation.enums import InvestigationStatus
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from hisiem_soc_copilot.infrastructure.llm.scripted import ScriptedModelProvider
from tests.fixtures.fakes import FakeUnitOfWorkFactory
from tests.fixtures.hisiem_fake import FakeHisiem


class _UnavailableHisiem(FakeHisiem):
    """Search/rule upstream is down → adapter raises ExternalServiceError."""

    async def search_events(
        self,
        *,
        tenant_id: str,
        from_: str,
        to: str,
        conditions: list[dict[str, object]],
        limit: int = 100,
        sort: str = "desc",
    ) -> EventSearchResult:
        raise ExternalServiceError("upstream down", service="hisiem")

    async def get_detection_rule(
        self, *, tenant_id: str, rule_id: str
    ) -> DetectionRuleContext | None:
        raise ExternalServiceError("upstream down", service="hisiem")


def _start(
    tenant_id: str = "tenant-a", alert_id: str = "alert-x"
) -> tuple[FakeUnitOfWorkFactory, Investigation]:
    uows = FakeUnitOfWorkFactory()
    actor = ActorRef(subject_id="analyst", tenant_id=tenant_id)
    inv = Investigation.create(
        id=uuid4(),
        tenant_id=tenant_id,
        source_alert_ref=ExternalResourceRef(
            provider="hisiem", resource_type="alert", address_id=alert_id
        ),
        initiated_by=actor,
        budget_limits=BudgetLimits(),
    )
    return uows, inv


async def _run(
    script: dict[str, Any], hisiem: FakeHisiem | None = None
) -> tuple[Investigation, InvestigationResult | None]:
    """Start RUNNING, compile the graph with the given script, and run to END."""
    uows, inv = _start(tenant_id="tenant-a")
    uow = uows()
    await uow.investigations.add(inv)
    await uow.commit()
    inv.start(actor=inv.initiated_by)
    await uow.investigations.update(inv)
    await uow.commit()

    hisiem = hisiem or FakeHisiem(alert_id="alert-x")
    model = ScriptedModelProvider(script=script)
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

    uow2 = uows()
    completed = await uow2.investigations.get(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert completed is not None
    assert completed.status == InvestigationStatus.COMPLETED
    result = await uow2.results.get_by_investigation(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    return completed, result


async def test_inconclusive_when_no_evidence_and_model_finalizes() -> None:
    # No search_events turn → only alert-context evidence; model returns INCONCLUSIVE.
    script = {
        "decide": [{"decision": "FINALIZE", "reason": "no further reads"}],
        "findings": [],
        "verdict": {
            "disposition": "INCONCLUSIVE",
            "summary": "Not enough evidence to confirm a compromise",
            "confidence": 0.3,
            "uncertainty": "No authentication_success event was observed",
        },
    }
    completed, result = await _run(script)
    assert result is not None
    assert result.verdict.disposition.value == "INCONCLUSIVE"


async def test_benign_when_success_needed_but_absent() -> None:
    # The model looks for success, finds only failures, then FINALIZEs BENIGN.
    script = {
        "plan_steps": {"search": "Search for a successful login"},
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
        "findings": [
            "No successful login followed the brute force; the account was not "
            "compromised (only failures observed from the source IP)"
        ],
        "verdict": {
            "disposition": "BENIGN",
            "summary": "Brute force did not escalate into a login",
            "confidence": 0.8,
        },
    }
    completed, result = await _run(script)
    assert result is not None
    assert result.verdict.disposition.value == "BENIGN"


async def test_budget_exhaustion_yields_completed_inconclusive() -> None:
    # Budget max_steps = 1 in the aggregate → CONTINUE is not possible; the graph
    # must converge to COMPLETED + INCONCLUSIVE rather than FAILED.
    uows, inv = _start(tenant_id="tenant-a")
    inv.budget_limits = BudgetLimits(max_steps=1, max_tool_calls=1)
    uow = uows()
    await uow.investigations.add(inv)
    await uow.commit()
    inv.start(actor=inv.initiated_by)
    await uow.investigations.update(inv)
    await uow.commit()

    script = {
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
            },
            {
                "tool_name": "hisiem.search_events",
                "arguments": {
                    "from": "2026-09-01T09:55:00Z",
                    "to": "2026-09-01T10:05:00Z",
                    "conditions": [
                        {"field": "user.name", "operator": "is", "value": "root"}
                    ],
                },
            },
        ],
        "findings": [],
        "verdict": {
            "disposition": "INCONCLUSIVE",
            "summary": "Budget spent before a grounded conclusion",
            "confidence": 0.2,
            "uncertainty": "Tool budget exhausted mid-investigation",
        },
    }
    hisiem = FakeHisiem(alert_id="alert-x")
    runtime = GraphRuntime(
        uow_factory=uows,
        workflow_handler=InvestigationWorkflowHandler(unit_of_work_factory=uows),
        model=ScriptedModelProvider(script=script),
        executor=ToolExecutor(hisiem=hisiem),
        normalizer=EvidenceNormalizer(),
        registry=ToolRegistry(),
        hisiem=hisiem,
        tenant_id="tenant-a",
    )
    graph = build_investigation_graph(runtime)
    final = await graph.ainvoke(
        {"investigation_id": str(inv.id)}, thread_config(str(inv.id))
    )

    uow2 = uows()
    completed = await uow2.investigations.get(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert completed is not None
    assert completed.status == InvestigationStatus.COMPLETED  # not FAILED
    assert final["stop_reason"].startswith("COMPLETED_WITHOUT_RESPONSE")
    result = await uow2.results.get_by_investigation(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert result is not None
    assert result.verdict.disposition.value == "INCONCLUSIVE"


async def test_single_tool_unavailable_does_not_fail_investigation() -> None:
    # Upstream log-search is down; the executor returns UNAVAILABLE and the graph
    # must still COMPLETE (with whatever evidence exists), never FAILED.
    script = {
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
                            "value": "authentication_failure",
                        }
                    ],
                },
            }
        ],
        "findings": [],
        "verdict": {
            "disposition": "INCONCLUSIVE",
            "summary": "Event source unavailable; could not confirm",
            "confidence": 0.2,
            "uncertainty": "HISIEM log-search was unavailable during the run",
        },
    }
    hisiem = _UnavailableHisiem(alert_id="alert-x")
    uows, inv = _start(tenant_id="tenant-a")
    uow = uows()
    await uow.investigations.add(inv)
    await uow.commit()
    inv.start(actor=inv.initiated_by)
    await uow.investigations.update(inv)
    await uow.commit()
    runtime = GraphRuntime(
        uow_factory=uows,
        workflow_handler=InvestigationWorkflowHandler(unit_of_work_factory=uows),
        model=ScriptedModelProvider(script=script),
        executor=ToolExecutor(hisiem=hisiem),
        normalizer=EvidenceNormalizer(),
        registry=ToolRegistry(),
        hisiem=hisiem,
        tenant_id="tenant-a",
    )
    graph = build_investigation_graph(runtime)
    final = await graph.ainvoke(
        {"investigation_id": str(inv.id)}, thread_config(str(inv.id))
    )
    uow2 = uows()
    completed = await uow2.investigations.get(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert completed is not None
    assert completed.status == InvestigationStatus.COMPLETED  # not FAILED
    assert final["stop_reason"].startswith("COMPLETED_WITHOUT_RESPONSE")
