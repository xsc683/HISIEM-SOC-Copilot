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
class InvestigationWorkspaceReadModel:
    """The full analyst-facing Workspace projection for one Investigation."""

    investigation: WorkspaceInvestigation
    source_alert_ref: WorkspaceSourceAlertRef
    plan_revisions: tuple[WorkspacePlanRevision, ...] = ()
    evidence: tuple[WorkspaceEvidence, ...] = ()
    hypotheses: tuple[WorkspaceHypothesis, ...] = ()
    findings: tuple[WorkspaceFinding, ...] = ()
    result: WorkspaceResult | None = None
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
