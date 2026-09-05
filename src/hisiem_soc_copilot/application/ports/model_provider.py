"""ModelProvider port — the replaceable LLM boundary.

The agent depends on this Protocol, never on a concrete provider SDK. V1 wires a
deterministic fake (infrastructure/llm) so the graph runs without network; a real
provider adapter can replace it later without touching the agent layer.

The provider returns structured candidates (contracts/llm/types) — never prose to
be interpreted. Inputs are bounded working context objects; secrets never flow in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ...contracts.llm.types import (
    AssessmentSummary,
    InvestigationPlan,
    NextStep,
    VerdictCandidate,
)
from ...contracts.tools.types import ModelToolSpec


@dataclass(frozen=True)
class PlanRequest:
    investigation_id: str
    alert_summary: str
    tool_names: list[str]


@dataclass(frozen=True)
class PreviousToolOutcome:
    """Bounded outcome of the most recent tool call, for failure-aware re-planning.

    Never carries raw exception text, stack traces, HTTP bodies, credentials, or a
    full tool result — only the tool's stable name, the typed execution status, a
    bounded error code, and whether the failure is transient (retryable).
    """

    tool_name: str
    status: str
    error_code: str | None = None
    retryable: bool = False


@dataclass(frozen=True)
class DecideNextRequest:
    investigation_id: str
    iteration: int
    plan_goal: str
    evidence_summary: list[str]
    tool_names: list[str]
    previous_tool_outcome: PreviousToolOutcome | None = None
    # Provider-neutral tool specs (name/description/arguments_schema) for the real,
    # selectable tools — lets the model build arguments the parser will accept.
    tool_specs: list[ModelToolSpec] = field(default_factory=list)


@dataclass(frozen=True)
class AssessRequest:
    """Structured evidence + hypothesis context for the model's assess/verdict call.

    Evidence is passed as per-id bounded summaries (never full tool results or raw
    logs) so the model can ground its assessment on specific evidence ids; the
    hypotheses are passed with their ids so a per-hypothesis verdict can be formed.
    """

    investigation_id: str
    evidence_summary: list[str] = field(default_factory=list)
    evidence: list[dict[str, object]] = field(default_factory=list)
    hypotheses: list[dict[str, object]] = field(default_factory=list)
    finding_candidates: list[str] = field(default_factory=list)


class ModelProvider(Protocol):
    """Structured candidate producer for the read-only investigation loop."""

    async def plan(self, request: PlanRequest) -> InvestigationPlan: ...

    async def decide_next(self, request: DecideNextRequest) -> NextStep: ...

    async def assess(self, request: AssessRequest) -> AssessmentSummary: ...

    async def verdict(self, request: AssessRequest) -> VerdictCandidate: ...
