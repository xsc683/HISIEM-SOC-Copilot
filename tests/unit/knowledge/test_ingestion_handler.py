"""KnowledgeIngestionHandler tests over hand-written in-memory doubles.

The doubles are deliberately LOCAL to this module: they model the exact
repository surface, the projection state and the transaction boundary the
handler depends on, and both their stores and their call logs are the assertion
surface. A fake that drifts from the port contract therefore shows up as a
failing expectation rather than a silent pass.

Covers the two-phase contract (embed with no transaction open, then one short
transaction), idempotent re-ingestion, append-only version history, line-ending
and normalization fixed points, the ingestion bounds, embedding-profile
resolution and explicit switching, retirement, convergence on a concurrent
winner, the event ledger, and the fail-closed embedding-batch checks.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from hisiem_soc_copilot.application.commands.knowledge import (
    IngestKnowledgeDocument,
    IngestKnowledgeOutcome,
    RetireKnowledgeDocument,
    RetireKnowledgeOutcome,
)
from hisiem_soc_copilot.application.errors import (
    EmbeddingProfileSwitchRequiresReindexError,
    KnowledgeEmbeddingProfileError,
    KnowledgeIngestionConflictError,
    NotFoundError,
    SystemManagedKnowledgeSourceError,
)
from hisiem_soc_copilot.application.handlers.knowledge import (
    KnowledgeIngestionHandler,
    KnowledgeIngestionLimits,
)
from hisiem_soc_copilot.application.ports.durable import AppendableEvent
from hisiem_soc_copilot.application.ports.embedding import (
    EmbeddingBatch,
    EmbeddingProfileDescriptor,
    EmbeddingVector,
)
from hisiem_soc_copilot.application.ports.knowledge import (
    CHUNK_GENERATION_INITIAL,
    ChunkEmbeddingRecord,
    ChunkProjectionState,
    EmbeddingProfileRecord,
    KnowledgeChunkView,
    KnowledgeContentChunkRecord,
    LexicalCandidate,
    VectorCandidate,
)
from hisiem_soc_copilot.domain.knowledge.entities import (
    KnowledgeDocument,
    KnowledgeDocumentVersion,
)
from hisiem_soc_copilot.domain.knowledge.enums import (
    DocumentStatus,
    SourceKind,
    Visibility,
)
from hisiem_soc_copilot.domain.knowledge.errors import (
    InvalidEmbeddingVectorError,
    InvalidKnowledgeDocumentError,
    InvalidKnowledgeVersionError,
    KnowledgeBoundsExceededError,
    KnowledgeDocumentStateError,
)
from hisiem_soc_copilot.domain.knowledge.events import KnowledgeEvent
from hisiem_soc_copilot.domain.knowledge.value_objects import (
    ChunkerProfile,
    compute_content_hash,
    normalize_and_hash,
    normalize_knowledge_content,
)
from hisiem_soc_copilot.infrastructure.knowledge.chunker_port import StructureAwareChunker

TENANT = "tenant-a"
T0 = datetime(2026, 9, 12, 10, 0, 0, tzinfo=UTC)

PROVIDER = "openai-compatible"
MODEL_ID = "text-embedding-test"
DIMENSION = 4
OTHER_PROFILE_ID = UUID("44444444-4444-4444-8444-444444444444")

#: One heading plus one paragraph: the real chunker produces exactly one chunk.
CONTENT = "# Brute force\n\nAdversaries may use brute force to obtain credentials.\n"


# ---------------------------------------------------------------------------
# in-memory doubles
# ---------------------------------------------------------------------------


class FakeClock:
    """A frozen clock: ingestion timestamps must be reproducible."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def utc_now(self) -> datetime:
        return self._now


@dataclass(frozen=True)
class _ProviderCall:
    """One embed_documents call, plus the transaction state it observed."""

    texts: tuple[str, ...]
    open_transactions: int


class FakeEmbeddingProvider:
    """A deterministic EmbeddingProvider double that records every call.

    ``open_transactions`` is sampled from the shared runtime at the instant
    ``embed_documents`` runs. That is what makes the phase-discipline assertion
    possible: ingestion must never hold a transaction across a provider round
    trip (section 27).
    """

    def __init__(
        self,
        *,
        runtime: FakeRuntime,
        descriptor: EmbeddingProfileDescriptor | None = None,
        vectors: int | None = None,
        error: Exception | None = None,
        values_for: Callable[[int], tuple[float, ...]] | None = None,
    ) -> None:
        self._runtime = runtime
        self._descriptor = descriptor if descriptor is not None else _descriptor()
        self._vector_count = vectors
        self._error = error
        self._values_for = values_for
        #: When set, ``embed_documents`` returns this batch verbatim, which is how
        #: a test makes the provider lie about its own profile.
        self.batch_override: EmbeddingBatch | None = None
        self.calls: list[_ProviderCall] = []

    @property
    def descriptor(self) -> EmbeddingProfileDescriptor:
        return self._descriptor

    @property
    def document_calls(self) -> list[tuple[str, ...]]:
        return [call.texts for call in self.calls]

    async def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        self.calls.append(_ProviderCall(tuple(texts), self._runtime.open_transactions))
        if self._error is not None:
            raise self._error
        if self.batch_override is not None:
            return self.batch_override
        count = self._vector_count if self._vector_count is not None else len(texts)
        return EmbeddingBatch(
            descriptor=self._descriptor,
            vectors=tuple(self._vector(index) for index in range(count)),
        )

    def _vector(self, index: int) -> EmbeddingVector:
        values = (
            self._values_for(index)
            if self._values_for is not None
            else tuple(0.5 for _ in range(self._descriptor.dimension))
        )
        return EmbeddingVector(values=values, descriptor=self._descriptor, index=index)

    async def embed_query(self, text: str) -> EmbeddingVector:
        raise NotImplementedError("ingestion never embeds a query")


class FakeEventLedger:
    """Records (event, aggregate_revision) pairs; nothing else is needed."""

    def __init__(self) -> None:
        self.appended: list[tuple[AppendableEvent, int]] = []

    @property
    def event_types(self) -> list[str]:
        return [event.event_type for event, _ in self.appended]

    async def append(
        self,
        event: AppendableEvent,
        *,
        aggregate_revision: int,
        available_at: datetime | None = None,
    ) -> None:
        self.appended.append((event, aggregate_revision))

    async def get(self, *, event_id: UUID) -> None:
        raise NotImplementedError("ingestion only appends events")


class FakeKnowledgeDocumentRepository:
    """In-memory KnowledgeDocumentRepository double.

    ``find_overrides`` scripts the next ``find_by_external_key`` answers in
    order, which is how a test simulates a stale read-only pre-check followed by
    a concurrent winner becoming visible inside the transaction.
    """

    def __init__(self) -> None:
        self.documents: dict[UUID, KnowledgeDocument] = {}
        self.versions: dict[UUID, KnowledgeDocumentVersion] = {}
        self.added: list[UUID] = []
        self.saved: list[UUID] = []
        self.version_writes: list[UUID] = []
        self.find_by_external_key_calls = 0
        self.find_overrides: list[KnowledgeDocument | None] = []

    async def get(self, *, tenant_id: str, document_id: UUID) -> KnowledgeDocument | None:
        document = self.documents.get(document_id)
        if document is None or not document.is_visible_to(tenant_id):
            return None
        return document

    async def find_by_external_key(
        self,
        *,
        tenant_id: str | None,
        source_kind: SourceKind,
        external_key: str,
        visibility: Visibility,
    ) -> KnowledgeDocument | None:
        self.find_by_external_key_calls += 1
        if self.find_overrides:
            return self.find_overrides.pop(0)
        for document in self.documents.values():
            if (
                document.tenant_id == tenant_id
                and document.source_kind is source_kind
                and document.external_key == external_key
                and document.visibility is visibility
            ):
                return document
        return None

    async def add(self, *, document: KnowledgeDocument) -> None:
        self.added.append(document.id)
        self.documents[document.id] = document

    async def save(self, *, document: KnowledgeDocument) -> None:
        self.saved.append(document.id)
        self.documents[document.id] = document

    async def next_version_number(self, *, document_id: UUID) -> int:
        numbers = [
            version.version
            for version in self.versions.values()
            if version.document_id == document_id
        ]
        return max(numbers, default=0) + 1

    async def add_version(self, *, version: KnowledgeDocumentVersion) -> None:
        self.version_writes.append(version.id)
        self.versions[version.id] = version

    async def find_version_by_content_hash(
        self, *, document_id: UUID, content_hash: str
    ) -> KnowledgeDocumentVersion | None:
        for version in self.versions.values():
            if version.document_id == document_id and version.content_hash == content_hash:
                return version
        return None

    async def get_version(
        self, *, tenant_id: str, document_version_id: UUID
    ) -> KnowledgeDocumentVersion | None:
        return self.versions.get(document_version_id)

    async def list_versions(
        self, *, tenant_id: str, document_id: UUID
    ) -> tuple[KnowledgeDocumentVersion, ...]:
        return tuple(
            sorted(
                (
                    version
                    for version in self.versions.values()
                    if version.document_id == document_id
                ),
                key=lambda version: version.version,
            )
        )


