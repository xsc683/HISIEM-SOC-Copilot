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
class AssessmentSummary:
    """Whether evidence so far supports/contradicts/resolves the plan."""

    decision: Literal["CONTINUE", "FINALIZE"]
    reason: str
    findings: list[FindingCandidate] = field(default_factory=list)
    unresolved_evidence_gaps: list[str] = field(default_factory=list)


class ContractError(ValueError):
    """Raised when a model candidate violates a contract (structure or semantics)."""
