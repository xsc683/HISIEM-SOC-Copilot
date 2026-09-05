"""ModelProvider port — the replaceable LLM boundary.

The agent depends on this Protocol, never on a concrete provider SDK. V1 wires a
deterministic fake (infrastructure/llm) so the graph runs without network; a real
provider adapter can replace it later without touching the agent layer.

The provider returns structured candidates (contracts/llm/types) — never prose to
be interpreted. Inputs are bounded working context objects; secrets never flow in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from ...contracts.llm.types import (
    AssessmentSummary,
    InvestigationPlan,
    NextStep,
    VerdictCandidate,
)
from ...contracts.tools.types import ModelToolSpec
from ...domain.investigation.entities import Evidence


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
class DecideAlertContext:
    """Bounded authoritative alert context the decide consult needs.

    Only investigation-decision fields. NEVER tenant/auth/authorization data,
    provider secrets, credentials, or raw alert bodies. Values are bounded strings
    (None when the source did not supply them) — the model must use the REAL values,
    never guess.
    """

    rule_id: str | None = None
    detected_at: str | None = None
    source_ip: str | None = None
    user_name: str | None = None
    host_name: str | None = None
    event_category: str | None = None
    event_action: str | None = None
    severity: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "detected_at": self.detected_at,
            "source_ip": self.source_ip,
            "user_name": self.user_name,
            "host_name": self.host_name,
            "event_category": self.event_category,
            "event_action": self.event_action,
            "severity": self.severity,
        }


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
    # Bounded authoritative alert context (real rule_id/detected_at/entity values).
    alert_context: DecideAlertContext | None = None
    # Bounded persisted evidence of THIS investigation: {evidence_id, operation,
    # summary} — never raw ToolResults/Events/full event.original/credentials.
    evidence: list[dict[str, object]] = field(default_factory=list)


# Bounded deterministic working-context budget for DecideNextRequest.evidence.
# Each summary is bounded on its own, but the COUNT of evidence rows appended is
# not — a long-running investigation could hand the model an unbounded prompt.
# All THREE limits apply at once: item count, total characters, and per-item
# summary length. The selection is pure + deterministic (see
# select_decide_evidence_context) so a given investigation always yields the same
# bounded working context, independent of repository insertion order.
MAX_DECIDE_EVIDENCE_ITEMS = 40
MAX_DECIDE_EVIDENCE_TOTAL_CHARS = 12000
MAX_DECIDE_EVIDENCE_SUMMARY_CHARS = 250


def _bounded_evidence_summary(evidence: Evidence, limit: int) -> str:
    """The single summary the model actually sees — never longer than ``limit``.

    Truncation keeps the ellipsis inside the cap, so ``len(result) <= limit``
    strictly matches the working-context budget (no hidden +1 for the marker).
    """
    line = evidence.summary or str(evidence.observation)
    text = str(line).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def select_decide_evidence_context(
    evidence_rows: list[Evidence],
) -> list[dict[str, object]]:
    """Deterministically bound persisted Evidence into the decide working context.

    Returns newest-first-selected, chronologically-ordered ``{evidence_id,
    operation, summary}`` entries within BOTH the item and total-character budget,
    each summary bounded to MAX_DECIDE_EVIDENCE_SUMMARY_CHARS. Only bounded fields
    ever reach the model — never raw ToolResults/Events/observation JSON. Selection
    is deterministic and reproducible: sort the FULL list by newest collected time
    (tie-broken by id), take newest-first within the caps, then restore
    chronological order for the prompt. This is a pure selection over the rows it
    is given — tenant/investigation scoping happens in the caller's repository
    read, never here.
    """
    if not evidence_rows:
        return []
    # Deterministic newest-first over the FULL list (never trust insertion order).
    ordered = sorted(
        evidence_rows,
        key=lambda e: (
            _collected_timestamp(e),
            str(e.id),
        ),
        reverse=True,
    )
    kept: list[Evidence] = []
    total_chars = 0
    for evidence in ordered:
        summary = _bounded_evidence_summary(
            evidence, MAX_DECIDE_EVIDENCE_SUMMARY_CHARS
        )
        if len(kept) >= MAX_DECIDE_EVIDENCE_ITEMS:
            break
        if total_chars > 0 and total_chars + len(summary) > MAX_DECIDE_EVIDENCE_TOTAL_CHARS:
            break
        total_chars += len(summary)
        kept.append(evidence)
    kept.sort(key=lambda e: (_collected_timestamp(e), str(e.id)))  # chronological
    return [
        {
            "evidence_id": str(evidence.id),
            "operation": evidence.source.operation,
            "summary": _bounded_evidence_summary(
                evidence, MAX_DECIDE_EVIDENCE_SUMMARY_CHARS
            ),
        }
        for evidence in kept
    ]


def _collected_timestamp(evidence: Evidence) -> float:
    """Floating epoch for deterministic ordering (never reversed by tz/naive mix)."""
    dt = evidence.collected_at
    try:
        if dt.tzinfo is not None:
            return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        pass
    return _naive_epoch(dt)


def _naive_epoch(dt: datetime) -> float:
    try:
        return dt.replace(tzinfo=UTC).timestamp()
    except (OverflowError, OSError, ValueError):
        # datetime.min/max outside the platform epoch range (e.g. Windows) — the
        # caller always persists real collected_at values, so this only guards an
        # artificial boundary. Fall back to a plain ordinal so ordering stays
        # stable and total.
        return float(dt.toordinal())


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