class FakeKnowledgeChunkRepository:
    """In-memory KnowledgeChunkRepository double.

    Content chunks (the immutable citation targets) and embeddings (the
    rebuildable projection) live in SEPARATE maps, exactly as the two tables do,
    so a handler that re-embeds in place -- or that deletes content it should have
    kept -- is visible in the assertions instead of hidden behind one fused row.

    ``projection_state`` is DERIVED from the stored rows rather than scripted, so
    a handler that wrote chunks without a profile (or without a chunker version)
    fails the staleness comparison instead of passing a canned answer.
    """

    def __init__(self) -> None:
        self.chunks: dict[UUID, KnowledgeContentChunkRecord] = {}
        self.embeddings: dict[tuple[UUID, UUID], ChunkEmbeddingRecord] = {}
        self.added_batches: list[tuple[KnowledgeContentChunkRecord, ...]] = []
        self.added_embedding_batches: list[tuple[ChunkEmbeddingRecord, ...]] = []
        self.deleted_embeddings: list[tuple[UUID, int]] = []
        self.deleted_profiles: list[tuple[UUID, UUID]] = []

    # -- test-side helpers -------------------------------------------------

    def generation_of(self, document_version_id: UUID) -> int:
        generations = [
            chunk.generation
            for chunk in self.chunks.values()
            if chunk.document_version_id == document_version_id
        ]
        return max(generations) if generations else CHUNK_GENERATION_INITIAL

    def rows_for_version(
        self,
        document_version_id: UUID,
        generation: int | None = None,
    ) -> tuple[KnowledgeContentChunkRecord, ...]:
        """The version's content chunks, in ordinal order (default: current gen)."""
        if generation is None:
            generation = self.generation_of(document_version_id)
        return tuple(
            sorted(
                (
                    chunk
                    for chunk in self.chunks.values()
                    if chunk.document_version_id == document_version_id
                    and chunk.generation == generation
                ),
                key=lambda chunk: chunk.ordinal,
            )
        )

    def embeddings_for_version(
        self, document_version_id: UUID
    ) -> tuple[ChunkEmbeddingRecord, ...]:
        chunk_ids = {
            chunk.id
            for chunk in self.chunks.values()
            if chunk.document_version_id == document_version_id
        }
        return tuple(
            embedding
            for (chunk_id, _), embedding in self.embeddings.items()
            if chunk_id in chunk_ids
        )

    def profiles_for(self, chunk: KnowledgeContentChunkRecord) -> tuple[UUID, ...]:
        return tuple(
            sorted(
                embedding_profile_id
                for (chunk_id, embedding_profile_id) in self.embeddings
                if chunk_id == chunk.id
            )
        )

    # -- KnowledgeChunkRepository -----------------------------------------

    async def add_content_chunks(
        self, *, chunks: Sequence[KnowledgeContentChunkRecord]
    ) -> None:
        self.added_batches.append(tuple(chunks))
        for chunk in chunks:
            self.chunks[chunk.id] = chunk

    async def add_embeddings(self, *, embeddings: Sequence[ChunkEmbeddingRecord]) -> None:
        self.added_embedding_batches.append(tuple(embeddings))
        for embedding in embeddings:
            self.embeddings[
                (embedding.content_chunk_id, embedding.embedding_profile_id)
            ] = embedding

    async def count_for_version(self, *, document_version_id: UUID) -> int:
        return len(self.rows_for_version(document_version_id))

    async def projection_state(self, *, document_version_id: UUID) -> ChunkProjectionState:
        rows = self.rows_for_version(document_version_id)
        if not rows:
            return ChunkProjectionState(None, None, 0)
        per_profile: dict[UUID, int] = {}
        for row in rows:
            for profile_id in self.profiles_for(row):
                per_profile[profile_id] = per_profile.get(profile_id, 0) + 1
        # The ONE space that FULLY covers this generation, else None -- the same
        # rule the SQL probe implements, so an ambiguous or partial projection
        # reads as "not embedded" here too.
        covering = [
            profile_id for profile_id, count in per_profile.items() if count == len(rows)
        ]
        return ChunkProjectionState(
            embedding_profile_id=covering[0] if len(covering) == 1 else None,
            chunker_version=rows[0].chunker_version,
            chunk_count=len(rows),
            generation=rows[0].generation,
            embedding_count=sum(per_profile.values()),
        )

    async def list_content_chunks(
        self, *, document_version_id: UUID, generation: int
    ) -> tuple[KnowledgeContentChunkRecord, ...]:
        return self.rows_for_version(document_version_id, generation)

    async def delete_embeddings_for_version_generation(
        self, *, document_version_id: UUID, generation: int
    ) -> int:
        """Delete ONE generation's EMBEDDING rows; content chunks are untouched."""
        self.deleted_embeddings.append((document_version_id, generation))
        chunk_ids = {
            chunk.id for chunk in self.rows_for_version(document_version_id, generation)
        }
        removed = [key for key in self.embeddings if key[0] in chunk_ids]
        for key in removed:
            del self.embeddings[key]
        return len(removed)

    async def delete_embeddings_for_profile(
        self, *, document_version_id: UUID, embedding_profile_id: UUID
    ) -> int:
        self.deleted_profiles.append((document_version_id, embedding_profile_id))
        chunk_ids = {
            chunk.id
            for chunk in self.chunks.values()
            if chunk.document_version_id == document_version_id
        }
        removed = [
            key
            for key in self.embeddings
            if key[0] in chunk_ids and key[1] == embedding_profile_id
        ]
        for key in removed:
            del self.embeddings[key]
        return len(removed)

    async def lexical_candidates(
        self, *, tenant_id: str, search_terms: Sequence[str], limit: int
    ) -> tuple[LexicalCandidate, ...]:
        raise NotImplementedError("ingestion never searches")

    async def vector_candidates(
        self,
        *,
        tenant_id: str,
        embedding_profile_id: UUID,
        query_vector: Sequence[float],
        limit: int,
    ) -> tuple[VectorCandidate, ...]:
        raise NotImplementedError("ingestion never searches")

    async def get_chunk_view(
        self, *, tenant_id: str, chunk_id: UUID
    ) -> KnowledgeChunkView | None:
        raise NotImplementedError("ingestion never reads a chunk view")


class FakeEmbeddingProfileRepository:
    """In-memory EmbeddingProfileRepository double with at most one ACTIVE row."""

    def __init__(self, *, profiles: Sequence[EmbeddingProfileRecord] = ()) -> None:
        self.profiles: list[EmbeddingProfileRecord] = list(profiles)
        self.added: list[UUID] = []
        self.retired: list[datetime] = []

    async def get_active(self) -> EmbeddingProfileRecord | None:
        for profile in self.profiles:
            if profile.status == "ACTIVE":
                return profile
        return None

    async def find_by_identity(
        self,
        *,
        provider: str,
        model_id: str,
        dimension: int,
        distance_metric: str,
        normalization: str,
        profile_version: int,
    ) -> EmbeddingProfileRecord | None:
        identity = (
            provider,
            model_id,
            dimension,
            distance_metric,
            normalization,
            profile_version,
        )
        for profile in self.profiles:
            if (
                profile.provider,
                profile.model_id,
                profile.dimension,
                profile.distance_metric,
                profile.normalization,
                profile.profile_version,
            ) == identity:
                return profile
        return None

    async def add(self, *, profile: EmbeddingProfileRecord) -> None:
        self.added.append(profile.id)
        self.profiles.append(profile)

    async def retire_active(self, *, retired_at: datetime) -> None:
        self.retired.append(retired_at)
        self.profiles = [
            replace(profile, status="RETIRED", retired_at=retired_at)
            if profile.status == "ACTIVE"
            else profile
            for profile in self.profiles
        ]


