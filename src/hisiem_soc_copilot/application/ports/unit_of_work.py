"""UnitOfWork port.

Application handlers never touch AsyncSession/Connection/SQL — they only ask this
UoW to commit, roll back, or close. The SqlAlchemyUnitOfWork in infrastructure
implements the real transaction boundary (persistence-schema.md §3,
python-package-boundary.md §8).
"""

from __future__ import annotations

from typing import Protocol

from .durable import (
    CommandReceiptStore,
    EventLedger,
    OrchestrationBindingStore,
    ToolInvocationStore,
)
from .repositories import (
    EvidenceRepository,
    FindingRepository,
    HypothesisAssessmentRepository,
    HypothesisRepository,
    InvestigationRepository,
    PlanRevisionRepository,
    ResultRepository,
)


class UnitOfWork(Protocol):
    """Groups repository access behind one transactional boundary.

    Repositories are typed with the Protocol (not the concrete implementation) so
    any structural implementation satisfies the port.

    The durable stores (events/outbox-receipt/binding/tool audit) are bound to the
    SAME transaction: rows they append commit atomically with the domain rows
    (persistence-schema.md §31).
    """

    investigations: InvestigationRepository
    evidence: EvidenceRepository
    findings: FindingRepository
    hypotheses: HypothesisRepository
    hypothesis_assessments: HypothesisAssessmentRepository
    plan_revisions: PlanRevisionRepository
    results: ResultRepository
    events: EventLedger
    command_receipts: CommandReceiptStore
    bindings: OrchestrationBindingStore
    tool_invocations: ToolInvocationStore

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def close(self) -> None: ...

    async def __aenter__(self) -> UnitOfWork: ...

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None: ...
