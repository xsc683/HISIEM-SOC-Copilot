"""Investigation Workspace API schemas (boundary DTOs).

Explicit transport serializers for the Workspace read model — never generic ORM
serialization (docs/investigation-workspace.md §29). Every field is a bounded
passthrough of a persisted product fact; no credential, raw prompt/completion,
chain-of-thought, LangGraph checkpoint, or HTTP body is representable here.

The API depends on the application read models (allowed direction); it never
depends on infrastructure/ORM.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from ...application.queries.workspace import (
    AlertInvestigationLookup,
    InvestigationWorkspaceReadModel,
    WorkspaceAssessment,
    WorkspaceAttackMapping,
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


class InvestigationHeader(BaseModel):
    investigation_id: str
    tenant_id: str
    status: str
    phase: str | None = None
    initiated_by: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancelled_at: datetime | None = None
    termination_reason: str | None = None
    current_plan_revision: int = 0

    @classmethod
    def from_read_model(cls, value: WorkspaceInvestigation) -> InvestigationHeader:
        return cls(
            investigation_id=str(value.investigation_id),
            tenant_id=value.tenant_id,
            status=value.status,
            phase=value.phase,
            initiated_by=value.initiated_by,
            created_at=value.created_at,
            started_at=value.started_at,
            finished_at=value.finished_at,
            cancelled_at=value.cancelled_at,
            termination_reason=value.termination_reason,
            current_plan_revision=value.current_plan_revision,
        )


class SourceAlertRefSchema(BaseModel):
    provider: str
    resource_type: str
    address_id: str
    business_id: str | None = None

    @classmethod
    def from_read_model(cls, value: WorkspaceSourceAlertRef) -> SourceAlertRefSchema:
        return cls(
            provider=value.provider,
            resource_type=value.resource_type,
            address_id=value.address_id,
            business_id=value.business_id,
        )


class PlanStepSchema(BaseModel):
    step_id: str
    ordinal: int
    objective: str
    status: str

    @classmethod
    def from_read_model(cls, value: WorkspacePlanStep) -> PlanStepSchema:
        return cls(
            step_id=value.step_id,
            ordinal=value.ordinal,
            objective=value.objective,
            status=value.status,
        )


class PlanRevisionSchema(BaseModel):
    id: str
    revision: int
    generated_by: str
    created_at: datetime
    steps: list[PlanStepSchema] = []

    @classmethod
    def from_read_model(cls, value: WorkspacePlanRevision) -> PlanRevisionSchema:
        return cls(
            id=str(value.id),
            revision=value.revision,
            generated_by=value.generated_by,
            created_at=value.created_at,
            steps=[PlanStepSchema.from_read_model(s) for s in value.steps],
        )


class EvidenceSourceSchema(BaseModel):
    type: str
    provider: str
    operation: str

    @classmethod
    def from_read_model(cls, value: WorkspaceEvidenceSource) -> EvidenceSourceSchema:
        return cls(type=value.type, provider=value.provider, operation=value.operation)


class EntityRefSchema(BaseModel):
    kind: str
    value: str


class EvidenceSchema(BaseModel):
    evidence_id: str
    collected_at: datetime
    observed_at: datetime | None = None
    summary: str | None = None
    source: EvidenceSourceSchema
    source_tool_invocation_id: str | None = None
    observation: dict[str, Any]
    entity_refs: list[EntityRefSchema] = []
    raw_reference: dict[str, Any] | None = None
    content_hash: str | None = None
    dedup_key: str | None = None

    @classmethod
    def from_read_model(cls, value: WorkspaceEvidence) -> EvidenceSchema:
        return cls(
            evidence_id=str(value.evidence_id),
            collected_at=value.collected_at,
            observed_at=value.observed_at,
            summary=value.summary,
            source=EvidenceSourceSchema.from_read_model(value.source),
            source_tool_invocation_id=(
                str(value.source_tool_invocation_id)
                if value.source_tool_invocation_id is not None
                else None
            ),
            observation=value.observation,
            entity_refs=[
                EntityRefSchema(kind=ref.kind, value=ref.value)
                for ref in value.entity_refs
            ],
            raw_reference=value.raw_reference,
            content_hash=value.content_hash,
            dedup_key=value.dedup_key,
        )


class EvidenceRelationSchema(BaseModel):
    evidence_id: str
    relation: str

    @classmethod
    def from_read_model(
        cls, value: WorkspaceEvidenceRelation
    ) -> EvidenceRelationSchema:
        return cls(evidence_id=str(value.evidence_id), relation=value.relation)


class AssessmentSchema(BaseModel):
    revision: int
    status: str
    reason_summary: str
    created_at: datetime
    evidence_relations: list[EvidenceRelationSchema] = []

    @classmethod
    def from_read_model(cls, value: WorkspaceAssessment) -> AssessmentSchema:
        return cls(
            revision=value.revision,
            status=value.status,
            reason_summary=value.reason_summary,
            created_at=value.created_at,
            evidence_relations=[
                EvidenceRelationSchema.from_read_model(r)
                for r in value.evidence_relations
            ],
        )


class HypothesisSchema(BaseModel):
    hypothesis_id: str
    statement: str
    status: str
    assessment_revision: int
    latest_assessment: AssessmentSchema | None = None

    @classmethod
    def from_read_model(cls, value: WorkspaceHypothesis) -> HypothesisSchema:
        return cls(
            hypothesis_id=str(value.hypothesis_id),
            statement=value.statement,
            status=value.status,
            assessment_revision=value.assessment_revision,
            latest_assessment=(
                AssessmentSchema.from_read_model(value.latest_assessment)
                if value.latest_assessment is not None
                else None
            ),
        )


class FindingSchema(BaseModel):
    finding_id: str
    statement: str
    created_at: datetime
    evidence_citations: list[str] = []
    in_result: bool = False

    @classmethod
    def from_read_model(cls, value: WorkspaceFinding) -> FindingSchema:
        return cls(
            finding_id=str(value.finding_id),
            statement=value.statement,
            created_at=value.created_at,
            evidence_citations=[str(c) for c in value.evidence_citations],
            in_result=value.in_result,
        )


class VerdictSchema(BaseModel):
    disposition: str
    summary: str
    confidence: float

    @classmethod
    def from_read_model(cls, value: WorkspaceVerdict) -> VerdictSchema:
        return cls(
            disposition=value.disposition,
            summary=value.summary,
            confidence=value.confidence,
        )


class UncertaintySchema(BaseModel):
    description: str
    missing_information: str | None = None

    @classmethod
    def from_read_model(cls, value: WorkspaceUncertainty) -> UncertaintySchema:
        return cls(
            description=value.description,
            missing_information=value.missing_information,
        )


class AttackMappingSchema(BaseModel):
    framework: str
    technique_id: str | None = None
    name: str | None = None
    version: str | None = None
    source: str | None = None

    @classmethod
    def from_read_model(cls, value: WorkspaceAttackMapping) -> AttackMappingSchema:
        return cls(
            framework=value.framework,
            technique_id=value.technique_id,
            name=value.name,
            version=value.version,
            source=value.source,
        )


class ResponseRecommendationSchema(BaseModel):
    description: str
    reason: str

    @classmethod
    def from_read_model(
        cls, value: WorkspaceResponseRecommendation
    ) -> ResponseRecommendationSchema:
        return cls(description=value.description, reason=value.reason)


class ResultSchema(BaseModel):
    result_id: str
    verdict: VerdictSchema
    created_at: datetime
    finding_ids: list[str] = []
    uncertainties: list[UncertaintySchema] = []
    attack_mappings: list[AttackMappingSchema] = []
    response_recommendations: list[ResponseRecommendationSchema] = []

    @classmethod
    def from_read_model(cls, value: WorkspaceResult) -> ResultSchema:
        return cls(
            result_id=str(value.result_id),
            verdict=VerdictSchema.from_read_model(value.verdict),
            created_at=value.created_at,
            finding_ids=[str(f) for f in value.finding_ids],
            uncertainties=[
                UncertaintySchema.from_read_model(u) for u in value.uncertainties
            ],
            attack_mappings=[
                AttackMappingSchema.from_read_model(m) for m in value.attack_mappings
            ],
            response_recommendations=[
                ResponseRecommendationSchema.from_read_model(r)
                for r in value.response_recommendations
            ],
        )


class ToolActivitySchema(BaseModel):
    invocation_id: str
    tool_name: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = None
    safe_error_message: str | None = None
    duration_ms: int | None = None

    @classmethod
    def from_read_model(cls, value: WorkspaceToolActivity) -> ToolActivitySchema:
        return cls(
            invocation_id=str(value.invocation_id),
            tool_name=value.tool_name,
            status=value.status,
            started_at=value.started_at,
            finished_at=value.finished_at,
            error_code=value.error_code,
            safe_error_message=value.safe_error_message,
            duration_ms=value.duration_ms,
        )


class TimelineEntrySchema(BaseModel):
    kind: str
    occurred_at: datetime
    title: str
    status: str | None = None
    ref_type: str | None = None
    ref_id: str | None = None
    safe_metadata: dict[str, Any] = {}

    @classmethod
    def from_read_model(cls, value: WorkspaceTimelineEntry) -> TimelineEntrySchema:
        return cls(
            kind=value.kind,
            occurred_at=value.occurred_at,
            title=value.title,
            status=value.status,
            ref_type=value.ref_type,
            ref_id=value.ref_id,
            safe_metadata=value.safe_metadata,
        )


class InvestigationWorkspaceResponse(BaseModel):
    investigation: InvestigationHeader
    source_alert_ref: SourceAlertRefSchema
    plan_revisions: list[PlanRevisionSchema] = []
    evidence: list[EvidenceSchema] = []
    hypotheses: list[HypothesisSchema] = []
    findings: list[FindingSchema] = []
    result: ResultSchema | None = None
    tool_activity: list[ToolActivitySchema] = []
    timeline: list[TimelineEntrySchema] = []

    @classmethod
    def from_read_model(
        cls, value: InvestigationWorkspaceReadModel
    ) -> InvestigationWorkspaceResponse:
        return cls(
            investigation=InvestigationHeader.from_read_model(value.investigation),
            source_alert_ref=SourceAlertRefSchema.from_read_model(
                value.source_alert_ref
            ),
            plan_revisions=[
                PlanRevisionSchema.from_read_model(p) for p in value.plan_revisions
            ],
            evidence=[EvidenceSchema.from_read_model(e) for e in value.evidence],
            hypotheses=[
                HypothesisSchema.from_read_model(h) for h in value.hypotheses
            ],
            findings=[FindingSchema.from_read_model(f) for f in value.findings],
            result=(
                ResultSchema.from_read_model(value.result)
                if value.result is not None
                else None
            ),
            tool_activity=[
                ToolActivitySchema.from_read_model(t) for t in value.tool_activity
            ],
            timeline=[
                TimelineEntrySchema.from_read_model(t) for t in value.timeline
            ],
        )


class AlertInvestigationLookupResponse(BaseModel):
    """Server-to-server Alert re-entry lookup (HISIEM BFF, docs §22)."""

    active: InvestigationHeader | None = None
    latest: InvestigationHeader | None = None

    @classmethod
    def from_read_model(
        cls, value: AlertInvestigationLookup
    ) -> AlertInvestigationLookupResponse:
        return cls(
            active=(
                InvestigationHeader.from_read_model(value.active)
                if value.active is not None
                else None
            ),
            latest=(
                InvestigationHeader.from_read_model(value.latest)
                if value.latest is not None
                else None
            ),
        )