@dataclass(frozen=True)
class _Snapshot:
    """The committed state of every data store at one instant."""

    documents: dict[UUID, KnowledgeDocument]
    versions: dict[UUID, KnowledgeDocumentVersion]
    chunks: dict[UUID, KnowledgeContentChunkRecord]
    embeddings: dict[tuple[UUID, UUID], ChunkEmbeddingRecord]
    profiles: list[EmbeddingProfileRecord]
    events: list[tuple[AppendableEvent, int]]


class FakeRuntime:
    """The stores plus the transaction counters shared by every fake UoW.

    ``open_transactions`` is the phase probe, ``uows_opened`` counts how many
    transactions the handler asked for (zero proves a rejection happened before
    any read), and ``commit_conflicts_remaining`` makes the next N commits lose a
    concurrent-writer race.
    """

    def __init__(self) -> None:
        self.documents = FakeKnowledgeDocumentRepository()
        self.chunks = FakeKnowledgeChunkRepository()
        self.profiles = FakeEmbeddingProfileRepository()
        self.events = FakeEventLedger()
        self.uows_opened = 0
        self.open_transactions = 0
        self.commits = 0
        self.rollbacks = 0
        self.commit_conflicts_remaining = 0

    def factory(self) -> FakeUnitOfWork:
        self.uows_opened += 1
        return FakeUnitOfWork(self)

    def snapshot(self) -> _Snapshot:
        return _Snapshot(
            documents=dict(self.documents.documents),
            versions=dict(self.documents.versions),
            chunks=dict(self.chunks.chunks),
            embeddings=dict(self.chunks.embeddings),
            profiles=list(self.profiles.profiles),
            events=list(self.events.appended),
        )

    def restore(self, snapshot: _Snapshot) -> None:
        _fill(self.documents.documents, snapshot.documents)
        _fill(self.documents.versions, snapshot.versions)
        _fill(self.chunks.chunks, snapshot.chunks)
        _fill(self.chunks.embeddings, snapshot.embeddings)
        _fill_list(self.profiles.profiles, snapshot.profiles)
        _fill_list(self.events.appended, snapshot.events)


class FakeUnitOfWork:
    """One fake transaction over the shared stores.

    Writes go straight through to the stores, and a transaction that exits
    WITHOUT a successful commit restores the snapshot it opened with -- the
    fake's model of a real ROLLBACK. That model is what makes the loser of a race
    observable: a failed commit must not leave the loser's half-written rows
    visible to its own re-read.
    """

    def __init__(self, runtime: FakeRuntime) -> None:
        self._runtime = runtime
        self._snapshot: _Snapshot | None = None
        self._committed = False
        self.knowledge_documents = runtime.documents
        self.knowledge_chunks = runtime.chunks
        self.embedding_profiles = runtime.profiles
        self.events = runtime.events

    async def __aenter__(self) -> FakeUnitOfWork:
        self._snapshot = self._runtime.snapshot()
        self._runtime.open_transactions += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._runtime.open_transactions -= 1
        if not self._committed and self._snapshot is not None:
            self._runtime.restore(self._snapshot)

    async def commit(self) -> None:
        if self._runtime.commit_conflicts_remaining > 0:
            self._runtime.commit_conflicts_remaining -= 1
            self._runtime.rollbacks += 1
            raise KnowledgeIngestionConflictError(
                "a concurrent knowledge write won the race on "
                "uq_knowledge_document_version_content_hash"
            )
        self._committed = True
        self._runtime.commits += 1

    async def rollback(self) -> None:
        self._runtime.rollbacks += 1

    async def close(self) -> None:
        return None


def _fill(target: dict[UUID, Any], source: dict[UUID, Any]) -> None:
    target.clear()
    target.update(source)


def _fill_list(target: list[Any], source: list[Any]) -> None:
    target.clear()
    target.extend(source)


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


class _ProviderExploded(RuntimeError):
    """A provider failure that is not an application error."""


def _descriptor(
    *,
    model_id: str = MODEL_ID,
    dimension: int = DIMENSION,
    normalization: str = "NONE",
) -> EmbeddingProfileDescriptor:
    return EmbeddingProfileDescriptor(
        provider=PROVIDER,
        model_id=model_id,
        dimension=dimension,
        distance_metric="COSINE",
        normalization=normalization,
        profile_version=1,
    )


def _profile(
    *,
    profile_id: UUID = OTHER_PROFILE_ID,
    status: str = "ACTIVE",
    model_id: str = MODEL_ID,
    dimension: int = DIMENSION,
    retired_at: datetime | None = None,
) -> EmbeddingProfileRecord:
    return EmbeddingProfileRecord(
        id=profile_id,
        provider=PROVIDER,
        model_id=model_id,
        dimension=dimension,
        distance_metric="COSINE",
        normalization="NONE",
        profile_version=1,
        status=status,
        created_at=T0,
        retired_at=retired_at,
    )


def _provider(
    runtime: FakeRuntime,
    *,
    descriptor: EmbeddingProfileDescriptor | None = None,
    vectors: int | None = None,
    error: Exception | None = None,
    values_for: Callable[[int], tuple[float, ...]] | None = None,
) -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider(
        runtime=runtime,
        descriptor=descriptor,
        vectors=vectors,
        error=error,
        values_for=values_for,
    )


def _handler(
    *,
    runtime: FakeRuntime,
    provider: FakeEmbeddingProvider | None = None,
    chunker: StructureAwareChunker | None = None,
    limits: KnowledgeIngestionLimits | None = None,
) -> KnowledgeIngestionHandler:
    return KnowledgeIngestionHandler(
        unit_of_work_factory=runtime.factory,
        embedding_provider=provider,
        chunker=chunker if chunker is not None else StructureAwareChunker(),
        clock=FakeClock(T0),
        limits=limits,
    )


def _command(
    *,
    content: str = CONTENT,
    external_key: str = "runbook-1",
    source_kind: SourceKind = SourceKind.TENANT_RUNBOOK,
    visibility: Visibility = Visibility.TENANT,
    tenant_id: str | None = TENANT,
    title: str = "Brute Force Runbook",
    source_version: str | None = "2026.09",
    metadata: dict[str, Any] | None = None,
    allow_embedding_profile_switch: bool = False,
) -> IngestKnowledgeDocument:
    return IngestKnowledgeDocument(
        source_kind=source_kind,
        external_key=external_key,
        visibility=visibility,
        title=title,
        content=content,
        tenant_id=tenant_id,
        source_version=source_version,
        metadata=dict(metadata or {}),
        allow_embedding_profile_switch=allow_embedding_profile_switch,
    )


def _seed_document(
    runtime: FakeRuntime,
    *,
    external_key: str = "runbook-1",
    tenant_id: str | None = TENANT,
    visibility: Visibility = Visibility.TENANT,
    title: str = "Brute Force Runbook",
) -> KnowledgeDocument:
    """Store an ACTIVE document directly, as an earlier command would have."""
    document = KnowledgeDocument.create(
        id=uuid4(),
        source_kind=SourceKind.TENANT_RUNBOOK,
        external_key=external_key,
        visibility=visibility,
        tenant_id=tenant_id,
        title=title,
        now=T0,
    )
    document.clear_events()
    runtime.documents.documents[document.id] = document
    return document


_SEEDED_CHUNK_CONTENT = "Adversaries may use brute force to obtain credentials."


