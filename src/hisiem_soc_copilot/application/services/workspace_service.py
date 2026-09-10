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
from ..errors import NotFoundError
from ..ports.durable import ToolInvocationRecord
from ..ports.unit_of_work import UnitOfWork
from ..queries.workspace import (
    AlertInvestigationLookup,
    InvestigationWorkspaceReadModel,
    WorkspaceAssessment,
    WorkspaceAttackMapping,
    WorkspaceEntityRef,
    WorkspaceEvidence,
    WorkspaceEvidenceRelation,
    WorkspaceEvidenceSource,
    WorkspaceFinding,
    WorkspaceHypothesis,
    WorkspaceInvestigation,
    WorkspacePlanRevision,
    WorkspacePlanStep,
    WorkspaceResponseRecommendation,
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

_TOOL_SUCCEEDED = "SUCCEEDED"


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
        tool_activity=tuple(
            _tool_activity(r)
            for r in sorted(tool_records, key=lambda r: (r.started_at, str(r.id)))
        ),
        timeline=tuple(
            _build_timeline(
                investigation=investigation,
                plan_revisions=plan_revisions,
                evidence=evidence,
                assessments=assessments,
                findings=findings,
                result=result,
                tool_records=tool_records,
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
