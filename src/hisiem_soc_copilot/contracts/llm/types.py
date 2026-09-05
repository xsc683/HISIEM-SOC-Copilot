"""LLM structured-output contracts — the ONLY thing the model may produce.

The model is a candidate producer (application-commands...md §21). These typed
dataclasses are the deterministic contracts the ModelProvider must satisfy. The
agent never consumes raw model text as a business fact: each candidate goes
through schema validation → domain validation → reference resolution → persistence
in the application layer.

The investigation workflow uses a small set of read-only candidates:
- InvestigationPlan (steps to run),
- NextStep (either more tool work or finish → finalize),
- HypothesisSet / FindingSet / VerdictCandidate / AssessmentSummary.

Verbatim transcripts / chain-of-thought never enter state (python-package-
boundary.md §13).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    objective: str


@dataclass(frozen=True)
class InvestigationPlan:
    goal: str
    steps: list[PlanStep] = field(default_factory=list)


@dataclass(frozen=True)
class HypothesisCandidate:
    statement: str


@dataclass(frozen=True)
class FindingCandidate:
    statement: str
    evidence_citations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VerdictCandidate:
    disposition: Literal["MALICIOUS", "BENIGN", "INCONCLUSIVE"]
    summary: str
    confidence: float
    uncertainty: str | None = None


@dataclass(frozen=True)
class NextStep:
    """Structured "decide_next" decision: keep investigating or finalize."""

    decision: Literal["CONTINUE", "FINALIZE"]
    tool_name: str | None = None
    arguments: dict[str, object] = field(default_factory=dict)
    reason: str | None = None


@dataclass(frozen=True)
class AssessmentEvidenceRelation:
    """A semantic evidence↔hypothesis relation produced by the model (candidate).

    ``evidence_id`` is the evidence's UUID; the relation is the model's judgment of
    how that specific evidence bears on the specific hypothesis. The model may ONLY
    cite evidence ids that exist in this investigation (schema validation + same-
    investigation resolution reject anything else). Rule metadata or mere context
    evidence is expressed as ``CONTEXT`` and must never be the sole ground for a
    SUPPORTED/CONTRADICTED verdict.
    """

    evidence_id: str
    relation: Literal["SUPPORTS", "CONTRADICTS", "CONTEXT"]


@dataclass(frozen=True)
class HypothesisAssessmentCandidate:
    """The model's assessment of ONE hypothesis (structured, evidence-grounded).

    status / relations are candidate semantics — the application layer validates
    that the hypothesis belongs to the investigation and that every cited evidence
    id resolves to evidence in the SAME investigation before persisting.
    """

    hypothesis_id: str
    status: Literal["SUPPORTED", "CONTRADICTED", "UNRESOLVED"]
    reason_summary: str
    evidence_relations: list[AssessmentEvidenceRelation] = field(default_factory=list)


@dataclass(frozen=True)
class AssessmentSummary:
    """Per-hypothesis structured assessment of the evidence gathered so far."""

    decision: Literal["CONTINUE", "FINALIZE"]
    assessments: list[HypothesisAssessmentCandidate] = field(default_factory=list)
    findings: list[FindingCandidate] = field(default_factory=list)
    unresolved_evidence_gaps: list[str] = field(default_factory=list)


class ContractError(ValueError):
    """Raised when a model candidate violates a contract (structure or semantics)."""
