"""Investigation commands — expressions of intent to change business facts.

Commands are immutable messages. They never carry authoritative tenant/actor
values from clients: those are bound by the caller from the authenticated context.

Two families:
- user-triggered lifecycle commands (StartAlertInvestigation / CancelInvestigation);
- orchestrator/system commands issued by the LangGraph nodes for the read-only
  Investigation loop (application-commands-domain-events-langgraph-state.md §6.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from ...domain.investigation.enums import (
    EvidenceRelation,
    InvestigationPhase,
    VerdictDisposition,
)

CommandSource = Literal["USER", "ORCHESTRATOR", "SYSTEM"]


@dataclass(frozen=True, kw_only=True)
class _CommandBase:
    tenant_id: str
    investigation_id: UUID
    command_id: UUID = field(default_factory=uuid4)
    idempotency_key: str | None = None
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    source: CommandSource = "ORCHESTRATOR"


@dataclass(frozen=True)
class StartAlertInvestigation:
    """Create (or return existing active) Investigation for one HISIEM Alert.

    ``tenant_id`` and ``initiated_by`` are populated by the authenticated request
    context (never from the request body / model).
    """

    tenant_id: str
    source_alert_id: str
    initiated_by_subject: str
    initiated_by_display_name: str | None = None
    command_id: UUID = field(default_factory=uuid4)
    idempotency_key: str | None = None
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class CancelInvestigation:
    tenant_id: str
    investigation_id: UUID
    initiated_by_subject: str
    command_id: UUID = field(default_factory=uuid4)
    idempotency_key: str | None = None
    correlation_id: UUID | None = None


# ---------------------------------------------------------------------------
# Orchestrator / system commands (read-only investigation workflow)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class StartInvestigation(_CommandBase):
    """CREATED → RUNNING, issued by the durable runner once an outbox event arrives.

    The HTTP lifecycle (StartAlertInvestigation) deliberately leaves the aggregate
    CREATED; the runner bridges it to RUNNING before the graph executes, so a
    creation is always durably visible before the agent starts (persistence-schema
    §24/§27).
    """


@dataclass(frozen=True, kw_only=True)
class ChangeInvestigationPhase(_CommandBase):
    phase: InvestigationPhase


@dataclass(frozen=True)
class PlanStepCandidate:
    """A single structured plan step (system-generated or LLM candidate)."""

    step_key: str
    objective: str
    ordinal: int


@dataclass(frozen=True, kw_only=True)
class ReviseInvestigationPlan(_CommandBase):
    """Persist a new immutable PlanRevision (never overwrite an old one)."""

    revision: int
    goal: str
    steps: list[PlanStepCandidate]
    generated_by: str = "system"


@dataclass(frozen=True)
class HypothesisCandidate:
    statement: str


@dataclass(frozen=True, kw_only=True)
class RegisterHypotheses(_CommandBase):
    hypotheses: list[HypothesisCandidate]


@dataclass(frozen=True)
class EvidenceObservation:
    """A normalized observation ready for Evidence persistence.

    Already validated + normalized by the Evidence Normalizer (Tool Result data
    only; provenance/source/refs are system-bound, never model-supplied).
    """

    source_type: str
    source_provider: str
    source_operation: str
    observation: dict[str, Any]
    source_resource_provider: str | None = None
    source_resource_type: str | None = None
    source_resource_address_id: str | None = None
    source_resource_business_id: str | None = None
    source_tool_invocation_id: UUID | None = None
    observed_at: datetime | None = None
    raw_reference: dict[str, Any] | None = None
    entity_refs: list[dict[str, str]] = field(default_factory=list)
    provenance_authority: str = "EXTERNAL_EVIDENCE"


@dataclass(frozen=True, kw_only=True)
class RecordEvidenceBatch(_CommandBase):
    """Record 0..N Evidence rows produced by one tool result."""

    observations: list[EvidenceObservation]


@dataclass(frozen=True)
class AssessmentEvidenceRelation:
    evidence_id: UUID
    relation: EvidenceRelation


@dataclass(frozen=True)
class HypothesisAssessmentCandidate:
    hypothesis_id: UUID
    status: str
    reason_summary: str
    evidence_relations: list[AssessmentEvidenceRelation]


@dataclass(frozen=True, kw_only=True)
class AssessHypotheses(_CommandBase):
    """Append immutable HypothesisAssessment revision(s); update hypothesis status."""

    assessments: list[HypothesisAssessmentCandidate]


@dataclass(frozen=True)
class FindingCandidate:
    statement: str
    evidence_citations: list[UUID]


@dataclass(frozen=True, kw_only=True)
class RecordFindings(_CommandBase):
    """Record immutable evidence-grounded Finding(s)."""

    findings: list[FindingCandidate]


@dataclass(frozen=True)
class UncertaintyCandidate:
    description: str
    missing_information: str | None = None
    related_hypothesis_ids: list[UUID] = field(default_factory=list)


@dataclass(frozen=True)
class AttackMappingCandidate:
    framework: str = "mitre-attack"
    technique_id: str | None = None
    name: str | None = None
    version: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class ResponseRecommendationCandidate:
    description: str
    reason: str


@dataclass(frozen=True)
class ResultVerdictCandidate:
    disposition: VerdictDisposition
    summary: str
    confidence: float


@dataclass(frozen=True, kw_only=True)
class FinalizeInvestigationResult(_CommandBase):
    """Finalize one immutable InvestigationResult for the Investigation.

    Invariants (application-commands...md §6.2): MALICIOUS/BENIGN requires at
    least one grounded Finding; INCONCLUSIVE requires at least one Uncertainty.
    """

    verdict: ResultVerdictCandidate
    finding_ids: list[UUID]
    uncertainties: list[UncertaintyCandidate] = field(default_factory=list)
    attack_mappings: list[AttackMappingCandidate] = field(default_factory=list)
    response_recommendations: list[ResponseRecommendationCandidate] = field(
        default_factory=list
    )


@dataclass(frozen=True, kw_only=True)
class CompleteInvestigation(_CommandBase):
    """RUNNING → COMPLETED (final result has no executable response this round)."""

    reason: str = "COMPLETED_WITHOUT_RESPONSE"


@dataclass(frozen=True, kw_only=True)
class FailInvestigation(_CommandBase):
    reason: str = "FAILED_FATAL"


# placeholder union for future typed command dispatch
InvestigationCommand = (
    StartAlertInvestigation
    | CancelInvestigation
    | StartInvestigation
    | ChangeInvestigationPhase
    | ReviseInvestigationPlan
    | RegisterHypotheses
    | RecordEvidenceBatch
    | AssessHypotheses
    | RecordFindings
    | FinalizeInvestigationResult
    | CompleteInvestigation
    | FailInvestigation
)
