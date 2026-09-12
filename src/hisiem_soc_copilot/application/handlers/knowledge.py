"""Knowledge ingestion handler (brief sections 26-28, 38).

Two-phase by construction, and the phases are the point:

**Phase 1 -- outside any transaction.** Bound-check the raw input, normalize it,
hash it, chunk it, and call the embedding provider. These are CPU work plus a
network round trip that can take seconds or time out entirely. Holding a
PostgreSQL transaction open across them would pin a connection, hold locks, and
leave a half-written version behind on timeout (section 27 forbids exactly that).

**Phase 2 -- one short transaction.** Re-resolve the document, decide between
no-op / new version / projection rebuild, persist the version and its chunks, and
move the active pointer. Version, chunks, and pointer therefore converge
together or not at all.

Concurrency (section 28) is resolved by the DATABASE, not by hoping: the unique
constraints on ``(document_id, version)`` and ``(document_id, content_hash)`` are
the arbiter. A loser of the race does not leak an ``IntegrityError``; the
UnitOfWork translates the conflict into ``KnowledgeIngestionConflictError`` and
this handler re-reads. If the winner wrote the same content, the loser converges
on the winner's version and reports "no new version". If the content differed,
the conflict is real and is raised as a deterministic, retryable signal.

Authority: this handler writes knowledge truth and a rebuildable retrieval
projection. It grants nothing, emits no orchestration, and is reachable only
from the CLI in P3-A (sections 4/88).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import uuid4

from ...domain.knowledge.entities import KnowledgeDocument, KnowledgeDocumentVersion
from ...domain.knowledge.enums import DocumentStatus
from ...domain.knowledge.errors import (
    InvalidKnowledgeDocumentError,
    InvalidKnowledgeVersionError,
    KnowledgeBoundsExceededError,
    KnowledgeDocumentStateError,
)
from ...domain.knowledge.value_objects import normalize_and_hash
from ..commands.knowledge import (
    IngestKnowledgeDocument,
    IngestKnowledgeOutcome,
    RetireKnowledgeDocument,
    RetireKnowledgeOutcome,
)
from ..errors import (
    KnowledgeEmbeddingProfileError,
    KnowledgeIngestionConflictError,
    NotFoundError,
)
from ..ports.chunking import ChunkingPort, DocumentChunk
from ..ports.clock import ClockPort
from ..ports.embedding import EmbeddingBatch, EmbeddingProvider
from ..ports.knowledge import EmbeddingProfileRecord, KnowledgeChunkRecord
from ..ports.unit_of_work import UnitOfWork

#: How many times a losing racer re-reads and retries before reporting a
#: deterministic conflict. Two is enough for the ordinary "two operators ingested
#: the same file" case; a sustained stampede deserves an explicit conflict rather
#: than an unbounded retry loop.
_CONFLICT_ATTEMPTS = 2


@dataclass(frozen=True)
class KnowledgeIngestionLimits:
    """Explicit ingestion bounds (brief section 25).

    Every one of these is a REJECTION threshold. Silently truncating an oversized
    document would produce a version whose hash does not describe the bytes the
    operator supplied, which would quietly destroy provenance.
    """

    max_document_bytes: int = 2_000_000
    max_normalized_chars: int = 2_000_000
    max_chunks_per_document: int = 512
    max_chunk_chars: int = 8_000


class KnowledgeIngestionHandler:
    """Ingest and retire knowledge documents against short transactions.

    ``embedding_provider`` is OPTIONAL, and the reason is a boundary rather than
    a convenience: this handler owns two use cases, and only one of them embeds
    anything. Retiring a document is a lifecycle transition on rows that already
    exist, so requiring an embedding configuration for it would make an operator
    unable to withdraw knowledge during an embedding outage -- exactly when they
    are most likely to want to.

    The requirement therefore lives where it is actually true: ``ingest`` refuses
    without a provider, before it reads anything, rather than a factory refusing
    to build a handler that is perfectly capable of doing the other half of its
    job.
    """

    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        embedding_provider: EmbeddingProvider | None = None,
        chunker: ChunkingPort,
        clock: ClockPort,
        limits: KnowledgeIngestionLimits | None = None,
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._embedding_provider = embedding_provider
        self._chunker = chunker
        self._clock = clock
        self._limits = limits or KnowledgeIngestionLimits()

    @property
    def chunker_version(self) -> str:
        return self._chunker.chunker_version

    # ------------------------------------------------------------------
    # ingestion
    # ------------------------------------------------------------------
    async def ingest(self, command: IngestKnowledgeDocument) -> IngestKnowledgeOutcome:
        """Ingest one document body, idempotently and atomically.

        Fails closed on a missing embedding provider: every new version is
        indexed in an embedding space, so there is no way to ingest one without
        asking a provider for its identity -- and inventing vectors to get past
        that check is the one thing section 87 forbids outright.
        """
        provider = self._embedding_provider
        if provider is None:
            raise KnowledgeEmbeddingProfileError(
                "ingesting a document requires an embedding provider; configure "
                "EMBEDDING_PROVIDER, EMBEDDING_BASE_URL, EMBEDDING_MODEL and "
                "EMBEDDING_DIMENSION before ingesting knowledge"
            )
        self._require_bounds(command.content)
        normalized, content_hash = normalize_and_hash(command.content)
        if len(normalized) > self._limits.max_normalized_chars:
            raise KnowledgeBoundsExceededError(
                bound="max_normalized_chars",
                limit=self._limits.max_normalized_chars,
                actual=len(normalized),
            )

        expected_identity = provider.descriptor.identity

        # Phase 1a: a read-only pre-check. Re-ingesting an unchanged file is a
        # real operational case, so it should not cost an embedding round trip.
        early = await self._try_existing(command, content_hash, expected_identity)
        if early is not None:
            return early

        # Phase 1b: pure CPU plus external I/O. NO transaction is open here.
        chunks = self._chunk(normalized)
        batch = await provider.embed_documents([chunk.content for chunk in chunks])
        self._require_batch_matches(batch, expected_identity, len(chunks))

        # Phase 2: one short transaction, retried once if we lose a race.
        last_conflict: KnowledgeIngestionConflictError | None = None
        for _ in range(_CONFLICT_ATTEMPTS):
            try:
                async with self._uow_factory() as uow:
                    return await self._persist(
                        uow,
                        command=command,
                        normalized=normalized,
                        content_hash=content_hash,
                        chunks=chunks,
                        batch=batch,
                    )
            except KnowledgeIngestionConflictError as exc:
                last_conflict = exc
                converged = await self._try_existing(
                    command, content_hash, expected_identity
                )
                if converged is not None:
                    return converged
        if last_conflict is None:  # pragma: no cover - loop returns or sets it
            raise InvalidKnowledgeDocumentError("ingestion produced no outcome")
        raise last_conflict

    async def retire(self, command: RetireKnowledgeDocument) -> RetireKnowledgeOutcome:
        """Retire one document (terminal). Idempotent on an already-retired doc."""
        if not command.tenant_id.strip():
            raise InvalidKnowledgeDocumentError("tenant_id is required to retire")
        async with self._uow_factory() as uow:
            document = await uow.knowledge_documents.get(
                tenant_id=command.tenant_id, document_id=command.document_id
            )
            if document is None:
                raise NotFoundError(
                    "knowledge document not found",
                    resource_type="knowledge_document",
                    resource_id=str(command.document_id),
                )
            if document.status is DocumentStatus.RETIRED:
                return RetireKnowledgeOutcome(document=document, already_retired=True)
            document.retire(now=self._clock.utc_now())
            await uow.knowledge_documents.save(document=document)
            await self._flush_events(uow, document)
            await uow.commit()
        return RetireKnowledgeOutcome(document=document, already_retired=False)

    # ------------------------------------------------------------------
    # phase 2
    # ------------------------------------------------------------------
    async def _persist(
        self,
        uow: UnitOfWork,
        *,
        command: IngestKnowledgeDocument,
        normalized: str,
        content_hash: str,
        chunks: Sequence[DocumentChunk],
        batch: EmbeddingBatch,
    ) -> IngestKnowledgeOutcome:
        profile = await self._resolve_embedding_profile(uow, command, batch)

        document = await uow.knowledge_documents.find_by_external_key(
            tenant_id=command.tenant_id,
            source_kind=command.source_kind,
            external_key=command.external_key,
            visibility=command.visibility,
        )
        document_created = document is None
        if document is None:
            document = KnowledgeDocument.create(
                id=uuid4(),
                source_kind=command.source_kind,
                external_key=command.external_key,
                visibility=command.visibility,
                tenant_id=command.tenant_id,
                title=command.title,
                now=self._clock.utc_now(),
            )
            await uow.knowledge_documents.add(document=document)
        elif document.status is DocumentStatus.RETIRED:
            # Re-ingesting into a retired document would silently resurrect it in
            # every retrieval that can see it. Retirement is terminal, so the
            # operator must be told instead (section 6).
            raise KnowledgeDocumentStateError(
                document_id=document.id,
                current_status=document.status.value,
                command="ingest",
            )

        existing = await uow.knowledge_documents.find_version_by_content_hash(
            document_id=document.id, content_hash=content_hash
        )
        if existing is not None:
            return await self._converge_on_existing(
                uow,
                document=document,
                version=existing,
                profile=profile,
                chunks=chunks,
                batch=batch,
                document_created=document_created,
            )

        version = KnowledgeDocumentVersion.create(
            id=uuid4(),
            document_id=document.id,
            version=await uow.knowledge_documents.next_version_number(
                document_id=document.id
            ),
            normalized_content=normalized,
            content_hash=content_hash,
            title=command.title,
            language=command.language,
            source_version=command.source_version,
            metadata=dict(command.metadata),
            ingested_at=self._clock.utc_now(),
        )
        await uow.knowledge_documents.add_version(version=version)
        await uow.knowledge_chunks.add_many(
            chunks=self._records(
                document=document,
                version=version,
                chunks=chunks,
                batch=batch,
                profile=profile,
            )
        )
        document.activate_version(
            version_id=version.id,
            version=version.version,
            content_hash=version.content_hash,
            title=version.title,
        )
        await uow.knowledge_documents.save(document=document)
        await self._flush_events(uow, document)
        await uow.commit()
        return IngestKnowledgeOutcome(
            document=document,
            version=version,
            chunk_count=len(chunks),
            document_created=document_created,
            version_created=True,
            projection_rebuilt=False,
        )

    async def _converge_on_existing(
        self,
        uow: UnitOfWork,
        *,
        document: KnowledgeDocument,
        version: KnowledgeDocumentVersion,
        profile: EmbeddingProfileRecord,
        chunks: Sequence[DocumentChunk],
        batch: EmbeddingBatch,
        document_created: bool,
    ) -> IngestKnowledgeOutcome:
        """Handle content that is already versioned.

        The version row is IMMUTABLE, so the only thing that can legitimately
        change is the rebuildable projection. That happens when the ACTIVE
        embedding space or the chunker version moved on, and it is the reason
        ``chunker_version`` is persisted per chunk (section 22): without it a
        re-index would be indistinguishable from a fresh index.

        A matching version that is NOT the active one is left completely alone --
        it is history, and re-ingesting old content must never roll the active
        pointer backwards.
        """
        if version.id != document.active_version_id:
            return IngestKnowledgeOutcome(
                document=document,
                version=version,
                chunk_count=await uow.knowledge_chunks.count_for_version(
                    document_version_id=version.id
                ),
                document_created=document_created,
                version_created=False,
                projection_rebuilt=False,
            )

        state = await uow.knowledge_chunks.projection_state(
            document_version_id=version.id
        )
        stale = (
            state.is_empty
            or state.embedding_profile_id != profile.id
            or state.chunker_version != self._chunker.chunker_version
        )
        if not stale:
            return IngestKnowledgeOutcome(
                document=document,
                version=version,
                chunk_count=state.chunk_count,
                document_created=document_created,
                version_created=False,
                projection_rebuilt=False,
            )

        # The projection is stale. Rebuild it wholesale rather than patching: a
        # partial re-index would mix two chunkers or two vector spaces inside one
        # version, which is exactly the state the ACTIVE-profile rule exists to
        # make impossible (section 16).
        await uow.knowledge_chunks.delete_for_version(document_version_id=version.id)
        await uow.knowledge_chunks.add_many(
            chunks=self._records(
                document=document,
                version=version,
                chunks=chunks,
                batch=batch,
                profile=profile,
            )
        )
        await uow.commit()
        return IngestKnowledgeOutcome(
            document=document,
            version=version,
            chunk_count=len(chunks),
            document_created=document_created,
            version_created=False,
            projection_rebuilt=True,
        )

    # ------------------------------------------------------------------
    # embedding profile resolution
    # ------------------------------------------------------------------
    async def _resolve_embedding_profile(
        self,
        uow: UnitOfWork,
        command: IngestKnowledgeDocument,
        batch: EmbeddingBatch,
    ) -> EmbeddingProfileRecord:
        """Resolve (or create) the ACTIVE embedding space for this ingestion.

        The identity comes from what the provider ACTUALLY returned, not from
        configuration, so the persisted profile can never disagree with the
        vectors stored beside it.
        """
        descriptor = batch.descriptor
        existing = await uow.embedding_profiles.find_by_identity(
            provider=descriptor.provider,
            model_id=descriptor.model_id,
            dimension=descriptor.dimension,
            distance_metric=descriptor.distance_metric,
            normalization=descriptor.normalization,
            profile_version=descriptor.profile_version,
        )
        if existing is not None:
            if existing.status == "ACTIVE":
                return existing
            raise KnowledgeEmbeddingProfileError(
                "the embedding profile for this provider exists but is RETIRED; "
                "activate an embedding profile explicitly before ingesting "
                "(a retired profile is never re-activated implicitly)"
            )

        active = await uow.embedding_profiles.get_active()
        if active is not None and not command.allow_embedding_profile_switch:
            # Refusing is the safe default: switching spaces retires the old
            # profile, and every chunk indexed under it stops being returned by
            # retrieval until it is re-ingested. That is an operator decision, not
            # a side effect of uploading a file (sections 16/18).
            raise KnowledgeEmbeddingProfileError(
                "a different embedding profile is already ACTIVE; ingesting in a "
                "new embedding space would make every chunk indexed under the "
                "current profile unreachable by retrieval. Re-run with "
                "allow_embedding_profile_switch to retire the current profile and "
                "index new content in this space (existing content must then be "
                "re-ingested to become retrievable again)"
            )
        now = self._clock.utc_now()
        if active is not None:
            await uow.embedding_profiles.retire_active(retired_at=now)
        profile = EmbeddingProfileRecord(
            id=uuid4(),
            provider=descriptor.provider,
            model_id=descriptor.model_id,
            dimension=descriptor.dimension,
            distance_metric=descriptor.distance_metric,
            normalization=descriptor.normalization,
            profile_version=descriptor.profile_version,
            status="ACTIVE",
            created_at=now,
        )
        await uow.embedding_profiles.add(profile=profile)
        return profile

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _require_bounds(self, content: str) -> None:
        size = len(content.encode("utf-8"))
        if size > self._limits.max_document_bytes:
            raise KnowledgeBoundsExceededError(
                bound="max_document_bytes",
                limit=self._limits.max_document_bytes,
                actual=size,
            )

    def _chunk(self, normalized: str) -> tuple[DocumentChunk, ...]:
        """Produce the retrieval projection, enforcing the declared chunk bounds.

        The chunker enforces its own limits, but those are its constructor
        arguments -- a handler built with a differently-configured chunker would
        otherwise declare ``max_chunks_per_document``/``max_chunk_chars`` and never
        apply them. Checking here as well makes those fields a real contract on
        every ingestion path, and keeps the bound an explicit REJECTION rather
        than a silently oversized projection (brief section 25).
        """
        chunks = self._chunker.chunk_document(normalized)
        if not chunks:
            raise InvalidKnowledgeDocumentError(
                "content produced no chunks; empty or whitespace-only documents "
                "cannot be versioned"
            )
        if len(chunks) > self._limits.max_chunks_per_document:
            raise KnowledgeBoundsExceededError(
                bound="max_chunks_per_document",
                limit=self._limits.max_chunks_per_document,
                actual=len(chunks),
            )
        for chunk in chunks:
            if len(chunk.content) > self._limits.max_chunk_chars:
                raise KnowledgeBoundsExceededError(
                    bound="max_chunk_chars",
                    limit=self._limits.max_chunk_chars,
                    actual=len(chunk.content),
                )
        return chunks

    def _require_batch_matches(
        self,
        batch: EmbeddingBatch,
        expected_identity: tuple[str, str, int, str, str, int],
        chunk_count: int,
    ) -> None:
        if batch.descriptor.identity != expected_identity:
            raise InvalidKnowledgeVersionError(
                "the embedding provider returned vectors from a different profile "
                "than it declared"
            )
        if len(batch.vectors) != chunk_count:
            # Fewer vectors than chunks would silently leave part of a version
            # unsearchable. Fail closed instead (section 69).
            raise InvalidKnowledgeVersionError(
                f"embedding provider returned {len(batch.vectors)} vectors for "
                f"{chunk_count} chunks"
            )

    def _records(
        self,
        *,
        document: KnowledgeDocument,
        version: KnowledgeDocumentVersion,
        chunks: Sequence[DocumentChunk],
        batch: EmbeddingBatch,
        profile: EmbeddingProfileRecord,
    ) -> tuple[KnowledgeChunkRecord, ...]:
        now = self._clock.utc_now()
        records: list[KnowledgeChunkRecord] = []
        for chunk, vector in zip(chunks, batch.vectors, strict=True):
            if len(vector.values) != profile.dimension:
                raise InvalidKnowledgeVersionError(
                    f"chunk {chunk.ordinal} has {len(vector.values)} dimensions but "
                    f"the embedding profile declares {profile.dimension}"
                )
            records.append(
                KnowledgeChunkRecord(
                    id=uuid4(),
                    document_id=document.id,
                    document_version_id=version.id,
                    ordinal=chunk.ordinal,
                    heading_path=chunk.heading_path,
                    content=chunk.content,
                    content_hash=chunk.content_hash,
                    token_count=chunk.token_count,
                    language=version.language,
                    embedding_profile_id=profile.id,
                    embedding=tuple(float(value) for value in vector.values),
                    created_at=now,
                    chunker_version=self._chunker.chunker_version,
                )
            )
        return tuple(records)

    async def _flush_events(self, uow: UnitOfWork, document: KnowledgeDocument) -> None:
        """Persist pending knowledge events in the SAME transaction as the state.

        Knowledge events have no outbox destination, so this records an audit
        fact and enqueues nothing (section 29).
        """
        for event in document.pending_events:
            await uow.events.append(event, aggregate_revision=document.revision)
        document.clear_events()

    # -- read-only pre-check -------------------------------------------------
    async def _try_existing(
        self,
        command: IngestKnowledgeDocument,
        content_hash: str,
        expected_identity: tuple[str, str, int, str, str, int],
    ) -> IngestKnowledgeOutcome | None:
        """Return an outcome when ingestion is provably a no-op, else ``None``.

        Read-only and best-effort: phase 2 re-derives everything inside its own
        transaction, so a stale answer here can only cost a wasted embedding call,
        never a wrong write.
        """
        async with self._uow_factory() as uow:
            document = await uow.knowledge_documents.find_by_external_key(
                tenant_id=command.tenant_id,
                source_kind=command.source_kind,
                external_key=command.external_key,
                visibility=command.visibility,
            )
            if document is None or document.status is DocumentStatus.RETIRED:
                return None
            version = await uow.knowledge_documents.find_version_by_content_hash(
                document_id=document.id, content_hash=content_hash
            )
            if version is None or version.id != document.active_version_id:
                return None
            state = await uow.knowledge_chunks.projection_state(
                document_version_id=version.id
            )
            if state.is_empty:
                return None
            profile = await uow.embedding_profiles.find_by_identity(
                provider=expected_identity[0],
                model_id=expected_identity[1],
                dimension=expected_identity[2],
                distance_metric=expected_identity[3],
                normalization=expected_identity[4],
                profile_version=expected_identity[5],
            )
            if profile is None or state.embedding_profile_id != profile.id:
                return None
            if state.chunker_version != self._chunker.chunker_version:
                return None
            return IngestKnowledgeOutcome(
                document=document,
                version=version,
                chunk_count=state.chunk_count,
                document_created=False,
                version_created=False,
                projection_rebuilt=False,
            )
