"""Workspace read service — composes the Analyst Workspace projection.

This service performs a PURE READ: it loads the Investigation aggregate and its
child rows through tenant-scoped repository/query ports inside ONE UnitOfWork,
then projects them into an immutable :class:`InvestigationWorkspaceReadModel`.

It MUST NOT (docs/investigation-workspace.md §7):
- mutate any domain state,
- run the Agent / LangGraph / a model / a tool,
- touch ORM or SQL directly (it depends on the ``UnitOfWork`` port only).

The composed timeline is derived deterministically from persisted facts; no entry
is fabricated and no debug/telemetry fact is exposed.
"""

from __future__ import annotations

from uuid import UUID

from ...domain.investigation.aggregate import Investigation
from ...domain.investigation.entities import (
    Evidence,
    Finding,
    Hypothesis,
    HypothesisAssessment,
    InvestigationResult,
    PlanRevision,
)
from ...domain.investigation.value_objects import ExternalResourceRef
from ...domain.response.value_objects import (
    ApprovalDecision,
    ApprovalRequest,
    ResponseExecutionRef,
    ResponseSubmission,
)
from ..errors import NotFoundError
from ..ports.durable import ToolInvocationRecord
from ..ports.unit_of_work import UnitOfWork
from ..queries.workspace import (
    AlertInvestigationLookup,
    InvestigationWorkspaceReadModel,
    WorkspaceApproval,
    WorkspaceApprovalDecision,
    WorkspaceAssessment,
    WorkspaceAttackMapping,
    WorkspaceEntityRef,
    WorkspaceEvidence,
    WorkspaceEvidenceRelation,
    WorkspaceEvidenceSource,
    WorkspaceExecution,
    WorkspaceFinding,
    WorkspaceHypothesis,
    WorkspaceInvestigation,
    WorkspacePlanRevision,
    WorkspacePlanStep,
    WorkspaceResponseProjection,
    WorkspaceResponseProposal,
    WorkspaceResponseRecommendation,
    WorkspaceResponseSubmission,
    WorkspaceResponseTarget,
    WorkspaceResult,
    WorkspaceSourceAlertRef,
    WorkspaceTimelineEntry,
    WorkspaceToolActivity,
    WorkspaceUncertainty,
    WorkspaceVerdict,
)

# Product activity-timeline kinds (docs §10). Never a LangGraph node name.
TL_INVESTIGATION_CREATED = "INVESTIGATION_CREATED"
TL_INVESTIGATION_STARTED = "INVESTIGATION_STARTED"
TL_PLAN_CREATED = "PLAN_CREATED"
TL_PLAN_REVISED = "PLAN_REVISED"
TL_TOOL_STARTED = "TOOL_STARTED"
TL_TOOL_SUCCEEDED = "TOOL_SUCCEEDED"
TL_TOOL_FAILED = "TOOL_FAILED"
TL_EVIDENCE_RECORDED = "EVIDENCE_RECORDED"
TL_HYPOTHESIS_ASSESSED = "HYPOTHESIS_ASSESSED"
TL_FINDING_RECORDED = "FINDING_RECORDED"
TL_RESULT_FINALIZED = "RESULT_FINALIZED"
TL_INVESTIGATION_COMPLETED = "INVESTIGATION_COMPLETED"
TL_INVESTIGATION_CANCELLED = "INVESTIGATION_CANCELLED"
TL_INVESTIGATION_FAILED = "INVESTIGATION_FAILED"
# Response workflow timeline kinds (spec §38). Persisted-fact backed only.
TL_RESPONSE_PROPOSAL_CREATED = "RESPONSE_PROPOSAL_CREATED"
TL_RESPONSE_POLICY_EVALUATED = "RESPONSE_POLICY_EVALUATED"
TL_APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
TL_RESPONSE_APPROVED = "RESPONSE_APPROVED"
TL_RESPONSE_REJECTED = "RESPONSE_REJECTED"
TL_RESPONSE_EXECUTION_QUEUED = "RESPONSE_EXECUTION_QUEUED"
TL_RESPONSE_EXECUTION_STARTED = "RESPONSE_EXECUTION_STARTED"
TL_RESPONSE_EXECUTION_SUCCEEDED = "RESPONSE_EXECUTION_SUCCEEDED"
TL_RESPONSE_EXECUTION_FAILED = "RESPONSE_EXECUTION_FAILED"
#: Local submission lifecycle facts (never provider execution facts).
TL_RESPONSE_SUBMISSION_QUEUED = "RESPONSE_SUBMISSION_QUEUED"
TL_RESPONSE_SUBMISSION_RETRYING = "RESPONSE_SUBMISSION_RETRYING"
TL_RESPONSE_SUBMISSION_FAILED = "RESPONSE_SUBMISSION_FAILED"

