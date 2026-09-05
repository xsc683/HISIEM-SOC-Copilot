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

import time
from collections.abc import Awaitable
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
from ...application.ports.model_provider import (
    AssessRequest,
    DecideAlertContext,
    DecideNextRequest,
    PlanRequest,
    PreviousToolOutcome,
)
from ...contracts.llm.errors import (
    ModelConfigurationError,
    ModelProviderError,
)
from ...contracts.llm.types import (
    FindingCandidate as ModelFindingCandidate,
)
from ...contracts.llm.types import (
    HypothesisAssessmentCandidate as ModelAssessmentCandidate,
)
from ...contracts.llm.types import PlanStep
from ...contracts.tools.types import ToolCandidate
from ...domain.investigation.content import compute_content_hash
from ...domain.investigation.entities import Evidence
from ...domain.investigation.enums import (
    EvidenceRelation,
    InvestigationPhase,
    VerdictDisposition,
)
from ..tools.executor import ToolExecution
from .budget import RuntimeBudget
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


async def _model_consult(coro: Awaitable[Any]) -> Any | None:
    """Run ONE model consult; degrade deterministically on a provider outage.

    A real model failure (unavailable / timeout / rate-limit / refusal / invalid
    output) must NOT fail the investigation: the node applies its deterministic
    fallback (default plan / converge / UNRESOLVED / INCONCLUSIVE) so the run ends
    COMPLETED + INCONCLUSIVE. ``ModelConfigurationError`` is a deployment bug, not an
    outage — it re-raises so the operator sees it (never silently defaulted).
    """
    try:
        return await coro
    except ModelConfigurationError:
        raise
    except ModelProviderError:
        # Outage/refusal/malformed output → deterministic fallback, never FAILED.
        return None


def _deadline_passed(state: InvestigationGraphState) -> bool:
    """True when the runtime wall-clock deadline (epoch-seconds) has passed."""
    return RuntimeBudget.from_state(state).deadline_exceeded


# Upper bound on consecutive re-plans of the SAME failing tool call (same request
# fingerprint) before the graph stops re-planning and converges to a bounded
# finalize. Independent of the step/tool budget, so a degenerate model cannot spin
# forever proposing one unavailable call (Fix #5 repeated-attempt guard).
_MAX_SAME_FAILING_CALL_RETRIES = 2


def _outcome_for(
    tool_name: str,
    status: str,
    error_code: str | None,
    *,
    retryable: bool = False,
) -> dict[str, object]:
    """Bounded outcome for the graph state (only the tool's stable identity)."""
    return {
        "tool_name": tool_name,
        "status": status,
        "error_code": error_code,
        "retryable": retryable,
    }


def _previous_outcome_of(state: InvestigationGraphState) -> PreviousToolOutcome | None:
    """Reconstruct the bounded PreviousToolOutcome handed to the model on re-plan."""
    raw = state.get("previous_tool_outcome")
    if not raw:
        return None
    return PreviousToolOutcome(
        tool_name=str(raw.get("tool_name", "")),
        status=str(raw.get("status", "")),
        error_code=str(raw["error_code"]) if raw.get("error_code") else None,
        retryable=bool(raw.get("retryable", False)),
    )


def _repeat_budget_for(
    state: InvestigationGraphState, fingerprint: str
) -> tuple[int, str | None]:
    """Return (retries_used, current_fingerprint) for the same failing call.

    ``same_call_retries`` counts consecutive re-plans of ONE exact request
    fingerprint; proposing a different request resets the counter to zero.
    """
    current = state.get("failing_call_fingerprint")
    used = int(state.get("same_call_retries") or 0)
    if current != fingerprint:
        return 0, None
    return used, current


def _exceeded_repeat_budget(used_retries: int) -> bool:
    return used_retries >= _MAX_SAME_FAILING_CALL_RETRIES


def _invocation_uuid(inv_id: UUID, audit_key: str) -> UUID:
    """Deterministic UUID for one logical tool invocation (stable across replay).

    Namespaced under the investigation id + audit key so the same tool call on the
    same candidate always maps to the same invocation identity.
    """
    import hashlib

    digest = hashlib.sha256(f"{inv_id}:{audit_key}".encode()).hexdigest()
    return UUID(digest[:32])


