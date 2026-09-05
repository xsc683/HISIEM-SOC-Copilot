"""Recoverable tool-failure behavior.

A transient tool/data-source failure (UNAVAILABLE) must NOT immediately finalize
the investigation. When budget remains and alternative investigation paths are
available, the graph loops back to decide_next so the model can try another read.
Only when the budget is exhausted (or the model has no further path) does the graph
converge to finalize with available facts → COMPLETED + INCONCLUSIVE.

Deterministic candidate rejections (unknown tool / invalid schema / policy
violation) are NOT provider-transient errors and are never retried as such.
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
from hisiem_soc_copilot.application.ports.hisiem import EventSearchResult
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.enums import InvestigationStatus
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from hisiem_soc_copilot.infrastructure.llm.scripted import ScriptedModelProvider
from tests.fixtures.fakes import FakeUnitOfWorkFactory
from tests.fixtures.hisiem_fake import FakeHisiem


class _SearchUnavailableHisiem(FakeHisiem):
    """search_events is down but get_detection_rule works."""

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
        self.calls.append("search_events")
        raise ExternalServiceError("search upstream down", service="hisiem")


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


async def _run(
    script: dict[str, Any], hisiem: FakeHisiem | None = None
) -> tuple[FakeUnitOfWorkFactory, Investigation, dict[str, Any]]:
    uows, inv = _start()
    await _boot(uows, inv)
    hisiem = hisiem or FakeHisiem(alert_id="alert-x")
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
    assert completed.status == InvestigationStatus.COMPLETED  # never FAILED
    return uows, inv, final


async def test_tool_a_unavailable_then_tool_b_succeeds_continues() -> None:
    """Tool A (search) is unavailable; the model then issues tool B (rule read)
    which succeeds → the investigation CONTINUES and gathers grounded evidence."""
    script = {
        "plan_steps": {"read_rule": "read rule", "search": "search"},
        # Turn 1 picks the unavailable search; on re-plan the model picks the rule.
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
                "tool_name": "hisiem.get_detection_rule",
                "arguments": {"rule_id": "ssh_brute_force"},
            },
            {"decision": "FINALIZE"},
        ],
        "findings": [
            "The detection rule describes a brute-force credential attempt from a "
            "single source; event search was unavailable so the follow-on login "
            "could not be confirmed"
        ],
        "verdict": {
            "disposition": "INCONCLUSIVE",
            "summary": "Rule read succeeded; event search unavailable",
            "confidence": 0.4,
            "uncertainty": "search_events was temporarily unavailable",
        },
    }
    hisiem = _SearchUnavailableHisiem(alert_id="alert-x")
    uows, inv, final = await _run(script, hisiem=hisiem)

    # The graph attempted BOTH tools: search failed (recoverable → re-plan) and the
    # rule read succeeded and produced evidence.
    assert "search_events" in hisiem.calls
    assert any(c.startswith("get_detection_rule") for c in hisiem.calls)
    uow = uows()
    evidence = await uow.evidence.list_by_investigation(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert any(e.source.operation == "get_detection_rule" for e in evidence)
    # The search_events invocation is audited FAILED, not dropped.
    audits = uow.tool_invocations.by_investigation(inv.id)
    search_audits = [a for a in audits if a.tool_name == "hisiem.search_events"]
    assert search_audits and all(a.status == "FAILED" for a in search_audits)


async def test_tool_unavailable_with_no_alternative_finalizes_inconclusive() -> None:
    """search unavailable and the model has NO further path → finalize available
    facts as COMPLETED + INCONCLUSIVE (never FAILED)."""
    script = {
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
            },
            {"decision": "FINALIZE"},  # no alternative path after the failure
        ],
        "findings": [],
        "verdict": {
            "disposition": "INCONCLUSIVE",
            "summary": "Event source unavailable and no alternative read existed",
            "confidence": 0.2,
            "uncertainty": "HISIEM log-search was unavailable during the run",
        },
    }
    uows, inv, final = await _run(script, hisiem=_SearchUnavailableHisiem(alert_id="alert-x"))
    assert final["stop_reason"].startswith("COMPLETED_WITHOUT_RESPONSE")
    uow = uows()
    result = await uow.results.get_by_investigation(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert result is not None
    assert result.verdict.disposition.value == "INCONCLUSIVE"


async def test_no_data_allows_another_bounded_query() -> None:
    """NO_DATA on one query is a SUCCESSFUL read — the model may issue another
    bounded query (here a different event-action search) rather than finalizing."""
    script = {
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
                            "value": "network_session",  # FakeHisiem → NO_DATA
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
                        {
                            "field": "event.action",
                            "operator": "is",
                            "value": "authentication_success",
                        }
                    ],
                },
            },
            {"decision": "FINALIZE"},
        ],
        "findings": [],
        "verdict": {
            "disposition": "INCONCLUSIVE",
            "summary": "queried two bounded windows",
            "confidence": 0.3,
            "uncertainty": "insufficient signal",
        },
    }
    uows, inv, final = await _run(script)  # default FakeHisiem (search works)
    assert final["stop_reason"].startswith("COMPLETED_WITHOUT_RESPONSE")
    # Two bounded search queries were issued (NO_DATA did not force finalize).
    uow = uows()
    audits = uow.tool_invocations.by_investigation(inv.id)
    search_audits = [a for a in audits if a.tool_name == "hisiem.search_events"]
    assert len(search_audits) == 2
    assert all(a.status == "SUCCEEDED" for a in search_audits)