_TOOL_SUCCEEDED = "SUCCEEDED"
_EXEC_SUCCEEDED = "SUCCEEDED"
_EXEC_FAILED = "FAILED"
_EXEC_QUEUED = "QUEUED"
#: The human approved and the durable SUBMIT command is queued, but NO provider
#: execution exists yet. Deliberately not a provider execution status: nothing is
#: claimed about a provider execution because none exists.
_EXEC_AWAITING_SUBMISSION = "AWAITING_SUBMISSION"
_EXEC_SUBMISSION_RETRYING = "SUBMISSION_RETRYING"
_EXEC_SUBMISSION_FAILED = "SUBMISSION_FAILED"
_SUBMISSION_RETRYING = "RETRYING"
_SUBMISSION_FAILED_DEFINITIVE = "FAILED_DEFINITIVE"


def _header(investigation: Investigation) -> WorkspaceInvestigation:
    return WorkspaceInvestigation(
        investigation_id=investigation.id,
        tenant_id=investigation.tenant_id,
        status=investigation.status.value,
        phase=investigation.phase.value if investigation.phase else None,
        initiated_by=investigation.initiated_by.subject_id,
        created_at=investigation.created_at,
        started_at=investigation.started_at,
        finished_at=investigation.finished_at,
        cancelled_at=investigation.cancelled_at,
        termination_reason=(
            investigation.termination_reason.value
            if investigation.termination_reason
            else None
        ),
        current_plan_revision=investigation.current_plan_revision,
    )


def _source_ref(investigation: Investigation) -> WorkspaceSourceAlertRef:
    ref = investigation.source_alert_ref
    return WorkspaceSourceAlertRef(
        provider=ref.provider,
        resource_type=ref.resource_type,
        address_id=ref.address_id,
        business_id=ref.business_id,
    )


def _plan_revision(plan: PlanRevision) -> WorkspacePlanRevision:
    steps = tuple(
        WorkspacePlanStep(
            step_id=step.step_id,
            ordinal=step.ordinal,
            objective=step.objective,
            status=step.status.value,
        )
        for step in sorted(plan.steps, key=lambda s: (s.ordinal, s.step_id))
    )
    return WorkspacePlanRevision(
        id=plan.id,
        revision=plan.revision,
        generated_by=plan.generated_by,
        created_at=plan.created_at,
        steps=steps,
    )


def _evidence(entity: Evidence) -> WorkspaceEvidence:
    return WorkspaceEvidence(
        evidence_id=entity.id,
        collected_at=entity.collected_at,
        observed_at=entity.observed_at,
        summary=entity.summary,
        source=WorkspaceEvidenceSource(
            type=entity.source.type.value,
            provider=entity.source.provider,
            operation=entity.source.operation,
        ),
        source_tool_invocation_id=entity.source_tool_call_id,
        observation=dict(entity.observation),
        entity_refs=tuple(
            WorkspaceEntityRef(kind=ref.kind, value=ref.value)
            for ref in entity.entity_refs
        ),
        raw_reference=dict(entity.raw_reference) if entity.raw_reference else None,
        content_hash=entity.content_hash,
        dedup_key=entity.dedup_key,
    )


