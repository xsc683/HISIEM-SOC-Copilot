"""Investigation child entities (Evidence, Hypothesis, Finding, Result pieces).

Per domain-model.md these are immutable/append-only records owned by an
Investigation. They are plain dataclasses; persistence maps them to rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from ..shared.identifiers import utc_now
from .enums import (
    EvidenceRelation,
    EvidenceSourceType,
    HypothesisStatus,
    PlanStepStatus,
    ProvenanceAuthority,
    VerdictDisposition,
)
from .value_objects import ExternalResourceRef


@dataclass(frozen=True)
class EvidenceSource:
    """Source classification for one piece of Evidence (value object)."""

    type: EvidenceSourceType
    provider: str
    operation: str


@dataclass(frozen=True)
class EntityRef:
    """An entity (user/host/ip/...) referenced by evidence."""

    kind: str
    value: str


@dataclass(frozen=True)
class Evidence:
    """Immutable evidence ledger entry.

    Invariants (domain-model.md §8): belongs to one Investigation, has provenance,
    has collected_at, immutable after creation, and can never be created from
    unsupported model imagination.
    """

    id: UUID
    investigation_id: UUID
    source: EvidenceSource
    collected_at: datetime
    observation: dict[str, Any]
    source_resource_ref: ExternalResourceRef | None = None
    source_tool_call_id: UUID | None = None
    observed_at: datetime | None = None
    summary: str | None = None
    raw_reference: dict[str, Any] | None = None
    entity_refs: list[EntityRef] = field(default_factory=list)
    provenance_authority: ProvenanceAuthority = ProvenanceAuthority.EXTERNAL_EVIDENCE
    content_hash: str | None = None
    dedup_key: str | None = None


@dataclass(frozen=True)
class HypothesisAssessmentEvidence:
    """One (evidence_id, relation) pairing within a hypothesis assessment."""

    evidence_id: UUID
    relation: EvidenceRelation


@dataclass(frozen=True)
class HypothesisAssessment:
    """An immutable hypothesis assessment revision."""

    id: UUID
    hypothesis_id: UUID
    investigation_id: UUID
    revision: int
    status: HypothesisStatus
    evidence_relations: list[HypothesisAssessmentEvidence]
    reason_summary: str
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class Hypothesis:
    """A security explanation pending evidence support/refutation."""

    id: UUID
    investigation_id: UUID
    statement: str
    status: HypothesisStatus = HypothesisStatus.OPEN
    assessment_revision: int = 0
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class Finding:
    """An evidence-grounded factual investigation judgement (immutable)."""

    id: UUID
    investigation_id: UUID
    statement: str
    evidence_citations: list[UUID] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class Verdict:
    disposition: VerdictDisposition
    summary: str
    confidence: float


@dataclass(frozen=True)
class Uncertainty:
    description: str
    missing_information: str | None = None
    related_hypothesis_ids: list[UUID] = field(default_factory=list)


@dataclass(frozen=True)
class AttackMapping:
    framework: str = "mitre-attack"
    technique_id: str | None = None
    name: str | None = None
    version: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class ResponseRecommendation:
    description: str
    reason: str


@dataclass(frozen=True)
class InvestigationResult:
    """Immutable finalized result for an Investigation (0..1 per Investigation)."""

    id: UUID
    investigation_id: UUID
    verdict: Verdict
    finding_ids: list[UUID] = field(default_factory=list)
    uncertainties: list[Uncertainty] = field(default_factory=list)
    attack_mappings: list[AttackMapping] = field(default_factory=list)
    response_recommendations: list[ResponseRecommendation] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    content_hash: str | None = None


@dataclass
class PlanStep:
    step_id: str
    objective: str
    status: PlanStepStatus = PlanStepStatus.PENDING
    ordinal: int = 0


@dataclass(frozen=True)
class PlanRevision:
    """Immutable plan definition (revisioned; never overwritten in place)."""

    id: UUID
    investigation_id: UUID
    revision: int
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    generated_by: str = "system"
    created_at: datetime = field(default_factory=utc_now)