async def load_investigation(
    runtime: GraphRuntime, state: InvestigationGraphState
) -> dict[str, Any]:
    """Bind the aggregate by id; confirm RUNNING; seed runtime budget on a FRESH run.

    The graph input is ONLY investigation_id. Tenant is bound by the trusted
    orchestrator scope on the runtime (runtime.tenant_id) and used to scope every
    repository read — never supplied by the model or an ordinary client.

    Runtime budget (steps/tool-calls/LLM-calls/deadline) is checkpointed with the
    graph state. On a fresh run the aggregate's limits seed full remaining counters;
    on a crash/restart/resume the previously checkpointed remaining counters are
    kept — a resumed run never gets its budget reset to full. The budget is runtime
    authority: the model cannot raise it and no node may write it upward.
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
    limits = investigation.budget_limits
    now = time.time()
    base: dict[str, Any] = {
        "investigation_id": str(investigation.id),
        "investigation_revision": investigation.revision,
        "iteration": 0,
        "budget_deadline_at": now + limits.max_duration_seconds,
    }
    if investigation.status != InvestigationStatus.RUNNING:
        # Reconciliation (application-commands...md §27): a terminal investigation
        # (COMPLETED/FAILED/CANCELLED) is not re-run — the graph stops with a
        # no-op, so a retried/resumed run never duplicates rows.
        return {
            **base,
            "budget_remaining_steps": 0,
            "budget_remaining_tool_calls": 0,
            "budget_remaining_llm_calls": 0,
            "stop_reason": f"ALREADY_{investigation.status.value}",
        }
    # A resumed run has its counters checkpointed in state already; only a FRESH
    # run (no counters present) seeds them from the aggregate's limits.
    steps = int(state.get("budget_remaining_steps", limits.max_steps))
    tool_calls = int(state.get("budget_remaining_tool_calls", limits.max_tool_calls))
    llm_calls = int(state.get("budget_remaining_llm_calls", limits.max_llm_calls))
    # A resumed run also keeps its original deadline (derived at first load), so a
    # crash/restart can never extend the wall-clock budget.
    deadline = state.get("budget_deadline_at")
    if not isinstance(deadline, (int, float)):
        deadline = float(base["budget_deadline_at"])
    return {
        **base,
        "budget_remaining_steps": steps,
        "budget_remaining_tool_calls": tool_calls,
        "budget_remaining_llm_calls": llm_calls,
        "budget_deadline_at": deadline,
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
    """Change phase to PLANNING; produce a plan revision + an OPEN hypothesis.

    The plan consult is one model call against the runtime LLM-call budget.
    """
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
    budget = RuntimeBudget.from_state(state)
    alert: dict[str, Any] = dict(state.get("alert_context") or {})
    # Deterministic default plan: read the detection rule then run one bounded event
    # search (a safe, useful system default). Used when the model cannot be consulted
    # (no LLM-call slot / deadline passed) OR when a real model consult fails — an
    # outage never fails the investigation.
    plan_steps: list[PlanStep] = [
        PlanStep(step_id="read_rule", objective=_DEFAULT_PLAN_STEP_RULE),
        PlanStep(step_id="search_success", objective=_DEFAULT_PLAN_STEP_SEARCH),
    ]
    plan_goal = _DEFAULT_PLAN_GOAL
    if budget.can_call_llm():
        budget = budget.consume_llm_call()
        # A provider outage/refusal/malformed output degrades to the default plan.
        model_plan = await _model_consult(
            runtime.model.plan(
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
        )
        if model_plan is not None:
            plan_steps = model_plan.steps
            plan_goal = model_plan.goal
    steps = [
        PlanStepCandidate(step_key=s.step_id, objective=s.objective, ordinal=i)
        for i, s in enumerate(plan_steps)
    ]
    _, plan_revision = await handler.revise_plan(
        ReviseInvestigationPlan(
            tenant_id=runtime.tenant_id,
            investigation_id=inv_id,
            idempotency_key=_inv_key(inv_id, "plan:1"),
            revision=1,
            goal=plan_goal,
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
        **budget.to_updates(),
    }


_DEFAULT_PLAN_GOAL = (
    "Determine whether the SSH brute force escalated into a compromise"
)
_DEFAULT_PLAN_STEP_RULE = "Read the detection rule that fired on this alert"
_DEFAULT_PLAN_STEP_SEARCH = (
    "Search for a successful authentication after the failures"
)


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

    Each CONTINUE decision consumes one step and one tool call from the runtime
    budget; each consult of the model consumes one LLM call. When the model says
    FINALIZE (or step/tool-call budget is spent / the wall-clock deadline passed),
    route to the convergence ``assess`` node. Exhaustion/deadline yield a bounded
    finalize → COMPLETED + INCONCLUSIVE, never FAILED.
    """
    budget = RuntimeBudget.from_state(state)
    # The convergence path reserves the final two LLM-call slots (assess + verdict).
    # decide_next stops consulting once only that reserve remains, so the total model
    # consults can never exceed max_llm_calls — even against a model that always
    # answers CONTINUE. A deadline that has already passed also stops the consult.
    if not budget.can_consult_decide():
        # No LLM-call budget (or wall-clock deadline) left → bounded finalize.
        return {"next_action": CONVERGE, "assessment": "FINALIZE"}
    # Consume one LLM call for this model consult through the single authority.
    budget = budget.consume_llm_call()
    outcome = _previous_outcome_of(state)

    # Build the bounded working context from authoritative sources — a SHORT read
    # transaction that is CLOSED before the model consult (no DB transaction spans
    # the provider HTTP call). The model receives only bounded fields: real
    # rule_id/detected_at/entity from the alert snapshot + persisted Evidence
    # (evidence_id + operation + bounded summary) of THIS investigation — never raw
    # ToolResults/Events, tenant/authorization data, or secrets.
    evidence_ids: list[str] = []
    evidence_context: list[dict[str, object]] = []
    uow = runtime.new_unit_of_work()
    try:
        evidence_rows = await uow.evidence.list_by_investigation(
            tenant_id=runtime.tenant_id, investigation_id=_inv_id(state)
        )
    finally:
        await uow.close()
    for ev in evidence_rows:
        evidence_ids.append(str(ev.id))
        evidence_context.append(
            {
                "evidence_id": str(ev.id),
                "operation": ev.source.operation,
                "summary": _evidence_line(ev),
            }
        )
    alert_context = _decide_alert_context(dict(state.get("alert_context") or {}))

    # A provider outage/refusal degrades to a bounded finalize (converge) — never a
    # FAILED investigation just because the model was unavailable.
    next_step = await _model_consult(
        runtime.model.decide_next(
            DecideNextRequest(
                investigation_id=_inv_str(state),
                iteration=int(state.get("iteration") or 0),
                plan_goal="Investigate whether the alert indicates an account compromise",
                evidence_summary=evidence_ids,
                tool_names=runtime.registry.model_selectable_names,
                previous_tool_outcome=outcome,
                tool_specs=runtime.registry.model_tool_specs(),
                alert_context=alert_context,
                evidence=evidence_context,
            )
        )
    )
    if next_step is None:
        return {
            "next_action": CONVERGE,
            "assessment": "FINALIZE",
            **budget.to_updates(),
        }
    if (
        next_step.decision == "CONTINUE"
        and next_step.tool_name
        and budget.can_take_step()
        and budget.can_execute_tool()
    ):
        arguments = dict(next_step.arguments or {})
        call_fingerprint = compute_content_hash(
            {"tool": next_step.tool_name, "arguments": arguments}
        )
        # Bounded repeated-attempt guard (Fix #5): when the previous tool call failed
        # (transient/retryable) and the model proposes the EXACT SAME failing request
        # (identical fingerprint), cap the consecutive replays. ``same_call_retries``
        # is incremented by execute_and_ingest on each failure round, so the graph
        # stops re-planning after a bounded number of attempts — independently of the
        # step/tool budget — and converges to a bounded finalize (never FAILED).
        used_retries, _current = _repeat_budget_for(state, call_fingerprint)
        if (
            outcome is not None
            and outcome.retryable
            and _exceeded_repeat_budget(used_retries)
        ):
            return {
                "next_action": CONVERGE,
                "assessment": "FINALIZE",
                **budget.to_updates(),
                "previous_tool_outcome": None,
                "last_tool_error": (
                    "the model repeatedly proposed the same failing tool call; "
                    "investigation stopped"
                ),
            }
        budget = budget.consume_step().consume_tool_call()
        tool_call_key = f"iteration-{state.get('iteration') or 0}"
        return {
            "next_action": EXECUTE_TOOL,
            **budget.to_updates(),
            "pending_tool_request": {
                "tool": next_step.tool_name,
                "arguments": arguments,
                "step_key": tool_call_key,
                "call_fingerprint": call_fingerprint,
            },
            "previous_tool_outcome": None,
        }
    return {
        "next_action": CONVERGE,
        "assessment": "FINALIZE",
        **budget.to_updates(),
    }


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
        return {
            "next_action": CONVERGE,
            "last_tool_error": "no pending tool request",
            "previous_tool_outcome": None,
        }
    inv_id = _inv_id(state)
    tool_name = str(pending["tool"])
    arguments = dict(pending.get("arguments") or {})
    # The tool-call budget is consumed when the CONTINUE decision is accepted
    # (decide_next), so a scheduled request is always already budgeted. The wall-
    # clock deadline is the one runtime bound enforced here as a backstop: a
    # long-delayed replay must not execute a tool past the deadline.
    if _deadline_passed(state):
        return {
            "next_action": CONVERGE,
            "assessment": "FINALIZE",
            "last_tool_error": "runtime deadline reached before tool execution",
            "previous_tool_outcome": None,
        }
    # Deterministic across a checkpointed resume: same investigation + tool +
    # candidate arguments → same audit row + idempotency key.
    fingerprint = str(pending.get("call_fingerprint") or "")
    audit_key = f"tool:{tool_name}:{fingerprint}"
    # ONE stable invocation identity for this logical tool call: it is the audit
    # row id (ToolInvocationRow.id), the executor's tool_call_id, and
    # Evidence.source_tool_invocation_id — so provenance always resolves to the real
    # audit row even across a checkpointed replay.
    invocation_id = _invocation_uuid(inv_id, audit_key)
    alert: dict[str, Any] = dict(state.get("alert_context") or {})

    # 1) Audit STARTED in its own short transaction.
    uow = runtime.new_unit_of_work()
    try:
        await record_started(
            uow,
            tenant_id=runtime.tenant_id,
            investigation_id=inv_id,
            invocation_id=invocation_id,
            tool_name=tool_name,
            idempotency_key=audit_key,
            arguments=_bounded_arguments(tool_name, arguments),
        )
        await uow.commit()
    finally:
        await uow.close()

    # 2) Run the tool — no DB transaction open. The executor uses the SAME stable
    #    invocation id so the returned tool_call_id matches the audit row.
    try:
        execution = await runtime.executor.execute(
            candidate=ToolCandidate(tool_name=tool_name, arguments=arguments),
            tenant_id=runtime.tenant_id,
            source_alert_ref={
                "provider": "hisiem",
                "resource_type": "alert",
                "address_id": alert.get("alert_id") or "",
            },
            tool_call_id=str(invocation_id),
        )
    except Exception as exc:  # deterministic, typed; continue the investigation
        await _finish_audit(runtime, inv_id, audit_key, "FAILED", error_code="EXECUTION_ERROR")
        return {
            "next_action": CONVERGE,
            "last_tool_error": str(exc),
            "previous_tool_outcome": _outcome_for(tool_name, "REJECTED", "EXECUTION_ERROR"),
        }

    # 3) Audit FINISHED in its own short transaction.
    status, error_code, message, metadata = _audit_outcome(execution)
    await _finish_audit(
        runtime, inv_id, audit_key, status,
        error_code=error_code, safe_error_message=message, result_metadata=metadata,
    )

    # Recoverable-vs-deterministic outcome handling:
    #   - REJECTED (unknown tool / invalid schema / policy violation) is a
    #     deterministic candidate rejection, NOT a provider outage. It is never
    #     retried as a transient failure; the investigation converges to finalize
    #     with whatever grounded evidence exists.
    #   - UNAVAILABLE is a transient provider/data-source failure. With budget +
    #     alternative investigation paths still available, loop back to decide_next
    #     so the model can pick another read; only when the budget is exhausted (or
    #     the model has no further path) does the graph converge to finalize
    #     (COMPLETED + INCONCLUSIVE).
    #   - NO_DATA is a SUCCESSFUL bounded read (no events matched) — handled below.
    runtime_budget = RuntimeBudget.from_state(state)
    if execution.status == "REJECTED":
        # Deterministic rejection (unknown tool / bad schema / policy) — never
        # retried as transient; converge to finalize with whatever grounded evidence
        # exists. The bounded outcome is still recorded so the finalize decision can
        # reflect that the last read was rejected.
        return {
            "next_action": CONVERGE,
            "assessment": "FINALIZE",
            "last_tool_invocation_id": None,
            "last_tool_error": execution.result.error,
            "previous_tool_outcome": _outcome_for(
                tool_name,
                "REJECTED",
                execution.result.error_code or "POLICY_REJECTED",
                retryable=False,
            ),
        }
    if execution.status == "UNAVAILABLE":
        if not runtime_budget.can_take_step():
            # No budget left to try another path → finalize with what we have.
            return {
                "next_action": CONVERGE,
                "assessment": "FINALIZE",
                "last_tool_invocation_id": None,
                "last_tool_error": execution.result.error,
                "previous_tool_outcome": _outcome_for(
                    tool_name,
                    "UNAVAILABLE",
                    execution.result.error_code or "UPSTREAM_UNAVAILABLE",
                    retryable=True,
                ),
            }
        # Budget remains — loop back to decide_next so the model can choose an
        # alternative path (or FINALIZE). The failure is surfaced via last_tool_error
        # AND as a bounded previous_tool_outcome for the model's re-plan decision.
        # The bounded repeated-attempt counter is advanced here: scheduling the SAME
        # failing request again increments same_call_retries; a different request (a
        # fresh fingerprint) resets it. A model that keeps picking the SAME failing
        # call is stopped after _MAX_SAME_FAILING_CALL_RETRIES consecutive replans.
        used_retries, _ = _repeat_budget_for(state, fingerprint)
        return {
            "next_action": "decide_next",
            "assessment": None,
            "iteration": int(state.get("iteration") or 0) + 1,
            "last_tool_invocation_id": None,
            "last_tool_error": execution.result.error,
            "previous_tool_outcome": _outcome_for(
                tool_name,
                "UNAVAILABLE",
                execution.result.error_code or "UPSTREAM_UNAVAILABLE",
                retryable=True,
            ),
            "failing_call_fingerprint": fingerprint,
            "same_call_retries": used_retries + 1,
            "pending_tool_request": None,
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
    outcome_for_replan: dict[str, object] | None = None
    failing_fp: str | None = None
    same_call_retries = 0
    if execution.status == "NO_DATA":
        # A SUCCESSFUL bounded read that matched nothing: the model is told the last
        # query returned NO_DATA so it can refine the query/conditions — never passed
        # the raw (empty) result.
        outcome_for_replan = _outcome_for(
            tool_name, "NO_DATA", error_code=None, retryable=False
        )
    return {
        "next_action": "decide_next",
        "assessment": None,
        "last_tool_invocation_id": execution.tool_call_id,
        "last_tool_error": None,
        "previous_tool_outcome": outcome_for_replan,
        "failing_call_fingerprint": failing_fp,
        "same_call_retries": same_call_retries,
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
    """Convergence node — run once: structured AssessHypotheses + RecordFindings.

    Called when decide_next returns FINALIZE (budget spent or the model is done).
    The model returns a STRUCTURED per-hypothesis assessment that cites specific
    evidence ids with a semantic relation (SUPPORTS / CONTRADICTS / CONTEXT). The
    node then resolves every citation strictly against evidence that exists in this
    investigation and refuses to auto-support: rule metadata or unrelated context
    evidence can never, on its own, make the account-compromise hypothesis
    SUPPORTED. A hypothesis the model cannot ground is assessed UNRESOLVED.
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

    evidence_by_id = {e.id: e for e in evidence}
    evidence_ids = [str(e.id) for e in evidence]

    # The assess consult consumes one LLM-call slot through the single budget
    # authority. decide_next reserves this slot, but a replay arriving after the
    # wall-clock deadline (or with no slot left) must not make a further model call:
    # hypotheses are then left UNRESOLVED and findings are not produced, so the run
    # still ends COMPLETED + INCONCLUSIVE via the low-budget fallback.
    budget = RuntimeBudget.from_state(state)
    summary: Any = None
    if budget.can_call_llm():
        budget = budget.consume_llm_call()
        # Give the model the actual hypothesis ids and per-id evidence so it can
        # cite them precisely (candidate only — resolution happens below). A
        # provider outage/refusal degrades: hypotheses stay UNRESOLVED and no
        # findings are produced (deterministic, never FAILED).
        summary = await _model_consult(
            runtime.model.assess(
                AssessRequest(
                    investigation_id=_inv_str(state),
                    hypotheses=[
                        {"id": str(h.id), "statement": h.statement} for h in hypotheses
                    ],
                    evidence=[
                        {"id": str(e.id), "summary": _evidence_line(e),
                         "operation": e.source.operation}
                        for e in evidence
                    ],
                )
            )
        )

    # Build structured per-hypothesis assessments, resolving references strictly to
    # THIS investigation's evidence. Anything unresolved is UNRESOLVED (never an
    # automatic SUPPORTED).
    assessments: list[HypothesisAssessmentCandidate] = []
    if hypotheses:
        for h in hypotheses:
            candidate = _candidate_for(h.id, summary.assessments if summary else [])
            if candidate is None:
                # The model did not assess this hypothesis → deterministic UNRESOLVED.
                assessments.append(
                    HypothesisAssessmentCandidate(
                        hypothesis_id=h.id,
                        status="UNRESOLVED",
                        reason_summary="No evidence-based model assessment was produced",
                    )
                )
                continue
            status = candidate.status
            relations: list[AssessmentEvidenceRelation] = []
            resolvable = [
                r for r in candidate.evidence_relations
                if _is_known_evidence(evidence_by_id, r.evidence_id)
            ]
            if status in ("SUPPORTED", "CONTRADICTED"):
                # Directional grounding: SUPPORTED needs at least one SUPPORTS
                # relation to resolvable evidence in THIS investigation; CONTRADICTED
                # needs at least one CONTRADICTS. CONTEXT (rule metadata / unrelated
                # events) is never sufficient, and a SUPPORTS relation cannot support
                # a CONTRADICTED verdict (nor vice-versa). Citations that do not
                # resolve to this investigation's evidence are dropped entirely.
                needed = "SUPPORTS" if status == "SUPPORTED" else "CONTRADICTS"
                matching = [
                    r for r in resolvable if str(r.relation) == needed
                ]
                if not matching:
                    # No resolvable relation in the required direction → downgrade
                    # to UNRESOLVED (never SUPPORTED/CONTRADICTED on other evidence).
                    status = "UNRESOLVED"
                    relations = []
                else:
                    relations = [
                        AssessmentEvidenceRelation(
                            evidence_id=UUID(r.evidence_id), relation=EvidenceRelation(r.relation)
                        )
                        for r in resolvable
                    ]
            else:
                relations = [
                    AssessmentEvidenceRelation(
                        evidence_id=UUID(r.evidence_id), relation=EvidenceRelation(r.relation)
                    )
                    for r in resolvable
                ]
            assessments.append(
                HypothesisAssessmentCandidate(
                    hypothesis_id=h.id,
                    status=status,
                    reason_summary=candidate.reason_summary or "evidence-based assessment",
                    evidence_relations=relations,
                )
            )
        if assessments:
            await handler.assess_hypotheses(
                AssessHypotheses(
                    tenant_id=runtime.tenant_id,
                    investigation_id=inv_id,
                    idempotency_key=_inv_key(inv_id, "assess:convergence"),
                    assessments=assessments,
                )
            )

    # Findings come ONLY from model Finding Candidates whose evidence citations
    # resolve to real evidence in THIS investigation. There is deliberately NO
    # generic fallback that invents a "supports compromise" Finding from mere
    # evidence/rule metadata — CONTEXT evidence must never become a supporting
    # business fact. If no validated model finding survives resolution, zero
    # findings are persisted.
    grounded: list[ModelFindingCandidate] = []
    if summary is not None:
        for f in summary.findings:
            if not f.evidence_citations:
                continue  # a finding must cite at least one evidence id
            resolved = [
                str(eid) for eid in f.evidence_citations
                if _is_known_evidence_id(evidence_by_id, eid)
            ]
            if resolved:
                grounded.append(
                    ModelFindingCandidate(
                        statement=f.statement, evidence_citations=resolved
                    )
                )
    if grounded:
        await handler.record_findings(
            RecordFindings(
                tenant_id=runtime.tenant_id,
                investigation_id=inv_id,
                idempotency_key=_inv_key(inv_id, "findings:convergence"),
                findings=[
                    FindingCandidate(
                        statement=f.statement,
                        evidence_citations=[
                            UUID(str(eid)) for eid in f.evidence_citations
                        ],
                    )
                    for f in grounded
                ],
            )
        )
    return {
        "iteration": iteration,
        "assessment": "FINALIZE",
        **budget.to_updates(),
        "new_evidence_ids": evidence_ids,
    }


def _candidate_for(
    hypothesis_id: UUID,
    candidates: list[ModelAssessmentCandidate],
) -> ModelAssessmentCandidate | None:
    wanted = str(hypothesis_id)
    for c in candidates:
        if c.hypothesis_id == wanted:
            return c
    return None


def _is_known_evidence(
    evidence_by_id: dict[UUID, Evidence], evidence_id: str
) -> bool:
    return _is_known_evidence_id(evidence_by_id, evidence_id)


def _is_known_evidence_id(
    evidence_by_id: dict[UUID, Evidence], evidence_id: str
) -> bool:
    try:
        return UUID(evidence_id) in evidence_by_id
    except (ValueError, AttributeError):
        return False


async def finalize_result(
    runtime: GraphRuntime, state: InvestigationGraphState
) -> dict[str, Any]:
    """Finalize one immutable InvestigationResult from grounded facts + verdict.

    The verdict consult never begins once the wall-clock deadline has passed: the
    graph deterministically finalizes the available grounded facts as INCONCLUSIVE
    (COMPLETED, never FAILED), even when a replay arrives late.
    """
    inv_id = _inv_id(state)
    handler = runtime.workflow_handler
    budget = RuntimeBudget.from_state(state)
    verdict_candidate = None
    consult_failed = False

    # Read the grounded Findings + bounded evidence summaries BEFORE the verdict
    # consult so the model receives the real persisted findings (never an empty list,
    # never raw ToolResults/Events/full evidence payloads).
    uow = runtime.new_unit_of_work()
    try:
        findings = await uow.findings.list_by_investigation(
            tenant_id=runtime.tenant_id, investigation_id=inv_id
        )
        evidence = await uow.evidence.list_by_investigation(
            tenant_id=runtime.tenant_id, investigation_id=inv_id
        )
    finally:
        await uow.close()
    finding_statements = [f.statement for f in findings]
    evidence_summaries = [
        f"{e.source.operation}: {_evidence_line(e)}" for e in evidence
    ]

    # The verdict consult never begins without a remaining LLM slot or once the
    # wall-clock deadline has passed: the graph deterministically finalizes the
    # available grounded facts as INCONCLUSIVE (COMPLETED, never FAILED) even when a
    # replay arrives late.
    if budget.can_call_llm():
        budget = budget.consume_llm_call()
        # A provider outage/refusal/malformed output degrades to INCONCLUSIVE with
        # low confidence + explicit uncertainty (below) — never FAILED.
        verdict_candidate = await _model_consult(
            runtime.model.verdict(
                AssessRequest(
                    investigation_id=_inv_str(state),
                    evidence_summary=evidence_summaries,
                    finding_candidates=finding_statements,
                )
            )
        )
        consult_failed = verdict_candidate is None
    await handler.change_phase(
        ChangeInvestigationPhase(
            tenant_id=runtime.tenant_id,
            investigation_id=inv_id,
            idempotency_key=_inv_key(inv_id, "phase:finalizing"),
            phase=InvestigationPhase.FINALIZING,
        )
    )

    if verdict_candidate is not None:
        disposition = VerdictDisposition(verdict_candidate.disposition)
        summary = verdict_candidate.summary
        confidence = verdict_candidate.confidence
        uncertainties = []
        if verdict_candidate.uncertainty:
            uncertainties.append(
                UncertaintyCandidate(description=verdict_candidate.uncertainty)
            )
        result_key = _inv_key(inv_id, f"result:{verdict_candidate.disposition}")
        if disposition in (
            VerdictDisposition.MALICIOUS,
            VerdictDisposition.BENIGN,
        ) and not findings:
            # Grounding invariant: MALICIOUS/BENIGN requires at least one grounded
            # Finding. The model proposed a firm verdict with NO validated finding —
            # deterministically bounded to INCONCLUSIVE (never a guessed disposition,
            # never FAILED).
            disposition = VerdictDisposition.INCONCLUSIVE
            summary = "No grounded finding supported the model's proposed verdict"
            confidence = 0.0
            uncertainties = [
                UncertaintyCandidate(
                    description="The model proposed a MALICIOUS/BENIGN verdict but no "
                    "evidence-grounded finding was recorded"
                )
            ]
            result_key = _inv_key(inv_id, "result:INCONCLUSIVE")
    else:
        # Bounded finalize: the verdict model call could not produce a candidate —
        # the consult never started (deadline/no slot) OR the model call failed
        # (provider outage/refusal/malformed output). The graph deterministically
        # finalizes the available grounded facts INCONCLUSIVE with a low confidence
        # and explicit uncertainty (never FAILED).
        if budget.deadline_exceeded:
            summary = "Investigation stopped at the runtime duration deadline"
            uncertainty = (
                "The runtime duration deadline was reached before the model could "
                "render a verdict"
            )
        elif consult_failed:
            summary = "Investigation stopped because the model verdict call failed"
            uncertainty = (
                "The model provider did not return a usable verdict (unavailable, "
                "refused, or invalid output); the investigation converged INCONCLUSIVE"
            )
        else:
            summary = "Investigation stopped before a model verdict was possible"
            uncertainty = "model-call budget exhausted before the verdict consult"
        disposition = VerdictDisposition.INCONCLUSIVE
        confidence = 0.0
        uncertainties = [UncertaintyCandidate(description=uncertainty)]
        result_key = _inv_key(inv_id, "result:INCONCLUSIVE")
    _, result = await handler.finalize_result(
        FinalizeInvestigationResult(
            tenant_id=runtime.tenant_id,
            investigation_id=inv_id,
            idempotency_key=result_key,
            verdict=ResultVerdictCandidate(
                disposition=disposition,
                summary=summary,
                confidence=confidence,
            ),
            finding_ids=[f.id for f in findings],
            uncertainties=uncertainties,
        )
    )
    return {"result_id": str(result.id), **budget.to_updates()}


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


def _decide_alert_context(alert: dict[str, Any]) -> DecideAlertContext | None:
    """Map the bounded alert snapshot to the decide request's alert context.

    Only the investigation-decision fields a real tool call needs are exposed; the
    model must use the REAL values (never guess rule_id / entity), and tenant /
    authorization / secrets are never included.
    """
    if not alert:
        return None
    return DecideAlertContext(
        rule_id=_opt_str(alert.get("rule_id")),
        detected_at=_opt_str(alert.get("detected_at")),
        source_ip=_opt_str(alert.get("source_ip")),
        user_name=_opt_str(alert.get("user_name")),
        host_name=_opt_str(alert.get("host_name")),
        event_category=_opt_str(alert.get("event_category")),
        event_action=_opt_str(alert.get("event_action")),
        severity=_opt_str(alert.get("severity")),
    )


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