def _chunk_record(
    *,
    document: KnowledgeDocument,
    version: KnowledgeDocumentVersion,
    chunker_version: str = "structure-aware-v1",
) -> KnowledgeContentChunkRecord:
    """An IMMUTABLE content chunk of ``version``, generation 1, ordinal 0."""
    return KnowledgeContentChunkRecord(
        id=uuid4(),
        document_id=document.id,
        document_version_id=version.id,
        generation=CHUNK_GENERATION_INITIAL,
        ordinal=0,
        heading_path="Brute force",
        content=_SEEDED_CHUNK_CONTENT,
        content_hash=compute_content_hash(_SEEDED_CHUNK_CONTENT),
        token_count=8,
        language="en",
        chunker_version=chunker_version,
        created_at=T0,
    )


def _embedding_record(
    *,
    chunk: KnowledgeContentChunkRecord,
    profile_id: UUID,
) -> ChunkEmbeddingRecord:
    """The REBUILDABLE projection of ``chunk`` in one embedding space."""
    return ChunkEmbeddingRecord(
        id=uuid4(),
        content_chunk_id=chunk.id,
        embedding_profile_id=profile_id,
        embedding=(0.5,) * DIMENSION,
        indexed_at=T0,
    )


def _multi_chunk_content() -> str:
    """Three sections: the real chunker emits one chunk per heading change."""
    return "# A\n\none\n\n# B\n\ntwo\n\n# C\n\nthree\n"


# ---------------------------------------------------------------------------
# a new document
# ---------------------------------------------------------------------------


async def test_a_new_document_creates_the_document_version_and_its_chunks() -> None:
    runtime = FakeRuntime()
    provider = _provider(runtime)
    handler = _handler(runtime=runtime, provider=provider)

    outcome = await handler.ingest(_command())

    assert isinstance(outcome, IngestKnowledgeOutcome)
    assert outcome.document_created is True
    assert outcome.version_created is True
    # projection_rebuilt reports whether an EXISTING version's chunk projection
    # had to be REGENERATED. A brand-new version writes a projection for the
    # first time, so nothing was rebuilt; see the stale-projection test below for
    # the True case.
    assert outcome.projection_rebuilt is False
    assert outcome.chunk_count == 1
    assert runtime.documents.added == [outcome.document.id]
    assert runtime.documents.version_writes == [outcome.version.id]
    assert [chunk.ordinal for chunk in runtime.chunks.rows_for_version(outcome.version.id)] == [0]
    assert provider.document_calls == [
        ("Adversaries may use brute force to obtain credentials.",)
    ]


async def test_the_new_document_is_active_and_points_at_its_first_version() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=_provider(runtime))

    outcome = await handler.ingest(_command(metadata={"topic": "credential-access"}))

    stored = await runtime.documents.get(tenant_id=TENANT, document_id=outcome.document.id)
    assert stored is outcome.document
    assert stored.status is DocumentStatus.ACTIVE
    assert stored.active_version_id == outcome.version.id
    assert stored.tenant_id == TENANT
    assert runtime.documents.saved == [outcome.document.id]

    normalized, content_hash = normalize_and_hash(CONTENT)
    assert outcome.version.version == 1
    assert outcome.version.document_id == stored.id
    assert outcome.version.normalized_content == normalized
    assert outcome.version.content_hash == content_hash
    assert outcome.version.title == "Brute Force Runbook"
    assert outcome.version.language == "en"
    assert outcome.version.source_version == "2026.09"
    assert outcome.version.metadata == {"topic": "credential-access"}
    assert outcome.version.ingested_at == T0


async def test_the_handler_exposes_the_chunker_version_it_stamped_on_every_chunk() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=_provider(runtime))

    outcome = await handler.ingest(_command())

    assert handler.chunker_version == "structure-aware-v1"
    rows = runtime.chunks.rows_for_version(outcome.version.id)
    assert rows
    for chunk in rows:
        assert chunk.chunker_version == handler.chunker_version


async def test_chunks_are_indexed_in_the_embedding_space_that_was_activated() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=_provider(runtime))

    outcome = await handler.ingest(_command())

    active = await runtime.profiles.get_active()
    assert active is not None
    assert runtime.profiles.added == [active.id]
    assert active.status == "ACTIVE"
    assert (active.provider, active.model_id, active.dimension) == (
        PROVIDER,
        MODEL_ID,
        DIMENSION,
    )
    rows = runtime.chunks.rows_for_version(outcome.version.id)
    assert rows
    for chunk in rows:
        assert chunk.language == "en"
        assert chunk.generation == CHUNK_GENERATION_INITIAL
    # The vectors live on the REBUILDABLE half, keyed by the content chunk they
    # describe -- the content row itself carries no embedding and no profile.
    embeddings = runtime.chunks.embeddings_for_version(outcome.version.id)
    assert len(embeddings) == len(rows)
    assert {embedding.content_chunk_id for embedding in embeddings} == {
        chunk.id for chunk in rows
    }
    for embedding in embeddings:
        assert embedding.embedding_profile_id == active.id
        assert embedding.embedding == (0.5,) * DIMENSION


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


async def test_reingesting_identical_content_reports_no_new_version_and_no_rebuild() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=_provider(runtime))
    first = await handler.ingest(_command())

    second = await handler.ingest(_command())

    assert second.document_created is False
    assert second.version_created is False
    assert second.projection_rebuilt is False
    assert second.chunk_count == first.chunk_count
    assert second.document.id == first.document.id
    assert second.version.id == first.version.id


async def test_reingesting_identical_content_skips_the_embedding_round_trip() -> None:
    runtime = FakeRuntime()
    provider = _provider(runtime)
    handler = _handler(runtime=runtime, provider=provider)

    await handler.ingest(_command())
    await handler.ingest(_command())

    # The read-only pre-check short-circuits, so the provider is asked exactly
    # once even though the command ran twice.
    assert len(provider.calls) == 1


async def test_reingesting_identical_content_writes_no_rows_and_appends_no_events() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=_provider(runtime))
    first = await handler.ingest(_command())

    await handler.ingest(_command())

    assert runtime.documents.version_writes == [first.version.id]
    assert runtime.documents.saved == [first.document.id]
    assert len(runtime.chunks.added_batches) == 1
    assert runtime.events.event_types == [
        "knowledge_document_created",
        "knowledge_document_version_ingested",
    ]


# ---------------------------------------------------------------------------
# changed content: version history is append-only
# ---------------------------------------------------------------------------


async def test_changed_content_appends_a_second_version_and_moves_the_active_pointer() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=_provider(runtime))
    first = await handler.ingest(_command())

    second = await handler.ingest(
        _command(content="# Brute force\n\nUpdated guidance for the sshd path.\n")
    )

    assert second.document_created is False
    assert second.version_created is True
    assert second.version.version == 2
    assert second.version.content_hash != first.version.content_hash
    assert second.document.active_version_id == second.version.id
    assert runtime.documents.version_writes == [first.version.id, second.version.id]
    assert len(runtime.documents.versions) == 2


async def test_the_superseded_version_row_is_left_completely_untouched() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=_provider(runtime))
    first = await handler.ingest(_command())
    snapshot = (
        first.version.version,
        first.version.content_hash,
        first.version.normalized_content,
        first.version.title,
    )

    await handler.ingest(_command(content="# Brute force\n\nA different body.\n"))

    stored = runtime.documents.versions[first.version.id]
    # The version is the SAME object: it was never rewritten, and the new ingest
    # did not touch version 1's row at all.
    assert stored is first.version
    assert (
        stored.version,
        stored.content_hash,
        stored.normalized_content,
        stored.title,
    ) == snapshot


# ---------------------------------------------------------------------------
# line endings and the normalization fixed point (section 8)
# ---------------------------------------------------------------------------


async def test_crlf_and_lf_content_hash_identically() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=_provider(runtime))
    lf_content = "# Brute force\n\nLine ending check.\n"

    crlf = await handler.ingest(
        _command(content=lf_content.replace("\n", "\r\n"), external_key="crlf-runbook")
    )
    lf = await handler.ingest(_command(content=lf_content, external_key="lf-runbook"))

    assert crlf.version.content_hash == lf.version.content_hash
    assert crlf.version.normalized_content == lf.version.normalized_content
    assert "\r" not in crlf.version.normalized_content


