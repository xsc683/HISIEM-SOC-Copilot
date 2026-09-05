"""SSH Brute Force → Possible Account Compromise: full graph execution test.

Proves the read-only Investigation vertical slice runs end-to-end through the
LangGraph graph and the in-memory application fakes:
    load_investigation → hydrate_alert → plan → decide_next →
    execute_read_tool → ingest_evidence → (loop) → assess → finalize_result →
    complete → END
and that the persisted domain ledger contains Evidence → (SUPPORTED) Hypothesis
Assessment → Finding → InvestigationResult(MALICIOUS) with the Investigation
COMPLETED.

The model is the deterministic ScriptedModelProvider and HISIEM is a scripted
fake, so the whole flow is reproducible. No network, no real DB.
"""

from __future__ import annotations

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
from hisiem_soc_copilot.domain.shared.identifiers import utc_now
from hisiem_soc_copilot.infrastructure.llm.scripted import ScriptedModelProvider
from tests.fixtures.fakes import FakeUnitOfWorkFactory
from tests.fixtures.hisiem_fake import FakeHisiem
from tests.fixtures.ssh_models import GroundedSshModel


def _scripted_ssh_model() -> ScriptedModelProvider:
    """A deterministic model that drives the SSH golden path.

    decide turns:
    1. read the detection rule
    2. search for a successful login after the failures
    then FINALIZE. The assess step grounds the account-compromise hypothesis on the
    observed successful login and emits a MALICIOUS verdict.
    """
    return GroundedSshModel(
        script={
            "plan_steps": {
                "read_rule": "Read the detection rule that fired",
                "search_success": "Search for a successful authentication after failures",
            },
            "decide": [
                {"tool_name": "hisiem.get_detection_rule",
                 "arguments": {"rule_id": "ssh_brute_force"},
                 "reason": "Understand the rule that fired"},
                {"tool_name": "hisiem.search_events",
                 "arguments": {
                     "from": "2026-09-01T09:55:00Z",
                     "to": "2026-09-01T10:05:00Z",
                     "conditions": [
                         {
                             "field": "event.action",
                             "operator": "is",
                             "value": "authentication_success",
                         },
                         {"field": "user.name", "operator": "is", "value": "root"},
                     ],
                     "limit": 50,
                 },
                 "reason": "Look for a successful login after the failures"},
            ],
            "findings": [
                "A successful root login followed a burst of SSH brute-force failures "
                "from the same source IP (203.0.113.9)"
            ],
            "verdict": {
                "disposition": "MALICIOUS",
                "summary": "SSH brute force escalated into a successful account compromise",
                "confidence": 0.85,
                "uncertainty": None,
            },
        }
    )


async def _started_investigation() -> tuple[FakeUnitOfWorkFactory, Investigation, str]:
    """Start a real RUNNING investigation in the shared in-memory store."""
    uows = FakeUnitOfWorkFactory()
    uow = uows()
    alert_ref = ExternalResourceRef(
        provider="hisiem",
        resource_type="alert",
        address_id="ssh-bruteforce-alert-1",
    )
    actor = ActorRef(subject_id="analyst", tenant_id="tenant-a")
    investigation = Investigation.create(
        id=uuid4(),
        tenant_id="tenant-a",
        source_alert_ref=alert_ref,
        initiated_by=actor,
        budget_limits=BudgetLimits(),
        now=utc_now(),
    )
    await uow.investigations.add(investigation)
    await uow.commit()
    investigation.start(actor=actor)
    await uow.investigations.update(investigation)
    await uow.commit()
    return uows, investigation, "tenant-a"


def _runtime(
    uows: FakeUnitOfWorkFactory,
    *,
    tenant_id: str,
    hisiem: FakeHisiem,
) -> GraphRuntime:
    handler = InvestigationWorkflowHandler(unit_of_work_factory=uows)
    return GraphRuntime(
        uow_factory=uows,
        workflow_handler=handler,
        model=_scripted_ssh_model(),
        executor=ToolExecutor(hisiem=hisiem),
        normalizer=EvidenceNormalizer(),
        registry=ToolRegistry(),
        hisiem=hisiem,
        tenant_id=tenant_id,
    )


