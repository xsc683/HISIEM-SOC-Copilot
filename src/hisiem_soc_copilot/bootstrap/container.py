"""Composition Root container.

Only this layer knows every concrete implementation at once (python-package-
boundary.md §21). It wires domain ports to infrastructure adapters and exposes the
application services the API routers depend on. Async resources (engines,
sessions, HTTP clients) are owned by the lifespan via open()/close().
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.requests import Request

from ..application.handlers.investigation import InvestigationCommandHandler
from ..application.handlers.workflow import InvestigationWorkflowHandler
from ..application.ports.durable import OutboxStore
from ..application.ports.hisiem import HisiemPort
from ..application.ports.model_provider import ModelProvider
from ..application.ports.trust import TrustedContextProvider
from ..application.ports.unit_of_work import UnitOfWork
from ..application.services.investigation_service import InvestigationReadService
from ..config import Settings
from ..domain.investigation.value_objects import BudgetLimits
from ..infrastructure.auth.header_provider import HeaderTrustedContextProvider
from ..infrastructure.durable.dispatcher import AsyncOutboxDispatcher
from ..infrastructure.durable.investigation_runner import (
    AsyncInvestigationGraphRunner,
)
from ..infrastructure.hisiem.adapter import HisiemHttpAdapter
from ..infrastructure.persistence.database import build_engine, build_session_factory
from ..infrastructure.persistence.repositories.durable import SqlAlchemyOutboxStore
from ..infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork


class Container:
    """Holds configured service instances for the FastAPI process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.copilot_engine: AsyncEngine | None = None
        self.copilot_sessions: async_sessionmaker[AsyncSession] | None = None
        self.hisiem_adapter: HisiemHttpAdapter | None = None
        self.dispatcher: AsyncOutboxDispatcher | None = None

    # --- async resource lifecycle (called from lifespan) ---
    async def open(self) -> None:
        self.copilot_engine = build_engine(self.settings.database)
        self.copilot_sessions = build_session_factory(self.copilot_engine)
        self.hisiem_adapter = HisiemHttpAdapter(settings=self.settings.hisiem)
        if self.settings.app.enable_dispatcher:
            self.dispatcher = self.outbox_dispatcher()
            await self.dispatcher.start()

    async def close(self) -> None:
        if self.dispatcher is not None:
            await self.dispatcher.stop()
        if self.hisiem_adapter is not None:
            await self.hisiem_adapter.close()
        if self.copilot_engine is not None:
            await self.copilot_engine.dispose()

    # --- service graph (valid only after open()) ---
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self.copilot_sessions is None:
            raise RuntimeError("container must be opened before use")
        return self.copilot_sessions

    def unit_of_work_factory(self) -> Callable[[], UnitOfWork]:
        if self.copilot_sessions is None:
            raise RuntimeError("container must be opened before use")
        sessions = self.copilot_sessions

        def _factory() -> UnitOfWork:
            return SqlAlchemyUnitOfWork(sessions)

        return _factory

    def unit_of_work(self) -> UnitOfWork:
        return self.unit_of_work_factory()()

    def outbox_store(self) -> OutboxStore:
        store: OutboxStore = SqlAlchemyOutboxStore(self.session_factory())
        return store

    def investigation_runner(
        self,
        *,
        hisiem: HisiemPort | None = None,
        model: ModelProvider | None = None,
    ) -> AsyncInvestigationGraphRunner:
        """Build the durable runner that executes one investigation's graph.

        ``hisiem`` / ``model`` default to the real HISIEM HTTP adapter and the
        deterministic scripted model; tests inject fakes to run the graph without
        a live HISIEM.
        """
        from ..agent.evidence.normalizer import EvidenceNormalizer
        from ..agent.graph.builder import build_investigation_graph
        from ..agent.graph.runtime import GraphRuntime
        from ..agent.tools.executor import ToolExecutor
        from ..agent.tools.registry import ToolRegistry
        from ..infrastructure.llm.scripted import ScriptedModelProvider

        uow_factory = self.unit_of_work_factory()
        workflow_handler = self.investigation_workflow_handler()
        hisiem_adapter = hisiem if hisiem is not None else self.hisiem()
        model_provider = model if model is not None else ScriptedModelProvider()

        def _runtime(tenant_id: str) -> GraphRuntime:
            return GraphRuntime(
                uow_factory=uow_factory,
                workflow_handler=workflow_handler,
                model=model_provider,
                executor=ToolExecutor(hisiem=hisiem_adapter),
                normalizer=EvidenceNormalizer(),
                registry=ToolRegistry(),
                hisiem=hisiem_adapter,
                tenant_id=tenant_id,
            )

        return AsyncInvestigationGraphRunner(
            unit_of_work_factory=uow_factory,
            workflow_handler=workflow_handler,
            runtime_factory=_runtime,
            compile_graph=build_investigation_graph,
            checkpoint_settings=self.settings.langgraph,
        )

    def outbox_dispatcher(
        self,
        *,
        hisiem: HisiemPort | None = None,
        model: ModelProvider | None = None,
    ) -> AsyncOutboxDispatcher:
        """Build the durable outbox dispatcher for the investigation runner."""
        from ..infrastructure.durable.dispatcher import SqlAlchemyOutboxResolver

        resolver = SqlAlchemyOutboxResolver(self.session_factory())
        runner = self.investigation_runner(hisiem=hisiem, model=model)
        return AsyncOutboxDispatcher(
            outbox_store=self.outbox_store(),
            resolver=resolver,
            runner=runner,
            worker_name="copilot-dispatcher",
        )

    def trusted_context_provider(self, request: Request) -> TrustedContextProvider | None:
        """Build a TrustedContextProvider for the request from configuration.

        Returns None when no provider is configured (``none`` default) so the API
        fails closed. Only the ``header`` dev/test adapter is selectable today; a
        production deployment must wire a real authenticator here.
        """
        mode = self.settings.auth.trusted_context_provider
        if mode == "header":
            return HeaderTrustedContextProvider(request)
        if mode == "none":
            return None
        raise RuntimeError(f"unknown trusted-context provider: {mode}")

    def investigation_command_handler(self) -> InvestigationCommandHandler:
        if self.hisiem_adapter is None:
            raise RuntimeError("container must be opened before use")
        return InvestigationCommandHandler(
            unit_of_work_factory=self.unit_of_work_factory(),
            hisiem=self.hisiem_adapter,
            budget_limits=_budget_limits(self.settings),
        )

    def investigation_workflow_handler(self) -> InvestigationWorkflowHandler:
        return InvestigationWorkflowHandler(
            unit_of_work_factory=self.unit_of_work_factory()
        )

    def hisiem(self) -> HisiemHttpAdapter:
        if self.hisiem_adapter is None:
            raise RuntimeError("container must be opened before use")
        return self.hisiem_adapter

    def investigation_read_service(self) -> InvestigationReadService:
        return InvestigationReadService(unit_of_work=self.unit_of_work())


def _budget_limits(settings: Settings) -> BudgetLimits:
    b = settings.agent_budget
    return BudgetLimits(
        max_steps=b.max_steps,
        max_tool_calls=b.max_tool_calls,
        max_llm_calls=b.max_llm_calls,
        max_llm_tokens=b.max_llm_tokens,
        max_duration_seconds=b.max_duration_seconds,
    )


@lru_cache(maxsize=1)
def build_container() -> Container:
    """Return the process-wide container (used by the app lifespan)."""
    from ..config import get_settings

    return Container(get_settings())