async def test_crlf_content_is_recognised_as_unchanged_on_reingest() -> None:
    runtime = FakeRuntime()
    provider = _provider(runtime)
    handler = _handler(runtime=runtime, provider=provider)
    lf_content = "# Brute force\n\nLine ending check.\n"
    first = await handler.ingest(_command(content=lf_content.replace("\n", "\r\n")))

    second = await handler.ingest(_command(content=lf_content))

    assert second.version_created is False
    assert second.version.id == first.version.id
    assert len(provider.calls) == 1


async def test_stored_normalized_content_is_a_normalization_fixed_point() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=_provider(runtime))

    outcome = await handler.ingest(
        _command(content="# Brute force\r\n\r\nBody with trailing space  \r\n")
    )

    renormalized, content_hash = normalize_and_hash(outcome.version.normalized_content)
    assert renormalized == outcome.version.normalized_content
    assert content_hash == outcome.version.content_hash


# ---------------------------------------------------------------------------
# phase discipline and atomicity (section 27)
# ---------------------------------------------------------------------------


async def test_the_embedding_round_trip_runs_with_no_transaction_open() -> None:
    runtime = FakeRuntime()
    provider = _provider(runtime)
    handler = _handler(runtime=runtime, provider=provider)

    await handler.ingest(_command())

    assert [call.open_transactions for call in provider.calls] == [0]
    assert runtime.open_transactions == 0
    assert runtime.commits == 1


async def test_a_provider_failure_leaves_no_document_version_or_chunk_behind() -> None:
    runtime = FakeRuntime()
    provider = _provider(runtime, error=_ProviderExploded("provider exploded"))
    handler = _handler(runtime=runtime, provider=provider)

    with pytest.raises(_ProviderExploded):
        await handler.ingest(_command())

    # Nothing was even ATTEMPTED: the document row, the version row and the
    # chunk rows are all written by phase 2, after the provider returns.
    assert runtime.documents.added == []
    assert runtime.documents.saved == []
    assert runtime.documents.version_writes == []
    assert runtime.chunks.added_batches == []
    assert runtime.documents.documents == {}
    assert runtime.documents.versions == {}
    assert runtime.chunks.chunks == {}
    assert runtime.events.appended == []
    assert runtime.commits == 0
    assert runtime.open_transactions == 0


# ---------------------------------------------------------------------------
# concurrency: converge on the winner, never duplicate it (section 28)
# ---------------------------------------------------------------------------


def _seed_winner(
    runtime: FakeRuntime,
    *,
    profile_id: UUID | None = None,
) -> tuple[KnowledgeDocument, KnowledgeDocumentVersion]:
    """Store the document/version/chunk a concurrent writer just committed.

    ``profile_id`` lets a test seed the corpus in an ALREADY-EXISTING embedding
    space instead of registering a fresh one.
    """
    normalized, content_hash = normalize_and_hash(CONTENT)
    document = _seed_document(runtime)
    version = KnowledgeDocumentVersion.create(
        id=uuid4(),
        document_id=document.id,
        version=1,
        normalized_content=normalized,
        content_hash=content_hash,
        title=document.title,
        source_version="2026.09",
        ingested_at=T0,
    )
    document.activate_version(
        version_id=version.id,
        version=1,
        content_hash=content_hash,
        title=version.title,
    )
    document.clear_events()
    runtime.documents.versions[version.id] = version
    if profile_id is None:
        profile_id = uuid4()
        runtime.profiles.profiles.append(_profile(profile_id=profile_id))
    record = _chunk_record(document=document, version=version)
    runtime.chunks.chunks[record.id] = record
    embedding = _embedding_record(chunk=record, profile_id=profile_id)
    runtime.chunks.embeddings[(embedding.content_chunk_id, embedding.embedding_profile_id)] = (
        embedding
    )
    return document, version


async def test_a_concurrent_winner_is_converged_on_instead_of_duplicated() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=_provider(runtime))
    winner_document, winner_version = _seed_winner(runtime)
    # The read-only pre-check sees a stale snapshot (no document yet); the read
    # INSIDE the transaction then sees the winner.
    runtime.documents.find_overrides = [None]

    outcome = await handler.ingest(_command())

    assert outcome.document_created is False
    assert outcome.version_created is False
    assert outcome.projection_rebuilt is False
    assert outcome.version.id == winner_version.id
    assert outcome.document.id == winner_document.id
    assert len(runtime.documents.documents) == 1
    assert len(runtime.documents.versions) == 1
    assert len(runtime.chunks.rows_for_version(winner_version.id)) == 1
    assert runtime.documents.version_writes == []
    assert runtime.profiles.added == []


async def test_a_lost_race_is_retried_once_and_then_succeeds() -> None:
    """A lost race costs a retried TRANSACTION, never a second embedding call.

    The handler embeds in phase 1b, outside any transaction, and carries the
    pre-computed batch into the retry loop. Only the (short) phase-2 work is
    repeated, so the batch is REUSED rather than recomputed -- a losing racer
    must not pay for the expensive half twice.
    """
    runtime = FakeRuntime()
    provider = _provider(runtime)
    handler = _handler(runtime=runtime, provider=provider)
    runtime.commit_conflicts_remaining = 1

    outcome = await handler.ingest(_command())

    # The convergence property, observed rather than assumed: exactly one
    # document, one version, and its chunks -- no duplicate of any of them.
    assert outcome.document_created is True
    assert outcome.version_created is True
    assert outcome.chunk_count == 1
    assert len(runtime.documents.documents) == 1
    assert len(runtime.documents.versions) == 1
    assert set(runtime.documents.versions) == {outcome.version.id}
    assert len(runtime.chunks.rows_for_version(outcome.version.id)) == 1
    # Both attempts WROTE a version row (the log is the call surface), but the
    # rolled-back one did not survive: exactly one version is committed.
    assert len(runtime.documents.version_writes) == 2
    # One failed attempt and one that committed, with no third transaction.
    assert runtime.commits == 1
    assert runtime.rollbacks == 1
    # 1 read-only pre-check + 2 attempts + 1 convergence re-read.
    assert runtime.uows_opened == 4
    assert len(provider.calls) == 1
    assert runtime.open_transactions == 0


async def test_a_lost_race_that_cannot_converge_raises_the_conflict() -> None:
    """A conflict that cannot be converged is raised, after bounded retries.

    Nothing here is a duplicate-avoidance success: the point is that the handler
    gives up DETERMINISTICALLY (exactly ``_CONFLICT_ATTEMPTS`` attempts, then
    ``KnowledgeIngestionConflictError``) and that the loser's own half-written
    rows were rolled back rather than left visible. The batch is computed once,
    outside the loop, so the retries do not re-embed.
    """
    runtime = FakeRuntime()
    provider = _provider(runtime)
    handler = _handler(runtime=runtime, provider=provider)
    runtime.commit_conflicts_remaining = 99

    with pytest.raises(KnowledgeIngestionConflictError) as excinfo:
        await handler.ingest(_command())

    assert excinfo.value.code == "KNOWLEDGE_INGESTION_CONFLICT"
    # Two attempts, two rolled-back commits, and no third attempt.
    assert runtime.commits == 0
    assert runtime.rollbacks == 2
    # 1 read-only pre-check + 2 attempts + 2 convergence re-reads.
    assert runtime.uows_opened == 5
    # The batch is reused across retries, so the provider is asked exactly once.
    assert len(provider.calls) == 1
    # The loser's own half-written rows were rolled back, not left behind.
    assert runtime.documents.documents == {}
    assert runtime.documents.versions == {}
    assert runtime.chunks.chunks == {}
    assert runtime.open_transactions == 0


# ---------------------------------------------------------------------------
# bounds are rejections, never truncations (section 25)
# ---------------------------------------------------------------------------


