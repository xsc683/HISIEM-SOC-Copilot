"""Runtime budget enforcement (Fix #8).

The autonomy budget is RUNTIME authority, enforced deterministically across the
graph and checkpointed so a crash/restart/resume never resets it to full:

- ``max_steps`` / ``max_tool_calls`` bound the investigate loop; a CONTINUE with no
  steps or no tool calls left routes to convergence (never FAILED).
- ``max_llm_calls`` bounds every model consult (plan + decide + assess + verdict).
  ``max_llm_tokens`` stays reserved for a real provider's token accounting.
- ``max_duration_seconds`` becomes a wall-clock deadline; once it passes, no tool
  runs and no further model consult starts — the run deterministically finalizes
  the available grounded facts INCONCLUSIVE → COMPLETED.
- The remaining counters live in the graph state, so a resumed run keeps what was
  consumed instead of being re-seeded to full.

Every exhaustion/deadline path lands on COMPLETED (bounded finalize), never FAILED.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from hisiem_soc_copilot.agent.evidence.normalizer import EvidenceNormalizer
from hisiem_soc_copilot.agent.graph.builder import build_investigation_graph, thread_config
from hisiem_soc_copilot.agent.graph.nodes import load_investigation
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
    budget: BudgetLimits | None = None,
    tenant_id: str = "tenant-a",
    alert_id: str = "alert-x",
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
        budget_limits=budget or BudgetLimits(),
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
    script: dict[str, Any],
    hisiem: FakeHisiem | None = None,
    tenant_id: str = "tenant-a",
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
        tenant_id=tenant_id,
    )


def _search_args(value: str) -> dict[str, Any]:
    return {
        "from": "2026-09-01T09:55:00Z",
        "to": "2026-09-01T10:05:00Z",
        "conditions": [{"field": "event.action", "operator": "is", "value": value}],
    }


async def test_tool_call_limit_stops_further_tool_execution() -> None:
    """max_tool_calls = 1 → exactly one tool executes even though the model keeps
    proposing reads; the investigation still completes (never FAILED)."""
    uows, inv = _start(budget=BudgetLimits(max_steps=3, max_tool_calls=1, max_llm_calls=20))
    await _boot(uows, inv)
    script = {
        "decide": [
            {
                "tool_name": "hisiem.search_events",
                "arguments": _search_args("authentication_success"),
            },
            {
                "tool_name": "hisiem.search_events",
                "arguments": _search_args("user.name"),
            },
            {"decision": "FINALIZE"},
        ],
        "findings": [],
        "verdict": {
            "disposition": "INCONCLUSIVE",
            "summary": "tool budget spent",
            "confidence": 0.3,
            "uncertainty": "tool-call budget exhausted mid-investigation",
        },
    }
    hisiem = FakeHisiem(alert_id="alert-x")
    runtime = _runtime(uows, script, hisiem=hisiem)
    graph = build_investigation_graph(runtime)
    final = await graph.ainvoke({"investigation_id": str(inv.id)}, thread_config(str(inv.id)))

    # The model proposed TWO distinct reads, but max_tool_calls=1 gates the second.
    assert hisiem.calls.count("search_events") == 1
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


async def test_llm_call_limit_bounded_finalize() -> None:
    """max_llm_calls = 5 → total model consults never exceed 5 even though the
    scripted model would keep proposing reads forever; the run finalizes bounded."""
    uows, inv = _start(budget=BudgetLimits(max_steps=10, max_tool_calls=10, max_llm_calls=5))
    await _boot(uows, inv)
    # An eager model: many CONTINUE turns + a final FINALIZE.
    turns = [
        {"tool_name": "hisiem.search_events", "arguments": _search_args(v)}
        for v in ("authentication_success", "user.name", "process.name", "file.path")
    ]
    script = {
        "plan_steps": {"search": "search"},
        "decide": turns,
        "findings": [],
        "verdict": {
            "disposition": "INCONCLUSIVE",
            "summary": "LLM budget spent",
            "confidence": 0.2,
            "uncertainty": "model-call budget exhausted",
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
    final = await graph.ainvoke({"investigation_id": str(inv.id)}, thread_config(str(inv.id)))

    # plan + 2 decide consults + assess + verdict = 5 == max_llm_calls. The model
    # was NOT consulted 8 times (its full script) — the LLM-call budget capped it.
    assert len(model.calls) == 5
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


class _ClockBumpingModel(ScriptedModelProvider):
    """A scripted model that advances the wall clock past the deadline on its FIRST
    decide_next consult, so the subsequent nodes observe an expired budget."""

    def __init__(self, *, script: dict[str, Any], clock: list[float], offset: float) -> None:
        super().__init__(script=script)
        self._clock = clock
        self._offset = offset
        self._bumped = False

    async def decide_next(self, request: Any) -> Any:
        if not self._bumped:
            self._bumped = True
            self._clock[0] += self._offset
        return await super().decide_next(request)


async def test_deadline_exceeded_bounded_finalize_inconclusive() -> None:
    """Once the wall-clock deadline (max_duration_seconds) passes, no tool runs and
    no further model consult starts: the run finalizes the available facts
    INCONCLUSIVE → COMPLETED (never FAILED)."""
    uows, inv = _start(
        budget=BudgetLimits(
            max_steps=10,
            max_tool_calls=10,
            max_llm_calls=20,
            max_duration_seconds=60,
        )
    )
    await _boot(uows, inv)
    script = {
        "decide": [
            {
                "tool_name": "hisiem.search_events",
                "arguments": _search_args("authentication_success"),
            },
            {"decision": "FINALIZE"},
        ],
        "findings": ["search never ran"],
        "verdict": {
            "disposition": "MALICIOUS",  # would be used ONLY if the model is consulted
            "summary": "should not be reached past the deadline",
            "confidence": 0.9,
        },
    }
    # Fake clock: deadline = base + 60s; the first decide_next pushes time past it.
    import hisiem_soc_copilot.agent.graph.nodes as nodes

    clock: list[float] = [1_000_000.0]
    real_time = nodes.time.time
    try:
        nodes.time.time = lambda: clock[0]  # type: ignore[method-assign]
        model = _ClockBumpingModel(script=script, clock=clock, offset=61.0)
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
    finally:
        nodes.time.time = real_time  # type: ignore[method-assign]

    # The deadline passed after the first decide consult: the scheduled tool never
    # executed, and assess + verdict were never consulted (bounded finalize).
    assert hisiem.calls.count("search_events") == 0
    assert model.calls == ["plan", "decide_next"]  # no assess, no verdict
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
    assert any(
        "deadline" in (u.description or "").lower() for u in result.uncertainties
    )

async def test_resume_keeps_consumed_budget_not_reset_to_full() -> None:
    """A restarted/resumed graph run must keep its checkpointed remaining counters —
    never re-seed the aggregate's FULL budget (which would grant unlimited calls)."""
    uows, inv = _start(budget=BudgetLimits(max_steps=20, max_tool_calls=30, max_llm_calls=20))
    await _boot(uows, inv)
    runtime = _runtime(uows, {})
    deadline = 1_234_567.0

    # Fresh run (no counters in state) → seeded from the aggregate's limits.
    fresh = await load_investigation(
        runtime, {"investigation_id": str(inv.id)}
    )
    assert fresh["budget_remaining_steps"] == 20
    assert fresh["budget_remaining_tool_calls"] == 30
    assert fresh["budget_remaining_llm_calls"] == 20

    # Resume after a crash: counters were checkpointed mid-run (some consumed) →
    # load must KEEP them, NOT reset to the full 20/30/20.
    resumed = await load_investigation(
        runtime,
        {
            "investigation_id": str(inv.id),
            "budget_remaining_steps": 3,
            "budget_remaining_tool_calls": 1,
            "budget_remaining_llm_calls": 5,
            "budget_deadline_at": deadline,
        },
    )
    assert resumed["budget_remaining_steps"] == 3  # not 20
    assert resumed["budget_remaining_tool_calls"] == 1  # not 30
    assert resumed["budget_remaining_llm_calls"] == 5  # not 20
    # The deadline is also preserved (a restart cannot extend the wall-clock budget).
    assert resumed["budget_deadline_at"] == deadline