async def test_ssh_golden_path_graph_reaches_completed_with_result() -> None:
    uows, investigation, tenant = await _started_investigation()
    hisiem = FakeHisiem(alert_id="ssh-bruteforce-alert-1")
    runtime = _runtime(uows, tenant_id=tenant, hisiem=hisiem)
    graph = build_investigation_graph(runtime)
    config = thread_config(str(investigation.id))

    final = await graph.ainvoke(
        {"investigation_id": str(investigation.id)},
        config,
    )

    # --- Graph terminal state ---
    assert final["stop_reason"].startswith("COMPLETED_WITHOUT_RESPONSE")
    assert final["result_id"] is not None

    # --- The investigation aggregate reached COMPLETED ---
    uow = uows()
    completed = await uow.investigations.get(
        tenant_id=tenant, investigation_id=investigation.id
    )
    assert completed is not None
    assert completed.status == InvestigationStatus.COMPLETED

    # --- Alert was hydrated + a detection-rule read + event search happened ---
    assert "get_alert:ssh-bruteforce-alert-1" in hisiem.calls
    assert any(c.startswith("get_detection_rule") for c in hisiem.calls)
    assert "search_events" in hisiem.calls

    # --- Evidence ledger has both rule + success-event evidence ---
    evidence = await uow.evidence.list_by_investigation(
        tenant_id=tenant, investigation_id=investigation.id
    )
    assert len(evidence) >= 2
    ops = {e.source.operation for e in evidence}
    assert {"search_events", "get_detection_rule"} <= ops

    # --- At least one Finding grounded in that evidence exists ---
    findings = await uow.findings.list_by_investigation(
        tenant_id=tenant, investigation_id=investigation.id
    )
    assert len(findings) >= 1
    assert all(len(f.evidence_citations) >= 1 for f in findings)

    # --- The hypothesis was assessed SUPPORTED ---
    hypotheses = await uow.hypotheses.list_by_investigation(
        tenant_id=tenant, investigation_id=investigation.id
    )
    assert any(h.status.value == "SUPPORTED" for h in hypotheses)

    # --- A MALICIOUS InvestigationResult exists and references the findings ---
    result = await uow.results.get_by_investigation(
        tenant_id=tenant, investigation_id=investigation.id
    )
    assert result is not None
    assert result.verdict.disposition.value == "MALICIOUS"
    assert set(result.finding_ids) == {f.id for f in findings}


async def test_evidence_source_tool_invocation_matches_audit_row() -> None:
    """Evidence.source_tool_invocation_id must resolve to the EXACT audit row id.

    The invocation identity is generated ONCE per logical tool call and threads
    through ToolInvocationRow.id, ToolExecution.tool_call_id, and
    Evidence.source_tool_invocation_id — so provenance is never a dangling/random
    id that cannot be joined to the real tool_invocation audit row.
    """
    uows, investigation, tenant = await _started_investigation()
    hisiem = FakeHisiem(alert_id="ssh-bruteforce-alert-1")
    runtime = _runtime(uows, tenant_id=tenant, hisiem=hisiem)
    graph = build_investigation_graph(runtime)
    await graph.ainvoke(
        {"investigation_id": str(investigation.id)},
        thread_config(str(investigation.id)),
    )

    uow = uows()
    evidence = await uow.evidence.list_by_investigation(
        tenant_id=tenant, investigation_id=investigation.id
    )
    invocations = uow.tool_invocations.by_investigation(investigation.id)
    invocation_ids = {r.id for r in invocations}

    # Every piece of evidence that claims a tool-invocation source must point at a
    # REAL audit row in this investigation.
    linked = [e for e in evidence if e.source_tool_call_id is not None]
    assert linked, "expected some tool-linked evidence in the SSH golden path"
    for e in linked:
        assert e.source_tool_call_id in invocation_ids, (
            f"evidence {e.id} cites source_tool_invocation_id {e.source_tool_call_id} "
            "that is not an existing tool_invocation row"
        )