async def test_a_document_over_max_document_bytes_is_rejected() -> None:
    runtime = FakeRuntime()
    provider = _provider(runtime)
    content = "# Brute force\n\n" + "x" * 512 + "\n"
    handler = _handler(
        runtime=runtime,
        provider=provider,
        limits=KnowledgeIngestionLimits(max_document_bytes=64),
    )

    with pytest.raises(KnowledgeBoundsExceededError) as excinfo:
        await handler.ingest(_command(content=content))

    assert excinfo.value.code == "KNOWLEDGE_BOUNDS_EXCEEDED"
    assert excinfo.value.details == {
        "bound": "max_document_bytes",
        "limit": 64,
        "actual": len(content.encode("utf-8")),
    }
    # Rejected before anything was read or written.
    assert runtime.uows_opened == 0
    assert provider.calls == []
    assert runtime.documents.documents == {}


async def test_a_document_over_max_normalized_chars_is_rejected() -> None:
    runtime = FakeRuntime()
    provider = _provider(runtime)
    content = "# Brute force\n\n" + "y" * 200 + "\n"
    handler = _handler(
        runtime=runtime,
        provider=provider,
        limits=KnowledgeIngestionLimits(max_document_bytes=10_000, max_normalized_chars=32),
    )
    normalized, _ = normalize_and_hash(content)

    with pytest.raises(KnowledgeBoundsExceededError) as excinfo:
        await handler.ingest(_command(content=content))

    assert excinfo.value.details == {
        "bound": "max_normalized_chars",
        "limit": 32,
        "actual": len(normalized),
    }
    assert runtime.uows_opened == 0
    assert provider.calls == []
    assert runtime.documents.documents == {}


async def test_more_chunks_than_the_chunker_allows_is_rejected() -> None:
    runtime = FakeRuntime()
    provider = _provider(runtime)
    handler = _handler(
        runtime=runtime, provider=provider, chunker=StructureAwareChunker(max_chunks=2)
    )

    with pytest.raises(KnowledgeBoundsExceededError) as excinfo:
        await handler.ingest(_command(content=_multi_chunk_content()))

    assert excinfo.value.details == {
        "bound": "max_chunks_per_document",
        "limit": 2,
        "actual": 3,
    }
    # Chunking happens in phase 1b, before the provider round trip.
    assert provider.calls == []
    assert runtime.documents.documents == {}
    assert runtime.chunks.chunks == {}


async def test_a_line_over_the_chunk_size_bound_is_rejected() -> None:
    runtime = FakeRuntime()
    provider = _provider(runtime)
    content = "# Brute force\n\n" + "z" * 80 + "\n"
    handler = _handler(
        runtime=runtime, provider=provider, chunker=StructureAwareChunker(max_chunk_chars=32)
    )

    with pytest.raises(KnowledgeBoundsExceededError) as excinfo:
        await handler.ingest(_command(content=content))

    assert excinfo.value.details == {
        "bound": "max_chunk_chars",
        "limit": 32,
        "actual": 80,
    }
    assert provider.calls == []
    assert runtime.documents.documents == {}
    assert runtime.chunks.chunks == {}


# ---------------------------------------------------------------------------
# a missing embedding provider is a boundary, not a broken handler
# ---------------------------------------------------------------------------


async def test_ingest_without_an_embedding_provider_fails_before_reading_anything() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=None)

    with pytest.raises(KnowledgeEmbeddingProfileError) as excinfo:
        await handler.ingest(_command())

    assert excinfo.value.code == "KNOWLEDGE_EMBEDDING_PROFILE"
    assert "embedding provider" in str(excinfo.value)
    assert runtime.uows_opened == 0
    assert runtime.documents.documents == {}


# ---------------------------------------------------------------------------
# retirement
# ---------------------------------------------------------------------------


async def test_retire_works_without_any_embedding_configuration() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=None)
    document = _seed_document(runtime)

    outcome: RetireKnowledgeOutcome = await handler.retire(
        RetireKnowledgeDocument(tenant_id=TENANT, document_id=document.id)
    )

    assert outcome.already_retired is False
    assert outcome.document.status is DocumentStatus.RETIRED
    assert outcome.document.retired_at == T0
    assert runtime.documents.saved == [document.id]
    assert runtime.events.event_types == ["knowledge_document_retired"]


async def test_retire_is_idempotent_on_an_already_retired_document() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=None)
    document = _seed_document(runtime)
    first = await handler.retire(
        RetireKnowledgeDocument(tenant_id=TENANT, document_id=document.id)
    )
    revision_after_first = first.document.revision

    second = await handler.retire(
        RetireKnowledgeDocument(tenant_id=TENANT, document_id=document.id)
    )

    assert second.already_retired is True
    assert second.document.revision == revision_after_first
    assert runtime.documents.saved == [document.id]
    assert runtime.events.event_types == ["knowledge_document_retired"]


async def test_a_retired_document_cannot_be_un_retired() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=None)
    document = _seed_document(runtime)
    await handler.retire(RetireKnowledgeDocument(tenant_id=TENANT, document_id=document.id))

    with pytest.raises(KnowledgeDocumentStateError) as excinfo:
        document.retire(now=T0)

    assert excinfo.value.details["current_status"] == "RETIRED"
    assert document.status is DocumentStatus.RETIRED


async def test_retire_rejects_an_empty_tenant_id() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=None)

    with pytest.raises(InvalidKnowledgeDocumentError):
        await handler.retire(RetireKnowledgeDocument(tenant_id="   ", document_id=uuid4()))

    assert runtime.uows_opened == 0


async def test_retire_raises_not_found_for_an_unknown_document() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=None)

    with pytest.raises(NotFoundError) as excinfo:
        await handler.retire(RetireKnowledgeDocument(tenant_id=TENANT, document_id=uuid4()))

    assert excinfo.value.resource_type == "knowledge_document"


async def test_reingesting_into_a_retired_document_never_silently_reactivates_it() -> None:
    runtime = FakeRuntime()
    provider = _provider(runtime)
    handler = _handler(runtime=runtime, provider=provider)
    first = await handler.ingest(_command())
    await handler.retire(RetireKnowledgeDocument(tenant_id=TENANT, document_id=first.document.id))

    with pytest.raises(KnowledgeDocumentStateError) as excinfo:
        await handler.ingest(_command(content="# Brute force\n\nA different body.\n"))

    assert excinfo.value.details["command"] == "ingest"
    assert excinfo.value.details["current_status"] == "RETIRED"
    stored = await runtime.documents.get(tenant_id=TENANT, document_id=first.document.id)
    assert stored is not None
    assert stored.status is DocumentStatus.RETIRED
    assert stored.active_version_id == first.version.id
    assert runtime.documents.version_writes == [first.version.id]
    # A retired document is not a provable no-op, so the round trip is spent
    # before the state check refuses the write.
    assert len(provider.calls) == 2


# ---------------------------------------------------------------------------
# embedding profile resolution and switching (sections 16/18)
# ---------------------------------------------------------------------------


async def test_a_different_active_profile_is_refused_before_any_write() -> None:
    runtime = FakeRuntime()
    other = _profile(profile_id=OTHER_PROFILE_ID, model_id="text-embedding-other")
    runtime.profiles.profiles.append(other)
    handler = _handler(runtime=runtime, provider=_provider(runtime))

    with pytest.raises(KnowledgeEmbeddingProfileError) as excinfo:
        await handler.ingest(_command())

    assert "the ACTIVE embedding profile is" in str(excinfo.value)
    assert await runtime.profiles.get_active() == other
    assert runtime.profiles.added == []
    assert runtime.profiles.retired == []
    assert runtime.documents.added == []
    assert runtime.chunks.added_batches == []
    assert runtime.chunks.added_embedding_batches == []
    assert runtime.commits == 0


