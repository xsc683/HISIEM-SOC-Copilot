"""SqlAlchemyUnitOfWork — the one-command-one-transaction boundary.

Each UoW instance wraps ONE AsyncSession (lazily connected) and therefore one DB
transaction. Application handlers never see the AsyncSession; they call
UoW.commit()/rollback(). The session is closed by close()/__aexit__.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...application.ports.durable import (
    CommandReceiptStore,
    EventLedger,
    OrchestrationBindingStore,
    ToolInvocationStore,
)
from ...application.ports.repositories import (
    EvidenceRepository,
    FindingRepository,
    HypothesisAssessmentRepository,
    HypothesisRepository,
    InvestigationRepository,
    PlanRevisionRepository,
    ResultRepository,
)
from .repositories.child import (
    SqlAlchemyEvidenceRepository,
    SqlAlchemyFindingRepository,
    SqlAlchemyHypothesisAssessmentRepository,
    SqlAlchemyHypothesisRepository,
    SqlAlchemyPlanRevisionRepository,
    SqlAlchemyResultRepository,
)
from .repositories.durable import (
    SqlAlchemyCommandReceiptStore,
    SqlAlchemyEventLedger,
    SqlAlchemyOrchestrationBindingStore,
    SqlAlchemyToolInvocationStore,
)
from .repositories.investigation import SqlAlchemyInvestigationRepository


class SqlAlchemyUnitOfWork:
    """Concrete UnitOfWork bound to an async session factory.

    A fresh session is created per instance. The handler decides when to commit;
    exiting/close() with a pending transaction rolls it back and returns the
    session to the pool. Repository attributes are typed as the Protocol so the
    class structurally satisfies the application UnitOfWork port.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session = session_factory()
        self.investigations: InvestigationRepository = SqlAlchemyInvestigationRepository(
            self._session
        )
        self.evidence: EvidenceRepository = SqlAlchemyEvidenceRepository(self._session)
        self.findings: FindingRepository = SqlAlchemyFindingRepository(self._session)
        self.hypotheses: HypothesisRepository = SqlAlchemyHypothesisRepository(
            self._session
        )
        self.hypothesis_assessments: HypothesisAssessmentRepository = (
            SqlAlchemyHypothesisAssessmentRepository(self._session)
        )
        self.plan_revisions: PlanRevisionRepository = SqlAlchemyPlanRevisionRepository(
            self._session
        )
        self.results: ResultRepository = SqlAlchemyResultRepository(self._session)
        # Durable stores bound to the SAME session/transaction as the domain rows.
        self.events: EventLedger = SqlAlchemyEventLedger(self._session)
        self.command_receipts: CommandReceiptStore = SqlAlchemyCommandReceiptStore(
            self._session
        )
        self.bindings: OrchestrationBindingStore = (
            SqlAlchemyOrchestrationBindingStore(self._session)
        )
        self.tool_invocations: ToolInvocationStore = SqlAlchemyToolInvocationStore(
            self._session
        )

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._session.rollback()
        await self._session.close()

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
