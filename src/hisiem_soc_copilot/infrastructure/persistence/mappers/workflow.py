"""Explicit mappers for PlanRevision and HypothesisAssessment rows.

PlanRevision + PlanStep are immutable definitions. HypothesisAssessment is an
immutable revision; the assessment↔evidence links live in a composite-row table.
"""

from __future__ import annotations

from uuid import UUID, uuid5

from ....domain.investigation.entities import HypothesisAssessment, PlanRevision
from ..orm.evidence import HypothesisAssessmentRow
from ..orm.plan import PlanRevisionRow, PlanStepRow

_PLAN_STEP_NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def plan_revision_to_row(plan: PlanRevision) -> PlanRevisionRow:
    return PlanRevisionRow(
        id=plan.id,
        investigation_id=plan.investigation_id,
        revision=plan.revision,
        goal=plan.goal,
        generator_kind="system",
        created_at=plan.created_at,
    )


def plan_step_rows(plan: PlanRevision) -> list[PlanStepRow]:
    return [
        PlanStepRow(
            id=uuid5(_PLAN_STEP_NAMESPACE, f"{plan.id}:{step.step_id}"),
            plan_revision_id=plan.id,
            step_key=step.step_id,
            ordinal=step.ordinal,
            objective=step.objective,
        )
        for step in plan.steps
    ]


def hypothesis_assessment_to_row(
    assessment: HypothesisAssessment, *, investigation_id: UUID
) -> HypothesisAssessmentRow:
    return HypothesisAssessmentRow(
        id=assessment.id,
        investigation_id=investigation_id,
        hypothesis_id=assessment.hypothesis_id,
        revision=assessment.revision,
        status=assessment.status.value,
        reason_summary=assessment.reason_summary,
        created_at=assessment.created_at,
    )