async def test_allow_embedding_profile_switch_performs_no_partial_switch() -> None:
    """Section 4.1: the flag is refused, and refusing leaves everything intact.

    A different ACTIVE embedding space is a CORPUS-WIDE reindex, never a side
    effect of one document's ingest. The old profile must still be the ACTIVE one
    afterwards and the corpus already stored under it must still be
    vector-retrievable -- which is exactly what a partial switch would destroy.
    """
    runtime = FakeRuntime()
    other = _profile(profile_id=OTHER_PROFILE_ID, model_id="text-embedding-other")
    runtime.profiles.profiles.append(other)
    # An existing corpus indexed in the OLD space.
    document, version = _seed_winner(runtime, profile_id=OTHER_PROFILE_ID)
    corpus_embeddings = runtime.chunks.embeddings_for_version(version.id)
    assert corpus_embeddings and all(
        embedding.embedding_profile_id == OTHER_PROFILE_ID
        for embedding in corpus_embeddings
    )
    handler = _handler(runtime=runtime, provider=_provider(runtime))

    with pytest.raises(EmbeddingProfileSwitchRequiresReindexError) as excinfo:
        await handler.ingest(_command(allow_embedding_profile_switch=True))

    assert excinfo.value.code == "EMBEDDING_PROFILE_SWITCH_REQUIRES_CORPUS_REINDEX"
    # No partial switch: nothing retired, nothing added, no ACTIVE profile moved.
    assert runtime.profiles.retired == []
    assert runtime.profiles.added == []
    active = await runtime.profiles.get_active()
    assert active is not None
    assert active.id == OTHER_PROFILE_ID
    assert sum(
        1 for profile in runtime.profiles.profiles if profile.status == "ACTIVE"
    ) == 1
    # The old corpus is untouched and still addressable in its own space.
    assert runtime.chunks.embeddings_for_version(version.id) == corpus_embeddings
    assert runtime.chunks.added_batches == []
    assert runtime.chunks.added_embedding_batches == []
    assert runtime.documents.added == []
    assert runtime.commits == 0
    assert version.id == document.active_version_id


async def test_a_retired_profile_for_the_same_identity_is_never_reactivated() -> None:
    runtime = FakeRuntime()
    runtime.profiles.profiles.append(_profile(status="RETIRED", retired_at=T0))
    handler = _handler(runtime=runtime, provider=_provider(runtime))

    with pytest.raises(KnowledgeEmbeddingProfileError) as excinfo:
        await handler.ingest(_command())

    assert "RETIRED" in str(excinfo.value)
    assert runtime.profiles.added == []
    assert runtime.documents.added == []
    assert runtime.commits == 0


async def test_a_missing_projection_is_rebuilt_without_moving_a_citation() -> None:
    """Section 3.2: a projection rebuild is invisible to every citation.

    Only the EMBEDDING half is dropped -- the state a reindex, a lost vector
    table, or a fresh embedding profile leaves behind. The content chunks keep
    their ids, so a citation captured before the rebuild still names exactly the
    same rows afterwards, and the immutable version row is never rewritten.
    """
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=_provider(runtime))
    first = await handler.ingest(_command())
    active = await runtime.profiles.get_active()
    assert active is not None
    before = {
        chunk.id: chunk.content_hash
        for chunk in runtime.chunks.rows_for_version(first.version.id)
    }
    assert before
    content_writes_before = len(runtime.chunks.added_batches)
    runtime.chunks.embeddings.clear()

    second = await handler.ingest(_command())

    assert second.version_created is False
    assert second.projection_rebuilt is True
    assert second.chunk_count == 1
    assert second.version.id == first.version.id
    # Re-embedded IN PLACE: not one content chunk was added, removed or rewritten.
    assert {
        chunk.id: chunk.content_hash
        for chunk in runtime.chunks.rows_for_version(first.version.id)
    } == before
    assert len(runtime.chunks.added_batches) == content_writes_before
    assert runtime.chunks.deleted_profiles == [(first.version.id, active.id)]
    assert len(runtime.chunks.embeddings_for_version(first.version.id)) == len(before)
    # Only the projection was regenerated; the immutable version row was not
    # rewritten by the second ingest.
    assert runtime.documents.version_writes == [first.version.id]


async def test_a_rechunk_appends_a_generation_and_leaves_the_old_one_standing() -> None:
    """Section 3.3: a chunker change must never delete historical content.

    The second ingest runs a NEW chunker version. Replacing the version's chunks
    would delete every citation target the first chunker produced, so instead the
    new chunking is appended as generation 2. Normal retrieval moves on to the
    newest generation; generation 1 stays exactly where it was.
    """
    runtime = FakeRuntime()
    provider = _provider(runtime)
    first = await _handler(runtime=runtime, provider=provider).ingest(_command())
    generation_one = runtime.chunks.rows_for_version(first.version.id, 1)
    assert [chunk.chunker_version for chunk in generation_one] == ["structure-aware-v1"]

    rechunking = _handler(
        runtime=runtime,
        provider=provider,
        chunker=StructureAwareChunker(
            profile=ChunkerProfile(chunker_version="structure-aware-v2")
        ),
    )
    assert rechunking.chunker_version == "structure-aware-v2"

    second = await rechunking.ingest(_command())

    assert second.version_created is False
    assert second.projection_rebuilt is True
    assert second.version.id == first.version.id
    # Generation 1 is untouched: same rows, same ids, same hashes.
    assert runtime.chunks.rows_for_version(first.version.id, 1) == generation_one
    generation_two = runtime.chunks.rows_for_version(first.version.id, 2)
    assert generation_two
    assert {chunk.chunker_version for chunk in generation_two} == {"structure-aware-v2"}
    assert runtime.chunks.generation_of(first.version.id) == 2
    # Everything is still stored: nothing was deleted to make room.
    assert len(runtime.chunks.chunks) == len(generation_one) + len(generation_two)
    # The projection normal retrieval reads now describes generation 2.
    state = await runtime.chunks.projection_state(document_version_id=first.version.id)
    assert state.generation == 2
    assert state.chunk_count == len(generation_two)


# ---------------------------------------------------------------------------
# the event ledger (section 29)
# ---------------------------------------------------------------------------


async def test_a_successful_ingest_appends_created_and_version_ingested_events() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=_provider(runtime))

    outcome = await handler.ingest(_command())

    assert runtime.events.event_types == [
        "knowledge_document_created",
        "knowledge_document_version_ingested",
    ]
    assert outcome.document.revision == 1
    assert [revision for _, revision in runtime.events.appended] == [1, 1]
    created, ingested = (event for event, _ in runtime.events.appended)
    assert isinstance(created, KnowledgeEvent)
    assert created.aggregate_id == outcome.document.id
    assert created.payload["external_key"] == "runbook-1"
    assert created.payload["source_kind"] == "TENANT_RUNBOOK"
    assert created.tenant_id == TENANT
    assert ingested.payload["document_version_id"] == str(outcome.version.id)
    assert ingested.payload["content_hash"] == outcome.version.content_hash


async def test_the_aggregate_revision_tracks_the_events_it_was_appended_with() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=_provider(runtime))
    first = await handler.ingest(_command())
    first_revision = first.document.revision

    second = await handler.ingest(_command(content="# Brute force\n\nA second body.\n"))

    # first.document IS the same aggregate the second ingest mutated, so the
    # revision is read out before the second command runs.
    assert first_revision == 1
    assert second.document.revision == 2
    assert [revision for _, revision in runtime.events.appended] == [1, 1, 2]


async def test_retire_appends_a_retired_event_with_the_aggregate_revision() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=None)
    document = _seed_document(runtime)

    outcome = await handler.retire(
        RetireKnowledgeDocument(tenant_id=TENANT, document_id=document.id)
    )

    assert runtime.events.event_types == ["knowledge_document_retired"]
    assert runtime.events.appended[0][1] == outcome.document.revision == 1
    assert runtime.events.appended[0][0].aggregate_id == document.id
    assert runtime.events.appended[0][0].tenant_id == TENANT


# ---------------------------------------------------------------------------
# a lying provider fails closed (section 69)
# ---------------------------------------------------------------------------


async def test_a_batch_with_fewer_vectors_than_chunks_fails_closed() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=_provider(runtime, vectors=2))

    with pytest.raises(InvalidKnowledgeVersionError) as excinfo:
        await handler.ingest(_command(content=_multi_chunk_content()))

    assert "returned 2 vectors for 3 chunks" in str(excinfo.value)
    assert runtime.documents.documents == {}
    assert runtime.chunks.chunks == {}
    assert runtime.commits == 0


