"""Graph-level guards: retry/resume no-duplicate, budget gating, and the read-only
policy boundary (unknown/write tools never reach a provider).
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
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from hisiem_soc_copilot.infrastructure.llm.scripted import ScriptedModelProvider
from tests.fixtures.fakes import FakeUnitOfWorkFactory
from tests.fixtures.hisiem_fake import FakeHisiem


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


async def _boot(uows: FakeUnitOfWorkFactory, inv: Investigation) -> None:
    uow = uows()
    await uow.investigations.add(inv)
    await uow.commit()
    inv.start(actor=inv.initiated_by)
    await uow.investigations.update(inv)
    await uow.commit()


def _runtime(
    uows: FakeUnitOfWorkFactory,
    inv: Investigation,
    script: dict[str, Any],
    hisiem: FakeHisiem | None = None,
) -> GraphRuntime:
    hisiem = hisiem or FakeHisiem(alert_id="alert-x")
    return GraphRuntime(
        uow_factory=uows,
        workflow_handler=InvestigationWorkflowHandler(unit_of_work_factory=uows),
        model=ScriptedModelProvider(script=script),
        executor=ToolExecutor(hisiem=hisiem),
        normalizer=EvidenceNormalizer(),
        registry=ToolRegistry(),
        hisiem=hisiem,
        tenant_id="tenant-a",
    )


async def test_rerun_same_thread_does_not_duplicate_evidence() -> None:
    """A resumed/retried graph run (same investigation) never re-records evidence.

    The workflow handler dedups on the deterministic evidence dedup_key, so a
    second full run over an already-COMPLETED investigation must not create rows
    (and simply stops at the terminal state).
    """
    uows, inv = _start()
    await _boot(uows, inv)
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
            }
        ],
        "findings": ["success observed"],
        "verdict": {
            "disposition": "MALICIOUS",
            "summary": "compromised",
            "confidence": 0.9,
        },
    }
    runtime = _runtime(uows, inv, script)
    graph = build_investigation_graph(runtime)
    config = thread_config(str(inv.id))
    await graph.ainvoke({"investigation_id": str(inv.id)}, config)

    uow = uows()
    first_count = len(
        await uow.evidence.list_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
    )
    first_findings = len(
        await uow.findings.list_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
    )

    # A second invocation on the same thread (resume) must be a no-op for rows.
    runtime2 = _runtime(uows, inv, script)
    graph2 = build_investigation_graph(runtime2)
    await graph2.ainvoke({"investigation_id": str(inv.id)}, config)

    second_count = len(
        await uow.evidence.list_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
    )
    second_findings = len(
        await uow.findings.list_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
    )
    assert first_count >= 1
    assert second_count == first_count  # no duplicate evidence rows
    assert second_findings == first_findings  # no duplicate findings


async def test_model_selecting_unknown_tool_is_rejected_and_investigation_completes() -> None:
    """A model cannot pick an unregistered / write tool; the graph still completes."""
    uows, inv = _start()
    await _boot(uows, inv)
    script = {
        "decide": [
            {
                "tool_name": "write_alert",  # forbidden write tool
                "arguments": {"alert_id": "x"},
            },
            {
                "tool_name": "not_a_real_tool",
                "arguments": {},
            },
        ],
        "findings": [],
        "verdict": {
            "disposition": "INCONCLUSIVE",
            "summary": "only rejected candidates produced",
            "confidence": 0.2,
            "uncertainty": "No valid read could be executed",
        },
    }
    runtime = _runtime(uows, inv, script)
    graph = build_investigation_graph(runtime)
    final = await graph.ainvoke(
        {"investigation_id": str(inv.id)}, thread_config(str(inv.id))
    )

    uow = uows()
    completed = await uow.investigations.get(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert completed is not None
    assert completed.status.value == "COMPLETED"
    assert final["stop_reason"].startswith("COMPLETED_WITHOUT_RESPONSE")
    # No evidence was ever recorded from a rejected tool.
    evidence = await uow.evidence.list_by_investigation(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert evidence == []
