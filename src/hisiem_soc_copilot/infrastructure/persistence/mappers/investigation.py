"""Explicit mapper between the Investigation domain aggregate and its ORM row.

No ``from_attributes`` magic: ORM rows are translated to/from pure domain
dataclasses here (python-package-boundary.md §18).
"""

from __future__ import annotations

from ....domain.investigation.aggregate import Investigation
from ....domain.investigation.enums import (
    InvestigationPhase,
    InvestigationStatus,
    TerminationReason,
)
from ....domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from ..orm.investigation import InvestigationRow


def to_domain(row: InvestigationRow) -> Investigation:
    """Translate a persisted row into a fresh domain aggregate.

    Note: ``budget_limits`` and the source ref are reconstructed from stored
    columns; the alert tenant/actor are persisted snapshot columns.
    """
    budget = BudgetLimits(**row.budget_limits) if row.budget_limits else BudgetLimits()
    investigation = Investigation(
        id=row.id,
        tenant_id=row.tenant_id,
        source_alert_ref=ExternalResourceRef(
            provider=row.source_provider,
            resource_type=row.source_resource_type,
            address_id=row.source_address_id,
            business_id=row.source_business_id,
        ),
        initiated_by=ActorRef(
            subject_id=row.initiated_by_subject,
            tenant_id=row.tenant_id,
            display_name=row.initiated_by_display_name,
        ),
        status=InvestigationStatus(row.status),
        phase=InvestigationPhase(row.phase) if row.phase else None,
        current_plan_revision=row.current_plan_revision,
        budget_limits=budget,
        termination_reason=(
            TerminationReason(row.termination_reason)
            if row.termination_reason
            else None
        ),
        lock_version=row.lock_version,
        revision=row.revision,
        result_id=row.result_id,
        response_proposal_id=row.response_proposal_id,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        cancelled_at=row.cancelled_at,
    )
    # Rehydrate a domain aggregate must not carry stale pending events.
    investigation.clear_events()
    return investigation


def to_row(investigation: Investigation) -> InvestigationRow:
    """Translate a domain aggregate into a new ORM row for insert."""
    return InvestigationRow(
        id=investigation.id,
        tenant_id=investigation.tenant_id,
        source_provider=investigation.source_alert_ref.provider,
        source_resource_type=investigation.source_alert_ref.resource_type,
        source_address_id=investigation.source_alert_ref.address_id,
        source_business_id=investigation.source_alert_ref.business_id,
        initiated_by_subject=investigation.initiated_by.subject_id,
        initiated_by_display_name=investigation.initiated_by.display_name,
        status=investigation.status.value,
        phase=investigation.phase.value if investigation.phase else None,
        current_plan_revision=investigation.current_plan_revision,
        budget_limits=investigation.budget_limits.as_dict(),
        termination_reason=(
            investigation.termination_reason.value
            if investigation.termination_reason
            else None
        ),
        lock_version=investigation.lock_version,
        revision=investigation.revision,
        result_id=investigation.result_id,
        response_proposal_id=investigation.response_proposal_id,
        created_at=investigation.created_at,
        started_at=investigation.started_at,
        finished_at=investigation.finished_at,
        cancelled_at=investigation.cancelled_at,
    )
