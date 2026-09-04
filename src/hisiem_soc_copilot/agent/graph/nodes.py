"""LangGraph nodes for the read-only Investigation flow.

Nodes are thin (application-commands...md §20): each calls an Application command
via the workflow handler or reads through a fresh UnitOfWork. They never touch
SQL/ORM/domain internals directly.

Flow (application-commands...md §19, read-only prefix):
    START → load_investigation → hydrate_alert → plan → decide_next
        decide_next: CONTINUE+tool → execute_and_ingest
                     (loop)                    │
                     FINALIZE ──────────────────┴→ assess
        assess (convergence, once): AssessHypotheses + RecordFindings
            → finalize_result → complete → END

Budget: every CONTINUE decision consumes one step. A CONTINUE with no budget left
routes to FINALIZE, so exhaustion yields COMPLETED + INCONCLUSIVE (never FAILED).
A single tool/data-source failure is a typed UNAVAILABLE result and the graph
continues (the investigation must not fail because one provider is down).

``execute_and_ingest`` runs one read tool AND records its Evidence in a single
node: the bounded ToolResult is consumed inside one checkpoint step, so a crash
mid-node re-runs the whole node and both the tool audit (by-key) and Evidence (by
dedup key) are idempotent — a checkpointed resume neither loses nor duplicates
evidence.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ...application.commands.investigation import (
    AssessHypotheses,
    AssessmentEvidenceRelation,
    ChangeInvestigationPhase,
    CompleteInvestigation,
    EvidenceObservation,
    FinalizeInvestigationResult,
    FindingCandidate,
    HypothesisAssessmentCandidate,
    HypothesisCandidate,
    PlanStepCandidate,
    RecordEvidenceBatch,
    RecordFindings,
    RegisterHypotheses,
    ResultVerdictCandidate,
    ReviseInvestigationPlan,
    UncertaintyCandidate,
)
from ...application.ports.model_provider import AssessRequest, DecideNextRequest, PlanRequest
from ...contracts.llm.types import NextStep
from ...contracts.tools.types import ToolCandidate
from ...domain.investigation.content import compute_content_hash
from ...domain.investigation.entities import Evidence
from ...domain.investigation.enums import (
    EvidenceRelation,
    InvestigationPhase,
    VerdictDisposition,
)
from ..tools.executor import ToolExecution
from .runtime import GraphRuntime
from .state import InvestigationGraphState
from .tool_audit import record_finished, record_started

EXECUTE_TOOL = "execute_and_ingest"
CONVERGE = "assess"
FINALIZE = "finalize_result"
COMPLETE = "complete"


def _inv_id(state: InvestigationGraphState) -> UUID:
    value = state.get("investigation_id")
    if not value:
        raise ValueError("graph state requires an investigation_id")
    return UUID(value)


def _inv_str(state: InvestigationGraphState) -> str:
    return str(_inv_id(state))


def _inv_key(inv_id: UUID, suffix: str) -> str:
    """Deterministic command idempotency key for a graph-issued command.

    Keys are stable across a checkpointed resume, so a node whose commit landed
    but whose checkpoint was lost never re-applies the same business change
    (application-commands...md §5).
    """
    return f"investigation:{inv_id}:{suffix}"


async def load_investigation(
    runtime: GraphRuntime, state: InvestigationGraphState
) -> dict[str, Any]:
    """Bind the aggregate by id; confirm RUNNING; seed the step budget.

    The graph input is ONLY investigation_id. Tenant is bound by the trusted
    orchestrator scope on the runtime (runtime.tenant_id) and used to scope every
    repository read — never supplied by the model or an ordinary client.
    """
    from ...domain.investigation.enums import InvestigationStatus

    uow = runtime.new_unit_of_work()
    try:
        investigation = await uow.investigations.get(
            tenant_id=runtime.tenant_id, investigation_id=_inv_id(state)
        )
    finally:
        await uow.close()
    if investigation is None:
        raise RuntimeError(f"investigation {_inv_id(state)} not found")
    if investigation.status != InvestigationStatus.RUNNING:
        # Reconciliation (application-commands...md §27): a terminal investigation
        # (COMPLETED/FAILED/CANCELLED) is not re-run — the graph stops with a
        # no-op, so a retried/resumed run never duplicates rows.
        return {
            "investigation_id": str(investigation.id),
            "investigation_revision": investigation.revision,
            "iteration": 0,
            "budget_remaining_steps": 0,
            "stop_reason": f"ALREADY_{investigation.status.value}",
        }
    return {
        "investigation_id": str(investigation.id),
        "investigation_revision": investigation.revision,
        "iteration": 0,
        "budget_remaining_steps": investigation.budget_limits.max_steps,
        "next_action": None,
        "assessment": None,
        "alert_context": {},
        "plan_revision_id": None,
        "new_evidence_ids": [],
        "pending_tool_request": None,
        "last_tool_invocation_id": None,
        "last_tool_error": None,
        "result_id": None,
        "stop_reason": None,
    }


async def hydrate_alert(
    runtime: GraphRuntime, state: InvestigationGraphState
) -> dict[str, Any]:
    """Hydrate authoritative alert context from HISIEM into a bounded snapshot."""
    uow = runtime.new_unit_of_work()
    try:
        investigation = await uow.investigations.get(
            tenant_id=runtime.tenant_id, investigation_id=_inv_id(state)
        )
    finally:
        await uow.close()
    if investigation is None:
        raise RuntimeError("investigation vanished during hydrate")
    ref = investigation.source_alert_ref
    alert = await runtime.hisiem.get_alert(
        tenant_id=runtime.tenant_id, alert_id=ref.address_id
    )
    if alert is None:
        raise RuntimeError(f"alert {ref.address_id} is no longer available")
    return {
        "alert_context": {
            "alert_id": alert.alert_id,
            "tenant_id": runtime.tenant_id,
            "title": alert.description or alert.rule_name,
            "severity": alert.severity,
            "status": alert.status,
            "rule_name": alert.rule_name,
            "rule_id": alert.rule_id,
            "rule_type": alert.rule_type,
            "detected_at": alert.detected_at,
            "risk_score": alert.risk_score,
            "entity": alert.entity,
            "case_id": alert.case_id,
            "source_ip": alert.source_ip,
            "user_name": alert.user_name,
            "host_name": alert.host_name,
            "event_category": alert.event_category,
            "event_action": alert.event_action,
            "log_source_id": alert.log_source_id,
            "rule_tags": alert.rule_tags,
            "event_count": alert.event_count,
        }
    }


async def plan(runtime: GraphRuntime, state: InvestigationGraphState) -> dict[str, Any]:
    """Change phase to PLANNING; produce a plan revision + an OPEN hypothesis."""
    inv_id = _inv_id(state)
    handler = runtime.workflow_handler
    await handler.change_phase(
        ChangeInvestigationPhase(
            tenant_id=runtime.tenant_id,
            investigation_id=inv_id,
            idempotency_key=_inv_key(inv_id, "phase:planning"),
            phase=InvestigationPhase.PLANNING,
        )
    )
    alert: dict[str, Any] = dict(state.get("alert_context") or {})
    model_plan = await runtime.model.plan(
        PlanRequest(
            investigation_id=_inv_str(state),
            alert_summary=(
                f"{alert.get('title') or 'alert'} "
                f"(severity={alert.get('severity') or '?'}, "
                f"rule={alert.get('rule_name') or '?'}, "
                f"source_ip={alert.get('source_ip') or '?'})"
            ),
            tool_names=runtime.registry.model_selectable_names,
        )
    )
    steps = [
        PlanStepCandidate(step_key=s.step_id, objective=s.objective, ordinal=i)
        for i, s in enumerate(model_plan.steps)
    ]
    _, plan_revision = await handler.revise_plan(
        ReviseInvestigationPlan(
            tenant_id=runtime.tenant_id,
            investigation_id=inv_id,
            idempotency_key=_inv_key(inv_id, "plan:1"),
            revision=1,
            goal=model_plan.goal,
            steps=steps,
        )
    )
    await handler.register_hypotheses(
        RegisterHypotheses(
            tenant_id=runtime.tenant_id,
            investigation_id=inv_id,
            idempotency_key=_inv_key(inv_id, "hypotheses:default"),
            hypotheses=[HypothesisCandidate(statement=_default_hypothesis(alert))],
        )
    )
    return {
        "plan_revision_id": str(plan_revision.id),
        "iteration": 0,
    }


def _default_hypothesis(alert: dict[str, Any]) -> str:
    entity = alert.get("source_ip") or alert.get("user_name") or "an entity"
    rule = alert.get("rule_name") or "the detection rule"
    return (
        f"The {rule} alert involving {entity} reflects a real unauthorized-access "
        "attempt that succeeded (possible account compromise)."
    )


async def decide_next(
    runtime: GraphRuntime, state: InvestigationGraphState
) -> dict[str, Any]:
    """Ask the model what to read next (candidate only); gate CONTINUE on budget.

    Each CONTINUE decision consumes one budget step. When the model says FINALIZE
    (or the budget is spent), route to the convergence ``assess`` node.
    """
    budget = int(state.get("budget_remaining_steps") or 0)
    evidence_ids = state.get("new_evidence_ids") or []
    next_step: NextStep = await runtime.model.decide_next(
        DecideNextRequest(
            investigation_id=_inv_str(state),
            iteration=int(state.get("iteration") or 0),
            plan_goal="Investigate whether the alert indicates an account compromise",
            evidence_summary=evidence_ids,
            tool_names=runtime.registry.model_selectable_names,
        )
    )
    if next_step.decision == "CONTINUE" and next_step.tool_name and budget > 0:
        arguments = dict(next_step.arguments or {})
        tool_call_key = f"iteration-{state.get('iteration') or 0}"
        return {
            "next_action": EXECUTE_TOOL,
            "budget_remaining_steps": budget - 1,
            "pending_tool_request": {
                "tool": next_step.tool_name,
                "arguments": arguments,
                "step_key": tool_call_key,
                "call_fingerprint": compute_content_hash(
                    {"tool": next_step.tool_name, "arguments": arguments}
                ),
            },
        }
    return {"next_action": CONVERGE, "assessment": "FINALIZE"}


async def execute_and_ingest(
    runtime: GraphRuntime, state: InvestigationGraphState
) -> dict[str, Any]:
    """Run one allowlisted read tool, audit it, and record evidence — atomically.

    Executing the tool AND ingesting its normalized evidence in a SINGLE graph
    node closes the resume crash-window: the typed ToolResult is used immediately
    and never needs to survive across a checkpoint boundary (a crash mid-node
    re-runs the whole node, and both the tool audit (by-key) and Evidence (by
    dedup key) are idempotent, so nothing duplicates).

    Two short transactions bracket the tool call (audit-started before, audit-
    finished after) so the tool never executes inside a DB transaction. A single
    tool being unavailable / rejected / empty must not fail the whole
    investigation: route back to convergence with whatever evidence exists.
    """
    pending = state.get("pending_tool_request")
    if not pending:
        return {"next_action": CONVERGE, "last_tool_error": "no pending tool request"}
    inv_id = _inv_id(state)
    tool_name = str(pending["tool"])
    arguments = dict(pending.get("arguments") or {})
    # Deterministic across a checkpointed resume: same investigation + tool +
    # candidate arguments → same audit row + idempotency key.
    fingerprint = str(pending.get("call_fingerprint") or "")
    audit_key = f"tool:{tool_name}:{fingerprint}"
    alert: dict[str, Any] = dict(state.get("alert_context") or {})

    # 1) Audit STARTED in its own short transaction.
    uow = runtime.new_unit_of_work()
    try:
        await record_started(
            uow,
            tenant_id=runtime.tenant_id,
            investigation_id=inv_id,
            tool_name=tool_name,
            idempotency_key=audit_key,
            arguments=_bounded_arguments(tool_name, arguments),
        )
        await uow.commit()
    finally:
        await uow.close()

    # 2) Run the tool — no DB transaction open.
    try:
        execution = await runtime.executor.execute(
            candidate=ToolCandidate(tool_name=tool_name, arguments=arguments),
            tenant_id=runtime.tenant_id,
            source_alert_ref={
                "provider": "hisiem",
                "resource_type": "alert",
                "address_id": alert.get("alert_id") or "",
            },
        )
    except Exception as exc:  # deterministic, typed; continue the investigation
        await _finish_audit(runtime, inv_id, audit_key, "FAILED", error_code="EXECUTION_ERROR")
        return {"next_action": CONVERGE, "last_tool_error": str(exc)}

    # 3) Audit FINISHED in its own short transaction.
    status, error_code, message, metadata = _audit_outcome(execution)
    await _finish_audit(
        runtime, inv_id, audit_key, status,
        error_code=error_code, safe_error_message=message, result_metadata=metadata,
    )

    if execution.status in ("UNAVAILABLE", "REJECTED", "NO_DATA"):
        return {
            "next_action": CONVERGE,
            "last_tool_invocation_id": None,
            "last_tool_error": execution.result.error,
        }

    # 4) Ingest the normalized evidence (same node, same crash/replay window).
    observations: list[EvidenceObservation] = []
    if execution.result.tool_name == "hisiem.search_events":
        observations = runtime.normalizer.normalize_search_events(
            execution.result, tool_call_id=execution.tool_call_id
        )
    elif execution.result.tool_name == "hisiem.get_detection_rule":
        observations = runtime.normalizer.normalize_detection_rule(
            execution.result, tool_call_id=execution.tool_call_id
        )
    if observations:
        await runtime.workflow_handler.record_evidence_batch(
            RecordEvidenceBatch(
                tenant_id=runtime.tenant_id,
                investigation_id=inv_id,
                idempotency_key=_inv_key(inv_id, f"evidence:{execution.tool_call_id}"),
                observations=observations,
            )
        )

    # Loop back to decide_next to pick the next read (or finalize).
    return {
        "next_action": "decide_next",
        "assessment": None,
        "last_tool_invocation_id": execution.tool_call_id,
        "last_tool_error": None,
        "pending_tool_request": None,
    }


async def _finish_audit(
    runtime: GraphRuntime,
    investigation_id: UUID,
    audit_key: str,
    status: str,
    *,
    error_code: str | None = None,
    safe_error_message: str | None = None,
    result_metadata: dict[str, Any] | None = None,
) -> None:
    uow = runtime.new_unit_of_work()
    try:
        await record_finished(
            uow,
            tenant_id=runtime.tenant_id,
            investigation_id=investigation_id,
            idempotency_key=audit_key,
            status=status,
            error_code=error_code,
            safe_error_message=safe_error_message,
            result_metadata=result_metadata,
        )
        await uow.commit()
    finally:
        await uow.close()


def _audit_outcome(
    execution: ToolExecution,
) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
    """Map an executor outcome to bounded audit fields (never stores raw results)."""
    tool_name = execution.tool_name
    status = execution.status
    if status in ("SUCCESS", "NO_DATA"):
        data = execution.result.data or {}
        count = data.get("total") if tool_name == "hisiem.search_events" else None
        metadata: dict[str, Any] = {"tool": tool_name, "outcome": status}
        if count is not None:
            metadata["total"] = count
        return "SUCCEEDED", None, None, metadata
    return (
        "FAILED",
        execution.result.error_code or "TOOL_FAILED",
        (str(execution.result.error or "")[:300]) or "tool failed",
        {"tool": tool_name, "outcome": status},
    )


def _bounded_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Only query-shape arguments (no logs/raw content) may be persisted."""
    if tool_name == "hisiem.search_events":
        return {
            "from": arguments.get("from"),
            "to": arguments.get("to"),
            "limit": arguments.get("limit"),
            "conditions": arguments.get("conditions"),
        }
    if tool_name == "hisiem.get_detection_rule":
        return {"rule_id": arguments.get("rule_id")}
    return dict(arguments)