async def test_plan_is_an_llm_consult_within_budget() -> None:
    """plan consumes one LLM-call slot, so even the shortest run stays within
    max_llm_calls and the final counter never goes negative."""
    uows, inv = _start(budget=BudgetLimits(max_llm_calls=3))
    await _boot(uows, inv)
    model = ScriptedModelProvider(
        script={
            "decide": [{"decision": "FINALIZE"}],
            "verdict": {
                "disposition": "INCONCLUSIVE",
                "summary": "no reads needed",
                "confidence": 0.3,
                "uncertainty": "no event evidence gathered",
            },
        }
    )
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
    # plan + assess + verdict = 3 <= max_llm_calls (decide short-circuits to FINALIZE
    # without a consult when fewer than two slots remain).
    assert len(model.calls) == 3
    assert model.calls == ["plan", "assess", "verdict"]
    uow = uows()
    completed = await uow.investigations.get(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert completed is not None
    assert completed.status == InvestigationStatus.COMPLETED


def test_runtime_budget_constants_are_positive() -> None:
    """Budget bounds are strictly positive so a zero/negative ceiling can never
    deadlock the graph at seed time."""
    limits = BudgetLimits()
    assert limits.max_steps > 0
    assert limits.max_tool_calls > 0
    assert limits.max_llm_calls > 0
    assert limits.max_llm_tokens > 0
    assert limits.max_duration_seconds > 0
    assert "max_llm_calls" in limits.as_dict()
