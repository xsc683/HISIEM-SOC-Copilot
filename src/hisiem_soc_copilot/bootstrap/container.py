"""Composition Root container.

Only this layer knows every concrete implementation at once (python-package-
boundary.md §21). It wires domain ports to infrastructure adapters and exposes the
application services the API routers depend on. Async resources (engines,
sessions, HTTP clients) are owned by the lifespan via open()/close().
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.requests import Request

from ..application.handlers.investigation import InvestigationCommandHandler
from ..application.ports.trust import TrustedContextProvider
from ..application.ports.unit_of_work import UnitOfWork
from ..application.services.investigation_service import InvestigationReadService
from ..config import Settings
from ..domain.investigation.value_objects import BudgetLimits
from ..infrastructure.auth.header_provider import HeaderTrustedContextProvider
from ..infrastructure.hisiem.adapter import HisiemHttpAdapter
from ..infrastructure.persistence.database import build_engine, build_session_factory
from ..infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork


class Container:
    """Holds configured service instances for the FastAPI process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.copilot_engine: AsyncEngine | None = None
        self.copilot_sessions: async_sessionmaker[AsyncSession] | None = None
        self.hisiem_adapter: HisiemHttpAdapter | None = None

    # --- async resource lifecycle (called from lifespan) ---
    async def open(self) -> None:
        self.copilot_engine = build_engine(self.settings.database)
        self.copilot_sessions = build_session_factory(self.copilot_engine)
        self.hisiem_adapter = HisiemHttpAdapter(settings=self.settings.hisiem)

    async def close(self) -> None:
        if self.hisiem_adapter is not None:
            await self.hisiem_adapter.close()
        if self.copilot_engine is not None:
            await self.copilot_engine.dispose()

    # --- service graph (valid only after open()) ---
    def unit_of_work(self) -> UnitOfWork:
        if self.copilot_sessions is None:
            raise RuntimeError("container must be opened before use")
        return SqlAlchemyUnitOfWork(self.copilot_sessions)

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
            unit_of_work=self.unit_of_work(),
            hisiem=self.hisiem_adapter,
            budget_limits=_budget_limits(self.settings),
        )

    def investigation_read_service(self) -> InvestigationReadService:
        return InvestigationReadService(unit_of_work=self.unit_of_work())


def _budget_limits(settings: Settings) -> BudgetLimits:
    b = settings.agent_budget
    return BudgetLimits(
        max_steps=b.max_steps,
        max_tool_calls=b.max_tool_calls,
        max_llm_tokens=b.max_llm_tokens,
        max_duration_seconds=b.max_duration_seconds,
    )


@lru_cache(maxsize=1)
def build_container() -> Container:
    """Return the process-wide container (used by the app lifespan)."""
    from ..config import get_settings

    return Container(get_settings())