def _latest_assessments(
    assessments: list[HypothesisAssessment],
) -> dict[UUID, HypothesisAssessment]:
    """Pick each hypothesis's newest assessment by (revision, created_at)."""
    best: dict[UUID, HypothesisAssessment] = {}
    for assessment in assessments:
        current = best.get(assessment.hypothesis_id)
        if current is None or (assessment.revision, assessment.created_at) > (
            current.revision,
            current.created_at,
        ):
            best[assessment.hypothesis_id] = assessment
    return best


def _assessment(assessment: HypothesisAssessment) -> WorkspaceAssessment:
    return WorkspaceAssessment(
        revision=assessment.revision,
        status=assessment.status.value,
        reason_summary=assessment.reason_summary,
        created_at=assessment.created_at,
        evidence_relations=tuple(
            WorkspaceEvidenceRelation(
                evidence_id=rel.evidence_id, relation=rel.relation.value
            )
            for rel in assessment.evidence_relations
        ),
    )


def _result(result: InvestigationResult) -> WorkspaceResult:
    return WorkspaceResult(
        result_id=result.id,
        verdict=WorkspaceVerdict(
            disposition=result.verdict.disposition.value,
            summary=result.verdict.summary,
            confidence=result.verdict.confidence,
        ),
        created_at=result.created_at,
        finding_ids=tuple(result.finding_ids),
        uncertainties=tuple(
            WorkspaceUncertainty(
                description=u.description, missing_information=u.missing_information
            )
            for u in result.uncertainties
        ),
        attack_mappings=tuple(
            WorkspaceAttackMapping(
                framework=m.framework,
                technique_id=m.technique_id,
                name=m.name,
                version=m.version,
                source=m.source,
            )
            for m in result.attack_mappings
        ),
        response_recommendations=tuple(
            WorkspaceResponseRecommendation(
                description=r.description, reason=r.reason
            )
            for r in result.response_recommendations
        ),
    )


def _tool_activity(record: ToolInvocationRecord) -> WorkspaceToolActivity:
    duration_ms: int | None = None
    if record.finished_at is not None:
        delta = record.finished_at - record.started_at
        duration_ms = int(delta.total_seconds() * 1000)
    return WorkspaceToolActivity(
        invocation_id=record.id,
        tool_name=record.tool_name,
        status=record.status,
        started_at=record.started_at,
        finished_at=record.finished_at,
        error_code=record.error_code,
        safe_error_message=record.safe_error_message,
        duration_ms=duration_ms,
    )


