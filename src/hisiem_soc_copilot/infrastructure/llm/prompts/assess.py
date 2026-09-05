"""Assess prompt: AssessRequest → Chat messages.

Input: hypotheses (id + statement) and evidence (id + bounded summary + operation).
Output: per-hypothesis status with evidence_relations restricted to the given
evidence ids, plus findings that MUST cite real evidence ids. Deterministic
grounding checks still run in the graph regardless of a strict schema.
"""

from __future__ import annotations

from ....application.ports.model_provider import AssessRequest
from ..schemas import ASSESSMENT_JSON_SCHEMA
from .common import bounded, system_message, user_message


def _hypothesis_lines(hypotheses: list[dict[str, object]]) -> str:
    if not hypotheses:
        return "(no hypotheses supplied)"
    lines = []
    for h in hypotheses:
        hid = h.get("id") or "?"
        statement = bounded(h.get("statement"), limit=400)
        lines.append(f"- id: {hid}\n  statement: {statement}")
    return "\n".join(lines)


def _evidence_lines(evidence: list[dict[str, object]]) -> str:
    if not evidence:
        return "(no evidence supplied)"
    lines = []
    for e in evidence:
        eid = e.get("id") or "?"
        summary = bounded(e.get("summary"), limit=300)
        operation = e.get("operation") or "?"
        lines.append(f"- evidence_id: {eid}\n  operation: {operation}\n  summary: {summary}")
    return "\n".join(lines)


def _relation_block() -> str:
    return (
        "relation ∈ SUPPORTS | CONTRADICTS | CONTEXT:\n"
        "- SUPPORTS: this evidence directly supports the hypothesis.\n"
        "- CONTRADICTS: this evidence directly contradicts the hypothesis.\n"
        "- CONTEXT: related context only (rule metadata, background) — never on its "
        "own enough to support or contradict.\n"
        "You must cite ONLY evidence ids from the supplied evidence list."
    )


def build_messages(
    request: AssessRequest, *, json_only: bool = False
) -> list[dict[str, str]]:
    task = f"""Your task is to assess the current hypotheses against the gathered evidence.

Investigation id: {request.investigation_id}

Hypotheses:
{_hypothesis_lines(request.hypotheses)}

Evidence:
{_evidence_lines(request.evidence)}

For EACH supplied hypothesis produce:
- hypothesis_id: the supplied id (never invent one).
- status: SUPPORTED | CONTRADICTED | UNRESOLVED.
- reason_summary: a short bounded justification.
- evidence_relations: the semantic relation of each relevant supplied evidence to
  this hypothesis. {_relation_block()}
  A SUPPORTED/CONTRADICTED verdict REQUIRES at least one SUPPORTS/CONTRADICTS
  relation; otherwise the hypothesis is UNRESOLVED.
- findings (optional): short evidence-grounded findings. Each finding's
  evidence_citations MUST be real supplied evidence ids.

Prefer UNRESOLVED over guessing when evidence is insufficient."""
    return [system_message(), user_message(task, json_only=json_only)]


def strict_schema() -> dict[str, object]:
    """The strict-json_schema for the assess candidate (provider wire shape)."""
    return ASSESSMENT_JSON_SCHEMA
