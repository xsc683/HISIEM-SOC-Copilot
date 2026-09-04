"""ModelProvider port — the replaceable LLM boundary.

The agent depends on this Protocol, never on a concrete provider SDK. V1 wires a
deterministic fake (infrastructure/llm) so the graph runs without network; a real
provider adapter can replace it later without touching the agent layer.

The provider returns structured candidates (contracts/llm/types) — never prose to
be interpreted. Inputs are bounded working context objects; secrets never flow in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ...contracts.llm.types import (
    AssessmentSummary,
    InvestigationPlan,
    NextStep,
    VerdictCandidate,
)


@dataclass(frozen=True)
class PlanRequest:
    investigation_id: str
    alert_summary: str
    tool_names: list[str]


@dataclass(frozen=True)
class DecideNextRequest:
    investigation_id: str
    iteration: int
    plan_goal: str
    evidence_summary: list[str]
    tool_names: list[str]


@dataclass(frozen=True)
class AssessRequest:
    investigation_id: str
    evidence_summary: list[str]
    finding_candidates: list[str]


class ModelProvider(Protocol):
    """Structured candidate producer for the read-only investigation loop."""

    async def plan(self, request: PlanRequest) -> InvestigationPlan: ...

    async def decide_next(self, request: DecideNextRequest) -> NextStep: ...

    async def assess(self, request: AssessRequest) -> AssessmentSummary: ...

    async def verdict(self, request: AssessRequest) -> VerdictCandidate: ...
