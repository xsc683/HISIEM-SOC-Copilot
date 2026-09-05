"""Verdict prompt: AssessRequest → Chat messages.

Output: disposition ∈ MALICIOUS | BENIGN | INCONCLUSIVE + summary + confidence +
uncertainty. Insufficient evidence → INCONCLUSIVE; conflicting evidence → explicit
uncertainty; no grounded Finding → the application remains the final authority. No
chain-of-thought; only bounded reason summary / finding / verdict / uncertainty.
"""

from __future__ import annotations

from ....application.ports.model_provider import AssessRequest
from ..schemas import VERDICT_JSON_SCHEMA
from .common import bounded, system_message, user_message


def _finding_lines(finding_candidates: list[str]) -> str:
    if not finding_candidates:
        return "(no grounded findings recorded)"
    return "\n".join(f"- {bounded(f, limit=300)}" for f in finding_candidates)


def build_messages(
    request: AssessRequest, *, json_only: bool = False
) -> list[dict[str, str]]:
    evidence_summary = request.evidence_summary or []
    evidence_text = (
        "\n".join(f"- {bounded(e, limit=200)}" for e in evidence_summary)
        if evidence_summary
        else "(no evidence gathered)"
    )
    task = f"""Your task is to propose the final verdict disposition for one investigation.

Investigation id: {request.investigation_id}

Evidence gathered (bounded):
{evidence_text}

Grounded findings:
{_finding_lines(request.finding_candidates)}

Return:
- disposition: MALICIOUS | BENIGN | INCONCLUSIVE.
- summary: a short bounded reason summary.
- confidence: a number in [0.0, 1.0] expressing your bounded confidence.
- uncertainty: a short description of what remains uncertain/insufficient, or null
  when none.

Rules:
- Insufficient evidence → INCONCLUSIVE.
- Conflicting evidence → explicit uncertainty.
- A firm disposition (MALICIOUS/BENIGN) requires grounded findings that support it;
  otherwise prefer INCONCLUSIVE and say so in the summary.
- No chain-of-thought: output only the bounded structured result."""
    return [system_message(), user_message(task, json_only=json_only)]


def strict_schema() -> dict[str, object]:
    """The strict-json_schema for the verdict candidate (provider wire shape)."""
    return VERDICT_JSON_SCHEMA
