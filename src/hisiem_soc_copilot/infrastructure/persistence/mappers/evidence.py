"""Explicit mappers for Evidence / Finding / Hypothesis / Result rows.

Keeps the append-only child aggregates decoupled from their ORM rows.
"""

from __future__ import annotations

from uuid import UUID

from ....domain.investigation.entities import (
    Evidence,
    Finding,
    Hypothesis,
    InvestigationResult,
)
from ..orm.evidence import (
    EvidenceRow,
    FindingEvidenceRow,
    FindingRow,
    HypothesisRow,
)
from ..orm.result import InvestigationResultFindingRow, InvestigationResultRow


def evidence_to_row(evidence: Evidence) -> EvidenceRow:
    """Translate an Evidence domain object to a row for insert."""
    src = evidence.source_resource_ref
    return EvidenceRow(
        id=evidence.id,
        investigation_id=evidence.investigation_id,
        source_type=evidence.source.type.value,
        source_provider=evidence.source.provider,
        source_operation=evidence.source.operation,
        source_resource_provider=src.provider if src else None,
        source_resource_type=src.resource_type if src else None,
        source_resource_address_id=src.address_id if src else None,
        source_resource_business_id=src.business_id if src else None,
        source_tool_invocation_id=evidence.source_tool_call_id,
        observed_at=evidence.observed_at,
        collected_at=evidence.collected_at,
        observation=evidence.observation,
        summary=evidence.summary,
        raw_reference=evidence.raw_reference,
        entity_refs=[{"kind": e.kind, "value": e.value} for e in evidence.entity_refs],
        provenance_authority=evidence.provenance_authority.value,
        content_hash=evidence.content_hash or "",
        dedup_key=evidence.dedup_key or "",
    )


def finding_to_row(finding: Finding) -> FindingRow:
    return FindingRow(
        id=finding.id,
        investigation_id=finding.investigation_id,
        statement=finding.statement,
        created_at=finding.created_at,
    )


def finding_evidence_to_rows(
    finding_id: UUID, evidence_ids: list[UUID]
) -> list[FindingEvidenceRow]:
    return [
        FindingEvidenceRow(finding_id=finding_id, evidence_id=evidence_id)
        for evidence_id in evidence_ids
    ]


def hypothesis_to_row(hypothesis: Hypothesis) -> HypothesisRow:
    return HypothesisRow(
        id=hypothesis.id,
        investigation_id=hypothesis.investigation_id,
        statement=hypothesis.statement,
        current_status=hypothesis.status.value,
        assessment_revision=hypothesis.assessment_revision,
        created_at=hypothesis.created_at,
        updated_at=hypothesis.updated_at,
    )


def result_to_row(result: InvestigationResult) -> InvestigationResultRow:
    return InvestigationResultRow(
        id=result.id,
        investigation_id=result.investigation_id,
        verdict_disposition=result.verdict.disposition.value,
        verdict_summary=result.verdict.summary,
        confidence=result.verdict.confidence,
        uncertainties=[
            {
                "description": u.description,
                "missing_information": u.missing_information,
                "related_hypothesis_ids": [str(h) for h in u.related_hypothesis_ids],
            }
            for u in result.uncertainties
        ],
        attack_mappings=[
            {
                "framework": m.framework,
                "technique_id": m.technique_id,
                "name": m.name,
                "version": m.version,
                "source": m.source,
            }
            for m in result.attack_mappings
        ],
        response_recommendations=[
            {"description": r.description, "reason": r.reason}
            for r in result.response_recommendations
        ],
        content_hash=result.content_hash or "",
        created_at=result.created_at,
    )


def result_finding_rows(
    result_id: UUID, finding_ids: list[UUID]
) -> list[InvestigationResultFindingRow]:
    return [
        InvestigationResultFindingRow(result_id=result_id, finding_id=fid)
        for fid in finding_ids
    ]
