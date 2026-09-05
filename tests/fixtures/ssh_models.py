"""Shared grounded SSH model for e2e/integration graph tests.

A real model must ground its SUPPORTED hypothesis AND its findings on specific
evidence ids it was actually shown. The scripted SSH scripts below reach a
MALICIOUS verdict with a Finding, so the assess override resolves the real
``authentication_success`` search event id from the node's AssessRequest and cites
it — exactly what a grounded provider would emit.
"""

from __future__ import annotations

from typing import Any

from hisiem_soc_copilot.application.ports.model_provider import AssessRequest
from hisiem_soc_copilot.contracts.llm.types import (
    AssessmentEvidenceRelation,
    AssessmentSummary,
    FindingCandidate,
    HypothesisAssessmentCandidate,
)
from hisiem_soc_copilot.infrastructure.llm.scripted import ScriptedModelProvider


class GroundedSshModel(ScriptedModelProvider):
    """Scripted model whose assess grounds on the real search-event evidence.

    ``decide`` / ``plan`` / ``verdict`` come from the script; ``assess`` is
    overridden to cite the observed evidence id (a ``search_events`` hit whose
    summary mentions the ``evidence_marker``, default ``authentication_success``)
    so the hypothesis and the Finding are both evidence-grounded — required before
    a MALICIOUS/BENIGN result may be finalized. When no matching evidence exists the
    hypothesis stays UNRESOLVED and no Finding is emitted.
    """

    def __init__(
        self,
        *,
        script: dict[str, Any] | None = None,
        evidence_marker: str = "authentication_success",
        hypothesis_relation: str = "SUPPORTS",
    ) -> None:
        super().__init__(script=script)
        self._marker = evidence_marker
        self._hypothesis_relation = hypothesis_relation
        self._finding_statement: str = (
            (script or {}).get("findings") or [
                "A successful root login followed the SSH brute-force failures "
                "from the same source IP"
            ]
        )[0]

    async def assess(self, request: AssessRequest) -> AssessmentSummary:
        evidence = request.evidence or []
        match_id: str | None = None
        for e in evidence:
            if e.get("operation") == "search_events" and self._marker in str(
                e.get("summary", "")
            ):
                match_id = str(e["id"])
                break

        assessments: list[HypothesisAssessmentCandidate] = []
        for hyp in request.hypotheses or []:
            if match_id:
                assessments.append(
                    HypothesisAssessmentCandidate(
                        hypothesis_id=str(hyp["id"]),
                        status=(
                            "SUPPORTED"
                            if self._hypothesis_relation == "SUPPORTS"
                            else "CONTRADICTED"
                        ),
                        reason_summary=(
                            "Observed evidence is relevant to the hypothesis"
                        ),
                        evidence_relations=[
                            AssessmentEvidenceRelation(
                                evidence_id=match_id,
                                relation=self._hypothesis_relation,  # type: ignore[arg-type]
                            )
                        ],
                    )
                )
            else:
                assessments.append(
                    HypothesisAssessmentCandidate(
                        hypothesis_id=str(hyp["id"]),
                        status="UNRESOLVED",
                        reason_summary="No matching evidence observed to ground a verdict",
                    )
                )

        findings: list[FindingCandidate] = []
        if match_id:
            findings.append(
                FindingCandidate(
                    statement=self._finding_statement,
                    evidence_citations=[match_id],
                )
            )
        return AssessmentSummary(
            decision="FINALIZE",
            assessments=assessments,
            findings=findings,
        )
