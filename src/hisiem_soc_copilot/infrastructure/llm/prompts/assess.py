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


def build_messages(
    request: AssessRequest, *, json_only: bool = False
) -> list[dict[str, str]]:
    task = f"""Your task is to assess the current hypotheses against the gathered evidence.

Investigation id: {request.investigation_id}

Hypotheses:
{_hypothesis_lines(request.hypotheses)}

Evidence:
{_evidence_lines(request.evidence)}

Return JSON EXACTLY in this shape (no extra keys, no renames):

{{"assessments": [
    {{"hypothesis_id": "<a supplied hypothesis id>",
      "status": "SUPPORTED | CONTRADICTED | UNRESOLVED",
      "reason_summary": "<short bounded justification>",
      "evidence_relations": [
        {{"evidence_id": "<supplied id>",
          "relation": "SUPPORTS | CONTRADICTS | CONTEXT"}}
      ]}}
  ],
  "findings": [
    {{"statement": "<short evidence-grounded finding>",
      "evidence_citations": ["<supplied evidence id>"]}}
  ]}}

Rules:
- "assessments" is an ARRAY; include one entry per supplied hypothesis (use the
  supplied hypothesis_id verbatim; never invent one).
- A SUPPORTED/CONTRADICTED verdict REQUIRES at least one SUPPORTS/CONTRADICTS
  relation to a supplied evidence id; otherwise the hypothesis is UNRESOLVED.
- "evidence_relations" cites ONLY supplied evidence ids.
- "findings" is an ARRAY of objects that each use the EXACT keys "statement" and
  "evidence_citations" (never "finding", "summary", or any other name). Each
  evidence_citations MUST be a real supplied evidence id. Emit an empty array when
  there are no grounded findings.
- Prefer UNRESOLVED over guessing when evidence is insufficient."""
    return [system_message(), user_message(task, json_only=json_only)]


def strict_schema() -> dict[str, object]:
    """The strict-json_schema for the assess candidate (provider wire shape)."""
    return ASSESSMENT_JSON_SCHEMA