def _build_timeline(
    *,
    investigation: Investigation,
    plan_revisions: list[PlanRevision],
    evidence: list[Evidence],
    assessments: list[HypothesisAssessment],
    findings: list[Finding],
    result: InvestigationResult | None,
    tool_records: list[ToolInvocationRecord],
) -> list[WorkspaceTimelineEntry]:
    entries: list[WorkspaceTimelineEntry] = []

    entries.append(
        WorkspaceTimelineEntry(
            kind=TL_INVESTIGATION_CREATED,
            occurred_at=investigation.created_at,
            title="Investigation created",
            ref_type="investigation",
            ref_id=str(investigation.id),
        )
    )
    if investigation.started_at is not None:
        entries.append(
            WorkspaceTimelineEntry(
                kind=TL_INVESTIGATION_STARTED,
                occurred_at=investigation.started_at,
                title="Investigation started",
                ref_type="investigation",
                ref_id=str(investigation.id),
            )
        )

    for plan in plan_revisions:
        entries.append(
            WorkspaceTimelineEntry(
                kind=TL_PLAN_CREATED if plan.revision <= 1 else TL_PLAN_REVISED,
                occurred_at=plan.created_at,
                title="Plan created" if plan.revision <= 1 else "Plan revised",
                ref_type="plan_revision",
                ref_id=str(plan.id),
                safe_metadata={"revision": plan.revision},
            )
        )

    for record in tool_records:
        entries.append(
            WorkspaceTimelineEntry(
                kind=TL_TOOL_STARTED,
                occurred_at=record.started_at,
                title=f"Tool started: {record.tool_name}",
                status=record.status,
                ref_type="tool_invocation",
                ref_id=str(record.id),
                safe_metadata={"tool_name": record.tool_name},
            )
        )
        if record.finished_at is not None:
            succeeded = record.status == _TOOL_SUCCEEDED
            entries.append(
                WorkspaceTimelineEntry(
                    kind=TL_TOOL_SUCCEEDED if succeeded else TL_TOOL_FAILED,
                    occurred_at=record.finished_at,
                    title=(
                        f"Tool succeeded: {record.tool_name}"
                        if succeeded
                        else f"Tool failed: {record.tool_name}"
                    ),
                    status=record.status,
                    ref_type="tool_invocation",
                    ref_id=str(record.id),
                    safe_metadata={"tool_name": record.tool_name},
                )
            )

    for item in evidence:
        entries.append(
            WorkspaceTimelineEntry(
                kind=TL_EVIDENCE_RECORDED,
                occurred_at=item.collected_at,
                title="Evidence recorded",
                ref_type="evidence",
                ref_id=str(item.id),
                safe_metadata={
                    "source_provider": item.source.provider,
                    "source_operation": item.source.operation,
                },
            )
        )

    for assessment in assessments:
        entries.append(
            WorkspaceTimelineEntry(
                kind=TL_HYPOTHESIS_ASSESSED,
                occurred_at=assessment.created_at,
                title="Hypothesis assessed",
                status=assessment.status.value,
                ref_type="hypothesis",
                ref_id=str(assessment.hypothesis_id),
                safe_metadata={"revision": assessment.revision},
            )
        )

    for finding in findings:
        entries.append(
            WorkspaceTimelineEntry(
                kind=TL_FINDING_RECORDED,
                occurred_at=finding.created_at,
                title="Finding recorded",
                ref_type="finding",
                ref_id=str(finding.id),
            )
        )

    if result is not None:
        entries.append(
            WorkspaceTimelineEntry(
                kind=TL_RESULT_FINALIZED,
                occurred_at=result.created_at,
                title="Result finalized",
                status=result.verdict.disposition.value,
                ref_type="result",
                ref_id=str(result.id),
            )
        )

    if investigation.finished_at is not None:
        status = investigation.status.value
        kind = {
            "COMPLETED": TL_INVESTIGATION_COMPLETED,
            "CANCELLED": TL_INVESTIGATION_CANCELLED,
            "FAILED": TL_INVESTIGATION_FAILED,
        }.get(status)
        if kind is not None:
            title = {
                "COMPLETED": "Investigation completed",
                "CANCELLED": "Investigation cancelled",
                "FAILED": "Investigation failed",
            }[status]
            entries.append(
                WorkspaceTimelineEntry(
                    kind=kind,
                    occurred_at=investigation.finished_at,
                    title=title,
                    status=status,
                    ref_type="investigation",
                    ref_id=str(investigation.id),
                )
            )

    entries.sort(key=lambda e: (e.occurred_at, e.kind, e.ref_id or "", e.title))
    return entries


def _response_projection(
    *,
    result: InvestigationResult | None,
    proposal: object | None,
    approval: ApprovalRequest | None,
    decision: ApprovalDecision | None,
    submission: ResponseSubmission | None,
    execution: ResponseExecutionRef | None,
) -> WorkspaceResponseProjection:
    """Build the Response projection: informational recommendations + typed proposals.

    ``recommendations`` mirror the result's explanatory output (never executable);
    ``proposals`` are the validated, approval-bound contracts (spec §4/§30).
    """
    recommendations: tuple[WorkspaceResponseRecommendation, ...] = ()
    if result is not None:
        recommendations = tuple(
            WorkspaceResponseRecommendation(description=r.description, reason=r.reason)
            for r in result.response_recommendations
        )
    proposals: tuple[WorkspaceResponseProposal, ...] = ()
    if proposal is not None:
        proposals = (
            _response_proposal(proposal, approval, decision, submission, execution),
        )
    return WorkspaceResponseProjection(
        recommendations=recommendations, proposals=proposals
    )


