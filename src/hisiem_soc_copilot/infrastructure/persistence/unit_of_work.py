"""SqlAlchemyUnitOfWork — the one-command-one-transaction boundary.

Each UoW instance wraps ONE AsyncSession (lazily connected) and therefore one DB
transaction. Application handlers never see the AsyncSession; they call
UoW.commit()/rollback(). The session is closed by close()/__aexit__.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...application.ports.repositories import (
    EvidenceRepository,
    FindingRepository,
    HypothesisRepository,
    InvestigationRepository,
    ResultRepository,
)
from .repositories.child import (
    SqlAlchemyEvidenceRepository,
    SqlAlchemyFindingRepository,
    SqlAlchemyHypothesisRepository,
    SqlAlchemyResultRepository,
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
        self.results: ResultRepository = SqlAlchemyResultRepository(self._session)

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
