"""Durable runtime ports — the outbox-to-graph execution seam.

The Application layer only defines the shape; Infrastructure implements the real
runner (which owns graph/checkpointer/LLM/tool orchestration) and the dispatcher
worker. Unit tests drive a fake runner/dispatcher so command + outbox semantics
are testable without Postgres/LangGraph.
"""

from __future__ import annotations

from typing import Protocol


class InvestigationGraphRunner(Protocol):
    """Execute (start or resume) one investigation's graph to its terminal state.

    Callers never pass a DB transaction/session: the runner opens its own short
    transactions and treats the outbox record as an at-least-once dispatch signal
    (Domain state reconciliation keeps the result idempotent).
    """

    async def run_investigation(
        self, *, investigation_id: str, tenant_id: str
    ) -> None: ...


class DurableDispatcher(Protocol):
    """Claims ready outbox rows and hands each to the investigation runner.

    ``drain_once`` performs one claim + deliver cycle (used by tests and by the
    worker loop); implementations keep graph/LLM/tool calls outside DB transactions.
    """

    async def drain_once(self) -> int: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
