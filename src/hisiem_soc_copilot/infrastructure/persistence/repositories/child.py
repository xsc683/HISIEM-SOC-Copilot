"""SQLAlchemy repositories for append-only child entities.

These satisfy the application repository ports. They are intentionally thin:
rows are inserted as-is and read back for the workspace read models. The
immutability rules (no UPDATE) are enforced by never exposing an update path.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ....application.ports.repositories import (
    EvidenceRepository,
    FindingRepository,
    HypothesisRepository,
    ResultRepository,
)
from ....domain.investigation.entities import (
    Evidence,
    Finding,
    Hypothesis,
    InvestigationResult,
)
from ..mappers.evidence import (
    evidence_to_row,
    finding_evidence_to_rows,
    finding_to_row,
    hypothesis_to_row,
    result_finding_rows,
    result_to_row,
)
from ..orm.evidence import EvidenceRow, FindingRow, HypothesisRow
from ..orm.investigation import InvestigationRow
from ..orm.result import InvestigationResultRow


class SqlAlchemyEvidenceRepository(EvidenceRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, evidence: Evidence) -> None:
        self._session.add(evidence_to_row(evidence))

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[Evidence]:
        stmt = (
            select(EvidenceRow)
            .join(InvestigationRow, InvestigationRow.id == EvidenceRow.investigation_id)
            .where(
                InvestigationRow.tenant_id == tenant_id,
                EvidenceRow.investigation_id == investigation_id,
            )
        )
        result = await self._session.execute(stmt)
        return [_evidence_row_to_domain(row) for row in result.scalars().all()]


def _evidence_row_to_domain(row: EvidenceRow) -> Evidence:
    """Read an Evidence row back as a domain object.

    A full reverse mapper lives in mappers/evidence.py when the workspace read
    model needs complete fidelity; kept minimal here to stay honest.
    """
    from ....domain.investigation.entities import EntityRef, EvidenceSource
    from ....domain.investigation.enums import EvidenceSourceType
    from ....domain.investigation.value_objects import ExternalResourceRef

    src = None
    if row.source_resource_provider:
        src = ExternalResourceRef(
            provider=row.source_resource_provider,
            resource_type=row.source_resource_type or "",
            address_id=row.source_resource_address_id or "",
            business_id=row.source_resource_business_id,
        )
    return Evidence(
        id=row.id,
        investigation_id=row.investigation_id,
        source=EvidenceSource(
            type=EvidenceSourceType(row.source_type),
            provider=row.source_provider,
            operation=row.source_operation,
        ),
        collected_at=row.collected_at,
        observation=row.observation,
        source_resource_ref=src,
        source_tool_call_id=row.source_tool_invocation_id,
        observed_at=row.observed_at,
        summary=row.summary,
        raw_reference=row.raw_reference,
        entity_refs=[
            EntityRef(kind=item.get("kind", ""), value=item.get("value", ""))
            for item in (row.entity_refs or [])
        ],
        content_hash=row.content_hash,
        dedup_key=row.dedup_key,
    )


class SqlAlchemyFindingRepository(FindingRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, finding: Finding) -> None:
        self._session.add(finding_to_row(finding))
        for link in finding_evidence_to_rows(finding.id, finding.evidence_citations):
            self._session.add(link)

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[Finding]:
        rows = await self._session.execute(
            select(FindingRow)
            .join(InvestigationRow, InvestigationRow.id == FindingRow.investigation_id)
            .where(
                InvestigationRow.tenant_id == tenant_id,
                FindingRow.investigation_id == investigation_id,
            )
        )
        return [FindingRow_to_domain(r) for r in rows.scalars().all()]


def FindingRow_to_domain(row: FindingRow) -> Finding:
    from ....domain.investigation.entities import Finding as FindingDomain

    return FindingDomain(
        id=row.id,
        investigation_id=row.investigation_id,
        statement=row.statement,
        evidence_citations=[],  # links read separately by workspace query
        created_at=row.created_at,
    )


class SqlAlchemyHypothesisRepository(HypothesisRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, hypothesis: Hypothesis) -> None:
        self._session.add(hypothesis_to_row(hypothesis))

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[Hypothesis]:
        rows = await self._session.execute(
            select(HypothesisRow)
            .join(InvestigationRow, InvestigationRow.id == HypothesisRow.investigation_id)
            .where(
                InvestigationRow.tenant_id == tenant_id,
                HypothesisRow.investigation_id == investigation_id,
            )
        )
        return [HypothesisRow_to_domain(r) for r in rows.scalars().all()]


def HypothesisRow_to_domain(row: HypothesisRow) -> Hypothesis:
    from ....domain.investigation.enums import HypothesisStatus

    return Hypothesis(
        id=row.id,
        investigation_id=row.investigation_id,
        statement=row.statement,
        status=HypothesisStatus(row.current_status),
        assessment_revision=row.assessment_revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyResultRepository(ResultRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, result: InvestigationResult) -> None:
        self._session.add(result_to_row(result))
        for link in result_finding_rows(result.id, result.finding_ids):
            self._session.add(link)

    async def get_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> InvestigationResult | None:
        row = await self._session.execute(
            select(InvestigationResultRow)
            .join(InvestigationRow, InvestigationRow.id == InvestigationResultRow.investigation_id)
            .where(
                InvestigationRow.tenant_id == tenant_id,
                InvestigationResultRow.investigation_id == investigation_id,
            )
        )
        obj = row.scalar_one_or_none()
        return ResultRow_to_domain(obj) if obj is not None else None


def _str(value: object) -> str:
    return "" if value is None else str(value)


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def ResultRow_to_domain(row: InvestigationResultRow) -> InvestigationResult:
    from ....domain.investigation.entities import (
        AttackMapping,
        ResponseRecommendation,
        Uncertainty,
        Verdict,
    )
    from ....domain.investigation.enums import VerdictDisposition

    return InvestigationResult(
        id=row.id,
        investigation_id=row.investigation_id,
        verdict=Verdict(
            disposition=VerdictDisposition(row.verdict_disposition),
            summary=row.verdict_summary,
            confidence=row.confidence,
        ),
        finding_ids=[],  # links read separately
        uncertainties=[
            Uncertainty(
                description=_str(u["description"]),
                missing_information=_opt_str(u.get("missing_information")),
            )
            for u in row.uncertainties
        ],
        attack_mappings=[
            AttackMapping(
                framework=_str(m.get("framework", "mitre-attack")),
                technique_id=_opt_str(m.get("technique_id")),
                name=_opt_str(m.get("name")),
                version=_opt_str(m.get("version")),
                source=_opt_str(m.get("source")),
            )
            for m in row.attack_mappings
        ],
        response_recommendations=[
            ResponseRecommendation(
                description=_str(r["description"]), reason=_str(r["reason"])
            )
            for r in row.response_recommendations
        ],
        created_at=row.created_at,
        content_hash=row.content_hash,
    )
