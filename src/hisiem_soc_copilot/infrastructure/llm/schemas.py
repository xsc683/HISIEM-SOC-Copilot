"""Provider wire schemas — the strict boundary between provider JSON and contracts.

The provider returns JSON; this module (Pydantic v2, strict) is the ONLY place that
turns that JSON into typed wire objects, which are then mapped EXPLICITLY onto the
existing contract candidates (contracts/llm/types.py). No OpenAI SDK response object
ever leaves infrastructure, and no raw JSON is ever consumed as a contract.

Every failure mode (empty completion, invalid JSON, schema mismatch, missing field,
wrong enum, out-of-range number) surfaces as ``ModelOutputValidationError`` — never a
guess, a prose parse, or an invented business fact.
"""

from __future__ import annotations

import json
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...contracts.llm.errors import ModelOutputValidationError
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

# --- strict configuration shared by every wire model ---
_STRICT = ConfigDict(extra="forbid", strict=True)


class PlanStepOutput(BaseModel):
    model_config = _STRICT
    step_id: str
    objective: str


class PlanOutput(BaseModel):
    model_config = _STRICT
    goal: str
    steps: list[PlanStepOutput] = Field(default_factory=list)


class NextStepOutput(BaseModel):
    model_config = _STRICT
    decision: Literal["CONTINUE", "FINALIZE"]
    tool_name: str | None = None
    arguments: dict[str, object] | None = None
    reason: str | None = None


class AssessmentEvidenceRelationOutput(BaseModel):
    model_config = _STRICT
    evidence_id: str
    relation: Literal["SUPPORTS", "CONTRADICTS", "CONTEXT"]


class HypothesisAssessmentOutput(BaseModel):
    model_config = _STRICT
    hypothesis_id: str
    status: Literal["SUPPORTED", "CONTRADICTED", "UNRESOLVED"]
    reason_summary: str
    evidence_relations: list[AssessmentEvidenceRelationOutput] = Field(
        default_factory=list
    )


class FindingOutput(BaseModel):
    model_config = _STRICT
    statement: str
    evidence_citations: list[str] = Field(default_factory=list)


class AssessmentOutput(BaseModel):
    model_config = _STRICT
    assessments: list[HypothesisAssessmentOutput] = Field(default_factory=list)
    findings: list[FindingOutput] = Field(default_factory=list)


class VerdictOutput(BaseModel):
    model_config = _STRICT
    disposition: Literal["MALICIOUS", "BENIGN", "INCONCLUSIVE"]
    summary: str
    confidence: float = Field(ge=0.0, le=1.0)
    uncertainty: str | None = None


def parse_wire[T: BaseModel](content: str, model: type[T]) -> T:
    """JSON.parse + Pydantic strict validation of one provider completion body.

    Raises ``ModelOutputValidationError`` for every malformed/schema-invalid case.
    """
    if not content or not content.strip():
        raise ModelOutputValidationError(
            "provider returned an empty completion", code="EMPTY_COMPLETION"
        )
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ModelOutputValidationError(
            "provider returned invalid JSON", code="INVALID_JSON"
        ) from exc
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ModelOutputValidationError(
            "provider output failed strict schema validation",
            code="SCHEMA_INVALID",
        ) from exc


# --- explicit wire → contract mapping (never implicit) ---
def to_plan(wire: PlanOutput) -> InvestigationPlan:
    return InvestigationPlan(
        goal=wire.goal,
        steps=[PlanStep(step_id=s.step_id, objective=s.objective) for s in wire.steps],
    )


def to_next_step(wire: NextStepOutput) -> NextStep:
    return NextStep(
        decision=wire.decision,
        tool_name=wire.tool_name,
        arguments=dict(wire.arguments) if wire.arguments is not None else {},
        reason=wire.reason,
    )


def to_assessment(wire: AssessmentOutput) -> AssessmentSummary:
    return AssessmentSummary(
        # assess is the convergence node; the graph routes to finalize regardless.
        decision="FINALIZE",
        assessments=[
            HypothesisAssessmentCandidate(
                hypothesis_id=a.hypothesis_id,
                status=a.status,
                reason_summary=a.reason_summary,
                evidence_relations=[
                    AssessmentEvidenceRelation(
                        evidence_id=r.evidence_id, relation=r.relation
                    )
                    for r in a.evidence_relations
                ],
            )
            for a in wire.assessments
        ],
        findings=[
            FindingCandidate(
                statement=f.statement, evidence_citations=list(f.evidence_citations)
            )
            for f in wire.findings
        ],
    )


def to_verdict(wire: VerdictOutput) -> VerdictCandidate:
    return VerdictCandidate(
        disposition=wire.disposition,
        summary=wire.summary,
        confidence=wire.confidence,
        uncertainty=wire.uncertainty,
    )


# ---------------------------------------------------------------------------
# Strict json_schema fragments (for response_format=json_schema). Hand-written so
# every field is required and nested objects are closed (additionalProperties false),
# the shape strict providers expect. A provider that rejects this schema triggers the
# adapter's json_object / JSON-only fallback.
# ---------------------------------------------------------------------------


def _closed_object(properties: dict[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _literal(name: str, options: tuple[str, ...]) -> dict[str, object]:
    return {"type": "string", "enum": list(options)}


def _nullable_string() -> dict[str, object]:
    return {"type": ["string", "null"]}


def _string_array() -> dict[str, object]:
    return {"type": "array", "items": {"type": "string"}, "uniqueItems": True}


def _relation_options() -> tuple[str, ...]:
    annotation = AssessmentEvidenceRelationOutput.model_fields["relation"].annotation
    if annotation is None:
        return ("SUPPORTS", "CONTRADICTS", "CONTEXT")
    return tuple(str(o) for o in get_args(annotation))


PLAN_JSON_SCHEMA: dict[str, object] = _closed_object(
    {
        "goal": {"type": "string"},
        "steps": {
            "type": "array",
            "items": _closed_object(
                {"step_id": {"type": "string"}, "objective": {"type": "string"}}
            ),
        },
    }
)

NEXT_STEP_JSON_SCHEMA: dict[str, object] = _closed_object(
    {
        "decision": _literal("decision", ("CONTINUE", "FINALIZE")),
        "tool_name": _nullable_string(),
        "arguments": {"type": "object"},
        "reason": _nullable_string(),
    }
)

ASSESSMENT_JSON_SCHEMA: dict[str, object] = _closed_object(
    {
        "assessments": {
            "type": "array",
            "items": _closed_object(
                {
                    "hypothesis_id": {"type": "string"},
                    "status": _literal("status", ("SUPPORTED", "CONTRADICTED", "UNRESOLVED")),
                    "reason_summary": {"type": "string"},
                    "evidence_relations": {
                        "type": "array",
                        "items": _closed_object(
                            {
                                "evidence_id": {"type": "string"},
                                "relation": _literal("relation", _relation_options()),
                            }
                        ),
                    },
                }
            ),
        },
        "findings": {
            "type": "array",
            "items": _closed_object(
                {
                    "statement": {"type": "string"},
                    "evidence_citations": _string_array(),
                }
            ),
        },
    }
)

VERDICT_JSON_SCHEMA: dict[str, object] = _closed_object(
    {
        "disposition": _literal("disposition", ("MALICIOUS", "BENIGN", "INCONCLUSIVE")),
        "summary": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "uncertainty": _nullable_string(),
    }
)
