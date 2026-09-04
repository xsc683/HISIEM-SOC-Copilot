"""Graph runtime context — the composition injected into LangGraph nodes.

Nodes are thin (application-commands...md §20): they call Application commands /
Query ports and the ModelProvider. This context bundles the pieces the nodes need
so the compiled graph is a pure function of ``(state, config)`` over this context.

The workflow handler + unit-of-work factory + HISIEM adapter + model are
process-wide; the tenant is per-run (the authenticated orchestrator scope). Each
graph node opens a FRESH UnitOfWork via ``new_unit_of_work()`` so a full run spans
many independent transactions (one per node), matching how the handler works.
"""

from __future__ import annotations

from typing import Protocol

from ...application.handlers.workflow import InvestigationWorkflowHandler
from ...application.ports.hisiem import HisiemPort
from ...application.ports.model_provider import ModelProvider
from ...application.ports.unit_of_work import UnitOfWork
from ..evidence.normalizer import EvidenceNormalizer
from ..tools.executor import ToolExecutor
from ..tools.registry import ToolRegistry


class UnitOfWorkFactory(Protocol):
    """Builds a fresh UnitOfWork per transaction (never shares a session)."""

    def __call__(self) -> UnitOfWork: ...


class GraphRuntime:
    """Everything a node needs, bound at graph construction time."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        workflow_handler: InvestigationWorkflowHandler,
        model: ModelProvider,
        executor: ToolExecutor,
        normalizer: EvidenceNormalizer,
        registry: ToolRegistry,
        hisiem: HisiemPort,
        tenant_id: str,
    ) -> None:
        self.uow_factory = uow_factory
        self.workflow_handler = workflow_handler
        self.model = model
        self.executor = executor
        self.normalizer = normalizer
        self.registry = registry
        self.hisiem = hisiem
        self.tenant_id = tenant_id

    def new_unit_of_work(self) -> UnitOfWork:
        return self.uow_factory()
