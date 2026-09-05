"""Deterministic fake ModelProvider (infrastructure/llm adapter).

A scripted, deterministic model so the graph + tests run without a network and
with fully predictable candidates. Each method returns structured contract objects
built from the request; the agent still treats these as *candidates* and the
application layer still validates/grounds them before persistence.

The fake never fabricates provenance, tenant scope, or authority — it produces
only the candidate content the port allows.
"""

from __future__ import annotations

from typing import Any, Literal

from ...application.ports.model_provider import (
    AssessRequest,
    DecideNextRequest,
    PlanRequest,
)
from ...contracts.llm.types import (
    AssessmentEvidenceRelation,
    AssessmentSummary,
    FindingCandidate,
    HypothesisAssessmentCandidate,
    InvestigationPlan,
    NextStep,
    PlanStep,
    VerdictCandidate,
)

_DEFAULT_GOAL = "Determine whether the SSH brute force escalated into a compromise"

_RelationLiteral = Literal["SUPPORTS", "CONTRADICTS", "CONTEXT"]
_ASSESS_RELATIONS: frozenset[str] = frozenset({"SUPPORTS", "CONTRADICTS", "CONTEXT"})


def _relation_literal(value: str) -> _RelationLiteral:
    """Normalize a scripted relation string to the contract Literal."""
    if value in ("SUPPORTS", "CONTRADICTS", "CONTEXT"):
        return value  # type: ignore[return-value]
    return "CONTEXT"


class ScriptedModelProvider:
    """A scripted model with a configurable candidate script (deterministic).

    ``script`` keys:
    - ``plan_steps``: list of step keys/objectives (default: read rule + search).
    - ``decide``: either ``{"decision": "FINALIZE"}`` or a list of tool turns;
      a list is consumed one per call and FINALIZE is returned when exhausted.
    - ``next_tool``: when ``decide`` is a list, each item names the tool to call.
    - ``findings``: finding statements the assess step emits.
    - ``verdict``: disposition/summary/confidence (default INCONCLUSIVE).

    For SSH golden path tests, the caller supplies a script mirroring the real
    evidence: repeated failures → success-after-failures → post-login activity.
    """

    def __init__(self, *, script: dict[str, Any] | None = None) -> None:
        script = dict(script or {})
        self._plan_steps: list[tuple[str, str]] = [
            (str(k), str(v)) for k, v in (script.get("plan_steps") or {}).items()
        ]
        raw_decide: Any = script.get("decide") or {"decision": "FINALIZE"}
        self._final_decide: dict[str, Any] | None = None
        self._tool_turns: list[dict[str, Any]] = []
        if isinstance(raw_decide, list):
            self._tool_turns = list(raw_decide)
        elif isinstance(raw_decide, dict):
            self._final_decide = dict(raw_decide)
        else:
            self._final_decide = {"decision": "FINALIZE"}
        self._findings: list[str] = list(script.get("findings") or [])
        # Structured per-hypothesis assessment script: {hypothesis_id: {status,
        # reason, evidence_relations: [{evidence_id, relation}]}}.
        self._assess_script: dict[str, dict[str, Any]] = dict(
            script.get("assess") or {}
        )
        self._verdict: dict[str, Any] = dict(
            script.get("verdict")
            or {
                "disposition": "INCONCLUSIVE",
                "summary": "Insufficient evidence",
                "confidence": 0.3,
            }
        )
        self.calls: list[str] = []

    async def plan(self, request: PlanRequest) -> InvestigationPlan:
        self.calls.append("plan")
        steps = self._plan_steps or [
            ("read_rule", "Read the detection rule that fired on this alert"),
            ("search_success", "Search for a successful authentication after the failures"),
        ]
        return InvestigationPlan(
            goal=request.alert_summary or _DEFAULT_GOAL,
            steps=[PlanStep(step_id=sid, objective=obj) for sid, obj in steps],
        )

    async def decide_next(self, request: DecideNextRequest) -> NextStep:
        self.calls.append("decide_next")
        if self._final_decide is not None:
            return NextStep(
                decision=self._final_decide.get("decision", "FINALIZE"),
                tool_name=self._final_decide.get("tool_name"),
                arguments=self._final_decide.get("arguments") or {},
                reason=self._final_decide.get("reason"),
            )
        if self._tool_turns:
            turn = self._tool_turns.pop(0)
            return NextStep(
                decision="CONTINUE",
                tool_name=turn.get("tool_name"),
                arguments=turn.get("arguments") or {},
                reason=turn.get("reason"),
            )
        return NextStep(decision="FINALIZE", reason="no more investigation steps")

    async def assess(self, request: AssessRequest) -> AssessmentSummary:
        self.calls.append("assess")
        # Build a per-hypothesis structured assessment. The model grounds each
        # hypothesis it assesses on evidence ids it was given; an unscripted
        # hypothesis is left UNRESOLVED (never auto-supported).
        assessments: list[HypothesisAssessmentCandidate] = []
        for hyp in request.hypotheses:
            hyp_id = str(hyp.get("id", ""))
            scripted = self._assess_script.get(hyp_id)
            if scripted is None:
                continue  # the model produced no assessment for this hypothesis
            relations = [
                AssessmentEvidenceRelation(
                    evidence_id=str(rel.get("evidence_id", "")),
                    relation=_relation_literal(str(rel.get("relation", "CONTEXT"))),
                )
                for rel in scripted.get("evidence_relations") or []
            ]
            assessments.append(
                HypothesisAssessmentCandidate(
                    hypothesis_id=hyp_id,
                    status=scripted.get("status", "UNRESOLVED"),
                    reason_summary=str(scripted.get("reason") or "evidence-based assessment"),
                    evidence_relations=relations,
                )
            )
        return AssessmentSummary(
            decision="FINALIZE",
            assessments=assessments,
            findings=[FindingCandidate(statement=f) for f in self._findings],
        )

    async def verdict(self, request: AssessRequest) -> VerdictCandidate:
        self.calls.append("verdict")
        return VerdictCandidate(
            disposition=self._verdict["disposition"],
            summary=self._verdict["summary"],
            confidence=float(self._verdict.get("confidence", 0.3)),
            uncertainty=self._verdict.get("uncertainty"),
        )