async def assess(runtime: GraphRuntime, state: InvestigationGraphState) -> dict[str, Any]:
    """Convergence node — run once: AssessHypotheses + RecordFindings.

    Called when decide_next returns FINALIZE (budget spent or the model is done).
    Records the hypothesis assessment and the evidence-grounded findings that the
    verdict will rest on, then routes to finalize_result.
    """
    inv_id = _inv_id(state)
    handler = runtime.workflow_handler
    iteration = int(state.get("iteration") or 0) + 1
    await handler.change_phase(
        ChangeInvestigationPhase(
            tenant_id=runtime.tenant_id,
            investigation_id=inv_id,
            idempotency_key=_inv_key(inv_id, "phase:verifying"),
            phase=InvestigationPhase.VERIFYING,
        )
    )
    uow = runtime.new_unit_of_work()
    try:
        evidence = await uow.evidence.list_by_investigation(
            tenant_id=runtime.tenant_id, investigation_id=inv_id
        )
        hypotheses = await uow.hypotheses.list_by_investigation(
            tenant_id=runtime.tenant_id, investigation_id=inv_id
        )
    finally:
        await uow.close()

    evidence_uuids = [e.id for e in evidence]
    evidence_ids = [str(e.id) for e in evidence]
    summary = await runtime.model.assess(
        AssessRequest(
            investigation_id=_inv_str(state),
            evidence_summary=[_evidence_line(e) for e in evidence],
            finding_candidates=[],
        )
    )
    if hypotheses:
        status = "SUPPORTED" if evidence_ids else "UNRESOLVED"
        relations = []
        if evidence_ids:
            relations = [
                AssessmentEvidenceRelation(
                    evidence_id=e.id, relation=EvidenceRelation.SUPPORTS
                )
                for e in evidence
            ]
        await handler.assess_hypotheses(
            AssessHypotheses(
                tenant_id=runtime.tenant_id,
                investigation_id=inv_id,
                idempotency_key=_inv_key(inv_id, "assess:convergence"),
                assessments=[
                    HypothesisAssessmentCandidate(
                        hypothesis_id=h.id,
                        status=status,
                        reason_summary=summary.reason,
                        evidence_relations=relations,
                    )
                    for h in hypotheses
                ],
            )
        )
    if evidence_ids:
        statements = [f.statement for f in summary.findings] or [
            _default_finding(evidence)
        ]
        await handler.record_findings(
            RecordFindings(
                tenant_id=runtime.tenant_id,
                investigation_id=inv_id,
                idempotency_key=_inv_key(inv_id, "findings:convergence"),
                findings=[
                    FindingCandidate(
                        statement=statement,
                        evidence_citations=evidence_uuids,
                    )
                    for statement in statements
                ],
            )
        )
    return {
        "iteration": iteration,
        "assessment": "FINALIZE",
        "new_evidence_ids": evidence_ids,
    }