def _response_proposal(
    proposal: object,
    approval: ApprovalRequest | None,
    decision: ApprovalDecision | None,
    submission: ResponseSubmission | None,
    execution: ResponseExecutionRef | None,
) -> WorkspaceResponseProposal:
    from ...domain.response.aggregate import ResponseProposal

    assert isinstance(proposal, ResponseProposal)
    return WorkspaceResponseProposal(
        proposal_id=proposal.id,
        revision=proposal.content_revision,
        content_hash=proposal.content_hash,
        status=proposal.status.value,
        action_key=proposal.action_key,
        parameters=dict(proposal.parameters),
        reason=proposal.reason,
        target_refs=tuple(
            WorkspaceResponseTarget(
                provider=t.provider,
                resource_type=t.resource_type,
                address_id=t.address_id,
                business_id=t.business_id,
            )
            for t in proposal.target_refs
        ),
        evidence_ids=tuple(proposal.evidence_ids),
        policy_decision=(
            proposal.policy_decision.value if proposal.policy_decision else None
        ),
        policy_reason=proposal.policy_reason,
        created_at=proposal.created_at,
        created_by_subject=proposal.created_by_subject,
        created_by_display_name=proposal.created_by_display_name,
        approval=_workspace_approval(approval, decision),
        submission=_workspace_submission(submission),
        execution=_workspace_execution(execution),
    )


def _workspace_approval(
    approval: ApprovalRequest | None, decision: ApprovalDecision | None
) -> WorkspaceApproval | None:
    if approval is None:
        return None
    return WorkspaceApproval(
        request_id=approval.id,
        requested_at=approval.requested_at,
        requested_reason=approval.requested_reason,
        expected_revision=approval.proposal_content_revision,
        expected_content_hash=approval.proposal_content_hash,
        decision=(
            WorkspaceApprovalDecision(
                decision=decision.decision,
                actor_subject_id=decision.actor_subject_id,
                actor_display_name=decision.actor_display_name,
                reason=decision.reason,
                decided_at=decision.decided_at,
            )
            if decision is not None
            else None
        ),
    )


def _workspace_submission(
    submission: ResponseSubmission | None,
) -> WorkspaceResponseSubmission | None:
    """Local submission truth; carries NO provider execution identity."""
    if submission is None:
        return None
    return WorkspaceResponseSubmission(
        status=submission.status,
        submission_key=submission.submission_key,
        attempt_count=submission.attempt_count,
        last_error_code=submission.last_error_code,
        safe_error_message=submission.safe_error_message,
        created_at=submission.created_at,
        updated_at=submission.updated_at,
        submitted_at=submission.submitted_at,
        failed_at=submission.failed_at,
    )


def _workspace_execution(execution: ResponseExecutionRef | None) -> WorkspaceExecution | None:
    if execution is None:
        return None
    return WorkspaceExecution(
        provider=execution.provider,
        status=execution.status,
        submitted_at=execution.submitted_at,
        last_observed_at=execution.last_observed_at,
        # The projection row only exists AFTER HISIEM returned a non-empty real
        # execution id, so this is a real provider identity — never a proposal id
        # and never a placeholder (spec §2/§6).
        external_execution_id=execution.execution_id or None,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        safe_result=dict(execution.safe_result or {}),
        safe_error_code=execution.safe_error_code,
        safe_error_message=execution.safe_error_message,
    )