async def test_a_batch_with_more_vectors_than_chunks_fails_closed() -> None:
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=_provider(runtime, vectors=2))

    with pytest.raises(InvalidKnowledgeVersionError) as excinfo:
        await handler.ingest(_command())

    assert "returned 2 vectors for 1 chunks" in str(excinfo.value)
    assert runtime.documents.documents == {}
    assert runtime.chunks.chunks == {}


async def test_a_batch_from_a_different_profile_than_declared_fails_closed() -> None:
    runtime = FakeRuntime()
    provider = _provider(runtime)
    other = _descriptor(model_id="text-embedding-other")
    provider.batch_override = EmbeddingBatch(
        descriptor=other,
        vectors=(EmbeddingVector(values=(0.5,) * DIMENSION, descriptor=other, index=0),),
    )
    handler = _handler(runtime=runtime, provider=provider)

    with pytest.raises(InvalidKnowledgeVersionError) as excinfo:
        await handler.ingest(_command())

    assert "different profile" in str(excinfo.value)
    assert runtime.documents.documents == {}
    assert runtime.profiles.added == []


async def test_a_non_finite_vector_fails_closed() -> None:
    runtime = FakeRuntime()
    handler = _handler(
        runtime=runtime,
        provider=_provider(
            runtime, values_for=lambda index: tuple(float("nan") for _ in range(DIMENSION))
        ),
    )

    with pytest.raises(InvalidEmbeddingVectorError) as excinfo:
        await handler.ingest(_command())

    assert excinfo.value.code == "INVALID_EMBEDDING_VECTOR"
    assert runtime.documents.documents == {}
    assert runtime.chunks.chunks == {}


async def test_a_vector_whose_dimension_disagrees_with_its_profile_fails_closed() -> None:
    runtime = FakeRuntime()
    handler = _handler(
        runtime=runtime,
        provider=_provider(
            runtime, values_for=lambda index: tuple(0.0 for _ in range(DIMENSION + 1))
        ),
    )

    with pytest.raises(InvalidEmbeddingVectorError) as excinfo:
        await handler.ingest(_command())

    assert "dimension" in str(excinfo.value)
    assert runtime.documents.documents == {}


# ---------------------------------------------------------------------------
# the handler's OWN chunk bounds, independent of the injected chunker
# ---------------------------------------------------------------------------

# Long enough to split into several chunks under the small profile below.
# The separator is spelled with ``chr(10)`` rather than an escape so the
# fixture contains real newlines however this file is transported.
_NL = chr(10)
MULTI_SECTION_CONTENT = (_NL * 2).join(
    f"## Section {index}" + _NL * 2 + " ".join(f"term{index}x{j}" for j in range(30))
    for index in range(6)
)


async def test_the_declared_chunk_count_bound_binds_even_without_chunker_limits() -> None:
    """A bound declared on the handler must BIND, not be decoration.

    The chunker enforces its own constructor limits, so a handler whose
    ``KnowledgeIngestionLimits`` say ``max_chunks_per_document=1`` would otherwise
    reject nothing: the chunker returns several chunks, the version is written,
    and the declared contract is a comment. This test injects a chunker with no
    limits of its own, so only the handler's bound can fire.
    """
    chunker = StructureAwareChunker(
        profile=ChunkerProfile(target_tokens=20, max_tokens=40, overlap_tokens=0)
    )
    normalized = normalize_knowledge_content(MULTI_SECTION_CONTENT)
    assert len(chunker.chunk_document(normalized)) > 1, "the fixture must actually split"

    runtime = FakeRuntime()
    handler = _handler(
        runtime=runtime,
        provider=_provider(runtime),
        chunker=chunker,
        limits=KnowledgeIngestionLimits(max_chunks_per_document=1),
    )

    with pytest.raises(KnowledgeBoundsExceededError) as excinfo:
        await handler.ingest(_command(content=MULTI_SECTION_CONTENT))

    assert "max_chunks_per_document" in str(excinfo.value)
    # Rejected before anything was written: a bound is not a truncation.
    assert runtime.documents.documents == {}
    assert runtime.documents.versions == {}
    assert runtime.chunks.chunks == {}


async def test_the_declared_chunk_size_bound_binds_even_without_chunker_limits() -> None:
    """The same rule for a single oversized chunk."""
    runtime = FakeRuntime()
    handler = _handler(
        runtime=runtime,
        provider=_provider(runtime),
        chunker=StructureAwareChunker(),
        limits=KnowledgeIngestionLimits(max_chunk_chars=10),
    )

    with pytest.raises(KnowledgeBoundsExceededError) as excinfo:
        await handler.ingest(_command())

    assert "max_chunk_chars" in str(excinfo.value)
    assert runtime.documents.documents == {}
    assert runtime.chunks.chunks == {}

# ---------------------------------------------------------------------------
# system-managed sources (closure-3 section 4)
# ---------------------------------------------------------------------------

async def test_the_ordinary_handler_refuses_a_mitre_ingest_before_any_write() -> None:
    """MITRE_ATTACK has exactly one writer: the ATT&CK import path.

    An ordinary handler given a MITRE command must refuse with
    SYSTEM_MANAGED_KNOWLEDGE_SOURCE, and the refusal must leave the stores
    untouched. The ATT&CK importer stages through a handler built with the MITRE
    capability instead -- that the importer still works is covered where the
    importer lives.
    """
    runtime = FakeRuntime()
    handler = _handler(runtime=runtime, provider=_provider(runtime))

    with pytest.raises(SystemManagedKnowledgeSourceError) as excinfo:
        await handler.ingest(
            _command(
                source_kind=SourceKind.MITRE_ATTACK,
                external_key="mitre-attack:T1110",
                visibility=Visibility.GLOBAL,
                tenant_id=None,
            )
        )

    assert excinfo.value.code == "SYSTEM_MANAGED_KNOWLEDGE_SOURCE"
    assert runtime.documents.documents == {}
    assert runtime.documents.versions == {}
    assert runtime.chunks.chunks == {}


async def test_the_ordinary_handler_cannot_retire_a_mitre_document() -> None:
    """Withdrawing the authoritative projection would orphan the release.

    The release would stay ACTIVE while retrieval serves nothing -- so the
    ordinary retire path refuses with the same code, and the document stays
    ACTIVE. MITRE lifecycle, if it ever needs one, belongs to an ATT&CK-specific
    workflow that does not exist yet.
    """
    runtime = FakeRuntime()
    document = KnowledgeDocument.create(
        id=uuid4(),
        source_kind=SourceKind.MITRE_ATTACK,
        external_key="mitre-attack:T1110",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        title="T1110: Brute Force",
        now=T0,
    )
    document.clear_events()
    runtime.documents.documents[document.id] = document
    handler = _handler(runtime=runtime, provider=_provider(runtime))

    with pytest.raises(SystemManagedKnowledgeSourceError) as excinfo:
        await handler.retire(
            RetireKnowledgeDocument(
                tenant_id=TENANT, document_id=document.id, reason="operator error"
            )
        )

    assert excinfo.value.code == "SYSTEM_MANAGED_KNOWLEDGE_SOURCE"
    assert document.status is DocumentStatus.ACTIVE


async def test_a_dedicated_handler_still_writes_mitre_when_wired_for_it() -> None:
    """The refusal is about the CAPABILITY, not the source kind.

    A handler built with the MITRE capability -- the shape the container's
    ``attack_projection_ingestion_handler`` builds -- stages a MITRE version
    without moving the pointer, proving the boundary is not a global MITRE ban.
    """
    runtime = FakeRuntime()
    handler = KnowledgeIngestionHandler(
        unit_of_work_factory=runtime.factory,
        embedding_provider=_provider(runtime),
        chunker=StructureAwareChunker(),
        clock=FakeClock(T0),
        allowed_source_kinds=frozenset({SourceKind.MITRE_ATTACK}),
    )

    outcome = await handler.ingest(
        IngestKnowledgeDocument(
            source_kind=SourceKind.MITRE_ATTACK,
            external_key="mitre-attack:T1110",
            visibility=Visibility.GLOBAL,
            title="T1110: Brute Force",
            content=CONTENT,
            tenant_id=None,
            activate_version=False,
        )
    )

    assert outcome.version_created is True
    assert outcome.document.source_kind is SourceKind.MITRE_ATTACK
