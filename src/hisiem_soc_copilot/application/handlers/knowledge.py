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

Rebuilding a projection NEVER deletes content. The immutable content chunks (the
rows a citation names) and the rebuildable embedding rows are separate tables, so
a re-embed keeps the chunk ids and a rechunk appends a new generation beside the
old one (brief section 3). Moving the corpus to a different embedding space is
refused outright here -- that is a corpus-wide reindex, not an ingest (section 4).

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
from ...domain.knowledge.value_objects import (
    CHUNK_GENERATION_INITIAL,
    normalize_and_hash,
)
from ..commands.knowledge import (
    IngestKnowledgeDocument,
    IngestKnowledgeOutcome,
    RetireKnowledgeDocument,
    RetireKnowledgeOutcome,
)
from ..errors import (
    EmbeddingProfileSwitchRequiresReindexError,
    KnowledgeEmbeddingProfileError,
    KnowledgeIngestionConflictError,
    NotFoundError,
)
from ..ports.chunking import ChunkingPort, DocumentChunk
from ..ports.clock import ClockPort
from ..ports.embedding import EmbeddingBatch, EmbeddingProvider
from ..ports.knowledge import (
    ChunkEmbeddingRecord,
    EmbeddingProfileRecord,
    KnowledgeContentChunkRecord,
)
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
        if command.allow_embedding_profile_switch:
            # The flag is kept so an existing caller gets a DIAGNOSIS instead of an
            # unexpected keyword error, but it no longer enables anything: moving
            # the corpus to another embedding space is a corpus-wide reindex, and
            # P3-A deliberately has no per-document path that starts one (section
            # 4.1). Rejecting it before any read is what makes "no partial switch"
            # true even for a caller that asked for one.
            raise EmbeddingProfileSwitchRequiresReindexError(
                "allow_embedding_profile_switch is not available: changing the "
                "ACTIVE embedding space requires a whole-corpus reindex "
                "(stage -> reindex every document -> validate completeness -> "
                "activate atomically), which P3-A does not implement. Ingest "
                "without the flag in the current embedding space, or run a "
                "corpus-wide reindex out of band."
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
        profile = await self._resolve_embedding_profile(uow, batch)

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
        # Content identity and the vector projection are written as two sets of
        # rows in ONE transaction: the immutable chunks that citations name, and
        # the rebuildable embeddings that retrieval compares. A first chunking of a
        # version is generation 1, and nothing in P3-A ever rewrites it (section 3.3).
        content_chunks = self._content_chunk_records(
            document=document,
            version=version,
            chunks=chunks,
            generation=CHUNK_GENERATION_INITIAL,
        )
        await uow.knowledge_chunks.add_content_chunks(chunks=content_chunks)
        await uow.knowledge_chunks.add_embeddings(
            embeddings=self._embedding_records(
                content_chunks=content_chunks, batch=batch, profile=profile
            )
        )
        if command.activate_version:
            # The ONLY writer of ``active_version_id`` on this path. A staged
            # ingest (``activate_version=False``) persists the same immutable
            # version, chunks and embeddings and then stops: the document row is
            # saved unchanged, because nothing about which version retrieval
            # serves has changed. That is what keeps a staged ATT&CK release
            # invisible to normal retrieval while still being fully projected
            # (brief sections 2.4/2.5).
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
        change is the retrieval projection -- and the projection has two halves
        that go stale independently, which is precisely why they are two tables:

        * the CHUNKING (content chunks), which is generation-scoped and
          append-only. A changed ``chunker_version`` adds generation N+1 and
          leaves N exactly where it is, because deleting N would delete the rows
          historical citations resolve against (section 3.3). ``chunker_version``
          is persisted per chunk (section 22) so that a re-index is
          distinguishable from a fresh index.
        * the EMBEDDING space (embedding rows), which is freely rebuildable. A
          missing or non-ACTIVE projection re-embeds the EXISTING content chunks
          in place -- same ids, same content, so not one citation moves.

        A matching version that is NOT the active one is left completely alone --
        it is history, and re-ingesting old content must never roll the active
        pointer backwards.

        Idempotence follows from the three-way split: whatever state the version
        is in, re-running the identical ingest either returns the no-op outcome
        (projection matches) or converges to a state whose projection matches, and
        the caller can only distinguish them by ``projection_rebuilt``.
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
        if (
            state.is_embedded
            and state.embedding_profile_id == profile.id
            and state.chunker_version == self._chunker.chunker_version
        ):
            return IngestKnowledgeOutcome(
                document=document,
                version=version,
                chunk_count=state.chunk_count,
                document_created=document_created,
                version_created=False,
                projection_rebuilt=False,
            )

        if not state.is_empty and state.chunker_version == self._chunker.chunker_version:
            stored = await uow.knowledge_chunks.list_content_chunks(
                document_version_id=version.id, generation=state.generation
            )
            if self._same_chunking(stored, chunks):
                # Only the VECTOR half is stale. Re-embed the immutable chunks in
                # place rather than re-chunking them: new chunk rows would move
                # every citation target of this version for no reason at all, and a
                # citation has to survive a reindex (section 3.2).
                await uow.knowledge_chunks.delete_embeddings_for_profile(
                    document_version_id=version.id, embedding_profile_id=profile.id
                )
                await uow.knowledge_chunks.add_embeddings(
                    embeddings=self._embedding_records(
                        content_chunks=stored, batch=batch, profile=profile
                    )
                )
                await uow.commit()
                return IngestKnowledgeOutcome(
                    document=document,
                    version=version,
                    chunk_count=len(stored),
                    document_created=document_created,
                    version_created=False,
                    projection_rebuilt=True,
                )

        # The CHUNKING differs (or there is none yet). Append a new generation
        # rather than replacing the old one, so the previous generation's rows --
        # and every citation into them -- stay resolvable while normal retrieval
        # moves on to the newest generation (section 3.3). Nothing here deletes a
        # content chunk, and that is the point.
        generation = (
            CHUNK_GENERATION_INITIAL if state.is_empty else state.generation + 1
        )
        content_chunks = self._content_chunk_records(
            document=document,
            version=version,
            chunks=chunks,
            generation=generation,
        )
        await uow.knowledge_chunks.add_content_chunks(chunks=content_chunks)
        await uow.knowledge_chunks.add_embeddings(
            embeddings=self._embedding_records(
                content_chunks=content_chunks, batch=batch, profile=profile
            )
        )
        await uow.commit()
        return IngestKnowledgeOutcome(
            document=document,
            version=version,
            chunk_count=len(content_chunks),
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
        if active is not None:
            # A different space with an ACTIVE profile already in place: refuse, and
            # refuse BEFORE any write. Retiring the old profile here -- which is
            # what a switch would amount to -- is a corpus-wide act, because every
            # vector stored under it was produced by a different model and is not
            # comparable with the new one. One document's ingest must never decide
            # that (section 4.1). The ACTIVE profile is left exactly as it was, so
            # the existing corpus stays vector-retrievable.
            raise EmbeddingProfileSwitchRequiresReindexError(
                "the ACTIVE embedding profile is "
                f"{active.provider}:{active.model_id} (dimension {active.dimension}) "
                "but this ingestion is configured for "
                f"{descriptor.provider}:{descriptor.model_id} "
                f"(dimension {descriptor.dimension}); refusing to move the corpus "
                "to a different embedding space from a single document ingest. "
                "Every vector already stored belongs to the current profile and is "
                "not comparable with the new one, so the switch is a corpus-wide "
                "reindex: stage the new profile, reindex the whole corpus, validate "
                "completeness, then activate atomically. No partial switch is "
                "performed and the current profile stays ACTIVE."
            )
        now = self._clock.utc_now()
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

    def _content_chunk_records(
        self,
        *,
        document: KnowledgeDocument,
        version: KnowledgeDocumentVersion,
        chunks: Sequence[DocumentChunk],
        generation: int,
    ) -> tuple[KnowledgeContentChunkRecord, ...]:
        """Build the IMMUTABLE half: one row per chunk, never rewritten later.

        Each row carries the chunker that produced it and the generation it belongs
        to, which is what lets a later chunker append a new generation instead of
        destroying this one. ``content_hash`` comes from the chunker, which derives
        it with the one domain hash function -- and the persistence mapper
        recomputes it through the domain entity on the way in, so a hash that does
        not describe its content can never be stored (section 8).
        """
        now = self._clock.utc_now()
        return tuple(
            KnowledgeContentChunkRecord(
                id=uuid4(),
                document_id=document.id,
                document_version_id=version.id,
                generation=generation,
                ordinal=chunk.ordinal,
                heading_path=chunk.heading_path,
                content=chunk.content,
                content_hash=chunk.content_hash,
                token_count=chunk.token_count,
                language=version.language,
                chunker_version=self._chunker.chunker_version,
                created_at=now,
            )
            for chunk in chunks
        )

    def _embedding_records(
        self,
        *,
        content_chunks: Sequence[KnowledgeContentChunkRecord],
        batch: EmbeddingBatch,
        profile: EmbeddingProfileRecord,
    ) -> tuple[ChunkEmbeddingRecord, ...]:
        """Build the REBUILDABLE half: one projection row per content chunk.

        The vectors must line up one-for-one with the chunks they describe, in the
        same order, so the count is checked here as well as in
        :meth:`_require_batch_matches` -- this method is also called on the
        re-embed path, where the batch was produced for the freshly chunked content
        and is being applied to the chunks already stored.
        """
        if len(batch.vectors) != len(content_chunks):
            raise InvalidKnowledgeVersionError(
                f"embedding provider returned {len(batch.vectors)} vectors for "
                f"{len(content_chunks)} chunks"
            )
        now = self._clock.utc_now()
        records: list[ChunkEmbeddingRecord] = []
        for chunk, vector in zip(content_chunks, batch.vectors, strict=True):
            if len(vector.values) != profile.dimension:
                raise InvalidKnowledgeVersionError(
                    f"chunk {chunk.ordinal} has {len(vector.values)} dimensions but "
                    f"the embedding profile declares {profile.dimension}"
                )
            records.append(
                ChunkEmbeddingRecord(
                    id=uuid4(),
                    content_chunk_id=chunk.id,
                    embedding_profile_id=profile.id,
                    embedding=tuple(float(value) for value in vector.values),
                    indexed_at=now,
                )
            )
        return tuple(records)

    def _same_chunking(
        self,
        stored: Sequence[KnowledgeContentChunkRecord],
        fresh: Sequence[DocumentChunk],
    ) -> bool:
        """True when the stored chunking is byte-identical to a fresh chunking.

        Compared as ``(ordinal, content_hash)`` pairs so the batch computed in
        phase 1 lines up positionally with the rows that will receive its vectors.
        A chunker that declares one version but emits different text is a chunker
        bug, not a reason to move a citation target: when this returns False the
        caller appends a new generation instead, which is correct either way and
        keeps the old rows resolvable.
        """
        if len(stored) != len(fresh):
            return False
        return all(
            row.ordinal == chunk.ordinal and row.content_hash == chunk.content_hash
            for row, chunk in zip(stored, fresh, strict=True)
        )

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
            if not state.is_embedded:
                # Either nothing is chunked or the ACTIVE space does not FULLY
                # cover the current generation; both mean phase 2 has real work.
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