def _response_timeline_entries(
    *,
    proposal: object | None,
    approval: ApprovalRequest | None,
    decision: ApprovalDecision | None,
    submission: ResponseSubmission | None,
    execution: ResponseExecutionRef | None,
) -> list[WorkspaceTimelineEntry]:
    """Deterministic response timeline entries derived from PERSISTED FACTS only.

    Nothing here is inferred from an outbox row, a browser cache, or a delivery
    attempt that may not have committed (spec §2/§3):

    * the LOCAL submission lifecycle (``response_submission``) is what says whether
      the approved submission is queued, retrying, or was definitively refused —
      the timeline never invents a provider execution identity for any of them;
    * a definitive provider refusal is a fact about the SUBMISSION, so it is
      reported as such and the timeline stops there;
    * once the provider projection row exists, the provider execution identity is
      REAL and the provider status (QUEUED/RUNNING/SUCCEEDED/FAILED) is displayed
      against it;
    * the proposal id is never passed off as an execution identity.
    """
    from ...domain.response.aggregate import ResponseProposal

    if not isinstance(proposal, ResponseProposal):
        return []
    entries: list[WorkspaceTimelineEntry] = [
        WorkspaceTimelineEntry(
            kind=TL_RESPONSE_PROPOSAL_CREATED,
            occurred_at=proposal.created_at,
            title="Response proposal created",
            ref_type="response_proposal",
            ref_id=str(proposal.id),
            safe_metadata={
                "action_key": proposal.action_key,
                "revision": proposal.content_revision,
                "proposed_by": proposal.created_by_subject,
                "proposed_by_display_name": proposal.created_by_display_name,
            },
        )
    ]
    if proposal.policy_decision is not None:
        entries.append(
            WorkspaceTimelineEntry(
                kind=TL_RESPONSE_POLICY_EVALUATED,
                occurred_at=proposal.created_at,
                title="Response policy evaluated",
                status=proposal.policy_decision.value,
                ref_type="response_proposal",
                ref_id=str(proposal.id),
            )
        )
    if approval is not None:
        entries.append(
            WorkspaceTimelineEntry(
                kind=TL_APPROVAL_REQUESTED,
                occurred_at=approval.requested_at,
                title="Approval requested",
                ref_type="response_approval",
                ref_id=str(approval.id),
            )
        )
    if decision is not None:
        approved = decision.decision == "APPROVE"
        entries.append(
            WorkspaceTimelineEntry(
                kind=TL_RESPONSE_APPROVED if approved else TL_RESPONSE_REJECTED,
                occurred_at=decision.decided_at,
                title="Response approved" if approved else "Response rejected",
                status=decision.decision,
                ref_type="approval_decision",
                ref_id=str(decision.id),
                safe_metadata={"actor": decision.actor_subject_id},
            )
        )

    if execution is None:
        # No provider execution exists, and the ONLY authority on whether the local
        # submission is still pending, retrying, or was refused is the persisted
        # submission projection.
        if submission is not None and submission.status == _SUBMISSION_FAILED_DEFINITIVE:
            entries.append(
                WorkspaceTimelineEntry(
                    kind=TL_RESPONSE_SUBMISSION_FAILED,
                    occurred_at=submission.failed_at or submission.updated_at,
                    title="Response submission rejected by provider",
                    status=_EXEC_SUBMISSION_FAILED,
                    ref_type="response_proposal",
                    ref_id=str(proposal.id),
                    safe_metadata={
                        "error_code": submission.last_error_code,
                        "attempts": submission.attempt_count,
                    },
                )
            )
            return entries
        if decision is not None and decision.decision == "APPROVE":
            retrying = (
                submission is not None
                and submission.status == _SUBMISSION_RETRYING
            )
            occurred = (
                submission.updated_at
                if retrying and submission is not None
                else decision.decided_at
            )
            entries.append(
                WorkspaceTimelineEntry(
                    kind=(
                        TL_RESPONSE_SUBMISSION_RETRYING
                        if retrying
                        else TL_RESPONSE_SUBMISSION_QUEUED
                    ),
                    occurred_at=occurred,
                    title=(
                        "Response submission retrying"
                        if retrying
                        else "Response approved; submission queued"
                    ),
                    status=(
                        _EXEC_SUBMISSION_RETRYING
                        if retrying
                        else _EXEC_AWAITING_SUBMISSION
                    ),
                    ref_type="response_proposal",
                    ref_id=str(proposal.id),
                    safe_metadata=(
                        {
                            "error_code": submission.last_error_code,
                            "attempts": submission.attempt_count,
                        }
                        if retrying and submission is not None
                        else {}
                    ),
                )
            )
        return entries

    # A REAL provider execution identity exists from here on.
    ref = execution.execution_id
    entries.append(
        WorkspaceTimelineEntry(
            kind=TL_RESPONSE_EXECUTION_QUEUED,
            occurred_at=execution.submitted_at,
            title="Response execution submitted",
            status=_EXEC_QUEUED,
            ref_type="response_execution",
            ref_id=ref,
        )
    )
    if execution.started_at is not None and execution.status != _EXEC_QUEUED:
        entries.append(
            WorkspaceTimelineEntry(
                kind=TL_RESPONSE_EXECUTION_STARTED,
                occurred_at=execution.started_at,
                title="Response execution started",
                status=execution.status,
                ref_type="response_execution",
                ref_id=ref,
            )
        )
    if execution.finished_at is not None and execution.status in (
        _EXEC_SUCCEEDED,
        _EXEC_FAILED,
    ):
        succeeded = execution.status == _EXEC_SUCCEEDED
        entries.append(
            WorkspaceTimelineEntry(
                kind=(
                    TL_RESPONSE_EXECUTION_SUCCEEDED
                    if succeeded
                    else TL_RESPONSE_EXECUTION_FAILED
                ),
                occurred_at=execution.finished_at,
                title=(
                    "Response execution succeeded"
                    if succeeded
                    else "Response execution failed"
                ),
                status=execution.status,
                ref_type="response_execution",
                ref_id=ref,
                safe_metadata=(
                    {"error_code": execution.safe_error_code}
                    if execution.safe_error_code
                    else {}
                ),
            )
        )
    return entries


