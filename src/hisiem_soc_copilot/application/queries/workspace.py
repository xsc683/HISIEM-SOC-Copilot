"""Investigation Workspace read model — immutable product projection.

This is a QUERY-side projection (V1 product slice): the Analyst Workspace is
composed from persisted domain facts (the Investigation aggregate + its child
rows) and is NEVER derived from ORM entities at the API boundary. It imports
only domain types + stdlib (application-layer boundary — see
python-package-boundary.md).

Nothing here mutates domain state, and nothing here is produced by a
model/graph/tool: every field is a bounded passthrough of a persisted fact
(docs/investigation-workspace.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class WorkspaceInvestigation:
    """Aggregate-root header facts."""

    investigation_id: UUID
    tenant_id: str
    status: str
    phase: str | None
    initiated_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancelled_at: datetime | None
    termination_reason: str | None
    current_plan_revision: int


@dataclass(frozen=True)
class WorkspaceSourceAlertRef:
    """The HISIEM alert this Investigation is about (a reference, never a copy)."""

    provider: str
    resource_type: str
    address_id: str
    business_id: str | None


@dataclass(frozen=True)
class WorkspacePlanStep:
    step_id: str
    ordinal: int
    objective: str
    status: str


@dataclass(frozen=True)
class WorkspacePlanRevision:
    id: UUID
    revision: int
    generated_by: str
    created_at: datetime
    steps: tuple[WorkspacePlanStep, ...] = ()


@dataclass(frozen=True)
class WorkspaceEvidenceSource:
    type: str
    provider: str
    operation: str


@dataclass(frozen=True)
class WorkspaceEntityRef:
    kind: str
    value: str


@dataclass(frozen=True)
class WorkspaceEvidence:
    evidence_id: UUID
    collected_at: datetime
    observed_at: datetime | None
    summary: str | None
    source: WorkspaceEvidenceSource
    source_tool_invocation_id: UUID | None
    observation: dict[str, Any]
    entity_refs: tuple[WorkspaceEntityRef, ...] = ()
    raw_reference: dict[str, Any] | None = None
    content_hash: str | None = None
    dedup_key: str | None = None


@dataclass(frozen=True)
class WorkspaceEvidenceRelation:
    evidence_id: UUID
    relation: str


@dataclass(frozen=True)
class WorkspaceAssessment:
    revision: int
    status: str
    reason_summary: str
    created_at: datetime
    evidence_relations: tuple[WorkspaceEvidenceRelation, ...] = ()


@dataclass(frozen=True)
class WorkspaceHypothesis:
    hypothesis_id: UUID
    statement: str
    status: str
    assessment_revision: int
    latest_assessment: WorkspaceAssessment | None = None


@dataclass(frozen=True)
class WorkspaceFinding:
    finding_id: UUID
    statement: str
    created_at: datetime
    evidence_citations: tuple[UUID, ...] = ()
    # Whether this Finding participates in the finalized result's finding_ids
    # (domain-model: a correct Finding merely existing in the DB is not the same
    # as the Finding being part of the final InvestigationResult).
    in_result: bool = False


@dataclass(frozen=True)
class WorkspaceVerdict:
    disposition: str
    summary: str
    confidence: float


@dataclass(frozen=True)
class WorkspaceUncertainty:
    description: str
    missing_information: str | None


@dataclass(frozen=True)
class WorkspaceAttackMapping:
    framework: str
    technique_id: str | None
    name: str | None
    version: str | None
    source: str | None


@dataclass(frozen=True)
class WorkspaceResponseRecommendation:
    description: str
    reason: str


@dataclass(frozen=True)
class WorkspaceResult:
    result_id: UUID
    verdict: WorkspaceVerdict
    created_at: datetime
    finding_ids: tuple[UUID, ...] = ()
    uncertainties: tuple[WorkspaceUncertainty, ...] = ()
    attack_mappings: tuple[WorkspaceAttackMapping, ...] = ()
    response_recommendations: tuple[WorkspaceResponseRecommendation, ...] = ()


@dataclass(frozen=True)
class WorkspaceToolActivity:
    invocation_id: UUID
    tool_name: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = None
    safe_error_message: str | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class WorkspaceTimelineEntry:
    """One product activity-timeline entry (never debug telemetry).

    Every entry is backed by a persisted fact; ``kind`` is the stable machine
    discriminator and ``title`` a short neutral label the UI may localize.
    """

    kind: str
    occurred_at: datetime
    title: str
    status: str | None = None
    ref_type: str | None = None
    ref_id: str | None = None
    safe_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkspaceResponseTarget:
    provider: str
    resource_type: str
    address_id: str
    business_id: str | None = None


@dataclass(frozen=True)
class WorkspaceApprovalDecision:
    decision: str
    actor_subject_id: str
    actor_display_name: str | None
    reason: str | None
    decided_at: datetime


@dataclass(frozen=True)
class WorkspaceApproval:
    """Approval contract + (optional) immutable decision for one proposal."""

    request_id: UUID
    requested_at: datetime
    requested_reason: str
    expected_revision: int
    expected_content_hash: str
    decision: WorkspaceApprovalDecision | None = None

    @property
    def status(self) -> str:
        return "DECIDED" if self.decision is not None else "PENDING"


@dataclass(frozen=True)
class WorkspaceExecution:
    provider: str
    status: str
    submitted_at: datetime
    last_observed_at: datetime
    external_execution_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    safe_result: dict[str, Any] = field(default_factory=dict)
    safe_error_code: str | None = None
    safe_error_message: str | None = None


@dataclass(frozen=True)
class WorkspaceResponseSubmission:
    """The LOCAL submission lifecycle (independent of the provider execution).

    ``FAILED_DEFINITIVE`` means the provider refused the submission and no provider
    execution exists, so the workspace must NOT render an external execution id and
    must NOT keep telling the analyst that submission is still pending.

    ``ATTENTION_REQUIRED`` means the automatic retry budget ran out on UNCERTAIN
    failures: we cannot claim the provider refused it, and we cannot claim no
    execution exists. The workspace must say so plainly rather than showing a
    retry that is no longer happening.
    """

    status: str
    submission_key: str
    attempt_count: int = 0
    last_error_code: str | None = None
    safe_error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    submitted_at: datetime | None = None
    failed_at: datetime | None = None
    attention_required_at: datetime | None = None


@dataclass(frozen=True)
class WorkspaceResponseProposal:
    proposal_id: UUID
    revision: int
    content_hash: str
    status: str
    action_key: str
    parameters: dict[str, Any]
    reason: str
    target_refs: tuple[WorkspaceResponseTarget, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()
    policy_decision: str | None = None
    policy_reason: str | None = None
    created_at: datetime | None = None
    #: Immutable proposer provenance (server-derived at creation time).
    created_by_subject: str | None = None
    created_by_display_name: str | None = None
    approval: WorkspaceApproval | None = None
    submission: WorkspaceResponseSubmission | None = None
    execution: WorkspaceExecution | None = None


@dataclass(frozen=True)
class WorkspaceResponseProjection:
    """The Response tab projection: informational recommendations + typed proposals.

    ``recommendations`` are the InvestigationResult's explanatory output (never
    executable); ``proposals`` are the validated, approval-bound, executable
    contracts. The two are deliberately SEPARATE (spec §4).
    """

    recommendations: tuple[WorkspaceResponseRecommendation, ...] = ()
    proposals: tuple[WorkspaceResponseProposal, ...] = ()


@dataclass(frozen=True)
class InvestigationWorkspaceReadModel:
    """The full analyst-facing Workspace projection for one Investigation."""

    investigation: WorkspaceInvestigation
    source_alert_ref: WorkspaceSourceAlertRef
    plan_revisions: tuple[WorkspacePlanRevision, ...] = ()
    evidence: tuple[WorkspaceEvidence, ...] = ()
    hypotheses: tuple[WorkspaceHypothesis, ...] = ()
    findings: tuple[WorkspaceFinding, ...] = ()
    result: WorkspaceResult | None = None
    response: WorkspaceResponseProjection = field(
        default_factory=WorkspaceResponseProjection
    )
    tool_activity: tuple[WorkspaceToolActivity, ...] = ()
    timeline: tuple[WorkspaceTimelineEntry, ...] = ()


@dataclass(frozen=True)
class AlertInvestigationLookup:
    """Server-to-server Alert re-entry lookup (HISIEM BFF, docs §22).

    ``active`` is the at-most-one active Investigation for the alert, if any;
    ``latest`` is the most recent Investigation (any status) so a terminal one can
    still be reopened. Both are bounded header summaries — never child rows.
    """

    active: WorkspaceInvestigation | None = None
    latest: WorkspaceInvestigation | None = None


@dataclass(frozen=True)
class GetInvestigationWorkspace:
    tenant_id: str
    investigation_id: UUID


@dataclass(frozen=True)
class LookupAlertInvestigation:
    tenant_id: str
    provider: str
    resource_type: str
    address_id: str
