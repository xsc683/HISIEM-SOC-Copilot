"""SQLAlchemy repositories for append-only child entities + plan revisions.

These satisfy the application repository ports. They are intentionally thin:
rows are inserted as-is and read back for the workspace read models. The
immutability rules (no UPDATE) are enforced by never exposing an update path;
the one mutable exception is the ``hypothesis`` status/revision column which moves
with each appended assessment.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ....application.ports.repositories import (
    EvidenceRepository,
    FindingRepository,
    HypothesisAssessmentRepository,
    HypothesisRepository,
    PlanRevisionRepository,
    ResultRepository,
)
from ....domain.investigation.entities import (
    Evidence,
    Finding,
    Hypothesis,
    HypothesisAssessment,
    InvestigationResult,
    PlanRevision,
)
from ..mappers.evidence import (
    evidence_to_row,
    finding_evidence_to_rows,
    finding_to_row,
    hypothesis_to_row,
    result_finding_rows,
    result_to_row,
)
from ..mappers.workflow import (
    hypothesis_assessment_to_row,
    plan_revision_to_row,
    plan_step_rows,
)
from ..orm.evidence import (
    EvidenceRow,
    FindingEvidenceRow,
    FindingRow,
    HypothesisAssessmentEvidenceRow,
    HypothesisAssessmentRow,
    HypothesisRow,
)
from ..orm.investigation import InvestigationRow
from ..orm.plan import PlanRevisionRow, PlanStepRow
from ..orm.result import InvestigationResultFindingRow, InvestigationResultRow


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

    async def find_existing_dedup_keys(
        self, *, investigation_id: UUID, dedup_keys: list[str]
    ) -> set[str]:
        if not dedup_keys:
            return set()
        result = await self._session.execute(
            select(EvidenceRow.dedup_key).where(
                EvidenceRow.investigation_id == investigation_id,
                EvidenceRow.dedup_key.in_(dedup_keys),
            )
        )
        return {str(key) for key in result.scalars().all()}

    async def find_by_ids(
        self, *, tenant_id: str, investigation_id: UUID, evidence_ids: list[UUID]
    ) -> list[Evidence]:
        if not evidence_ids:
            return []
        stmt = (
            select(EvidenceRow)
            .join(InvestigationRow, InvestigationRow.id == EvidenceRow.investigation_id)
            .where(
                InvestigationRow.tenant_id == tenant_id,
                EvidenceRow.investigation_id == investigation_id,
                EvidenceRow.id.in_(evidence_ids),
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
        # Ensure the finding INSERT is flushed before its evidence-link INSERTs so a
        # later autoflush (e.g. a same-transaction UPDATE) cannot emit the links
        # first (same ordering hazard as hypothesis_assessment_evidence).
        await self._session.flush()
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
        findings = rows.scalars().all()
        citations = await self._citation_map(investigation_id=investigation_id)
        return [_finding_row_to_domain(r, citations) for r in findings]

    async def _citation_map(
        self, *, investigation_id: UUID
    ) -> dict[UUID, list[UUID]]:
        link_rows = await self._session.execute(
            select(
                FindingEvidenceRow.finding_id, FindingEvidenceRow.evidence_id
            )
            .join(FindingRow, FindingRow.id == FindingEvidenceRow.finding_id)
            .where(FindingRow.investigation_id == investigation_id)
        )
        mapping: dict[UUID, list[UUID]] = {}
        for row in link_rows.all():
            finding_id = row.finding_id
            mapping.setdefault(finding_id, []).append(row.evidence_id)
        return mapping


def _finding_row_to_domain(
    row: FindingRow, citations: dict[UUID, list[UUID]]
) -> Finding:
    return Finding(
        id=row.id,
        investigation_id=row.investigation_id,
        statement=row.statement,
        evidence_citations=citations.get(row.id, []),
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

    async def get(
        self, *, tenant_id: str, investigation_id: UUID, hypothesis_id: UUID
    ) -> Hypothesis | None:
        row = await self._session.execute(
            select(HypothesisRow)
            .join(InvestigationRow, InvestigationRow.id == HypothesisRow.investigation_id)
            .where(
                InvestigationRow.tenant_id == tenant_id,
                HypothesisRow.investigation_id == investigation_id,
                HypothesisRow.id == hypothesis_id,
            )
        )
        obj = row.scalar_one_or_none()
        return HypothesisRow_to_domain(obj) if obj is not None else None


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


class SqlAlchemyHypothesisAssessmentRepository(HypothesisAssessmentRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, assessment: HypothesisAssessment) -> None:
        self._session.add(
            hypothesis_assessment_to_row(
                assessment, investigation_id=assessment.investigation_id
            )
        )
        # Flush the assessment INSERT now so any evidence-link INSERTs in the SAME
        # transaction see their FK parent. SQLAlchemy cannot order two unrelated
        # pending rows (assessment vs hypothesis_assessment_evidence), so without
        # this an autoflush triggered by a later statement can emit the link first
        # and violate fk_hypothesis_assessment_evidence_assessment_id.
        await self._session.flush()

    async def add_evidence_links(
        self, assessment_id: UUID, evidence_relations: list[tuple[UUID, str]]
    ) -> None:
        for evidence_id, relation in evidence_relations:
            self._session.add(
                HypothesisAssessmentEvidenceRow(
                    assessment_id=assessment_id,
                    evidence_id=evidence_id,
                    relation=relation,
                )
            )

    async def update_hypothesis_status(
        self, *, hypothesis_id: UUID, status: str, assessment_revision: int
    ) -> None:
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime
        from typing import cast

        from sqlalchemy import CursorResult
        from sqlalchemy import update as sa_update

        from ....domain.shared.errors import OptimisticConcurrencyError
        from ..orm.evidence import HypothesisRow

        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                sa_update(HypothesisRow)
                .where(HypothesisRow.id == hypothesis_id)
                .values(
                    current_status=status,
                    assessment_revision=assessment_revision,
                    updated_at=_datetime.now(_UTC),
                )
            ),
        )
        if result.rowcount == 0:
            raise OptimisticConcurrencyError(
                aggregate_type="hypothesis", aggregate_id=str(hypothesis_id)
            )

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[HypothesisAssessment]:
        rows = await self._session.execute(
            select(HypothesisAssessmentRow)
            .join(
                InvestigationRow,
                InvestigationRow.id == HypothesisAssessmentRow.investigation_id,
            )
            .where(
                InvestigationRow.tenant_id == tenant_id,
                HypothesisAssessmentRow.investigation_id == investigation_id,
            )
        )
        assessments = rows.scalars().all()
        links = await self._link_map(investigation_id=investigation_id)
        return [_assessment_row_to_domain(a, links) for a in assessments]

    async def _link_map(
        self, *, investigation_id: UUID
    ) -> dict[UUID, list[tuple[UUID, str]]]:
        link_rows = await self._session.execute(
            select(
                HypothesisAssessmentEvidenceRow.assessment_id,
                HypothesisAssessmentEvidenceRow.evidence_id,
                HypothesisAssessmentEvidenceRow.relation,
            )
            .join(
                HypothesisAssessmentRow,
                HypothesisAssessmentRow.id
                == HypothesisAssessmentEvidenceRow.assessment_id,
            )
            .where(HypothesisAssessmentRow.investigation_id == investigation_id)
        )
        mapping: dict[UUID, list[tuple[UUID, str]]] = {}
        for row in link_rows.all():
            mapping.setdefault(row.assessment_id, []).append(
                (row.evidence_id, row.relation)
            )
        return mapping


def _assessment_row_to_domain(
    row: HypothesisAssessmentRow,
    links: dict[UUID, list[tuple[UUID, str]]],
) -> HypothesisAssessment:
    from ....domain.investigation.entities import HypothesisAssessmentEvidence
    from ....domain.investigation.enums import EvidenceRelation, HypothesisStatus

    return HypothesisAssessment(
        id=row.id,
        hypothesis_id=row.hypothesis_id,
        investigation_id=row.investigation_id,
        revision=row.revision,
        status=HypothesisStatus(row.status),
        evidence_relations=[
            HypothesisAssessmentEvidence(
                evidence_id=evidence_id,
                relation=EvidenceRelation(relation),
            )
            for evidence_id, relation in links.get(row.id, [])
        ],
        reason_summary=row.reason_summary,
        created_at=row.created_at,
    )


class SqlAlchemyPlanRevisionRepository(PlanRevisionRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, plan_revision: PlanRevision) -> None:
        self._session.add(plan_revision_to_row(plan_revision))
        for step in plan_step_rows(plan_revision):
            self._session.add(step)

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[PlanRevision]:
        rows = await self._session.execute(
            select(PlanRevisionRow)
            .join(
                InvestigationRow,
                InvestigationRow.id == PlanRevisionRow.investigation_id,
            )
            .where(
                InvestigationRow.tenant_id == tenant_id,
                PlanRevisionRow.investigation_id == investigation_id,
            )
        )
        revisions = rows.scalars().all()
        steps = await self._step_map(investigation_id=investigation_id)
        return [_plan_row_to_domain(r, steps) for r in revisions]

    async def _step_map(
        self, *, investigation_id: UUID
    ) -> dict[UUID, list[PlanStepRow]]:
        step_rows = await self._session.execute(
            select(PlanStepRow)
            .join(
                PlanRevisionRow,
                PlanRevisionRow.id == PlanStepRow.plan_revision_id,
            )
            .where(PlanRevisionRow.investigation_id == investigation_id)
            .order_by(PlanStepRow.ordinal)
        )
        mapping: dict[UUID, list[PlanStepRow]] = {}
        for row in step_rows.scalars().all():
            mapping.setdefault(row.plan_revision_id, []).append(row)
        return mapping


def _plan_row_to_domain(
    row: PlanRevisionRow, steps: dict[UUID, list[PlanStepRow]]
) -> PlanRevision:
    from ....domain.investigation.entities import PlanStep

    return PlanRevision(
        id=row.id,
        investigation_id=row.investigation_id,
        revision=row.revision,
        goal=row.goal,
        steps=[
            PlanStep(
                step_id=step.step_key,
                objective=step.objective,
                ordinal=step.ordinal,
            )
            for step in steps.get(row.id, [])
        ],
        generated_by=row.generator_kind,
        created_at=row.created_at,
    )


class SqlAlchemyResultRepository(ResultRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, result: InvestigationResult) -> None:
        self._session.add(result_to_row(result))
        # Ensure the result INSERT precedes its finding-link INSERTs when a later
        # autoflush fires (finalize_result also UPDATEs the investigation).
        await self._session.flush()
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
        if obj is None:
            return None
        finding_ids = await self._finding_ids(investigation_id=investigation_id)
        return ResultRow_to_domain(obj, finding_ids)

    async def _finding_ids(self, *, investigation_id: UUID) -> list[UUID]:
        rows = await self._session.execute(
            select(InvestigationResultFindingRow.finding_id)
            .join(
                InvestigationResultRow,
                InvestigationResultRow.id
                == InvestigationResultFindingRow.result_id,
            )
            .where(InvestigationResultRow.investigation_id == investigation_id)
        )
        return [UUID(str(f)) for f in rows.scalars().all()]


def _str(value: object) -> str:
    return "" if value is None else str(value)


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def ResultRow_to_domain(
    row: InvestigationResultRow, finding_ids: list[UUID] | None = None
) -> InvestigationResult:
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
        finding_ids=finding_ids or [],
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