def _compose(
    *,
    investigation: Investigation,
    plan_revisions: list[PlanRevision],
    evidence: list[Evidence],
    hypotheses: list[Hypothesis],
    assessments: list[HypothesisAssessment],
    findings: list[Finding],
    result: InvestigationResult | None,
    tool_records: list[ToolInvocationRecord],
    response: WorkspaceResponseProjection | None = None,
    response_entries: list[WorkspaceTimelineEntry] | None = None,
) -> InvestigationWorkspaceReadModel:
    latest = _latest_assessments(assessments)
    result_finding_ids = set(result.finding_ids) if result is not None else set()

    return InvestigationWorkspaceReadModel(
        investigation=_header(investigation),
        source_alert_ref=_source_ref(investigation),
        plan_revisions=tuple(
            sorted(
                (_plan_revision(p) for p in plan_revisions),
                key=lambda p: (p.revision, str(p.id)),
            )
        ),
        evidence=tuple(
            sorted(
                (_evidence(e) for e in evidence),
                key=lambda e: (e.observed_at or e.collected_at, str(e.evidence_id)),
            )
        ),
        hypotheses=tuple(
            WorkspaceHypothesis(
                hypothesis_id=h.id,
                statement=h.statement,
                status=h.status.value,
                assessment_revision=h.assessment_revision,
                latest_assessment=(
                    _assessment(latest[h.id]) if h.id in latest else None
                ),
            )
            for h in sorted(hypotheses, key=lambda h: (h.created_at, str(h.id)))
        ),
        findings=tuple(
            WorkspaceFinding(
                finding_id=f.id,
                statement=f.statement,
                created_at=f.created_at,
                evidence_citations=tuple(f.evidence_citations),
                in_result=f.id in result_finding_ids,
            )
            for f in sorted(findings, key=lambda f: (f.created_at, str(f.id)))
        ),
        result=_result(result) if result is not None else None,
        response=response or WorkspaceResponseProjection(),
        tool_activity=tuple(
            _tool_activity(r)
            for r in sorted(tool_records, key=lambda r: (r.started_at, str(r.id)))
        ),
        timeline=tuple(
            sorted(
                [
                    *_build_timeline(
                        investigation=investigation,
                        plan_revisions=plan_revisions,
                        evidence=evidence,
                        assessments=assessments,
                        findings=findings,
                        result=result,
                        tool_records=tool_records,
                    ),
                    *(response_entries or []),
                ],
                key=lambda e: (e.occurred_at, e.kind, e.ref_id or "", e.title),
            )
        ),
    )


