"""Read-scoped UnitOfWork lifetime.

The knowledge read paths (``KnowledgeRetrievalService``, ``KnowledgeCitationResolver``)
are built on their OWN UnitOfWork so a slow read can never join -- or hold open --
a command's write transaction. That design has a consequence worth a test: no
command scope exists to close that UnitOfWork, so unless the container closes it,
its pooled connection is checked out, left INTRANS, and only released when the
garbage collector drops it. SQLAlchemy warns loudly in that case because the
connection cannot then be terminated safely.

These tests pin the container as the owner of that lifetime.
"""

from __future__ import annotations

import asyncio
from typing import Any

from hisiem_soc_copilot.bootstrap.container import Container
from hisiem_soc_copilot.config import Settings


class _SpyUnitOfWork:
    """Records whether the container closed it, and what the factories touched."""

    def __init__(self, closed: list[str]) -> None:
        self._closed = closed
        self.knowledge_chunks: Any = object()
        self.embedding_profiles: Any = object()

    async def close(self) -> None:
        self._closed.append("closed")


def _container_with_spies() -> tuple[Container, list[_SpyUnitOfWork], list[str]]:
    container = Container(Settings())
    made: list[_SpyUnitOfWork] = []
    closed: list[str] = []

    def factory() -> Any:
        uow = _SpyUnitOfWork(closed)
        made.append(uow)
        return uow

    container.unit_of_work = factory  # type: ignore[method-assign]
    return container, made, closed


def test_the_retrieval_service_registers_its_unit_of_work_for_closing() -> None:
    """Building a read path must hand the lifetime to the container."""
    container, made, closed = _container_with_spies()
    container.knowledge_retrieval_service()
    assert len(made) == 1
    assert closed == []
    asyncio.run(container.close())
    assert closed == ["closed"]


def test_the_citation_resolver_registers_its_unit_of_work_for_closing() -> None:
    """The same rule, for the other read path."""
    container, made, closed = _container_with_spies()
    container.knowledge_citation_resolver()
    assert len(made) == 1
    asyncio.run(container.close())
    assert closed == ["closed"]


def test_every_read_scoped_unit_of_work_is_closed_exactly_once() -> None:
    """Repeated reads accumulate; the container must release all of them."""
    container, made, closed = _container_with_spies()
    for _ in range(3):
        container.knowledge_citation_resolver()
        container.knowledge_retrieval_service()
    assert len(made) == 6
    asyncio.run(container.close())
    assert closed == ["closed"] * 6
    # The registry is emptied, so a second close is a no-op rather than a
    # double-close of a session that is already gone.
    asyncio.run(container.close())
    assert closed == ["closed"] * 6