async def finalize_result(
    runtime: GraphRuntime, state: InvestigationGraphState
) -> dict[str, Any]:
    """Finalize one immutable InvestigationResult from grounded facts + verdict."""
    inv_id = _inv_id(state)
    handler = runtime.workflow_handler
    await handler.change_phase(
        ChangeInvestigationPhase(
            tenant_id=runtime.tenant_id,
            investigation_id=inv_id,
            idempotency_key=_inv_key(inv_id, "phase:finalizing"),
            phase=InvestigationPhase.FINALIZING,
        )
    )
    verdict_candidate = await runtime.model.verdict(
        AssessRequest(
            investigation_id=_inv_str(state),
            evidence_summary=state.get("new_evidence_ids") or [],
            finding_candidates=[],
        )
    )
    uow = runtime.new_unit_of_work()
    try:
        findings = await uow.findings.list_by_investigation(
            tenant_id=runtime.tenant_id, investigation_id=inv_id
        )
    finally:
        await uow.close()

    disposition = VerdictDisposition(verdict_candidate.disposition)
    uncertainties = []
    if verdict_candidate.uncertainty:
        uncertainties.append(
            UncertaintyCandidate(description=verdict_candidate.uncertainty)
        )
    result_key = _inv_key(inv_id, f"result:{verdict_candidate.disposition}")
    _, result = await handler.finalize_result(
        FinalizeInvestigationResult(
            tenant_id=runtime.tenant_id,
            investigation_id=inv_id,
            idempotency_key=result_key,
            verdict=ResultVerdictCandidate(
                disposition=disposition,
                summary=verdict_candidate.summary,
                confidence=verdict_candidate.confidence,
            ),
            finding_ids=[f.id for f in findings],
            uncertainties=uncertainties,
        )
    )
    return {"result_id": str(result.id)}


async def complete(
    runtime: GraphRuntime, state: InvestigationGraphState
) -> dict[str, Any]:
    """RUNNING → COMPLETED (read-only round has no executable response)."""
    inv_id = _inv_id(state)
    investigation = await runtime.workflow_handler.complete(
        CompleteInvestigation(
            tenant_id=runtime.tenant_id,
            investigation_id=inv_id,
            idempotency_key=_inv_key(inv_id, "complete:no_response"),
        )
    )
    return {
        "stop_reason": f"COMPLETED_WITHOUT_RESPONSE status={investigation.status.value}"
    }


def _evidence_line(evidence: Evidence) -> str:
    line = evidence.summary or str(evidence.observation)
    return str(line)[:200]


def _default_finding(evidence: list[Evidence]) -> str:
    first = evidence[0]
    op = first.source.operation
    return (
        f"Evidence gathered by {op} supports the alert's account-compromise "
        "hypothesis (grounded in collected HISIEM events)."
    )
