"""Durable graph runner — drives one investigation to terminal via the checkpointer.

The runner is the orchestration the outbox dispatcher invokes. These tests prove
it end-to-end over the IN-MEMORY application fakes + a LangGraph MemorySaver:

- a CREATED investigation is bridged RUNNING and the graph runs to COMPLETED;
- an OrchestrationBinding is created (stable thread_id) and reused;
- re-running the same investigation does not duplicate evidence/result rows
  (receipt + dedup + result-immutability hold across checkpointed runs);
- tool executions are audited (RUNNING → SUCCEEDED/FAILED) with bounded metadata.

No Postgres is required. The real AsyncPostgresSaver restart/resume chain is in
the Postgres integration suite.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver

from hisiem_soc_copilot.agent.evidence.normalizer import EvidenceNormalizer
from hisiem_soc_copilot.agent.graph.builder import build_investigation_graph
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
from hisiem_soc_copilot.infrastructure.durable.investigation_runner import (
    AsyncInvestigationGraphRunner,
)
from hisiem_soc_copilot.infrastructure.llm.scripted import ScriptedModelProvider
from tests.fixtures.fakes import FakeUnitOfWorkFactory
from tests.fixtures.hisiem_fake import FakeHisiem
from tests.fixtures.ssh_models import GroundedSshModel


def _runner_factory(
    uows: FakeUnitOfWorkFactory,
    *,
    model: ScriptedModelProvider,
    hisiem: FakeHisiem,
) -> tuple[Callable[[str], GraphRuntime], Any]:
    handler = InvestigationWorkflowHandler(unit_of_work_factory=uows)

    def _runtime(tenant_id: str) -> GraphRuntime:
        return GraphRuntime(
            uow_factory=uows,
            workflow_handler=handler,
            model=model,
            executor=ToolExecutor(hisiem=hisiem),
            normalizer=EvidenceNormalizer(),
            registry=ToolRegistry(),
            hisiem=hisiem,
            tenant_id=tenant_id,
        )

    return _runtime, handler


async def _create_investigation(
    uows: FakeUnitOfWorkFactory, *, tenant_id: str = "tenant-a"
) -> Investigation:
    inv = Investigation.create(
        id=uuid4(),
        tenant_id=tenant_id,
        source_alert_ref=ExternalResourceRef(
            provider="hisiem", resource_type="alert", address_id="alert-runner-1"
        ),
        initiated_by=ActorRef(subject_id="analyst", tenant_id=tenant_id),
        budget_limits=BudgetLimits(),
    )
    uow = uows()
    await uow.investigations.add(inv)
    await uow.commit()
    return inv


def _ssh_model() -> ScriptedModelProvider:
    return GroundedSshModel(
        script={
            "plan_steps": {
                "read_rule": "Read the detection rule that fired",
                "search_success": "Search for a successful authentication after failures",
            },
            "decide": [
                {
                    "tool_name": "hisiem.get_detection_rule",
                    "arguments": {"rule_id": "ssh_brute_force"},
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
            ],
            "findings": ["success observed after brute force"],
            "verdict": {
                "disposition": "MALICIOUS",
                "summary": "SSH compromise confirmed",
                "confidence": 0.9,
            },
        }
    )


async def test_runner_bridges_created_to_running_and_completes() -> None:
    uows = FakeUnitOfWorkFactory()
    inv = await _create_investigation(uows)
    model = _ssh_model()
    hisiem = FakeHisiem(alert_id="alert-runner-1")
    runtime_factory, handler = _runner_factory(uows, model=model, hisiem=hisiem)

    runner = AsyncInvestigationGraphRunner(
        unit_of_work_factory=uows,
        workflow_handler=handler,
        runtime_factory=runtime_factory,
        compile_graph=build_investigation_graph,
        checkpointer_factory=lambda: _memctx(),
    )
    # The runner is invoked by the dispatcher with the investigation id/tenant.
    await runner.run_investigation(
        investigation_id=str(inv.id), tenant_id="tenant-a"
    )

    uow = uows()
    try:
        loaded = await uow.investigations.get(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        assert loaded is not None
        assert loaded.status == InvestigationStatus.COMPLETED
        binding = await uow.bindings.get(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        assert binding is not None
        assert binding.thread_id == f"inv:{inv.id}"
        result = await uow.results.get_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        assert result is not None
        assert result.verdict.disposition.value == "MALICIOUS"
        # Evidence was recorded through the tool/ingest path.
        evidence = await uow.evidence.list_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        assert len(evidence) >= 1
    finally:
        await uow.close()


async def test_repeat_run_does_not_duplicate_rows_and_reuses_binding() -> None:
    uows = FakeUnitOfWorkFactory()
    inv = await _create_investigation(uows)
    hisiem = FakeHisiem(alert_id="alert-runner-1")

    def _runner() -> AsyncInvestigationGraphRunner:
        runtime_factory, handler = _runner_factory(
            uows, model=_ssh_model(), hisiem=hisiem
        )
        return AsyncInvestigationGraphRunner(
            unit_of_work_factory=uows,
            workflow_handler=handler,
            runtime_factory=runtime_factory,
            compile_graph=build_investigation_graph,
            checkpointer_factory=lambda: _memctx(),
        )

    await _runner().run_investigation(
        investigation_id=str(inv.id), tenant_id="tenant-a"
    )

    uow = uows()
    try:
        evidence_first = await uow.evidence.list_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        findings_first = await uow.findings.list_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        result_first = await uow.results.get_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        binding_first = await uow.bindings.get(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        audit_first = uow.tool_invocations.by_investigation(inv.id)
    finally:
        await uow.close()

    # A second run (as if a duplicate outbox delivery arrived after a crash) must
    # be a no-op for domain rows: the runner sees COMPLETED and short-circuits.
    await _runner().run_investigation(
        investigation_id=str(inv.id), tenant_id="tenant-a"
    )

    uow = uows()
    try:
        evidence_second = await uow.evidence.list_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        findings_second = await uow.findings.list_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        result_second = await uow.results.get_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        binding_second = await uow.bindings.get(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        audit_second = uow.tool_invocations.by_investigation(inv.id)
    finally:
        await uow.close()

    assert len(evidence_second) == len(evidence_first)
    assert len(findings_second) == len(findings_first)
    assert result_second is not None and result_first is not None
    assert result_second.id == result_first.id  # immutable result reused
    assert binding_second is not None and binding_first is not None
    assert binding_second.thread_id == binding_first.thread_id  # stable
    # Tool audit rows are also stable (by-key finish, not duplicated).
    assert len(audit_second) == len(audit_first)
    assert all(r.status == "SUCCEEDED" for r in audit_second)


async def test_tool_execution_is_audited_with_bounded_metadata() -> None:
    uows = FakeUnitOfWorkFactory()
    inv = await _create_investigation(uows)
    model = _ssh_model()
    hisiem = FakeHisiem(alert_id="alert-runner-1")
    runtime_factory, handler = _runner_factory(uows, model=model, hisiem=hisiem)
    runner = AsyncInvestigationGraphRunner(
        unit_of_work_factory=uows,
        workflow_handler=handler,
        runtime_factory=runtime_factory,
        compile_graph=build_investigation_graph,
        checkpointer_factory=lambda: _memctx(),
    )
    await runner.run_investigation(
        investigation_id=str(inv.id), tenant_id="tenant-a"
    )

    audits = uows.tool_invocations.by_investigation(inv.id)
    assert len(audits) >= 2  # detection-rule read + event search
    for audit in audits:
        assert audit.status == "SUCCEEDED"
        # arguments are bounded (no raw logs / full conditions in result).
        assert audit.tool_version is None
        # result_metadata holds only bounded counts/status.
        assert audit.result_metadata is not None
        assert "tool" in audit.result_metadata
        assert audit.safe_error_message is None


class _FailingThenWorkingHisiem(FakeHisiem):
    """Fails the FIRST search_events call, then works — a recoverable tool error."""

    def __init__(self, *, alert_id: str) -> None:
        super().__init__(alert_id=alert_id)
        self.search_calls = 0

    async def search_events(self, **kwargs: Any) -> Any:
        from hisiem_soc_copilot.application.errors import ExternalServiceError

        self.search_calls += 1
        if self.search_calls == 1:
            raise ExternalServiceError("upstream down", service="hisiem")
        return await super().search_events(**kwargs)


async def test_recoverable_tool_failure_is_audited_failed_but_investigation_completes() -> None:
    """A single tool failure → FAILED audit row; the graph keeps going to COMPLETED.

    The detection-rule read succeeds first (evidence grounded), then the event
    search fails (UPSTREAM_UNAVAILABLE → FAILED audit). The graph converges with
    the rule evidence, finalizes MALICIOUS, and reaches COMPLETED — never FAILED.
    """
    uows = FakeUnitOfWorkFactory()
    inv = await _create_investigation(uows)
    hisiem = _FailingThenWorkingHisiem(alert_id="alert-runner-1")
    model = ScriptedModelProvider(
        script={
            "decide": [
                # Turn 1: read the detection rule → SUCCESS (grounds a finding).
                {
                    "tool_name": "hisiem.get_detection_rule",
                    "arguments": {"rule_id": "ssh_brute_force"},
                },
                # Turn 2: the event search — HISIEM is down → recoverable FAILED.
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
            ],
            "findings": ["rule confirms SSH brute force pattern"],
            "verdict": {
                "disposition": "MALICIOUS",
                "summary": "compromised",
                "confidence": 0.9,
            },
        }
    )
    runtime_factory, handler = _runner_factory(uows, model=model, hisiem=hisiem)
    runner = AsyncInvestigationGraphRunner(
        unit_of_work_factory=uows,
        workflow_handler=handler,
        runtime_factory=runtime_factory,
        compile_graph=build_investigation_graph,
        checkpointer_factory=lambda: _memctx(),
    )
    await runner.run_investigation(
        investigation_id=str(inv.id), tenant_id="tenant-a"
    )

    audits = uows.tool_invocations.by_investigation(inv.id)
    # The rule read SUCCEEDED; the search FAILED (upstream down).
    failed = [a for a in audits if a.status == "FAILED"]
    succeeded = [a for a in audits if a.status == "SUCCEEDED"]
    assert len(failed) == 1
    assert len(succeeded) >= 1
    assert failed[0].error_code is not None  # UPSTREAM_UNAVAILABLE mapped

    uow = uows()
    try:
        loaded = await uow.investigations.get(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        assert loaded is not None
        assert loaded.status == InvestigationStatus.COMPLETED  # not FAILED
    finally:
        await uow.close()


class _Memctx:
    """Minimal async context manager yielding a LangGraph MemorySaver."""

    def __init__(self) -> None:
        self.saver = MemorySaver()

    async def __aenter__(self) -> Any:
        return self.saver

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def _memctx() -> _Memctx:
    return _Memctx()
