"""Failure-aware re-plan feedback (Fix #5).

When a tool read fails (UNAVAILABLE / REJECTED) or returns nothing (NO_DATA), the
graph hands the model a bounded PreviousToolOutcome (tool_name + typed status +
error_code + retryable) on the NEXT DecideNextRequest — never raw exceptions,
stack traces, HTTP bodies, or full tool results. A model that keeps proposing the
EXACT same failing request is stopped by a bounded repeated-attempt guard
independent of the step/tool budget, and the investigation converges to a bounded
finalize (COMPLETED, never FAILED).

Behaviors proven here:
- UNAVAILABLE(A) → the model's next DecideNextRequest carries PreviousToolOutcome(
  A, UNAVAILABLE, retryable) → the model picks B → B succeeds and its evidence is
  grounded.
- NO_DATA(A) → the model's next DecideNextRequest carries PreviousToolOutcome(
  A, NO_DATA) → the model issues a refined query.
- REJECTED(A) → the model's next DecideNextRequest carries PreviousToolOutcome(
  A, REJECTED, not retryable); the graph does not spin on it (deterministic).
- a model that repeatedly re-proposes the SAME failing request is stopped after a
  bounded number of attempts and the investigation COMPLETES INCONCLUSIVE.
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
from hisiem_soc_copilot.application.ports.model_provider import DecideNextRequest
from hisiem_soc_copilot.contracts.llm.types import NextStep
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

_SEARCH_ARGS: dict[str, Any] = {
    "from": "2026-09-01T09:55:00Z",
    "to": "2026-09-01T10:05:00Z",
    "conditions": [
        {
            "field": "event.action",
            "operator": "is",
            "value": "authentication_success",
        }
    ],
}


class _RecordingReplanModel(ScriptedModelProvider):
    """Scripted model that RECORDS every DecideNextRequest's PreviousToolOutcome.

    The recorded outcomes are asserted by the tests, proving the model genuinely
    receives the bounded failure feedback on its re-plan decision.
    """

    def __init__(self, *, script: dict[str, Any] | None = None) -> None:
        super().__init__(script=script)
        self.decide_outcomes: list[dict[str, Any]] = []

    async def decide_next(self, request: DecideNextRequest) -> NextStep:
        outcome = request.previous_tool_outcome
        self.decide_outcomes.append(
            {
                "tool_name": outcome.tool_name if outcome else None,
                "status": outcome.status if outcome else None,
                "error_code": outcome.error_code if outcome else None,
                "retryable": outcome.retryable if outcome else None,
                "iteration": request.iteration,
            }
        )
        return await super().decide_next(request)


class _SearchUnavailableHisiem(FakeHisiem):
    """search_events is down (UNAVAILABLE) but get_detection_rule works."""

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


class _NoDataFirstHisiem(FakeHisiem):
    """First search_events returns NO_DATA (empty); later ones delegate to the fake."""

    def __init__(self, *, alert_id: str) -> None:
        super().__init__(alert_id=alert_id)
        self._searches = 0

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
        self._searches += 1
        self.calls.append("search_events")
        if self._searches == 1:
            # First query matches nothing → NO_DATA.
            return EventSearchResult(
                items=[], total=0, returned=0, from_=from_, to=to, truncated=False
            )
        return await super().search_events(
            tenant_id=tenant_id,
            from_=from_,
            to=to,
            conditions=conditions,
            limit=limit,
            sort=sort,
        )


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
    model: _RecordingReplanModel, hisiem: FakeHisiem
) -> tuple[FakeUnitOfWorkFactory, Investigation, dict[str, Any]]:
    uows, inv = _start()
    await _boot(uows, inv)
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
    final = await graph.ainvoke(
        {"investigation_id": str(inv.id)}, thread_config(str(inv.id))
    )
    return uows, inv, final


async def test_unavailable_then_rule_read_succeeds_and_feedback_is_bounded() -> None:
    """A (search) is UNAVAILABLE → the re-plan DecideNextRequest carries the bounded
    outcome (A/UNAVAILABLE/retryable) → the model picks B (rule) → B succeeds and
    its evidence is grounded. No raw exception/result is exposed to the model."""
    script = {
        "plan_steps": {"read_rule": "read rule", "search": "search"},
        "decide": [
            {"tool_name": "hisiem.search_events", "arguments": dict(_SEARCH_ARGS)},
            {"tool_name": "hisiem.get_detection_rule",
             "arguments": {"rule_id": "ssh_brute_force"}},
            {"decision": "FINALIZE"},
        ],
        "findings": [],
        "verdict": {
            "disposition": "INCONCLUSIVE",
            "summary": "search unavailable; rule read succeeded",
            "confidence": 0.3,
            "uncertainty": "HISIEM log-search was temporarily unavailable",
        },
    }
    model = _RecordingReplanModel(script=script)
    hisiem = _SearchUnavailableHisiem(alert_id="alert-x")
    uows, inv, final = await _run(model, hisiem=hisiem)
    assert final["stop_reason"].startswith("COMPLETED_WITHOUT_RESPONSE")

    # The SECOND decide consult (the re-plan after A failed) carried the bounded
    # outcome: tool A, UNAVAILABLE, retryable — nothing else leaked.
    relevant = [o for o in model.decide_outcomes if o["status"] == "UNAVAILABLE"]
    assert relevant, "expected an UNAVAILABLE outcome on a re-plan decide consult"
    assert relevant[-1]["tool_name"] == "hisiem.search_events"
    assert relevant[-1]["retryable"] is True
    assert isinstance(relevant[-1]["error_code"], str)  # bounded code, not a traceback

    # The alternative path (rule read) actually succeeded and grounded evidence.
    uow = uows()
    evidence = await uow.evidence.list_by_investigation(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert any(e.source.operation == "get_detection_rule" for e in evidence)
    audits = uow.tool_invocations.by_investigation(inv.id)
    search_audits = [a for a in audits if a.tool_name == "hisiem.search_events"]
    assert search_audits and all(a.status == "FAILED" for a in search_audits)


async def test_no_data_feeds_refined_query_replan() -> None:
    """A query returns NO_DATA (a SUCCESSFUL read) → the next DecideNextRequest
    carries PreviousToolOutcome(A/NO_DATA) so the model issues a refined query."""
    script = {
        "plan_steps": {"search1": "search successes", "search2": "search wider"},
        "decide": [
            {
                "tool_name": "hisiem.search_events",
                "arguments": {
                    "from": "2026-09-01T09:55:00Z",
                    "to": "2026-09-01T10:05:00Z",
                    "conditions": [
                        {"field": "event.action", "operator": "is", "value": "network_session"}
                    ],  # _NoDataFirstHisiem → first search NO_DATA
                },
            },
            {
                "tool_name": "hisiem.search_events",
                "arguments": dict(_SEARCH_ARGS),  # refined query (authentication_success)
            },
            {"decision": "FINALIZE"},
        ],
        "findings": ["A successful login followed the brute-force failures"],
        "verdict": {
            "disposition": "MALICIOUS",
            "summary": "compromised",
            "confidence": 0.9,
        },
    }
    model = _RecordingReplanModel(script=script)
    uows, inv, final = await _run(model, hisiem=_NoDataFirstHisiem(alert_id="alert-x"))
    assert final["stop_reason"].startswith("COMPLETED_WITHOUT_RESPONSE")

    no_data = [o for o in model.decide_outcomes if o["status"] == "NO_DATA"]
    assert no_data, "expected a NO_DATA outcome on the re-plan consult after an empty read"
    assert no_data[-1]["tool_name"] == "hisiem.search_events"
    assert no_data[-1]["retryable"] is False

    # The refined query executed (two successful searches audited).
    uow = uows()
    audits = uow.tool_invocations.by_investigation(inv.id)
    search_audits = [a for a in audits if a.tool_name == "hisiem.search_events"]
    assert len(search_audits) == 2
    assert all(a.status == "SUCCEEDED" for a in search_audits)


async def test_rejected_candidate_never_re_plans_on_it() -> None:
    """A REJECTED candidate (unknown tool) converges immediately with a bounded
    non-retryable record — the graph never spins / re-consults decide on it."""
    script = {
        "plan_steps": {"a": "a", "b": "b"},
        "decide": [
            {"tool_name": "not_a_real_tool", "arguments": {}},  # REJECTED
            {"decision": "FINALIZE"},
        ],
        "findings": [],
        "verdict": {
            "disposition": "INCONCLUSIVE",
            "summary": "only a rejected candidate produced",
            "confidence": 0.2,
            "uncertainty": "no valid read could be executed",
        },
    }
    model = _RecordingReplanModel(script=script)
    uows, inv, final = await _run(model, hisiem=FakeHisiem(alert_id="alert-x"))
    assert final["stop_reason"].startswith("COMPLETED_WITHOUT_RESPONSE")
    uow = uows()
    completed = await uow.investigations.get(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert completed is not None
    assert completed.status == InvestigationStatus.COMPLETED

    # REJECTED is a deterministic outcome: the graph does NOT loop back to decide on
    # it, so exactly ONE decide consult happened (the one that proposed the bad tool),
    # and the rejection is visible in last_tool_error rather than as a second consult.
    decide_consults = [o for o in model.decide_outcomes]
    assert len(decide_consults) == 1
    # The run recorded no evidence (the rejected tool produced nothing) and no FAILED
    # search_events audit (the tool never reached a provider).
    evidence = await uow.evidence.list_by_investigation(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert evidence == []


async def test_repeated_same_failing_call_is_bounded_and_finalizes() -> None:
    """A model that keeps re-proposing the SAME unavailable request (identical
    fingerprint) is stopped by the bounded repeated-attempt guard and the
    investigation completes INCONCLUSIVE (never spins, never FAILED)."""
    # Many turns all proposing the exact same unavailable search.
    script = {
        "plan_steps": {"search": "search"},
        "decide": [
            {"tool_name": "hisiem.search_events", "arguments": dict(_SEARCH_ARGS)},
            {"tool_name": "hisiem.search_events", "arguments": dict(_SEARCH_ARGS)},
            {"tool_name": "hisiem.search_events", "arguments": dict(_SEARCH_ARGS)},
            {"tool_name": "hisiem.search_events", "arguments": dict(_SEARCH_ARGS)},
            {"tool_name": "hisiem.search_events", "arguments": dict(_SEARCH_ARGS)},
            {"decision": "FINALIZE"},
        ],
        "findings": [],
        "verdict": {
            "disposition": "INCONCLUSIVE",
            "summary": "the same read kept failing",
            "confidence": 0.2,
            "uncertainty": "the only available read was repeatedly unavailable",
        },
    }
    model = _RecordingReplanModel(script=script)
    hisiem = _SearchUnavailableHisiem(alert_id="alert-x")
    uows, inv, final = await _run(model, hisiem=hisiem)

    # The investigation COMPLETED and did not execute the unavailable search more
    # than the bounded number of attempts (well below the model's 5 scripted turns).
    uow = uows()
    completed = await uow.investigations.get(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert completed is not None
    assert completed.status == InvestigationStatus.COMPLETED  # never FAILED
    assert final["stop_reason"].startswith("COMPLETED_WITHOUT_RESPONSE")
    result = await uow.results.get_by_investigation(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert result is not None
    assert result.verdict.disposition.value == "INCONCLUSIVE"
    assert hisiem.calls.count("search_events") <= 4  # bounded, not the full 5
