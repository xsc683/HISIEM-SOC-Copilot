"""Async investigation graph runner.

For one outbox record (an ``investigation_created`` domain event) the runner:

1. loads the current Domain investigation (short transactions);
2. refuses to re-run a terminal investigation (Domain wins over checkpoint);
3. ensures an OrchestrationBinding exists (deterministic thread_id);
4. bridges CREATED → RUNNING via the workflow start command;
5. compiles + runs the graph against the AsyncPostgresSaver checkpointer for the
   bound thread, resuming from whatever checkpoint already exists.

The dispatcher marks the outbox PUBLISHED after a successful run. Crash/replay
safety: workflow commands are receipt-idempotent (deterministic node keys) and
Evidence dedups, so a checkpointed resume never duplicates rows.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from ...agent.graph.builder import build_investigation_graph, thread_config
from ...agent.graph.runtime import GraphRuntime
from ...agent.graph.state import SCHEMA_VERSION
from ...application.commands.investigation import StartInvestigation
from ...application.handlers.workflow import InvestigationWorkflowHandler
from ...application.ports.durable import OrchestrationBinding
from ...application.ports.unit_of_work import UnitOfWork
from ...config import LangGraphSettings
from ...domain.investigation.enums import InvestigationStatus

GRAPH_NAME = "investigation"
GRAPH_VERSION = "v1"

# Callable building a GraphRuntime bound to a tenant (process-wide deps injected).
RuntimeFactory = Callable[[str], GraphRuntime]
# Callable compiling the graph with an optional checkpointer.
CompileGraph = Callable[[GraphRuntime, Any], Any]
# Async context manager factory yielding a checkpointer (LangGraph saver).
CheckpointerFactory = Callable[[], Any]


class AsyncInvestigationGraphRunner:
    """Runs one investigation to a terminal graph state over the Postgres saver."""

    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        workflow_handler: InvestigationWorkflowHandler,
        runtime_factory: RuntimeFactory,
        compile_graph: CompileGraph = build_investigation_graph,
        checkpoint_settings: LangGraphSettings | None = None,
        checkpointer_factory: CheckpointerFactory | None = None,
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._workflow_handler = workflow_handler
        self._runtime_factory = runtime_factory
        self._compile_graph = compile_graph
        self._checkpoint_settings = checkpoint_settings
        self._checkpointer_factory = checkpointer_factory

    async def run_investigation(
        self, *, investigation_id: str, tenant_id: str
    ) -> None:
        investigation_uuid = UUID(investigation_id)
        status = await self._domain_status(tenant_id, investigation_uuid)
        if status is None:
            return
        if InvestigationStatus(status).is_terminal:
            # Domain reconciliation: a terminal investigation is never re-run.
            return

        binding = await self._ensure_binding(tenant_id, investigation_uuid)

        if status == InvestigationStatus.CREATED.value:
            await self._workflow_handler.start_investigation(
                StartInvestigation(
                    tenant_id=tenant_id,
                    investigation_id=investigation_uuid,
                    idempotency_key=f"investigation:{investigation_uuid}:start",
                )
            )

        runtime = self._runtime_factory(tenant_id)
        checkpointer_ctx = (
            self._checkpointer_factory()
            if self._checkpointer_factory is not None
            else self._default_checkpointer()
        )
        async with checkpointer_ctx as saver:
            graph = self._compile_graph(runtime, saver)
            await graph.ainvoke(
                {"investigation_id": investigation_id},
                thread_config(binding.thread_id),
            )

    def _default_checkpointer(self) -> Any:
        from ..checkpoint.postgres import PostgresCheckpointer

        if self._checkpoint_settings is None:
            raise RuntimeError("checkpoint_settings is required without a checkpointer_factory")
        return PostgresCheckpointer(self._checkpoint_settings)

    async def _domain_status(
        self, tenant_id: str, investigation_id: UUID
    ) -> str | None:
        uow = self._uow_factory()
        try:
            investigation = await uow.investigations.get(
                tenant_id=tenant_id, investigation_id=investigation_id
            )
        finally:
            await uow.close()
        return investigation.status.value if investigation is not None else None

    async def _ensure_binding(
        self, tenant_id: str, investigation_id: UUID
    ) -> OrchestrationBinding:
        uow = self._uow_factory()
        try:
            binding = await uow.bindings.get(
                tenant_id=tenant_id, investigation_id=investigation_id
            )
            if binding is not None:
                return binding
            thread_id = f"inv:{investigation_id}"
            binding = OrchestrationBinding(
                investigation_id=investigation_id,
                thread_id=thread_id,
                graph_name=GRAPH_NAME,
                graph_version=GRAPH_VERSION,
                state_schema_version=SCHEMA_VERSION,
            )
            await uow.bindings.put(binding)
            await uow.commit()
            return binding
        finally:
            await uow.close()
