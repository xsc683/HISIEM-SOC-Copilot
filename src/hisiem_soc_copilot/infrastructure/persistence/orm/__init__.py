"""ORM model registry.

Importing this module registers every ``copilot`` schema table on the shared
``metadata``, which Alembic autogenerate and the UoW use.
"""

from __future__ import annotations

from .base import NAMING_CONVENTION, CopilotBase, metadata
from .events import (
    CommandReceiptRow,
    DomainEventRow,
    OrchestrationBindingRow,
    OutboxMessageRow,
    ToolInvocationRow,
)
from .evidence import (
    EvidenceRow,
    FindingEvidenceRow,
    FindingRow,
    HypothesisAssessmentEvidenceRow,
    HypothesisAssessmentRow,
    HypothesisRow,
)
from .investigation import InvestigationRow
from .plan import PlanRevisionRow, PlanStepRow, PlanStepStateRow
from .response import (
    ApprovalDecisionRow,
    ApprovalRequestRow,
    ResponseExecutionRefRow,
    ResponseProposalEvidenceRow,
    ResponseProposalRow,
    ResponseProposalTargetRow,
)
from .result import InvestigationResultFindingRow, InvestigationResultRow

__all__ = [
    "CopilotBase",
    "NAMING_CONVENTION",
    "metadata",
    "InvestigationRow",
    "PlanRevisionRow",
    "PlanStepRow",
    "PlanStepStateRow",
    "EvidenceRow",
    "HypothesisRow",
    "HypothesisAssessmentRow",
    "HypothesisAssessmentEvidenceRow",
    "FindingRow",
    "FindingEvidenceRow",
    "InvestigationResultRow",
    "InvestigationResultFindingRow",
    "ResponseProposalRow",
    "ResponseProposalTargetRow",
    "ResponseProposalEvidenceRow",
    "ApprovalRequestRow",
    "ApprovalDecisionRow",
    "ResponseExecutionRefRow",
    "OrchestrationBindingRow",
    "CommandReceiptRow",
    "DomainEventRow",
    "OutboxMessageRow",
    "ToolInvocationRow",
]
