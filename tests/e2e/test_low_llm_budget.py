"""Low LLM-call budget: deterministic fallbacks + the max_llm_calls invariant.

Fix #6: every model call (plan/decide/assess/verdict) is gated by the single
RuntimeBudget authority. The total number of model consults across a run must never
exceed ``max_llm_calls`` for ANY ``max_llm_calls >= 1``, and when the budget is so
low that a consult cannot start the graph applies deterministic fallbacks
(no-budget system default plan; hypotheses UNRESOLVED; verdict INCONCLUSIVE with a
"model-call budget exhausted" uncertainty). The run always ends COMPLETED +
INCONCLUSIVE — never FAILED and never an over-budget model call.
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
from hisiem_soc_copilot.domain.investigation.enums import InvestigationStatus
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from hisiem_soc_copilot.infrastructure.llm.scripted import ScriptedModelProvider
from tests.fixtures.fakes import FakeUnitOfWorkFactory
from tests.fixtures.hisiem_fake import FakeHisiem


def _start(
    max_llm_calls: int,
) -> tuple[FakeUnitOfWorkFactory, Investigation]:
    uows = FakeUnitOfWorkFactory()
    actor = ActorRef(subject_id="analyst", tenant_id="tenant-a")
    inv = Investigation.create(
        id=uuid4(),
        tenant_id="tenant-a",
        source_alert_ref=ExternalResourceRef(
            provider="hisiem", resource_type="alert", address_id="alert-x"
        ),
        initiated_by=actor,
        budget_limits=BudgetLimits(
            max_steps=5, max_tool_calls=5, max_llm_calls=max_llm_calls
        ),
    )
    return uows, inv


async def _boot(uows: FakeUnitOfWorkFactory, inv: Investigation) -> None:
    uow = uows()
    await uow.investigations.add(inv)
    await uow.commit()
    inv.start(actor=inv.initiated_by)
    await uow.investigations.update(inv)
    await uow.commit()


async def _run(max_llm_calls: int) -> tuple[FakeUnitOfWorkFactory, Investigation, list[str]]:
    """Run the graph with an eager model (would read + render a MALICIOUS verdict if
    consulted). Assert via the caller that model consults stay within max_llm_calls."""
    uows, inv = _start(max_llm_calls)
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
            },
            {"decision": "FINALIZE"},
        ],
        "findings": ["a finding"],
        "verdict": {
            "disposition": "MALICIOUS",
            "summary": "compromised",
            "confidence": 0.9,
        },
    }
    model = ScriptedModelProvider(script=script)
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
    final = await graph.ainvoke(
        {"investigation_id": str(inv.id)}, thread_config(str(inv.id))
    )
    assert final["stop_reason"].startswith("COMPLETED_WITHOUT_RESPONSE")
    return uows, inv, model.calls


async def _completed_result(
    uows: FakeUnitOfWorkFactory, inv: Investigation
) -> tuple[Investigation, Any | None]:
    uow = uows()
    completed = await uow.investigations.get(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert completed is not None
    assert completed.status == InvestigationStatus.COMPLETED  # never FAILED
    result = await uow.results.get_by_investigation(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    return completed, result


async def test_max_llm_calls_1_completes_inconclusive_without_verdict() -> None:
    """max_llm_calls = 1 → only plan may be consulted; decide reserves the final two
    convergence slots and never consults; the verdict is never rendered (low-budget
    fallback → INCONCLUSIVE, "model-call budget exhausted")."""
    uows, inv, calls = await _run(max_llm_calls=1)
    assert len(calls) == 1  # plan consumed the single slot
    assert calls[0] == "plan"  # decide/assess/verdict never overran the budget
    _completed, result = await _completed_result(uows, inv)
    assert result is not None
    assert result.verdict.disposition.value == "INCONCLUSIVE"
    assert any(
        "model-call budget exhausted" in (u.description or "") for u in result.uncertainties
    )


async def test_max_llm_calls_2_completes_inconclusive_without_verdict() -> None:
    """max_llm_calls = 2 → plan + assess may run; verdict is not consulted (no slot
    remains after the reserve is consumed) → INCONCLUSIVE low-budget fallback."""
    uows, inv, calls = await _run(max_llm_calls=2)
    assert len(calls) <= 2
    # decide never consumed a slot (it needs >2 to consult), so the calls are plan
    # and then at most one convergence consult (assess). verdict would be the 3rd.
    assert "verdict" not in calls
    _completed, result = await _completed_result(uows, inv)
    assert result is not None
    assert result.verdict.disposition.value == "INCONCLUSIVE"


async def test_max_llm_calls_3_reaches_verdict_exactly_on_budget() -> None:
    """max_llm_calls = 3 → exactly plan + assess + verdict (decide short-circuits to
    the convergence path when only the two reserved slots remain). The total equals
    max_llm_calls — never exceeds it. The consulted MALICIOUS verdict is still
    grounded-bounded to INCONCLUSIVE (no tool read ever ran → no grounded Finding)."""
    uows, inv, calls = await _run(max_llm_calls=3)
    assert len(calls) <= 3
    assert len(calls) == 3  # plan + assess + verdict
    assert calls == ["plan", "assess", "verdict"]
    _completed, result = await _completed_result(uows, inv)
    assert result is not None
    assert result.verdict.disposition.value == "INCONCLUSIVE"


async def test_resume_keeps_consumed_llm_calls_not_reset() -> None:
    """A resumed run preserves the checkpointed remaining LLM-call counter rather
    than re-seeding the aggregate's full budget (Fix #6 restart/resume invariant)."""
    uows, inv = _start(max_llm_calls=20)
    await _boot(uows, inv)
    from hisiem_soc_copilot.agent.graph.nodes import load_investigation

    runtime = GraphRuntime(
        uow_factory=uows,
        workflow_handler=InvestigationWorkflowHandler(unit_of_work_factory=uows),
        model=ScriptedModelProvider(script={}),
        executor=ToolExecutor(hisiem=FakeHisiem(alert_id="alert-x")),
        normalizer=EvidenceNormalizer(),
        registry=ToolRegistry(),
        hisiem=FakeHisiem(alert_id="alert-x"),
        tenant_id="tenant-a",
    )
    # A mid-run resume checkpoint: plan already consumed one LLM call (19 left) and
    # the deadline is preserved.
    resumed = await load_investigation(
        runtime,
        {
            "investigation_id": str(inv.id),
            "budget_remaining_llm_calls": 19,
            "budget_remaining_steps": 4,
            "budget_remaining_tool_calls": 4,
            "budget_deadline_at": 1_234_567.0,
        },
    )
    assert resumed["budget_remaining_llm_calls"] == 19  # not re-seeded to 20
    assert resumed["budget_deadline_at"] == 1_234_567.0  # deadline preserved
