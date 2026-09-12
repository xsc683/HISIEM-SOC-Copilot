"""The evaluation driver's corpus bootstrap.

``run_knowledge_evaluation`` has to be RE-RUNNABLE. The sealed fixture retires one
corpus document by design, and ``RETIRED`` is terminal -- the domain refuses
``RETIRED`` -> ingest, correctly. Without the driver accounting for that, a SECOND
``evaluate`` run dies part-way through the corpus with
``KnowledgeDocumentStateError``, and the operator is shown a domain rule that is
working exactly as intended as if it were the fault.

These tests pin the two halves of the fix: the lookup that finds an
already-retired corpus document, and the loop that carries it forward rather than
re-ingesting it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from hisiem_soc_copilot.domain.knowledge.enums import DocumentStatus
from hisiem_soc_copilot.domain.knowledge.errors import KnowledgeDocumentStateError
from hisiem_soc_copilot.evaluation.knowledge import CORPUS, RETIRED_DOCUMENT_KEYS
from hisiem_soc_copilot.knowledge.evaluation import (
    _corpus_documents_already_retired,
    _ingest_corpus,
)

CORPUS_KEYS = tuple(document.document_key for document in CORPUS)
A_RETAINED_KEY = next(key for key in CORPUS_KEYS if key not in RETIRED_DOCUMENT_KEYS)
THE_RETIRED_KEY = next(iter(RETIRED_DOCUMENT_KEYS))


class _FakeDocumentRepository:
    """Returns a RETIRED row for whatever keys the test declares retired."""

    def __init__(self, retired: dict[str, UUID]) -> None:
        self._retired = retired
        self.lookups: list[str] = []

    async def find_by_external_key(self, **kwargs: Any) -> Any:
        key = kwargs["external_key"]
        self.lookups.append(key)
        document_id = self._retired.get(key)
        if document_id is None:
            return None
        return SimpleNamespace(id=document_id, status=DocumentStatus.RETIRED)


class _FakeUnitOfWork:
    def __init__(self, repository: _FakeDocumentRepository) -> None:
        self.knowledge_documents = repository

    async def __aenter__(self) -> _FakeUnitOfWork:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeIngestionHandler:
    """Records ingests, and refuses a retired document the way the domain does."""

    def __init__(self, refuse: set[str]) -> None:
        self._refuse = refuse
        self.ingested: list[str] = []

    async def ingest(self, command: Any) -> Any:
        if command.external_key in self._refuse:
            raise KnowledgeDocumentStateError(
                document_id=uuid4(),
                current_status="RETIRED",
                command="ingest",
            )
        self.ingested.append(command.external_key)
        return SimpleNamespace(
            document=SimpleNamespace(id=uuid4()), chunk_count=1, version_created=True
        )


class _FakeContainer:
    def __init__(self, handler: _FakeIngestionHandler, repository: _FakeDocumentRepository):
        self._handler = handler
        self._repository = repository
        self.handler_builds: list[Any] = []

    def unit_of_work(self) -> _FakeUnitOfWork:
        return _FakeUnitOfWork(self._repository)

    def knowledge_ingestion_handler(self, **kwargs: Any) -> _FakeIngestionHandler:
        self.handler_builds.append(kwargs)
        return self._handler


@pytest.mark.asyncio
async def test_only_the_corpus_keys_the_fixture_retires_are_looked_up() -> None:
    """The lookup is scoped to the fixture's own retired set.

    An unrelated document found in a retired state must NOT be silently carried
    forward: that would be an unexpected corpus state, and the ingest below has to
    fail loudly on it rather than quietly skip it.
    """
    repository = _FakeDocumentRepository({THE_RETIRED_KEY: uuid4(), A_RETAINED_KEY: uuid4()})
    container = _FakeContainer(_FakeIngestionHandler(set()), repository)

    withdrawn = await _corpus_documents_already_retired(container)  # type: ignore[arg-type]

    assert set(withdrawn) == {THE_RETIRED_KEY}
    assert set(repository.lookups) == set(RETIRED_DOCUMENT_KEYS)


@pytest.mark.asyncio
async def test_an_already_retired_corpus_document_is_carried_forward() -> None:
    """The re-run must complete instead of dying on the fixture's own retirement."""
    retired_id = uuid4()
    repository = _FakeDocumentRepository({THE_RETIRED_KEY: retired_id})
    # The handler refuses the retired key exactly as the real domain does, so a
    # driver that did NOT skip would fail here rather than pass by luck.
    handler = _FakeIngestionHandler({THE_RETIRED_KEY})
    container = _FakeContainer(handler, repository)

    entries = await _ingest_corpus(container, provider=None, allow_switch=False)  # type: ignore[arg-type]

    assert THE_RETIRED_KEY not in handler.ingested
    assert len(entries) == len(CORPUS)
    # Carried forward under its EXISTING id, not a new one: the already-ingested
    # version is the one the suite scores against.
    assert retired_id in entries
    assert entries[retired_id].document_key == THE_RETIRED_KEY


@pytest.mark.asyncio
async def test_a_corpus_document_that_is_not_retired_is_still_ingested() -> None:
    """The skip is narrow: it must not become a general "skip what exists"."""
    repository = _FakeDocumentRepository({THE_RETIRED_KEY: uuid4()})
    handler = _FakeIngestionHandler({THE_RETIRED_KEY})
    container = _FakeContainer(handler, repository)

    await _ingest_corpus(container, provider=None, allow_switch=False)  # type: ignore[arg-type]

    assert set(handler.ingested) == set(CORPUS_KEYS) - set(RETIRED_DOCUMENT_KEYS)
    assert A_RETAINED_KEY in handler.ingested


@pytest.mark.asyncio
async def test_a_fresh_database_ingests_every_corpus_document() -> None:
    """First run: nothing is retired yet, so nothing is skipped."""
    repository = _FakeDocumentRepository({})
    handler = _FakeIngestionHandler(set())
    container = _FakeContainer(handler, repository)

    entries = await _ingest_corpus(container, provider=None, allow_switch=False)  # type: ignore[arg-type]

    assert set(handler.ingested) == set(CORPUS_KEYS)
    assert len(entries) == len(CORPUS)