class InvestigationWorkspaceService:
    def __init__(self, *, unit_of_work: UnitOfWork) -> None:
        self._uow = unit_of_work

    async def get_workspace(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> InvestigationWorkspaceReadModel:
        uow = self._uow
        try:
            investigation = await uow.investigations.get(
                tenant_id=tenant_id, investigation_id=investigation_id
            )
            if investigation is None:
                raise NotFoundError(
                    "investigation not found",
                    resource_type="investigation",
                    resource_id=str(investigation_id),
                )
            plan_revisions = await uow.plan_revisions.list_by_investigation(
                tenant_id=tenant_id, investigation_id=investigation_id
            )
            evidence = await uow.evidence.list_by_investigation(
                tenant_id=tenant_id, investigation_id=investigation_id
            )
            hypotheses = await uow.hypotheses.list_by_investigation(
                tenant_id=tenant_id, investigation_id=investigation_id
            )
            assessments = await uow.hypothesis_assessments.list_by_investigation(
                tenant_id=tenant_id, investigation_id=investigation_id
            )
            findings = await uow.findings.list_by_investigation(
                tenant_id=tenant_id, investigation_id=investigation_id
            )
            result = await uow.results.get_by_investigation(
                tenant_id=tenant_id, investigation_id=investigation_id
            )
            tool_records = await uow.tool_invocations.list_by_investigation(
                tenant_id=tenant_id, investigation_id=investigation_id
            )
            proposal = await uow.response_proposals.get_by_investigation(
                tenant_id=tenant_id, investigation_id=investigation_id
            )
            approval = None
            decision = None
            execution = None
            submission = None
            if proposal is not None:
                approval = await uow.response_approvals.get_request_by_proposal(
                    tenant_id=tenant_id, proposal_id=proposal.id
                )
                if approval is not None:
                    decision = await uow.response_approvals.get_decision(
                        tenant_id=tenant_id, approval_request_id=approval.id
                    )
                execution = await uow.response_executions.get_by_proposal(
                    tenant_id=tenant_id, proposal_id=proposal.id
                )
                submission = await uow.response_submissions.get_by_proposal(
                    tenant_id=tenant_id, proposal_id=proposal.id
                )
        finally:
            await uow.close()

        return _compose(
            investigation=investigation,
            plan_revisions=plan_revisions,
            evidence=evidence,
            hypotheses=hypotheses,
            assessments=assessments,
            findings=findings,
            result=result,
            tool_records=tool_records,
            response=_response_projection(
                result=result,
                proposal=proposal,
                approval=approval,
                decision=decision,
                submission=submission,
                execution=execution,
            ),
            response_entries=_response_timeline_entries(
                proposal=proposal,
                approval=approval,
                decision=decision,
                submission=submission,
                execution=execution,
            ),
        )

    async def lookup_alert_investigation(
        self,
        *,
        tenant_id: str,
        provider: str,
        resource_type: str,
        address_id: str,
    ) -> AlertInvestigationLookup:
        """The active (if any) + latest Investigation for one source alert (§22)."""
        ref = ExternalResourceRef(
            provider=provider, resource_type=resource_type, address_id=address_id
        )
        uow = self._uow
        try:
            active = await uow.investigations.find_active_by_alert(
                tenant_id=tenant_id, source_alert_ref=ref
            )
            latest = await uow.investigations.find_latest_by_alert(
                tenant_id=tenant_id, source_alert_ref=ref
            )
        finally:
            await uow.close()

        return AlertInvestigationLookup(
            active=_header(active) if active is not None else None,
            latest=_header(latest) if latest is not None else None,
        )
