"""SqlAlchemyUnitOfWork — the one-command-one-transaction boundary.

Each UoW instance wraps ONE AsyncSession (lazily connected) and therefore one DB
transaction. Application handlers never see the AsyncSession; they call
UoW.commit()/rollback(). The session is closed by close()/__aexit__.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ...application.errors import CommandReceiptConflictError
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
from ...domain.investigation.errors import ActiveInvestigationExistsError
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

# The partial unique index that guarantees at most one Active Investigation per
# Tenant + Alert (persistence-schema.md §6). Only a conflict on THIS constraint is
# the concurrent-start convergence case; every other IntegrityError must propagate
# unchanged so a genuine data problem is never swallowed.
_ACTIVE_ALERT_CONSTRAINT = "uq_investigation_active_alert"

# The scoped command_receipt idempotency identity (tenant, command_type,
# idempotency_key). A concurrent same-key request that both pass the replay lookup
# collides HERE on commit — the handler must converge deterministically instead of
# leaking a raw IntegrityError. Every other IntegrityError propagates unchanged.
_RECEIPT_SCOPED_CONSTRAINT = "uq_command_receipt_tenant_command_key"


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
        try:
            await self._session.commit()
        except IntegrityError as exc:
            # Two benign concurrent races are translated so the handler can converge
            # deterministically (never leak a raw IntegrityError → HTTP 500):
            #   - the active-investigation partial unique-index race
            #   - the command_receipt scoped-unique idempotency race
            # All other IntegrityErrors propagate unchanged.
            await self._session.rollback()
            if _is_active_alert_conflict(exc):
                raise ActiveInvestigationExistsError(
                    alert_ref=_conflicting_address_id(exc)
                ) from exc
            if _is_receipt_scoped_conflict(exc):
                raise CommandReceiptConflictError(
                    "a concurrent request already recorded a command_receipt for "
                    "this tenant + command + Idempotency-Key"
                ) from exc
            raise

    async def rollback(self) -> None:
        await self._session.rollback()


def _is_active_alert_conflict(exc: IntegrityError) -> bool:
    """True only when the integrity error is the active-alert unique index.

    psycopg surfaces the violated constraint name on the wrapped exception; the
    SQLAlchemy IntegrityError is ``exc`` and the driver error is ``exc.orig``.
    """
    return _violates_constraint(exc, _ACTIVE_ALERT_CONSTRAINT)


def _is_receipt_scoped_conflict(exc: IntegrityError) -> bool:
    """True only when the integrity error is the command_receipt scoped unique."""
    return _violates_constraint(exc, _RECEIPT_SCOPED_CONSTRAINT)


def _violates_constraint(exc: IntegrityError, constraint: str) -> bool:
    orig = exc.orig
    name = getattr(orig, "constraint_name", None)
    if name == constraint:
        return True
    diag = getattr(orig, "diag", None)
    diag_name = getattr(diag, "constraint_name", None)
    return diag_name == constraint


def _conflicting_address_id(exc: IntegrityError) -> str:
    """Best-effort address id from the failed insert for a bounded error message."""
    params = exc.params
    if isinstance(params, dict):
        value = params.get("source_address_id")
        if value is not None:
            return str(value)
    return "unknown"
