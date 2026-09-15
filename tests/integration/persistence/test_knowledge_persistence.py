"""Knowledge persistence integration tests — real PostgreSQL + real pgvector.

These prove what the SCHEMA enforces, not what application code claims (brief
sections 12/13/15/16/44/45/51/52):

- the scope CHECK constraints make "GLOBAL with a tenant" unrepresentable;
- the two PARTIAL unique indexes prevent duplicated documents -- including the
  NULL-uniqueness trap that a single non-partial index would leave open;
- a version's content hash is unique per document, and the DB (not the retry
  logic) is the arbiter when two ingests race;
- ``save()`` is a real compare-and-swap, never last-write-wins;
- at most one embedding profile is ACTIVE at a time;
- retrieval filters (scope, ACTIVE document, ACTIVE version) happen in SQL, while
  citation resolution is scope-filtered ONLY.

The closure adds three more things this module proves against the REAL schema
rather than against a fake:

- ``attack_release`` owns the framework's authority, so "at most one
  authoritative ATT&CK release" is a partial unique index and not a convention;
- a citation names an IMMUTABLE content chunk, so it survives re-embedding, a
  projection rebuild and a rechunk, and it fails closed with
  ``CONTENT_INTEGRITY_MISMATCH`` when the stored content or its hash was edited
  out of band;
- the sealed-evaluation corpus snapshot is built in SQL, in the one deterministic
  semantic order, and contains no database-generated UUID.

Skipped when the pgvector-capable PostgreSQL on 127.0.0.1:5434 is unreachable, so
the suite stays green on a dev machine without it. Requires the schema already
migrated to head (``b6c2a4d19f30``).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import insert, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hisiem_soc_copilot.application.commands.knowledge import ImportAttackRelease
from hisiem_soc_copilot.application.errors import (
    AttackReleaseProjectionIncompleteError,
    AttackReleaseProjectionInvalidBindingError,
)
from hisiem_soc_copilot.application.handlers.attack_import import AttackImportHandler
from hisiem_soc_copilot.application.handlers.knowledge import KnowledgeIngestionHandler
from hisiem_soc_copilot.application.ports.attack import FRAMEWORK
from hisiem_soc_copilot.application.ports.clock import SystemClock
from hisiem_soc_copilot.application.ports.knowledge import (
    ATTACK_RELEASE_STATUS_ACTIVE,
    ATTACK_RELEASE_STATUS_INACTIVE,
    AttackReleaseRecord,
    AttackTechniqueRecord,
    ChunkEmbeddingRecord,
    ChunkProjectionState,
    CorpusChunkFact,
    EmbeddingProfileRecord,
    KnowledgeContentChunkRecord,
    KnowledgeQuery,
)
from hisiem_soc_copilot.application.ports.unit_of_work import UnitOfWork
from hisiem_soc_copilot.application.services.knowledge_retrieval import (
    KnowledgeCitationResolver,
    KnowledgeRetrievalService,
    RetrievalMode,
)
from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.domain.knowledge.entities import (
    KnowledgeDocument,
    KnowledgeDocumentVersion,
)
from hisiem_soc_copilot.domain.knowledge.enums import (
    DocumentStatus,
    SourceKind,
    Visibility,
)
from hisiem_soc_copilot.domain.knowledge.value_objects import (
    CHUNK_GENERATION_INITIAL,
    CHUNKER_VERSION,
    format_citation_id,
)
from hisiem_soc_copilot.domain.shared.errors import OptimisticConcurrencyError
from hisiem_soc_copilot.evaluation.knowledge import (
    CATEGORY_ATTACK_QUERY,
    CorpusCase,
    CorpusFact,
    CorpusMode,
    EvalQuery,
    RetrievedHit,
    SuiteResult,
    corpus_fingerprint,
    corpus_identity,
    run_suite,
)
from hisiem_soc_copilot.infrastructure.embedding.deterministic import (
    DeterministicEmbeddingProvider,
)
from hisiem_soc_copilot.infrastructure.knowledge.attack_source import (
    MitreStixAttackSource,
)
from hisiem_soc_copilot.infrastructure.knowledge.chunker_port import (
    StructureAwareChunker,
)
from hisiem_soc_copilot.infrastructure.persistence.orm.knowledge import (
    AttackReleaseProjectionRow,
    EmbeddingProfileRow,
    KnowledgeChunkEmbeddingRow,
    KnowledgeContentChunkRow,
    KnowledgeDocumentRow,
)
from hisiem_soc_copilot.infrastructure.persistence.repositories.knowledge import (
    SqlAlchemyAttackReleaseProjectionRepository,
    SqlAlchemyAttackReleaseRepository,
    SqlAlchemyAttackTechniqueRepository,
    SqlAlchemyEmbeddingProfileRepository,
    SqlAlchemyKnowledgeChunkRepository,
    SqlAlchemyKnowledgeDocumentRepository,
)
from hisiem_soc_copilot.infrastructure.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from tests.support.db_runtime import SKIP_REASON, apply_settings, server_reachable

#: Every table this module writes. The legacy ``knowledge_chunk`` is included
#: because the upgrade leaves it in place and a leak from a previous run would
#: otherwise be invisible; the closure's three new tables are included because a
#: citation test that inherited a chunk from another test would prove nothing.
_TRUNCATE = (
    "knowledge_chunk",
    "knowledge_chunk_embedding",
    "knowledge_content_chunk",
    "knowledge_document_version",
    "knowledge_document",
    "embedding_profile",
    "attack_technique",
    "attack_release",
)

# Naive UTC instants: the knowledge columns are ``timestamp without time zone``, so
# a naive value round-trips byte-identically and equality assertions stay honest.
_T0 = datetime(2026, 9, 12, 12, 0, 0)
_T1 = datetime(2026, 9, 12, 13, 0, 0)

_DOC_GLOBAL = UUID("00000000-0000-4000-8000-000000000001")
_DOC_TENANT_A = UUID("00000000-0000-4000-8000-000000000002")
_DOC_TENANT_B = UUID("00000000-0000-4000-8000-000000000003")

_VERSION_G1 = UUID("00000000-0000-4000-8000-000000000010")
_VERSION_A1 = UUID("00000000-0000-4000-8000-000000000011")
_VERSION_A2 = UUID("00000000-0000-4000-8000-000000000012")

_PROFILE_1 = UUID("00000000-0000-4000-8000-000000000020")
_PROFILE_2 = UUID("00000000-0000-4000-8000-000000000021")

_CHUNK_1 = UUID("00000000-0000-4000-8000-000000000030")
_CHUNK_2 = UUID("00000000-0000-4000-8000-000000000031")
_CHUNK_3 = UUID("00000000-0000-4000-8000-000000000032")
_CHUNK_5 = UUID("00000000-0000-4000-8000-000000000034")

#: The SECOND chunk generation of ``_VERSION_G1``: a rechunking appends these and
#: leaves the first generation's chunks -- the citation targets -- in place.
_CHUNK_G2 = UUID("00000000-0000-4000-8000-000000000070")

_TECHNIQUE_1 = UUID("00000000-0000-4000-8000-000000000040")
_TECHNIQUE_2 = UUID("00000000-0000-4000-8000-000000000041")

_RELEASE_141 = UUID("00000000-0000-4000-8000-000000000050")
_RELEASE_151 = UUID("00000000-0000-4000-8000-000000000051")
_RELEASE_152 = UUID("00000000-0000-4000-8000-000000000052")
_RELEASE_OTHER = UUID("00000000-0000-4000-8000-000000000053")
_UUID_UNUSED = UUID("00000000-0000-4000-8000-0000000000ff")


def _embedding_id_for(content_chunk_id: UUID) -> UUID:
    """A deterministic, INJECTIVE projection id for one content chunk.

    Derived rather than listed, because hand-assigned projection ids collide as
    soon as a version has more than one chunk in a module that seeds several
    versions. The high bit keeps the result out of the constant range above, and
    derivation keeps it stable across runs.
    """
    return UUID(int=content_chunk_id.int | (1 << 96))


#: ``knowledge_chunk_embedding.id`` values are deliberately DIFFERENT from the
#: content-chunk ids they project: a test that reused the ids could not tell a
#: surviving citation from a coincidentally recreated row.
_EMBED_1 = _embedding_id_for(_CHUNK_1)
_EMBED_2 = _embedding_id_for(_CHUNK_2)
_EMBED_3 = _embedding_id_for(_CHUNK_3)
_EMBED_G2 = _embedding_id_for(_CHUNK_G2)

#: Projection rows written by a REBUILD. New rows, same content chunks.
_EMBED_R1 = UUID("00000000-0000-4000-8000-000000000080")
_EMBED_R2 = UUID("00000000-0000-4000-8000-000000000081")
_EMBED_R3 = UUID("00000000-0000-4000-8000-000000000082")


# Dyadic values only: the column is pgvector ``vector`` (float4), so a value like
# 0.1 would not survive the round-trip exactly and the assertion would be a lie.
_V_AXIS_X: tuple[float, ...] = (1.0, 0.0, 0.0, 0.0)
_V_AXIS_Y: tuple[float, ...] = (0.0, 1.0, 0.0, 0.0)
_V_BOTH: tuple[float, ...] = (0.5, 0.5, 0.0, 0.0)


def _settings() -> Settings:
    settings = Settings()
    apply_settings(settings)
    return settings


pytestmark = pytest.mark.skipif(
    not server_reachable(),
    reason=SKIP_REASON,
)


@pytest_asyncio.fixture
async def session_factory(
    scratch_db_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(scratch_db_url)

    async def _clean() -> None:
        """Clear ONLY the eight knowledge tables (never anything else)."""
        targets = ", ".join(f"copilot.{table}" for table in _TRUNCATE)
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {targets} RESTART IDENTITY CASCADE"))

    await _clean()
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        yield factory
    finally:
        await _clean()
        await engine.dispose()


# ---------------------------------------------------------------------------
# builders (deterministic ids/content so ordering assertions mean something)
# ---------------------------------------------------------------------------


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _document(
    *,
    document_id: UUID,
    external_key: str,
    visibility: Visibility,
    tenant_id: str | None,
    title: str = "SSH Brute Force Guidance",
    source_kind: SourceKind = SourceKind.CURATED_GUIDANCE,
) -> KnowledgeDocument:
    # Constructed directly, not via ``create()``: no domain event should be emitted
    # for a fixture, exactly as for a row loaded from the database.
    return KnowledgeDocument(
        id=document_id,
        source_kind=source_kind,
        external_key=external_key,
        visibility=visibility,
        tenant_id=tenant_id,
        title=title,
        created_at=_T0,
    )


def _version(
    *,
    version_id: UUID,
    document_id: UUID,
    number: int,
    content: str,
    title: str = "SSH Brute Force Guidance",
    source_version: str | None = "2026-09-01",
    metadata: dict[str, object] | None = None,
) -> KnowledgeDocumentVersion:
    return KnowledgeDocumentVersion(
        id=version_id,
        document_id=document_id,
        version=number,
        content_hash=_hash(content),
        title=title,
        normalized_content=content,
        language="en",
        source_version=source_version,
        metadata=dict(metadata or {}),
        ingested_at=_T0,
        effective_at=None,
    )


def _chunk(
    *,
    chunk_id: UUID,
    document_id: UUID,
    document_version_id: UUID,
    ordinal: int,
    content: str,
    generation: int = CHUNK_GENERATION_INITIAL,
    chunker_version: str = CHUNKER_VERSION,
) -> KnowledgeContentChunkRecord:
    """One IMMUTABLE content chunk -- the citation target, and nothing else.

    It carries no embedding and no profile: those live in the projection beside
    it, which is what lets the projection be dropped and rebuilt without moving
    the thing a citation names (brief section 3.1).
    """
    return KnowledgeContentChunkRecord(
        id=chunk_id,
        document_id=document_id,
        document_version_id=document_version_id,
        generation=generation,
        ordinal=ordinal,
        heading_path="Authentication",
        content=content,
        content_hash=_hash(content),
        token_count=len(content.split()),
        language="en",
        chunker_version=chunker_version,
        created_at=_T0,
    )


def _embedding(
    *,
    embedding_id: UUID,
    content_chunk_id: UUID,
    embedding_profile_id: UUID,
    embedding: tuple[float, ...],
) -> ChunkEmbeddingRecord:
    """One REBUILDABLE embedding projection row.

    Distinct in identity from the content chunk it points at, so deleting and
    re-inserting this row is invisible to every citation.
    """
    return ChunkEmbeddingRecord(
        id=embedding_id,
        content_chunk_id=content_chunk_id,
        embedding_profile_id=embedding_profile_id,
        embedding=embedding,
        indexed_at=_T0,
    )


def _profile(
    *,
    profile_id: UUID,
    status: str = "ACTIVE",
    profile_version: int = 1,
    model_id: str = "text-embedding-3-small",
    normalization: str = "L2",
    dimension: int = 4,
    retired_at: datetime | None = None,
) -> EmbeddingProfileRecord:
    return EmbeddingProfileRecord(
        id=profile_id,
        provider="openai",
        model_id=model_id,
        dimension=dimension,
        distance_metric="COSINE",
        normalization=normalization,
        profile_version=profile_version,
        status=status,
        created_at=_T0,
        retired_at=retired_at,
    )


def _release(
    *,
    release_id: UUID,
    source_release: str = "v15.1",
    framework: str = "mitre-attack",
    status: str = ATTACK_RELEASE_STATUS_ACTIVE,
    content_fingerprint: str | None = "a" * 64,
    technique_count: int = 2,
    activated_at: datetime | None = _T0,
) -> AttackReleaseRecord:
    """One imported release. Authority lives HERE, never on a technique row."""
    return AttackReleaseRecord(
        id=release_id,
        framework=framework,
        source_release=source_release,
        content_fingerprint=content_fingerprint,
        status=status,
        technique_count=technique_count,
        created_at=_T0,
        activated_at=activated_at,
    )


def _technique(
    *,
    row_id: UUID,
    technique_id: str = "T1110",
    source_release: str = "v15.1",
    framework: str = "mitre-attack",
    name: str = "Brute Force",
) -> AttackTechniqueRecord:
    return AttackTechniqueRecord(
        id=row_id,
        framework=framework,
        technique_id=technique_id,
        source_release=source_release,
        name=name,
        description="Adversaries may use brute force to obtain credentials.",
        tactics=("credential-access", "defense-evasion"),
        platforms=("Linux", "Windows"),
        source_stix_id="attack-pattern--00000000-0000-4000-8000-000000000099",
        content_hash=_hash(f"{framework}:{technique_id}:{source_release}:{name}"),
        active=True,
        created_at=_T0,
    )


@dataclass(frozen=True)
class _VersionSeed:
    version_id: UUID
    number: int
    content: str
    chunks: tuple[tuple[UUID, str, tuple[float, ...]], ...] = ()


async def _add_profile(
    factory: async_sessionmaker[AsyncSession],
    *,
    profile_id: UUID = _PROFILE_1,
    status: str = "ACTIVE",
    profile_version: int = 1,
    model_id: str = "text-embedding-3-small",
    retired_at: datetime | None = None,
) -> None:
    async with factory() as session:
        profiles = SqlAlchemyEmbeddingProfileRepository(session)
        await profiles.add(
            profile=_profile(
                profile_id=profile_id,
                status=status,
                profile_version=profile_version,
                model_id=model_id,
                retired_at=retired_at,
            )
        )
        await session.commit()


async def _ingest(
    factory: async_sessionmaker[AsyncSession],
    *,
    document_id: UUID,
    external_key: str,
    visibility: Visibility,
    tenant_id: str | None,
    versions: Sequence[_VersionSeed],
    profile_id: UUID | None = None,
    source_kind: SourceKind = SourceKind.CURATED_GUIDANCE,
    title: str = "SSH Brute Force Guidance",
    activate_last: bool = True,
    retire: bool = False,
) -> None:
    """Persist a document, its versions (append-only) and their chunk projections.

    Mirrors the real ingestion shape: the version rows and the document's active
    pointer are written in ONE transaction, so the pointer can never reference a
    version whose retrieval projection is missing (brief section 27).
    """
    async with factory() as session:
        documents = SqlAlchemyKnowledgeDocumentRepository(session)
        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        document = _document(
            document_id=document_id,
            external_key=external_key,
            visibility=visibility,
            tenant_id=tenant_id,
            title=title,
            source_kind=source_kind,
        )
        await documents.add(document=document)
        await session.flush()
        for seed in versions:
            await documents.add_version(
                version=_version(
                    version_id=seed.version_id,
                    document_id=document_id,
                    number=seed.number,
                    content=seed.content,
                    title=title,
                )
            )
            await session.flush()
            if seed.chunks:
                if profile_id is None:
                    raise ValueError("seeding chunks requires an embedding profile")
                # Two writes, deliberately: the immutable content chunk first,
                # then its projection. That is also the order the real ingestion
                # path uses, so a crash between them leaves content that a
                # re-index can project rather than a projection of nothing.
                await chunks.add_content_chunks(
                    chunks=[
                        _chunk(
                            chunk_id=chunk_id,
                            document_id=document_id,
                            document_version_id=seed.version_id,
                            ordinal=ordinal,
                            content=chunk_content,
                        )
                        for ordinal, (chunk_id, chunk_content, _vector) in enumerate(
                            seed.chunks
                        )
                    ]
                )
                await chunks.add_embeddings(
                    embeddings=[
                        _embedding(
                            # Derived from the chunk it projects, so seeding a
                            # version with N chunks cannot collide with the next
                            # version's N.
                            embedding_id=_embedding_id_for(chunk_id),
                            content_chunk_id=chunk_id,
                            embedding_profile_id=profile_id,
                            embedding=vector,
                        )
                        for ordinal, (chunk_id, _content, vector) in enumerate(seed.chunks)
                    ]
                )
        last = versions[-1]
        if activate_last:
            document.activate_version(
                version_id=last.version_id,
                version=last.number,
                content_hash=_hash(last.content),
                title=title,
            )
            await documents.save(document=document)
        if retire:
            document.retire(now=_T1)
            await documents.save(document=document)
        await session.commit()


def _raw_document(
    *,
    document_id: UUID,
    visibility: str,
    tenant_id: str | None,
    external_key: str,
    source_kind: str = "CURATED_GUIDANCE",
) -> dict[str, object]:
    """Raw column values for an ``sa.insert`` that bypasses the domain entirely."""
    return {
        "id": document_id,
        "source_kind": source_kind,
        "external_key": external_key,
        "visibility": visibility,
        "tenant_id": tenant_id,
        "title": "Raw row",
        "status": "ACTIVE",
        "active_version_id": None,
        "revision": 0,
        "lock_version": 0,
        "created_at": _T0,
        "retired_at": None,
    }


# ---------------------------------------------------------------------------
# 1. round-trip
# ---------------------------------------------------------------------------


async def test_document_and_version_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    metadata = {"origin": "curated", "tags": ["ssh", "brute-force"]}
    async with session_factory() as session:
        documents = SqlAlchemyKnowledgeDocumentRepository(session)
        document = _document(
            document_id=_DOC_GLOBAL,
            external_key="curated:ssh-brute-force",
            visibility=Visibility.GLOBAL,
            tenant_id=None,
            title="Credential Access Guidance",
        )
        await documents.add(document=document)
        await session.flush()
        await documents.add_version(
            version=_version(
                version_id=_VERSION_G1,
                document_id=_DOC_GLOBAL,
                number=1,
                content="T1110 sshd authentication_failure\n",
                title="Credential Access Guidance",
                source_version="2026-09-01",
                metadata=metadata,
            )
        )
        await session.commit()

    async with session_factory() as session:
        documents = SqlAlchemyKnowledgeDocumentRepository(session)
        loaded = await documents.get(tenant_id="tenant-a", document_id=_DOC_GLOBAL)
        assert loaded is not None
        assert loaded.id == _DOC_GLOBAL
        assert loaded.source_kind is SourceKind.CURATED_GUIDANCE
        assert loaded.external_key == "curated:ssh-brute-force"
        assert loaded.visibility is Visibility.GLOBAL
        assert loaded.tenant_id is None
        assert loaded.title == "Credential Access Guidance"
        assert loaded.status is DocumentStatus.ACTIVE
        assert loaded.active_version_id is None
        assert loaded.lock_version == 0
        assert loaded.revision == 0
        assert loaded.created_at == _T0
        assert loaded.retired_at is None
        # Loading is not a business fact: no domain event may be replayed.
        assert loaded.pending_events == []

        # A GLOBAL document is readable by every tenant.
        assert await documents.get(tenant_id="tenant-b", document_id=_DOC_GLOBAL) is not None

        version = await documents.get_version(
            tenant_id="tenant-a", document_version_id=_VERSION_G1
        )
        assert version is not None
        assert version.id == _VERSION_G1
        assert version.document_id == _DOC_GLOBAL
        assert version.version == 1
        assert version.content_hash == _hash("T1110 sshd authentication_failure\n")
        assert version.title == "Credential Access Guidance"
        assert version.normalized_content == "T1110 sshd authentication_failure\n"
        assert version.language == "en"
        assert version.source_version == "2026-09-01"
        assert version.metadata == metadata
        assert version.ingested_at == _T0
        assert version.effective_at is None

        assert await documents.list_versions(
            tenant_id="tenant-a", document_id=_DOC_GLOBAL
        ) == (version,)
        assert await documents.next_version_number(document_id=_DOC_GLOBAL) == 2
        assert await documents.find_version_by_content_hash(
            document_id=_DOC_GLOBAL,
            content_hash=_hash("T1110 sshd authentication_failure\n"),
        ) == version
        assert (
            await documents.find_version_by_content_hash(
                document_id=_DOC_GLOBAL, content_hash=_hash("absent")
            )
            is None
        )


async def test_find_by_external_key_respects_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        documents = SqlAlchemyKnowledgeDocumentRepository(session)
        await documents.add(
            document=_document(
                document_id=_DOC_GLOBAL,
                external_key="shared-key",
                visibility=Visibility.GLOBAL,
                tenant_id=None,
            )
        )
        await documents.add(
            document=_document(
                document_id=_DOC_TENANT_A,
                external_key="shared-key",
                visibility=Visibility.TENANT,
                tenant_id="tenant-a",
            )
        )
        await session.commit()

    async with session_factory() as session:
        documents = SqlAlchemyKnowledgeDocumentRepository(session)
        # The same (source_kind, external_key) exists once GLOBALLY and once per
        # tenant: the lookup matches on the SCOPE, not only on the key.
        global_doc = await documents.find_by_external_key(
            tenant_id="tenant-b",
            source_kind=SourceKind.CURATED_GUIDANCE,
            external_key="shared-key",
            visibility=Visibility.GLOBAL,
        )
        assert global_doc is not None
        assert global_doc.id == _DOC_GLOBAL

        # A GLOBAL lookup ignores the tenant it is passed: a GLOBAL row has no
        # tenant by definition.
        assert (
            await documents.find_by_external_key(
                tenant_id=None,
                source_kind=SourceKind.CURATED_GUIDANCE,
                external_key="shared-key",
                visibility=Visibility.GLOBAL,
            )
            is not None
        )

        tenant_doc = await documents.find_by_external_key(
            tenant_id="tenant-a",
            source_kind=SourceKind.CURATED_GUIDANCE,
            external_key="shared-key",
            visibility=Visibility.TENANT,
        )
        assert tenant_doc is not None
        assert tenant_doc.id == _DOC_TENANT_A

        # Another tenant's key is simply not found (no TENANT row of its own).
        assert (
            await documents.find_by_external_key(
                tenant_id="tenant-b",
                source_kind=SourceKind.CURATED_GUIDANCE,
                external_key="shared-key",
                visibility=Visibility.TENANT,
            )
            is None
        )


# ---------------------------------------------------------------------------
# 2. scope CHECK constraints
# ---------------------------------------------------------------------------


async def test_scope_check_constraint_is_real(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                insert(KnowledgeDocumentRow).values(
                    **_raw_document(
                        document_id=_DOC_GLOBAL,
                        visibility="GLOBAL",
                        tenant_id="tenant-a",  # GLOBAL with a tenant: unrepresentable
                        external_key="raw:global-with-tenant",
                    )
                )
            )
            await session.commit()

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                insert(KnowledgeDocumentRow).values(
                    **_raw_document(
                        document_id=_DOC_TENANT_A,
                        visibility="TENANT",
                        tenant_id=None,  # TENANT without a tenant: unrepresentable
                        external_key="raw:tenant-without-tenant",
                    )
                )
            )
            await session.commit()


# ---------------------------------------------------------------------------
# 3. partial unique indexes
# ---------------------------------------------------------------------------


async def test_partial_unique_indexes_block_duplicate_documents(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await session.execute(
            insert(KnowledgeDocumentRow).values(
                **_raw_document(
                    document_id=_DOC_GLOBAL,
                    visibility="GLOBAL",
                    tenant_id=None,
                    external_key="curated:dup",
                )
            )
        )
        await session.commit()

    # A second GLOBAL row with the same key conflicts. This is the NULL-uniqueness
    # trap: in a plain unique index NULL never conflicts, so GLOBAL rows would
    # duplicate freely -- which is exactly why the index is PARTIAL.
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                insert(KnowledgeDocumentRow).values(
                    **_raw_document(
                        document_id=_DOC_TENANT_B,
                        visibility="GLOBAL",
                        tenant_id=None,
                        external_key="curated:dup",
                    )
                )
            )
            await session.commit()

    # The SAME key in two DIFFERENT tenants is not a conflict at all.
    async with session_factory() as session:
        await session.execute(
            insert(KnowledgeDocumentRow).values(
                **_raw_document(
                    document_id=_DOC_TENANT_A,
                    visibility="TENANT",
                    tenant_id="tenant-a",
                    external_key="runbook:ssh",
                )
            )
        )
        await session.execute(
            insert(KnowledgeDocumentRow).values(
                **_raw_document(
                    document_id=_DOC_TENANT_B,
                    visibility="TENANT",
                    tenant_id="tenant-b",
                    external_key="runbook:ssh",
                )
            )
        )
        await session.commit()

    # ...but twice inside ONE tenant is.
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                insert(KnowledgeDocumentRow).values(
                    **_raw_document(
                        document_id=_VERSION_G1,  # any unused id
                        visibility="TENANT",
                        tenant_id="tenant-a",
                        external_key="runbook:ssh",
                    )
                )
            )
            await session.commit()


# ---------------------------------------------------------------------------
# 4. version dedup + 13. concurrent convergence
# ---------------------------------------------------------------------------


async def test_version_content_hash_is_unique_per_document(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    content = "T1110 sshd authentication_failure\n"
    await _ingest(
        session_factory,
        document_id=_DOC_GLOBAL,
        external_key="curated:dedup",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        versions=(_VersionSeed(version_id=_VERSION_G1, number=1, content=content),),
    )

    async with session_factory() as session:
        documents = SqlAlchemyKnowledgeDocumentRepository(session)
        with pytest.raises(IntegrityError):
            await documents.add_version(
                version=_version(
                    version_id=_VERSION_A1,
                    document_id=_DOC_GLOBAL,
                    number=2,
                    content=content,  # identical bytes -> identical hash
                )
            )
            await session.commit()


async def test_concurrent_ingestion_converges_on_one_winner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two racing ingests: the DATABASE decides, not application code."""
    await _ingest(
        session_factory,
        document_id=_DOC_GLOBAL,
        external_key="curated:race",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        versions=(_VersionSeed(version_id=_VERSION_G1, number=1, content="seed\n"),),
    )

    async def _attempt(
        *, version_id: UUID, number: int, content: str
    ) -> IntegrityError | None:
        session = session_factory()
        try:
            documents = SqlAlchemyKnowledgeDocumentRepository(session)
            await documents.add_version(
                version=_version(
                    version_id=version_id,
                    document_id=_DOC_GLOBAL,
                    number=number,
                    content=content,
                )
            )
            await session.commit()
            return None
        except IntegrityError as exc:
            await session.rollback()
            return exc
        finally:
            await session.close()

    # Same (document_id, version): UNIQUE(document_id, version) arbitrates.
    outcomes = await asyncio.gather(
        _attempt(version_id=_VERSION_A1, number=2, content="race-a\n"),
        _attempt(version_id=_VERSION_A2, number=2, content="race-b\n"),
    )
    assert sum(1 for outcome in outcomes if outcome is not None) == 1

    # Same document, same CONTENT HASH, different version numbers: the content
    # dedup index arbitrates instead, so the winner keeps whichever number it was
    # assigned -- and exactly ONE row survives.
    outcomes = await asyncio.gather(
        _attempt(version_id=_VERSION_A1, number=3, content="identical\n"),
        _attempt(version_id=_VERSION_A2, number=4, content="identical\n"),
    )
    assert sum(1 for outcome in outcomes if outcome is not None) == 1

    async with session_factory() as session:
        documents = SqlAlchemyKnowledgeDocumentRepository(session)
        versions = await documents.list_versions(
            tenant_id="tenant-a", document_id=_DOC_GLOBAL
        )
        by_hash = await documents.find_version_by_content_hash(
            document_id=_DOC_GLOBAL, content_hash=_hash("identical\n")
        )
    # Exactly one winner from each round, plus the seed.
    assert len(versions) == 3
    assert [version.version for version in versions[:2]] == [1, 2]
    assert versions[2].version in {3, 4}
    assert by_hash is not None
    assert by_hash.id == versions[2].id


# ---------------------------------------------------------------------------
# 5. optimistic-lock save
# ---------------------------------------------------------------------------


async def test_save_is_a_compare_and_swap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _add_profile(session_factory)
    await _ingest(
        session_factory,
        document_id=_DOC_GLOBAL,
        external_key="curated:cas",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        versions=(
            _VersionSeed(version_id=_VERSION_G1, number=1, content="cas-v1\n"),
            _VersionSeed(version_id=_VERSION_A1, number=2, content="cas-v2\n"),
        ),
        profile_id=_PROFILE_1,
    )

    async with session_factory() as s1, session_factory() as s2:
        first = SqlAlchemyKnowledgeDocumentRepository(s1)
        second = SqlAlchemyKnowledgeDocumentRepository(s2)

        winner = await first.get(tenant_id="tenant-a", document_id=_DOC_GLOBAL)
        stale = await second.get(tenant_id="tenant-a", document_id=_DOC_GLOBAL)
        assert winner is not None
        assert stale is not None
        assert winner.lock_version == stale.lock_version == 1

        # The winner switches the active pointer to version 1 and commits.
        winner.activate_version(
            version_id=_VERSION_G1,
            version=1,
            content_hash=_hash("cas-v1\n"),
            title="Winner Title",
        )
        await first.save(document=winner)
        await s1.commit()
        assert winner.lock_version == 2

        # The stale copy still holds the old lock_version -> CAS failure.
        stale.retire(now=_T1)
        with pytest.raises(OptimisticConcurrencyError):
            await second.save(document=stale)

    async with session_factory() as session:
        documents = SqlAlchemyKnowledgeDocumentRepository(session)
        stored = await documents.get(tenant_id="tenant-a", document_id=_DOC_GLOBAL)
        assert stored is not None
        # The winner's values survive; the loser did not write anything.
        assert stored.title == "Winner Title"
        assert stored.active_version_id == _VERSION_G1
        assert stored.status is DocumentStatus.ACTIVE
        assert stored.retired_at is None
        assert stored.lock_version == 2
        assert stored.revision == 2


# ---------------------------------------------------------------------------
# 6. embedding profile lifecycle
# ---------------------------------------------------------------------------


async def test_only_one_embedding_profile_may_be_active(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        profiles = SqlAlchemyEmbeddingProfileRepository(session)
        # Retiring when nothing is ACTIVE is a no-op, never an error.
        await profiles.retire_active(retired_at=_T0)
        await session.commit()
        assert await profiles.get_active() is None

    async with session_factory() as session:
        profiles = SqlAlchemyEmbeddingProfileRepository(session)
        await profiles.add(profile=_profile(profile_id=_PROFILE_1))
        await session.commit()

    async with session_factory() as session:
        profiles = SqlAlchemyEmbeddingProfileRepository(session)
        with pytest.raises(IntegrityError):
            await profiles.add(
                profile=_profile(profile_id=_PROFILE_2, model_id="text-embedding-3-large")
            )
            await session.commit()

    async with session_factory() as session:
        profiles = SqlAlchemyEmbeddingProfileRepository(session)
        await profiles.retire_active(retired_at=_T1)
        await session.commit()
        assert await profiles.get_active() is None

    async with session_factory() as session:
        profiles = SqlAlchemyEmbeddingProfileRepository(session)
        retired = await profiles.find_by_identity(
            provider="openai",
            model_id="text-embedding-3-small",
            dimension=4,
            distance_metric="COSINE",
            normalization="L2",
            profile_version=1,
        )
        assert retired is not None
        assert retired.status == "RETIRED"
        assert retired.retired_at == _T1

        # With the slot free, a new ACTIVE profile inserts cleanly.
        await profiles.add(
            profile=_profile(profile_id=_PROFILE_2, model_id="text-embedding-3-large")
        )
        await session.commit()

    async with session_factory() as session:
        profiles = SqlAlchemyEmbeddingProfileRepository(session)
        active = await profiles.get_active()
        assert active is not None
        assert active.id == _PROFILE_2
        # The RETIRED row is still findable by identity, whatever its status.
        assert (
            await profiles.find_by_identity(
                provider="openai",
                model_id="text-embedding-3-large",
                dimension=4,
                distance_metric="COSINE",
                normalization="L2",
                profile_version=1,
            )
            is not None
        )


# ---------------------------------------------------------------------------
# 7. chunk insert + vector round-trip
# ---------------------------------------------------------------------------


async def test_chunk_vector_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _add_profile(session_factory)
    await _ingest(
        session_factory,
        document_id=_DOC_GLOBAL,
        external_key="curated:vector",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        versions=(
            _VersionSeed(
                version_id=_VERSION_G1,
                number=1,
                content="T1110 sshd authentication_failure\n",
                chunks=((_CHUNK_1, "T1110 sshd authentication_failure", _V_AXIS_X),),
            ),
        ),
        profile_id=_PROFILE_1,
    )

    async with session_factory() as session:
        # Content and projection are two rows with two lifetimes. The content row
        # is the citation target; the projection row is disposable.
        content_row = await session.get(KnowledgeContentChunkRow, _CHUNK_1)
        assert content_row is not None
        assert content_row.chunker_version == CHUNKER_VERSION
        assert content_row.generation == CHUNK_GENERATION_INITIAL
        assert content_row.document_version_id == _VERSION_G1
        assert content_row.content_hash == _hash("T1110 sshd authentication_failure")
        assert content_row.lexical_document is not None  # GENERATED by the database

        # The pgvector values themselves survive (float4, so dyadic inputs only).
        embedding_row = await session.get(KnowledgeChunkEmbeddingRow, _EMBED_1)
        assert embedding_row is not None
        assert embedding_row.content_chunk_id == _CHUNK_1
        assert embedding_row.embedding_profile_id == _PROFILE_1
        assert [float(value) for value in embedding_row.embedding] == [1.0, 0.0, 0.0, 0.0]

        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        assert await chunks.count_for_version(document_version_id=_VERSION_G1) == 1
        view = await chunks.get_chunk_view(tenant_id="tenant-a", chunk_id=_CHUNK_1)
        assert view is not None
        assert view.chunk_id == _CHUNK_1
        assert view.document_id == _DOC_GLOBAL
        assert view.document_version_id == _VERSION_G1
        assert view.ordinal == 0
        assert view.heading_path == "Authentication"
        assert view.content == "T1110 sshd authentication_failure"
        assert view.content_hash == _hash("T1110 sshd authentication_failure")
        assert view.title == "SSH Brute Force Guidance"
        assert view.source_kind is SourceKind.CURATED_GUIDANCE
        assert view.language == "en"
        assert view.source_version == "2026-09-01"
        assert view.document_status == "ACTIVE"
        assert view.visibility is Visibility.GLOBAL
        assert view.tenant_id is None
        # Provenance and RANKING metadata (brief section 5.2): the semantic facts a
        # deterministic tie-break explains itself with, never an authority.
        assert view.external_key == "curated:vector"
        assert view.document_version_number == 1
        assert view.chunk_generation == CHUNK_GENERATION_INITIAL

        assert await chunks.get_chunk_view(
            tenant_id="tenant-a", chunk_id=_CHUNK_5
        ) is None


# ---------------------------------------------------------------------------
# 8. exact vector search
# ---------------------------------------------------------------------------


async def test_vector_candidates_are_exact_and_profile_scoped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _add_profile(session_factory, profile_id=_PROFILE_1)
    # At most one profile may be ACTIVE, so the second space is RETIRED (and a
    # RETIRED row must carry a retired_at, per the coherence CHECK).
    await _add_profile(
        session_factory,
        profile_id=_PROFILE_2,
        model_id="legacy-model",
        status="RETIRED",
        retired_at=_T1,
    )
    async with session_factory() as session:
        documents = SqlAlchemyKnowledgeDocumentRepository(session)
        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        document = _document(
            document_id=_DOC_GLOBAL,
            external_key="curated:exact",
            visibility=Visibility.GLOBAL,
            tenant_id=None,
        )
        await documents.add(document=document)
        await session.flush()
        await documents.add_version(
            version=_version(
                version_id=_VERSION_G1,
                document_id=_DOC_GLOBAL,
                number=1,
                content="vector corpus\n",
            )
        )
        await session.flush()
        await chunks.add_content_chunks(
            chunks=[
                _chunk(
                    chunk_id=_CHUNK_1,
                    document_id=_DOC_GLOBAL,
                    document_version_id=_VERSION_G1,
                    ordinal=0,
                    content="identical direction",
                ),
                _chunk(
                    chunk_id=_CHUNK_2,
                    document_id=_DOC_GLOBAL,
                    document_version_id=_VERSION_G1,
                    ordinal=1,
                    content="orthogonal direction",
                ),
                _chunk(
                    chunk_id=_CHUNK_3,
                    document_id=_DOC_GLOBAL,
                    document_version_id=_VERSION_G1,
                    ordinal=2,
                    content="other space",
                ),
            ]
        )
        await chunks.add_embeddings(
            embeddings=[
                _embedding(
                    embedding_id=_EMBED_1,
                    content_chunk_id=_CHUNK_1,
                    embedding_profile_id=_PROFILE_1,
                    embedding=_V_AXIS_X,
                ),
                _embedding(
                    embedding_id=_EMBED_2,
                    content_chunk_id=_CHUNK_2,
                    embedding_profile_id=_PROFILE_1,
                    embedding=_V_AXIS_Y,
                ),
                # The SAME content chunk, at the same direction as the query, but
                # indexed in ANOTHER space: it must never be returned, because
                # distances across profiles are not comparable numbers.
                _embedding(
                    embedding_id=_EMBED_3,
                    content_chunk_id=_CHUNK_3,
                    embedding_profile_id=_PROFILE_2,
                    embedding=_V_AXIS_X,
                ),
            ]
        )
        document.activate_version(
            version_id=_VERSION_G1,
            version=1,
            content_hash=_hash("vector corpus\n"),
            title="Vector Corpus",
        )
        await documents.save(document=document)
        await session.commit()

    async with session_factory() as session:
        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        candidates = await chunks.vector_candidates(
            tenant_id="tenant-a",
            embedding_profile_id=_PROFILE_1,
            query_vector=_V_AXIS_X,
            limit=10,
        )
    assert [candidate.view.chunk_id for candidate in candidates] == [_CHUNK_1, _CHUNK_2]
    assert candidates[0].distance < 1e-6  # nearest first: cosine distance ascends
    assert abs(candidates[1].distance - 1.0) < 1e-6

    async with session_factory() as session:
        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        # The other space is queried with its own profile id and sees only its own.
        other = await chunks.vector_candidates(
            tenant_id="tenant-a",
            embedding_profile_id=_PROFILE_2,
            query_vector=_V_AXIS_X,
            limit=10,
        )
        assert [candidate.view.chunk_id for candidate in other] == [_CHUNK_3]
        assert await chunks.vector_candidates(
            tenant_id="tenant-a",
            embedding_profile_id=_PROFILE_2,
            query_vector=_V_AXIS_X,
            limit=10,
        ) != ()


# ---------------------------------------------------------------------------
# 9. lexical search
# ---------------------------------------------------------------------------


async def _seed_lexical_corpus(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _add_profile(factory, profile_id=_PROFILE_1)
    await _ingest(
        factory,
        document_id=_DOC_GLOBAL,
        external_key="curated:lexical",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        versions=(
            _VersionSeed(
                version_id=_VERSION_G1,
                number=1,
                content="T1110 sshd authentication_failure brute force\n",
                chunks=(
                    (
                        _CHUNK_1,
                        "T1110 sshd authentication_failure brute force on the bastion",
                        _V_AXIS_X,
                    ),
                    (_CHUNK_2, "sshd mentions only one token here", _V_AXIS_Y),
                ),
            ),
        ),
        profile_id=_PROFILE_1,
    )


async def test_lexical_candidates_find_exact_security_tokens(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_lexical_corpus(session_factory)

    async with session_factory() as session:
        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        # The `simple` configuration must not stem or rewrite security identifiers:
        # T1110, sshd and authentication_failure all match literally.
        candidates = await chunks.lexical_candidates(
            tenant_id="tenant-a",
            search_terms=["T1110", "sshd", "authentication_failure"],
            limit=10,
        )
        assert [candidate.view.chunk_id for candidate in candidates] == [
            _CHUNK_1,
            _CHUNK_2,
        ]
        # The chunk matching all three terms outranks the one matching a single term.
        assert candidates[0].rank > candidates[1].rank > 0.0

        # An unrelated word matches nothing at all.
        assert (
            await chunks.lexical_candidates(
                tenant_id="tenant-a", search_terms=["kerberoasting"], limit=10
            )
            == ()
        )
        # No terms means no query (never a match-everything scan).
        assert (
            await chunks.lexical_candidates(
                tenant_id="tenant-a", search_terms=[], limit=10
            )
            == ()
        )
        # The limit is applied in SQL.
        assert len(
            await chunks.lexical_candidates(
                tenant_id="tenant-a", search_terms=["sshd"], limit=1
            )
        ) == 1


async def test_lexical_multi_term_query_is_an_or_not_an_and(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``plainto_tsquery`` ANDs its words, so a whole query string would collapse
    recall: the repository ORs one per-term query instead."""
    await _add_profile(session_factory, profile_id=_PROFILE_1)
    await _ingest(
        session_factory,
        document_id=_DOC_GLOBAL,
        external_key="curated:or-semantics",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        versions=(
            _VersionSeed(
                version_id=_VERSION_G1,
                number=1,
                content="ssh tokens\n",
                chunks=(
                    (_CHUNK_1, "sshd listener on the bastion", _V_AXIS_X),
                    (_CHUNK_2, "authentication_failure recorded at the gateway", _V_AXIS_Y),
                ),
            ),
        ),
        profile_id=_PROFILE_1,
    )

    async with session_factory() as session:
        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        candidates = await chunks.lexical_candidates(
            tenant_id="tenant-a",
            search_terms=["sshd", "authentication_failure"],
            limit=10,
        )
    # NEITHER chunk contains both terms, so an AND would return nothing.
    assert {candidate.view.chunk_id for candidate in candidates} == {_CHUNK_1, _CHUNK_2}


# ---------------------------------------------------------------------------
# 10. retrieval only sees the ACTIVE version
# ---------------------------------------------------------------------------


async def test_retrieval_only_sees_the_active_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _add_profile(session_factory)
    await _ingest(
        session_factory,
        document_id=_DOC_GLOBAL,
        external_key="curated:active-version",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        versions=(
            _VersionSeed(
                version_id=_VERSION_G1,
                number=1,
                content="sshbrute guidance v1\n",
                chunks=((_CHUNK_1, "sshbrute guidance v1", _V_AXIS_X),),
            ),
            _VersionSeed(
                version_id=_VERSION_A1,
                number=2,
                content="sshbrute guidance v2\n",
                chunks=((_CHUNK_2, "sshbrute guidance v2 updated", _V_AXIS_Y),),
            ),
        ),
        profile_id=_PROFILE_1,
    )

    async with session_factory() as session:
        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        lexical = await chunks.lexical_candidates(
            tenant_id="tenant-a", search_terms=["sshbrute"], limit=10
        )
        vector = await chunks.vector_candidates(
            tenant_id="tenant-a",
            embedding_profile_id=_PROFILE_1,
            query_vector=_V_AXIS_X,
            limit=10,
        )
        # Both versions still exist and are readable by id...
        documents = SqlAlchemyKnowledgeDocumentRepository(session)
        assert (
            await documents.get_version(
                tenant_id="tenant-a", document_version_id=_VERSION_G1
            )
            is not None
        )
        # ...but retrieval is pinned to the ACTIVE pointer.
        assert [candidate.view.chunk_id for candidate in lexical] == [_CHUNK_2]
        assert [candidate.view.chunk_id for candidate in vector] == [_CHUNK_2]


# ---------------------------------------------------------------------------
# 11. tenant isolation at the SQL layer
# ---------------------------------------------------------------------------


async def _seed_isolation_corpus(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _add_profile(factory, profile_id=_PROFILE_1)
    await _ingest(
        factory,
        document_id=_DOC_GLOBAL,
        external_key="curated:isolation",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        versions=(
            _VersionSeed(
                version_id=_VERSION_G1,
                number=1,
                content="tokenscope global\n",
                chunks=((_CHUNK_1, "tokenscope global guidance", _V_AXIS_X),),
            ),
        ),
        profile_id=_PROFILE_1,
    )
    await _ingest(
        factory,
        document_id=_DOC_TENANT_A,
        external_key="runbook:isolation",
        visibility=Visibility.TENANT,
        tenant_id="tenant-a",
        versions=(
            _VersionSeed(
                version_id=_VERSION_A1,
                number=1,
                content="tokenscope tenant a\n",
                chunks=((_CHUNK_2, "tokenscope tenant a runbook", _V_AXIS_Y),),
            ),
        ),
        profile_id=_PROFILE_1,
    )
    await _ingest(
        factory,
        document_id=_DOC_TENANT_B,
        external_key="runbook:isolation",
        visibility=Visibility.TENANT,
        tenant_id="tenant-b",
        versions=(
            _VersionSeed(
                version_id=_VERSION_A2,
                number=1,
                content="tokenscope tenant b\n",
                chunks=((_CHUNK_3, "tokenscope tenant b runbook", _V_BOTH),),
            ),
        ),
        profile_id=_PROFILE_1,
    )


async def test_tenant_isolation_is_applied_in_sql(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_isolation_corpus(session_factory)

    async with session_factory() as session:
        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        for_tenant_b = await chunks.lexical_candidates(
            tenant_id="tenant-b", search_terms=["tokenscope"], limit=10
        )
        vector_b = await chunks.vector_candidates(
            tenant_id="tenant-b",
            embedding_profile_id=_PROFILE_1,
            query_vector=_V_AXIS_X,
            limit=10,
        )
        for_tenant_a = await chunks.lexical_candidates(
            tenant_id="tenant-a", search_terms=["tokenscope"], limit=10
        )

        # Tenant B sees its own document plus the GLOBAL one -- never tenant A's.
        assert {candidate.view.chunk_id for candidate in for_tenant_b} == {
            _CHUNK_1,
            _CHUNK_3,
        }
        assert {candidate.view.chunk_id for candidate in vector_b} == {_CHUNK_1, _CHUNK_3}
        assert {candidate.view.chunk_id for candidate in for_tenant_a} == {
            _CHUNK_1,
            _CHUNK_2,
        }

        # Citation resolution obeys the same scope rule.
        assert await chunks.get_chunk_view(tenant_id="tenant-b", chunk_id=_CHUNK_2) is None
        assert await chunks.get_chunk_view(tenant_id="tenant-b", chunk_id=_CHUNK_1) is not None
        assert await chunks.get_chunk_view(tenant_id="tenant-a", chunk_id=_CHUNK_2) is not None


# ---------------------------------------------------------------------------
# 12. citation resolution survives retirement and version supersession
# ---------------------------------------------------------------------------


async def test_get_chunk_view_resolves_retired_and_superseded_versions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _add_profile(session_factory)
    await _ingest(
        session_factory,
        document_id=_DOC_GLOBAL,
        external_key="curated:retired",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        versions=(
            _VersionSeed(
                version_id=_VERSION_G1,
                number=1,
                content="citresolver guidance v1\n",
                chunks=((_CHUNK_1, "citresolver v1 chunk", _V_AXIS_X),),
            ),
            _VersionSeed(
                version_id=_VERSION_A1,
                number=2,
                content="citresolver guidance v2\n",
                chunks=((_CHUNK_2, "citresolver v2 chunk", _V_AXIS_Y),),
            ),
        ),
        profile_id=_PROFILE_1,
        retire=True,
    )

    async with session_factory() as session:
        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        # A citation to the SUPERSEDED version of a RETIRED document still resolves:
        # scope is the only filter, so history stays provable.
        superseded = await chunks.get_chunk_view(tenant_id="tenant-a", chunk_id=_CHUNK_1)
        assert superseded is not None
        assert superseded.document_version_id == _VERSION_G1
        assert superseded.document_status == "RETIRED"

        current = await chunks.get_chunk_view(tenant_id="tenant-a", chunk_id=_CHUNK_2)
        assert current is not None
        assert current.document_version_id == _VERSION_A1
        assert current.document_status == "RETIRED"

        # Contrast, both halves of the asymmetry:
        #  - RETRIEVAL stops at a retired document / a superseded version...
        assert (
            await chunks.lexical_candidates(
                tenant_id="tenant-a", search_terms=["citresolver"], limit=10
            )
            == ()
        )
        assert (
            await chunks.vector_candidates(
                tenant_id="tenant-a",
                embedding_profile_id=_PROFILE_1,
                query_vector=_V_AXIS_X,
                limit=10,
            )
            == ()
        )
        # ...while RESOLUTION still explains a citation captured months ago.
        assert await chunks.get_chunk_view(tenant_id="tenant-a", chunk_id=_CHUNK_1) is not None


# ---------------------------------------------------------------------------
# 15. projection state / rebuild
# ---------------------------------------------------------------------------


async def test_projection_state_tracks_empty_populated_and_rebuild(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _add_profile(session_factory, profile_id=_PROFILE_1)
    await _add_profile(
        session_factory,
        profile_id=_PROFILE_2,
        status="RETIRED",
        profile_version=2,
        model_id="text-embedding-retired",
        retired_at=_T1,
    )
    await _ingest(
        session_factory,
        document_id=_DOC_GLOBAL,
        external_key="curated:projection",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        versions=(
            _VersionSeed(version_id=_VERSION_G1, number=1, content="projection body\n"),
        ),
    )

    async with session_factory() as session:
        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        state = await chunks.projection_state(
            document_version_id=_VERSION_G1, embedding_profile_id=_PROFILE_1
        )
        assert state == ChunkProjectionState(None, None, 0)
        assert state.is_empty is True

        # An unknown version has no projection either -- reported, never raised.
        unknown = await chunks.projection_state(
            document_version_id=_VERSION_A2, embedding_profile_id=_PROFILE_1
        )
        assert unknown.is_empty is True

        # Empty input is a no-op, not an empty INSERT statement.
        await chunks.add_content_chunks(chunks=[])
        await chunks.add_embeddings(embeddings=[])
        await chunks.add_content_chunks(
            chunks=[
                _chunk(
                    chunk_id=_CHUNK_1,
                    document_id=_DOC_GLOBAL,
                    document_version_id=_VERSION_G1,
                    ordinal=0,
                    content="projection chunk one",
                ),
                _chunk(
                    chunk_id=_CHUNK_2,
                    document_id=_DOC_GLOBAL,
                    document_version_id=_VERSION_G1,
                    ordinal=1,
                    content="projection chunk two",
                ),
            ]
        )
        await chunks.add_embeddings(
            embeddings=[
                _embedding(
                    embedding_id=_EMBED_1,
                    content_chunk_id=_CHUNK_1,
                    embedding_profile_id=_PROFILE_1,
                    embedding=_V_AXIS_X,
                ),
                _embedding(
                    embedding_id=_EMBED_2,
                    content_chunk_id=_CHUNK_2,
                    embedding_profile_id=_PROFILE_1,
                    embedding=_V_AXIS_Y,
                ),
                _embedding(
                    embedding_id=UUID(int=_EMBED_1.int + 1000),
                    content_chunk_id=_CHUNK_1,
                    embedding_profile_id=_PROFILE_2,
                    embedding=_V_AXIS_X,
                ),
                _embedding(
                    embedding_id=UUID(int=_EMBED_2.int + 1000),
                    content_chunk_id=_CHUNK_2,
                    embedding_profile_id=_PROFILE_2,
                    embedding=_V_AXIS_Y,
                ),
            ]
        )
        await session.commit()

    async with session_factory() as session:
        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        populated = await chunks.projection_state(
            document_version_id=_VERSION_G1, embedding_profile_id=_PROFILE_1
        )
        also_covered = await chunks.projection_state(
            document_version_id=_VERSION_G1, embedding_profile_id=_PROFILE_2
        )
        assert populated.is_empty is False
        assert populated.embedding_profile_id == _PROFILE_1
        assert also_covered.embedding_profile_id == _PROFILE_2
        assert populated.chunker_version == CHUNKER_VERSION
        assert populated.chunk_count == 2
        assert populated.embedding_count == 4
        assert await chunks.count_for_version(document_version_id=_VERSION_G1) == 2

        # A rebuild drops ONLY the embedding projections. The content chunks are
        # the citation targets and are NOT the projection, so the count of what a
        # version IS stays exactly the same while the index is rebuilt
        # (brief section 3.1).
        removed = await chunks.delete_embeddings_for_version_generation(
            document_version_id=_VERSION_G1, generation=CHUNK_GENERATION_INITIAL
        )
        assert removed == 4
        await session.commit()

    async with session_factory() as session:
        documents = SqlAlchemyKnowledgeDocumentRepository(session)
        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        rebuilt = await chunks.projection_state(
            document_version_id=_VERSION_G1, embedding_profile_id=_PROFILE_1
        )
        # The PROJECTION is empty -- no space covers the generation any more --
        # while the version is not: "unembedded" and "no content" are different
        # states, and conflating them would make a rebuild look like a deletion.
        assert rebuilt.is_embedded is False
        assert rebuilt.embedding_profile_id is None
        assert rebuilt.embedding_count == 0
        assert rebuilt.is_empty is False
        assert rebuilt.chunk_count == 2
        assert await chunks.count_for_version(document_version_id=_VERSION_G1) == 2
        # The immutable version itself is untouched by a projection rebuild.
        assert (
            await documents.get_version(
                tenant_id="tenant-a", document_version_id=_VERSION_G1
            )
            is not None
        )
        # ...and neither are the content chunks the projection was built from.
        assert [
            chunk.id
            for chunk in await chunks.list_content_chunks(
                document_version_id=_VERSION_G1, generation=CHUNK_GENERATION_INITIAL
            )
        ] == [_CHUNK_1, _CHUNK_2]


# ---------------------------------------------------------------------------
# 14. ATT&CK techniques
# ---------------------------------------------------------------------------


async def test_attack_technique_rows_require_a_registered_release(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A technique row cannot be the place authority is answered (section 2.3).

    ``attack_technique`` carries a composite foreign key onto ``attack_release``,
    so a canonical row for an unregistered release is not merely discouraged --
    it is unrepresentable. That is what makes the release, and not the row, the
    one place "which release is current" can be read from.
    """
    async with session_factory() as session:
        techniques = SqlAlchemyAttackTechniqueRepository(session)
        with pytest.raises(IntegrityError):
            await techniques.add_many(techniques=[_technique(row_id=_TECHNIQUE_1)])
            await session.commit()


async def test_attack_release_authority_is_one_per_framework(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """At most one authoritative release per framework, enforced by the DATABASE.

    The bug this replaces was a model in which two releases could both claim to be
    current, so the invariant is proven here against the real partial unique index
    rather than against application code that promises to keep it (section 2.1).
    """
    async with session_factory() as session:
        releases = SqlAlchemyAttackReleaseRepository(session)
        await releases.add(
            release=_release(release_id=_RELEASE_141, source_release="v14.1")
        )
        await session.commit()

    async with session_factory() as session:
        releases = SqlAlchemyAttackReleaseRepository(session)
        authoritative = await releases.get_active(framework="mitre-attack")
        assert authoritative is not None
        assert authoritative.source_release == "v14.1"
        assert authoritative.is_active is True

    # Staging a second release does NOT depose the first: importing a bundle is
    # not the same act as adopting it.
    async with session_factory() as session:
        releases = SqlAlchemyAttackReleaseRepository(session)
        await releases.add(
            release=_release(
                release_id=_RELEASE_151,
                source_release="v15.1",
                status=ATTACK_RELEASE_STATUS_INACTIVE,
                activated_at=None,
            )
        )
        await session.commit()

    async with session_factory() as session:
        releases = SqlAlchemyAttackReleaseRepository(session)
        assert [
            release.source_release
            for release in await releases.list_for_framework(framework="mitre-attack")
        ] == ["v14.1", "v15.1"]
        staged = await releases.find(framework="mitre-attack", source_release="v15.1")
        assert staged is not None
        assert staged.is_active is False
        assert staged.activated_at is None
        active = await releases.get_active(framework="mitre-attack")
        assert active is not None
        assert active.source_release == "v14.1"

    # A SECOND ACTIVE release for the same framework is refused at COMMIT -- the
    # constraint, not a check somebody remembered to write.
    async with session_factory() as session:
        releases = SqlAlchemyAttackReleaseRepository(session)
        with pytest.raises(IntegrityError):
            await releases.add(
                release=_release(release_id=_UUID_UNUSED, source_release="v16.0")
            )
            await session.commit()

    # Switching authority is ONE operation, and it flips both halves together.
    async with session_factory() as session:
        releases = SqlAlchemyAttackReleaseRepository(session)
        await releases.activate(
            framework="mitre-attack", source_release="v15.1", now=_T1
        )
        await session.commit()

    async with session_factory() as session:
        releases = SqlAlchemyAttackReleaseRepository(session)
        assert [
            release.source_release
            for release in await releases.list_active()
        ] == ["v15.1"]
        previous = await releases.find(framework="mitre-attack", source_release="v14.1")
        assert previous is not None
        assert previous.status == ATTACK_RELEASE_STATUS_INACTIVE
        current = await releases.find(framework="mitre-attack", source_release="v15.1")
        assert current is not None
        assert current.is_active is True
        assert current.activated_at == _T1

    # A different framework keeps its own authority: the rule is per framework.
    async with session_factory() as session:
        releases = SqlAlchemyAttackReleaseRepository(session)
        await releases.add(
            release=_release(
                release_id=_RELEASE_OTHER,
                framework="mitre-ics",
                source_release="v15.1",
            )
        )
        await session.commit()

    async with session_factory() as session:
        releases = SqlAlchemyAttackReleaseRepository(session)
        assert [
            (release.framework, release.source_release)
            for release in await releases.list_active()
        ] == [("mitre-attack", "v15.1"), ("mitre-ics", "v15.1")]


async def test_attack_techniques_are_unique_per_release(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        releases = SqlAlchemyAttackReleaseRepository(session)
        await releases.add(
            release=_release(release_id=_RELEASE_141, source_release="v14.1")
        )
        await releases.add(
            release=_release(
                release_id=_RELEASE_151,
                source_release="v15.1",
                status=ATTACK_RELEASE_STATUS_INACTIVE,
                activated_at=None,
            )
        )
        await session.commit()

    async with session_factory() as session:
        techniques = SqlAlchemyAttackTechniqueRepository(session)
        await techniques.add_many(techniques=[])
        await techniques.add_many(
            techniques=[
                _technique(row_id=_TECHNIQUE_1),
                _technique(row_id=_TECHNIQUE_2, technique_id="T1059", name="Command and Scripting"),
            ]
        )
        await session.commit()

    async with session_factory() as session:
        techniques = SqlAlchemyAttackTechniqueRepository(session)
        found = await techniques.find(
            framework="mitre-attack", technique_id="T1110", source_release="v15.1"
        )
        assert found is not None
        assert found.id == _TECHNIQUE_1
        assert found.name == "Brute Force"
        assert found.tactics == ("credential-access", "defense-evasion")
        assert found.platforms == ("Linux", "Windows")
        assert found.active is True
        assert found.created_at == _T0
        assert await techniques.count_for_release(
            framework="mitre-attack", source_release="v15.1"
        ) == 2
        assert await techniques.count_for_release(
            framework="mitre-attack", source_release="v14.0"
        ) == 0
        assert (
            await techniques.find(
                framework="mitre-attack", technique_id="T1110", source_release="v14.0"
            )
            is None
        )

    # Re-importing the same release cannot duplicate a technique.
    async with session_factory() as session:
        techniques = SqlAlchemyAttackTechniqueRepository(session)
        with pytest.raises(IntegrityError):
            await techniques.add_many(
                techniques=[_technique(row_id=_VERSION_A2, technique_id="T1110")]
            )
            await session.commit()

    # A NEW release is a new set of rows; old releases are never deleted.
    async with session_factory() as session:
        releases = SqlAlchemyAttackReleaseRepository(session)
        await releases.add(
            release=_release(
                release_id=_RELEASE_152,
                source_release="v15.2",
                status=ATTACK_RELEASE_STATUS_INACTIVE,
                activated_at=None,
            )
        )
        await session.commit()

    async with session_factory() as session:
        techniques = SqlAlchemyAttackTechniqueRepository(session)
        await techniques.add_many(
            techniques=[_technique(row_id=_VERSION_G1, source_release="v15.2")]
        )
        await session.commit()

    async with session_factory() as session:
        techniques = SqlAlchemyAttackTechniqueRepository(session)
        assert await techniques.count_for_release(
            framework="mitre-attack", source_release="v15.1"
        ) == 2
        assert await techniques.count_for_release(
            framework="mitre-attack", source_release="v15.2"
        ) == 1
        assert (
            await techniques.find(
                framework="mitre-attack", technique_id="T1110", source_release="v15.2"
            )
            is not None
        )


async def test_row_to_document_never_replays_a_creation_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A loaded aggregate carries no pending events -- there was no new fact."""
    async with session_factory() as session:
        documents = SqlAlchemyKnowledgeDocumentRepository(session)
        await documents.add(
            document=_document(
                document_id=_DOC_GLOBAL,
                external_key="curated:events",
                visibility=Visibility.GLOBAL,
                tenant_id=None,
            )
        )
        await session.commit()

    created = KnowledgeDocument.create(
        id=_DOC_TENANT_A,
        source_kind=SourceKind.CURATED_GUIDANCE,
        external_key="curated:events",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        title="Emitted",
        now=_T0,
    )
    assert created.pending_events  # the factory DOES emit

    async with session_factory() as session:
        documents = SqlAlchemyKnowledgeDocumentRepository(session)
        loaded = await documents.get(tenant_id="tenant-a", document_id=_DOC_GLOBAL)
    assert loaded is not None
    assert loaded.pending_events == []
    assert DocumentStatus(loaded.status.value) is DocumentStatus.ACTIVE


# ---------------------------------------------------------------------------
# 16. citation durability across a rebuilt retrieval projection
# ---------------------------------------------------------------------------


async def test_a_citation_survives_re_embedding_under_the_same_generation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Re-embedding replaces VECTORS; it must not move a citation target.

    The citation names an immutable ``knowledge_content_chunk`` id and the hash of
    its content. Re-embedding deletes and re-inserts ``knowledge_chunk_embedding``
    rows beside it, so the citation must be untouched -- which is exactly what the
    old single-table model could not promise, because the row a citation named was
    the row the re-index deleted (brief section 3.2).
    """
    await _add_profile(session_factory)
    await _ingest(
        session_factory,
        document_id=_DOC_GLOBAL,
        external_key="curated:reembed",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        versions=(
            _VersionSeed(
                version_id=_VERSION_G1,
                number=1,
                content="reembed guidance\n",
                chunks=((_CHUNK_1, "reembed citation target", _V_AXIS_X),),
            ),
        ),
        profile_id=_PROFILE_1,
    )
    citation = format_citation_id(
        chunk_id=_CHUNK_1, content_hash=_hash("reembed citation target")
    )

    async with session_factory() as session:
        repository = SqlAlchemyKnowledgeChunkRepository(session)
        resolver = KnowledgeCitationResolver(chunks=repository)
        assert (
            await resolver.resolve(tenant_id="tenant-a", citation_id=citation)
        ).resolved

        # The real re-embedding path: reuse the SAME content chunks, drop the
        # projection, write fresh vectors beside them.
        stored = await repository.list_content_chunks(
            document_version_id=_VERSION_G1, generation=CHUNK_GENERATION_INITIAL
        )
        assert [chunk.id for chunk in stored] == [_CHUNK_1]
        removed = await repository.delete_embeddings_for_version_generation(
            document_version_id=_VERSION_G1, generation=CHUNK_GENERATION_INITIAL
        )
        assert removed == 1
        await repository.add_embeddings(
            embeddings=[
                _embedding(
                    embedding_id=_EMBED_R1,
                    content_chunk_id=chunk.id,
                    embedding_profile_id=_PROFILE_1,
                    embedding=_V_AXIS_Y,
                )
                for chunk in stored
            ]
        )
        await session.commit()

    async with session_factory() as session:
        repository = SqlAlchemyKnowledgeChunkRepository(session)
        # The old PROJECTION row is gone...
        assert await session.get(KnowledgeChunkEmbeddingRow, _EMBED_1) is None
        # ...and the citation is not.
        resolution = await KnowledgeCitationResolver(chunks=repository).resolve(
            tenant_id="tenant-a", citation_id=citation
        )
        assert resolution.resolved is True
        assert resolution.chunk_id == _CHUNK_1
        assert resolution.document_version_id == _VERSION_G1
        assert resolution.excerpt == "reembed citation target"
        # Retrieval follows the rebuilt projection, not the citation.
        candidates = await repository.vector_candidates(
            tenant_id="tenant-a",
            embedding_profile_id=_PROFILE_1,
            query_vector=_V_AXIS_Y,
            limit=10,
        )
        assert [candidate.view.chunk_id for candidate in candidates] == [_CHUNK_1]


async def test_a_citation_survives_a_projection_rebuild_under_a_new_profile(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A new embedding space re-projects the corpus; it does not re-author it."""
    await _add_profile(session_factory, profile_id=_PROFILE_1)
    await _add_profile(
        session_factory,
        profile_id=_PROFILE_2,
        model_id="replacement-model",
        status="RETIRED",
        retired_at=_T1,
    )
    await _ingest(
        session_factory,
        document_id=_DOC_GLOBAL,
        external_key="curated:reprofile",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        versions=(
            _VersionSeed(
                version_id=_VERSION_G1,
                number=1,
                content="reprofile guidance\n",
                chunks=((_CHUNK_1, "reprofile citation target", _V_AXIS_X),),
            ),
        ),
        profile_id=_PROFILE_1,
    )
    citation = format_citation_id(
        chunk_id=_CHUNK_1, content_hash=_hash("reprofile citation target")
    )

    async with session_factory() as session:
        repository = SqlAlchemyKnowledgeChunkRepository(session)
        assert (
            await repository.delete_embeddings_for_profile(
                document_version_id=_VERSION_G1, embedding_profile_id=_PROFILE_1
            )
            == 1
        )
        await repository.add_embeddings(
            embeddings=[
                _embedding(
                    embedding_id=_EMBED_R2,
                    content_chunk_id=_CHUNK_1,
                    embedding_profile_id=_PROFILE_2,
                    embedding=_V_AXIS_Y,
                )
            ]
        )
        await session.commit()

    async with session_factory() as session:
        repository = SqlAlchemyKnowledgeChunkRepository(session)
        resolution = await KnowledgeCitationResolver(chunks=repository).resolve(
            tenant_id="tenant-a", citation_id=citation
        )
        assert resolution.resolved is True
        assert resolution.chunk_id == _CHUNK_1
        # The rebuilt projection is now the only space with a vector, and the
        # content chunk it points at did not move.
        assert (
            await repository.vector_candidates(
                tenant_id="tenant-a",
                embedding_profile_id=_PROFILE_1,
                query_vector=_V_AXIS_X,
                limit=10,
            )
            == ()
        )
        rebuilt = await repository.vector_candidates(
            tenant_id="tenant-a",
            embedding_profile_id=_PROFILE_2,
            query_vector=_V_AXIS_Y,
            limit=10,
        )
        assert [candidate.view.chunk_id for candidate in rebuilt] == [_CHUNK_1]


async def test_rechunking_appends_a_generation_and_keeps_the_older_citation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A chunker change must not DELETE the chunking citations already point at.

    The new generation is what retrieval serves; the old generation stays readable
    by id, so a citation captured under the previous chunker still explains itself
    (brief section 3.3).
    """
    await _add_profile(session_factory, profile_id=_PROFILE_1)
    await _ingest(
        session_factory,
        document_id=_DOC_GLOBAL,
        external_key="curated:rechunk",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        versions=(
            _VersionSeed(
                version_id=_VERSION_G1,
                number=1,
                content="rechunk guidance\n",
                chunks=((_CHUNK_1, "rechunkedfirsttoken original chunk", _V_AXIS_X),),
            ),
        ),
        profile_id=_PROFILE_1,
    )
    stale_citation = format_citation_id(
        chunk_id=_CHUNK_1, content_hash=_hash("rechunkedfirsttoken original chunk")
    )

    async with session_factory() as session:
        repository = SqlAlchemyKnowledgeChunkRepository(session)
        # A rechunking of the SAME immutable version: generation 2, new ordinals,
        # new content chunks. Generation 1 is appended to, never replaced.
        await repository.add_content_chunks(
            chunks=[
                _chunk(
                    chunk_id=_CHUNK_G2,
                    document_id=_DOC_GLOBAL,
                    document_version_id=_VERSION_G1,
                    ordinal=0,
                    content="rechunkedfirsttoken finer chunk",
                    generation=2,
                    chunker_version="chunker/v2",
                )
            ]
        )
        await repository.add_embeddings(
            embeddings=[
                _embedding(
                    embedding_id=_EMBED_R3,
                    content_chunk_id=_CHUNK_G2,
                    embedding_profile_id=_PROFILE_1,
                    embedding=_V_AXIS_Y,
                )
            ]
        )
        await session.commit()

    async with session_factory() as session:
        repository = SqlAlchemyKnowledgeChunkRepository(session)
        # The CURRENT generation is what retrieval and the projection describe...
        assert await repository.count_for_version(document_version_id=_VERSION_G1) == 1
        state = await repository.projection_state(
            document_version_id=_VERSION_G1, embedding_profile_id=_PROFILE_1
        )
        assert state.generation == 2
        assert state.chunker_version == "chunker/v2"
        lexical = await repository.lexical_candidates(
            tenant_id="tenant-a", search_terms=["rechunkedfirsttoken"], limit=10
        )
        assert [candidate.view.chunk_id for candidate in lexical] == [_CHUNK_G2]
        assert lexical[0].view.chunk_generation == 2

        # ...while the OLDER generation is still exactly where it was.
        older = await repository.list_content_chunks(
            document_version_id=_VERSION_G1, generation=CHUNK_GENERATION_INITIAL
        )
        assert [chunk.id for chunk in older] == [_CHUNK_1]
        resolution = await KnowledgeCitationResolver(chunks=repository).resolve(
            tenant_id="tenant-a", citation_id=stale_citation
        )
        assert resolution.resolved is True
        assert resolution.chunk_id == _CHUNK_1
        assert resolution.excerpt == "rechunkedfirsttoken original chunk"


# ---------------------------------------------------------------------------
# 17. citation resolution: scope + content/hash integrity
# ---------------------------------------------------------------------------


async def _seed_one_citation_corpus(
    factory: async_sessionmaker[AsyncSession],
) -> str:
    await _add_profile(factory, profile_id=_PROFILE_1)
    await _ingest(
        factory,
        document_id=_DOC_TENANT_A,
        external_key="runbook:integrity",
        visibility=Visibility.TENANT,
        tenant_id="tenant-a",
        versions=(
            _VersionSeed(
                version_id=_VERSION_A1,
                number=1,
                content="integrity guidance\n",
                chunks=((_CHUNK_1, "integritychecked citation body", _V_AXIS_X),),
            ),
        ),
        profile_id=_PROFILE_1,
    )
    return format_citation_id(
        chunk_id=_CHUNK_1, content_hash=_hash("integritychecked citation body")
    )


async def test_citation_resolution_is_tenant_scoped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    citation = await _seed_one_citation_corpus(session_factory)

    async with session_factory() as session:
        resolver = KnowledgeCitationResolver(
            chunks=SqlAlchemyKnowledgeChunkRepository(session)
        )
        # The owning tenant resolves it...
        assert (
            await resolver.resolve(tenant_id="tenant-a", citation_id=citation)
        ).resolved is True
        # ...and another tenant cannot even see that it exists. The repository
        # filter is the first gate; the resolver re-validates the scope itself.
        other = await resolver.resolve(tenant_id="tenant-b", citation_id=citation)
        assert other.resolved is False
        assert other.reason == "CHUNK_NOT_FOUND"


async def test_citation_resolution_rejects_a_tampered_stored_hash(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The stored hash is a CLAIM beside the content, never evidence (section 3.4)."""
    citation = await _seed_one_citation_corpus(session_factory)

    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE copilot.knowledge_content_chunk SET content_hash = :hash "
                "WHERE id = :chunk_id"
            ),
            {"hash": "b" * 64, "chunk_id": _CHUNK_1},
        )
        await session.commit()

    async with session_factory() as session:
        resolution = await KnowledgeCitationResolver(
            chunks=SqlAlchemyKnowledgeChunkRepository(session)
        ).resolve(tenant_id="tenant-a", citation_id=citation)
        assert resolution.resolved is False
        assert resolution.reason == "CONTENT_INTEGRITY_MISMATCH"


async def test_citation_resolution_rejects_tampered_content_under_an_unchanged_hash(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Editing the text without editing the hash is the OTHER half of the same lie."""
    citation = await _seed_one_citation_corpus(session_factory)

    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE copilot.knowledge_content_chunk SET content = :content "
                "WHERE id = :chunk_id"
            ),
            {"content": "integritychecked citation bodX", "chunk_id": _CHUNK_1},
        )
        await session.commit()

    async with session_factory() as session:
        resolution = await KnowledgeCitationResolver(
            chunks=SqlAlchemyKnowledgeChunkRepository(session)
        ).resolve(tenant_id="tenant-a", citation_id=citation)
        assert resolution.resolved is False
        assert resolution.reason == "CONTENT_INTEGRITY_MISMATCH"


async def test_citation_resolution_rejects_a_prefix_that_does_not_match_the_content(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A citation to the right chunk with the WRONG content hash resolves to nothing."""
    await _seed_one_citation_corpus(session_factory)
    forged = format_citation_id(chunk_id=_CHUNK_1, content_hash=_hash("some other body"))

    async with session_factory() as session:
        resolution = await KnowledgeCitationResolver(
            chunks=SqlAlchemyKnowledgeChunkRepository(session)
        ).resolve(tenant_id="tenant-a", citation_id=forged)
        assert resolution.resolved is False
        assert resolution.reason == "CONTENT_INTEGRITY_MISMATCH"


# ---------------------------------------------------------------------------
# 18. the sealed-evaluation corpus snapshot (brief sections 5.3/5.5/5.6)
# ---------------------------------------------------------------------------

_CORPUS_TENANT_DOCS = (
    (UUID("00000000-0000-4000-8000-0000000a0001"), "curated:corpus-a", "tenant-a"),
    (UUID("00000000-0000-4000-8000-0000000a0002"), "curated:corpus-b", "tenant-b"),
)

_CORPUS_FACT_FIELDS = {
    "visibility",
    "tenant_id",
    "source_kind",
    "external_key",
    "document_version_number",
    "document_content_hash",
    "chunk_generation",
    "ordinal",
    "chunk_content_hash",
}


async def _seed_corpus_for_fingerprint(
    factory: async_sessionmaker[AsyncSession],
    *,
    chunk_id: UUID,
) -> None:
    """Ingest an identical corpus. Surrogates differ; SEMANTIC facts do not."""
    await _add_profile(factory, profile_id=_PROFILE_1)
    # A GLOBAL document every tenant sees, plus one TENANT document each.
    await _ingest(
        factory,
        document_id=_DOC_GLOBAL,
        external_key="shared:corpus",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        versions=(
            _VersionSeed(
                version_id=_VERSION_G1,
                number=1,
                content="shared corpus body\n",
                chunks=((chunk_id, "shared corpus chunk", _V_AXIS_X),),
            ),
        ),
        profile_id=_PROFILE_1,
    )
    for offset, (document_id, external_key, tenant_id) in enumerate(_CORPUS_TENANT_DOCS):
        await _ingest(
            factory,
            document_id=document_id,
            external_key=external_key,
            visibility=Visibility.TENANT,
            tenant_id=tenant_id,
            versions=(
                _VersionSeed(
                    version_id=UUID(int=_VERSION_A1.int + offset),
                    number=1,
                    content=f"{external_key} body\n",
                    chunks=(
                        (
                            UUID(int=chunk_id.int + offset + 1),
                            f"{external_key} chunk",
                            _V_AXIS_Y,
                        ),
                    ),
                ),
            ),
            profile_id=_PROFILE_1,
        )


async def test_corpus_snapshot_is_tenant_scoped_and_free_of_database_identity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The snapshot is built in SQL, in the stable semantic order, with no UUID.

    A fingerprint over database-generated ids could not be reproduced by a second,
    independently ingested database -- which is the entire point of the sealed
    corpus (brief sections 5.5/44).
    """
    await _seed_corpus_for_fingerprint(session_factory, chunk_id=_CHUNK_1)

    async with session_factory() as session:
        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        snapshot = await chunks.corpus_snapshot(tenant_id="tenant-a")
        other = await chunks.corpus_snapshot(tenant_id="tenant-b")

    # tenant-a sees its own TENANT document and the GLOBAL one; tenant-b sees the
    # other tenant's document instead. Neither sees all three.
    # Ordered by the stable semantic key -- (source_kind, visibility, tenant_id,
    # external_key, ...) -- so GLOBAL precedes TENANT and the order is a fact
    # about the corpus, not about the order rows came back in.
    assert [fact.external_key for fact in snapshot.chunks] == [
        "shared:corpus",
        "curated:corpus-a",
    ]
    assert [fact.external_key for fact in other.chunks] == [
        "shared:corpus",
        "curated:corpus-b",
    ]
    assert snapshot.document_count == 2
    assert snapshot.version_count == 2
    assert snapshot.chunk_count == 2
    # The fact a fingerprint is taken over carries no surrogate and no ambient
    # value: nothing in it can differ between two identically ingested databases.
    assert {field.name for field in fields(CorpusChunkFact)} == _CORPUS_FACT_FIELDS
    for fact in snapshot.chunks:
        for field in fields(CorpusChunkFact):
            value = getattr(fact, field.name)
            assert not isinstance(value, UUID), field.name
            assert not isinstance(value, datetime), field.name


async def test_two_independent_ingestions_of_one_corpus_produce_one_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same corpus, different UUIDs and a different insert order: same facts.

    This is the reproducibility property the sealed baseline rests on, proved at
    the SQL layer, and it is checked all the way through to the fingerprint the
    runner writes into an evaluation artifact (brief section 5.6).
    """
    await _seed_corpus_for_fingerprint(session_factory, chunk_id=_CHUNK_1)
    async with session_factory() as session:
        first = await SqlAlchemyKnowledgeChunkRepository(session).corpus_snapshot(
            tenant_id="tenant-a"
        )

    async with session_factory() as session:
        targets = ", ".join(f"copilot.{table}" for table in _TRUNCATE)
        await session.execute(text(f"TRUNCATE {targets} RESTART IDENTITY CASCADE"))
        await session.commit()

    await _seed_corpus_for_fingerprint(session_factory, chunk_id=_CHUNK_5)
    async with session_factory() as session:
        second = await SqlAlchemyKnowledgeChunkRepository(session).corpus_snapshot(
            tenant_id="tenant-a"
        )

    assert first.chunks == second.chunks
    assert first.document_count == second.document_count
    assert first.version_count == second.version_count
    assert first.chunk_count == second.chunk_count

    def _facts(snapshot: object) -> list[CorpusFact]:
        return [
            CorpusFact(
                visibility=fact.visibility.value,
                tenant_id=fact.tenant_id or "",
                source_kind=fact.source_kind.value,
                external_key=fact.external_key,
                document_version_number=fact.document_version_number,
                document_content_hash=fact.document_content_hash,
                chunk_generation=fact.chunk_generation,
                ordinal=fact.ordinal,
                chunk_content_hash=fact.chunk_content_hash,
            )
            for fact in snapshot.chunks  # type: ignore[attr-defined]
        ]

    assert corpus_fingerprint(_facts(first)) == corpus_fingerprint(_facts(second))
    sealed = corpus_identity(mode=CorpusMode.SEALED, facts=_facts(first))
    assert sealed == corpus_identity(mode=CorpusMode.SEALED, facts=_facts(second))
    assert sealed.eligible_document_count == 2
    assert sealed.eligible_version_count == 2
    assert sealed.eligible_chunk_count == 2
    assert len(sealed.fingerprint) == 64


def test_truncate_targets_only_knowledge_tables() -> None:
    """Guard-rail: this module may never clear a table outside the ones it owns."""
    assert set(_TRUNCATE) == {
        # The pre-closure chunk table: superseded, still present, never live.
        "knowledge_chunk",
        # The closure's immutable citation target and its rebuildable projection.
        "knowledge_content_chunk",
        "knowledge_chunk_embedding",
        "knowledge_document_version",
        "knowledge_document",
        "embedding_profile",
        # ATT&CK canonical rows and the release that owns their authority.
        "attack_technique",
        "attack_release",
    }


# ---------------------------------------------------------------------------
# 19. sealed-baseline reproducibility at the RUNNER level (brief section 5.6)
# ---------------------------------------------------------------------------

#: Surrogate bases for the two independent ingestions. Every id one seed writes
#: is derived from its base, so the two corpora share no UUID at all -- which is
#: the point: neither the rankings nor the metrics may be able to depend on one.
_RANK_BASE_A = UUID("00000000-0000-4000-8000-000000001000")
_RANK_BASE_B = UUID("00000000-0000-4000-8000-000000002000")

#: The deterministic fixture's dimension. It is a PLUMBING provider (see its
#: module docstring): this test proves two independent databases RANK AND SCORE
#: IDENTICALLY, which is a statement about reproducibility. It is not, and must
#: never be read as, evidence of retrieval quality.
_RANK_DIMENSION = 64

_RANK_DOCUMENTS = (
    (
        "curated:brute-force",
        "T1110 Brute Force",
        (
            "T1110 sshd authentication_failure brute force on the bastion",
            "kerberos ticket lifetime and service principal rotation",
        ),
    ),
    (
        "curated:spraying",
        "Password Spraying",
        ("password spraying against the vpn gateway",),
    ),
)

_RANK_CASES = (
    CorpusCase(
        case_id="c-brute-force",
        tenant_id="tenant-a",
        topic="brute force sshd authentication failure",
        context_terms=("T1110", "sshd"),
        relevant_document_keys=("curated:brute-force",),
        relevant_chunk_labels=(),
        forbidden_document_keys=(),
        expected_limit=5,
        category=CATEGORY_ATTACK_QUERY,
    ),
    CorpusCase(
        case_id="c-password-spraying",
        tenant_id="tenant-a",
        topic="password spraying vpn gateway",
        context_terms=("vpn",),
        relevant_document_keys=("curated:spraying",),
        relevant_chunk_labels=(),
        forbidden_document_keys=(),
        expected_limit=5,
        category=CATEGORY_ATTACK_QUERY,
    ),
)


async def _add_deterministic_rank_profile(
    factory: async_sessionmaker[AsyncSession],
    *,
    provider: DeterministicEmbeddingProvider,
) -> None:
    """Register the fixture's embedding space as the one ACTIVE profile.

    The row is built from the PROVIDER's own descriptor rather than from
    hand-written strings, so the stored identity and the vectors stored beside it
    cannot drift apart -- the same reason the assertion below can compare across
    two independent databases at all.
    """
    descriptor = provider.descriptor
    async with factory() as session:
        profiles = SqlAlchemyEmbeddingProfileRepository(session)
        await profiles.add(
            profile=EmbeddingProfileRecord(
                id=_PROFILE_1,
                provider=descriptor.provider,
                model_id=descriptor.model_id,
                dimension=descriptor.dimension,
                distance_metric=descriptor.distance_metric,
                normalization=descriptor.normalization,
                profile_version=descriptor.profile_version,
                status="ACTIVE",
                created_at=_T0,
                retired_at=None,
            )
        )
        await session.commit()


async def _seed_ranking_corpus(
    factory: async_sessionmaker[AsyncSession],
    *,
    base: UUID,
    reverse: bool,
) -> dict[str, UUID]:
    """Ingest the ranking corpus with surrogates derived from ``base``.

    ``reverse`` flips the INSERT order, so the second run differs from the first
    in both the ids it writes and the order the rows arrive in -- the two things
    a ranking or a fingerprint that secretly depended on row order would notice.
    """
    provider = DeterministicEmbeddingProvider(dimension=_RANK_DIMENSION)
    await _add_deterministic_rank_profile(factory, provider=provider)

    document_ids: dict[str, UUID] = {}
    offset = 0
    documents = list(reversed(_RANK_DOCUMENTS)) if reverse else list(_RANK_DOCUMENTS)
    for external_key, title, bodies in documents:
        document_id = UUID(int=base.int + offset)
        version_id = UUID(int=base.int + offset + 1)
        offset += 2
        chunk_ids = [UUID(int=base.int + offset + index) for index in range(len(bodies))]
        offset += len(bodies)
        batch = await provider.embed_documents(list(bodies))
        await _ingest(
            factory,
            document_id=document_id,
            external_key=external_key,
            visibility=Visibility.GLOBAL,
            tenant_id=None,
            versions=(
                _VersionSeed(
                    version_id=version_id,
                    number=1,
                    content=f"{title} body\n",
                    chunks=tuple(
                        (chunk_id, body, vector.values)
                        for chunk_id, body, vector in zip(
                            chunk_ids, bodies, batch.vectors, strict=True
                        )
                    ),
                ),
            ),
            profile_id=_PROFILE_1,
        )
        document_ids[external_key] = document_id
    return document_ids


async def _run_ranking_suite(
    factory: async_sessionmaker[AsyncSession],
    *,
    document_ids: dict[str, UUID],
) -> tuple[dict[tuple[str, str], tuple[str, ...]], SuiteResult]:
    """Drive the real retrieval service through the sealed runner.

    Nothing here is faked below the database: the lexical channel is PostgreSQL
    FTS, the vector channel is pgvector over the real projection, and the hybrid
    channel is the service's own RRF. Only the embedding provider is the
    deterministic fixture, which is what makes the run reproducible enough to
    compare two databases at all.
    """
    provider = DeterministicEmbeddingProvider(dimension=_RANK_DIMENSION)
    by_id = {document_id: key for key, document_id in document_ids.items()}
    rankings: dict[tuple[str, str], tuple[str, ...]] = {}

    async def retrieve(
        tenant_id: str, query: EvalQuery, mode: str
    ) -> Sequence[RetrievedHit]:
        async with factory() as session:
            service = KnowledgeRetrievalService(
                chunks=SqlAlchemyKnowledgeChunkRepository(session),
                embedding_profiles=SqlAlchemyEmbeddingProfileRepository(session),
                embedding_provider=provider,
                clock=SystemClock(),
            )
            result = await service.retrieve(
                tenant_id=tenant_id,
                query=KnowledgeQuery(
                    topic=query.topic,
                    context_terms=query.context_terms,
                    limit=query.limit,
                ),
                mode=RetrievalMode(mode),
            )
        hits = tuple(
            RetrievedHit(
                citation_id=hit.citation_id,
                document_key=by_id[hit.document_id],
                document_id=str(hit.document_id),
                document_version_id=str(hit.document_version_id),
                # A retrieval hit exposes no chunk ordinal, and the runner does
                # not score one; the position in the ranking is the honest value
                # to put here rather than a fabricated chunk index.
                chunk_ordinal=position,
                title=hit.title,
                excerpt=hit.excerpt,
                source_kind=hit.source_kind.value,
                language=hit.language,
                source_version=hit.source_version,
                owning_tenant_id=None,
                resolved=False,
            )
            for position, hit in enumerate(result.hits)
        )
        rankings[(RetrievalMode(mode).value, query.case_id)] = tuple(
            hit.document_key for hit in hits
        )
        return hits

    async def resolve(tenant_id: str, citation_id: str) -> bool:
        async with factory() as session:
            resolution = await KnowledgeCitationResolver(
                chunks=SqlAlchemyKnowledgeChunkRepository(session)
            ).resolve(tenant_id=tenant_id, citation_id=citation_id)
        return resolution.resolved

    suite = await run_suite(cases=_RANK_CASES, retrieve=retrieve, resolve=resolve)
    return rankings, suite


async def test_two_fresh_databases_rank_and_score_one_corpus_identically(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Section 5.6: the baseline is reproducible, not merely repeatable.

    Two ingestions of the same corpus -- different surrogates, different insert
    order, one database truncated back to empty in between -- must produce
    IDENTICAL lexical, vector and hybrid rankings and identical Recall / MRR /
    nDCG. A ranking that fell back on a row UUID as its final tie-break would
    pass every other test in this module and fail here, which is exactly why
    this one exists.
    """
    first_ids = await _seed_ranking_corpus(session_factory, base=_RANK_BASE_A, reverse=False)
    first_rankings, first_suite = await _run_ranking_suite(
        session_factory, document_ids=first_ids
    )

    async with session_factory() as session:
        targets = ", ".join(f"copilot.{table}" for table in _TRUNCATE)
        await session.execute(text(f"TRUNCATE {targets} RESTART IDENTITY CASCADE"))
        await session.commit()

    second_ids = await _seed_ranking_corpus(session_factory, base=_RANK_BASE_B, reverse=True)
    second_rankings, second_suite = await _run_ranking_suite(
        session_factory, document_ids=second_ids
    )

    # The two corpora share no surrogate at all, so nothing below can be passing
    # by coincidence of identical ids.
    assert not set(first_ids.values()) & set(second_ids.values())
    assert first_rankings == second_rankings
    # Non-vacuous: every mode really did return a ranking for every case.
    assert set(first_rankings) == {
        (mode.value, case.case_id)
        for mode in (RetrievalMode.LEXICAL_ONLY, RetrievalMode.VECTOR_ONLY, RetrievalMode.HYBRID)
        for case in _RANK_CASES
    }
    assert all(ranking for ranking in first_rankings.values())

    for first_mode, second_mode in zip(first_suite.modes, second_suite.modes, strict=True):
        assert first_mode.mode == second_mode.mode
        assert first_mode.available is second_mode.available is True
        assert first_mode.mean_recall_at_k == second_mode.mean_recall_at_k
        assert first_mode.mean_reciprocal_rank == second_mode.mean_reciprocal_rank
        assert first_mode.mean_ndcg_at_k == second_mode.mean_ndcg_at_k
        # Per-case results, compared on everything that is NOT a surrogate. The
        # citation handle embeds the content chunk's UUID by design, and the two
        # runs write different UUIDs, so the handle is the one field that must
        # differ -- comparing it would assert that two independent databases
        # happen to share ids, which is the opposite of what this test proves.
        assert [
            (
                result.case_id,
                result.category,
                result.tenant_id,
                result.ranked_document_keys,
                result.returned_document_keys,
                result.top_document_key,
                result.first_relevant_document_key,
                tuple((hit.document_key, hit.resolved) for hit in result.hits),
            )
            for result in first_mode.case_results
        ] == [
            (
                result.case_id,
                result.category,
                result.tenant_id,
                result.ranked_document_keys,
                result.returned_document_keys,
                result.top_document_key,
                result.first_relevant_document_key,
                tuple((hit.document_key, hit.resolved) for hit in result.hits),
            )
            for result in second_mode.case_results
        ]
        assert first_mode.hits_returned == second_mode.hits_returned
        assert first_mode.resolved_hits == second_mode.resolved_hits
        assert first_mode.citation_resolution_rate == second_mode.citation_resolution_rate
        assert first_mode.cross_tenant_leakage_count == second_mode.cross_tenant_leakage_count

    assert first_suite.hybrid_gate == second_suite.hybrid_gate

# ---------------------------------------------------------------------------
# ATT&CK import/stage/cutover against the real schema (closure-3 section 26)
#
# The unit tests prove the handler's decisions against doubles. These prove the
# same flow against PostgreSQL + pgvector: the cutover's SQL (advisory lock,
# set-based mirror, pointer moves), the projection repositories, and the full
# stage -> activate -> reactivate cycle with real chunks and real embeddings.
# ---------------------------------------------------------------------------

_T1110_STIX = "attack-pattern--11111111-1111-4111-8111-111111111111"
_T1059_STIX = "attack-pattern--22222222-2222-4222-8222-222222222222"
# (unused; SystemClock supplies handler time)


def _pattern(
    *,
    stix_id: str,
    technique_id: str,
    name: str,
    description: str,
) -> dict[str, object]:
    return {
        "type": "attack-pattern",
        "id": stix_id,
        "name": name,
        "description": description,
        "external_references": [
            {
                "source_name": "mitre-attack",
                "external_id": technique_id,
                "url": f"https://attack.mitre.org/techniques/{technique_id}",
            }
        ],
        "kill_chain_phases": [
            {"kill_chain_name": "mitre-attack", "phase_name": "credential-access"}
        ],
        "x_mitre_platforms": ["Linux"],
        "x_mitre_domains": ["enterprise-attack"],
    }


def _attack_bundle(*, t1110_description: str) -> bytes:
    import json as _json

    return _json.dumps(
        {
            "type": "bundle",
            "id": "bundle--00000000-0000-4000-8000-000000000000",
            "spec_version": "2.1",
            "objects": [
                _pattern(
                    stix_id=_T1110_STIX,
                    technique_id="T1110",
                    name="Brute Force",
                    description=t1110_description,
                ),
                _pattern(
                    stix_id=_T1059_STIX,
                    technique_id="T1059",
                    name="Command and Scripting Interpreter",
                    description="Adversaries may abuse command shells.",
                ),
            ],
        }
    ).encode("utf-8")


def _attack_setup(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[AttackImportHandler, DeterministicEmbeddingProvider]:
    """The REAL importer, wired as the container wires it.

    The ingestion handler carries the MITRE capability (the container's
    ``attack_projection_ingestion_handler`` shape), staged against the real
    repositories through a real UnitOfWork per transaction. Anything here that
    drifts from the container's wiring is a test bug, not flexibility.
    """
    provider = DeterministicEmbeddingProvider(dimension=4)

    def _factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    ingestion = KnowledgeIngestionHandler(
        unit_of_work_factory=_factory,
        embedding_provider=provider,
        chunker=StructureAwareChunker(),
        clock=SystemClock(),
        allowed_source_kinds=frozenset({SourceKind.MITRE_ATTACK}),
    )
    handler = AttackImportHandler(
        unit_of_work_factory=_factory,
        parser=MitreStixAttackSource(),
        ingestion=ingestion,
        clock=SystemClock(),
    )
    return handler, provider


async def _served_t1110(session_factory: async_sessionmaker[AsyncSession]) -> str:
    """What normal retrieval would serve for T1110: the ACTIVE version's bytes."""
    async with session_factory() as session:
        documents = SqlAlchemyKnowledgeDocumentRepository(session)
        document = await documents.find_by_external_key(
            tenant_id=None,
            source_kind=SourceKind.MITRE_ATTACK,
            external_key="mitre-attack:T1110",
            visibility=Visibility.GLOBAL,
        )
        assert document is not None
        assert document.active_version_id is not None
        version = await documents.get_version(
            tenant_id=None, document_version_id=document.active_version_id
        )
        assert version is not None
        return version.normalized_content


async def _active_release(session_factory: async_sessionmaker[AsyncSession]) -> str:
    async with session_factory() as session:
        releases = SqlAlchemyAttackReleaseRepository(session)
        active = await releases.get_active(framework=FRAMEWORK)
        assert active is not None
        return active.source_release


async def test_attack_stage_activate_reactivate_against_real_postgres(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Stage is inert, activation cuts over, reactivation restores -- for real.

    v14.1 activates and serves A. v15.1 stages with revised content and serves
    nothing new. Activating v15.1 serves B. Re-activating v14.1 serves A again,
    the exact staged version, all through real SQL.
    """
    handler, _ = _attack_setup(session_factory)

    await handler.import_release(
        ImportAttackRelease(
            framework=FRAMEWORK,
            release="v14.1",
            payload=_attack_bundle(t1110_description="v14.1 body."),
            activate=True,
        )
    )
    assert await _active_release(session_factory) == "v14.1"
    assert "v14.1 body." in await _served_t1110(session_factory)

    await handler.import_release(
        ImportAttackRelease(
            framework=FRAMEWORK,
            release="v15.1",
            payload=_attack_bundle(t1110_description="v15.1 revised body."),
            activate=False,
        )
    )
    assert await _active_release(session_factory) == "v14.1"
    assert "v14.1 body." in await _served_t1110(session_factory)

    await handler.import_release(
        ImportAttackRelease(
            framework=FRAMEWORK,
            release="v15.1",
            payload=_attack_bundle(t1110_description="v15.1 revised body."),
            activate=True,
        )
    )
    assert await _active_release(session_factory) == "v15.1"
    assert "v15.1 revised body." in await _served_t1110(session_factory)

    await handler.import_release(
        ImportAttackRelease(
            framework=FRAMEWORK,
            release="v14.1",
            payload=_attack_bundle(t1110_description="v14.1 body."),
            activate=True,
        )
    )
    assert await _active_release(session_factory) == "v14.1"
    assert "v14.1 body." in await _served_t1110(session_factory)


async def test_attack_invalid_binding_refused_against_real_postgres(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A cross-document binding fails closed against the real schema too.

    v14.1 activates. v15.1 stages. Its T1110 binding is then rewritten to name
    T1059's version, and activation refuses with INVALID_BINDING while v14.1
    keeps serving.
    """
    handler, _ = _attack_setup(session_factory)

    await handler.import_release(
        ImportAttackRelease(
            framework=FRAMEWORK,
            release="v14.1",
            payload=_attack_bundle(t1110_description="v14.1 body."),
            activate=True,
        )
    )
    await handler.import_release(
        ImportAttackRelease(
            framework=FRAMEWORK,
            release="v15.1",
            payload=_attack_bundle(t1110_description="v15.1 revised body."),
            activate=False,
        )
    )

    async with session_factory() as session:
        projections = SqlAlchemyAttackReleaseProjectionRepository(session)
        t1110 = next(
            binding
            for binding in await projections.list_for_release(
                framework=FRAMEWORK, source_release="v15.1"
            )
            if binding.technique_id == "T1110"
        )
        t1059 = next(
            binding
            for binding in await projections.list_for_release(
                framework=FRAMEWORK, source_release="v15.1"
            )
            if binding.technique_id == "T1059"
        )
        await session.execute(
            update(AttackReleaseProjectionRow)
            .where(AttackReleaseProjectionRow.id == t1110.id)
            .values(document_version_id=t1059.document_version_id)
        )
        await session.commit()

    with pytest.raises(AttackReleaseProjectionInvalidBindingError) as excinfo:
        await handler.import_release(
            ImportAttackRelease(
                framework=FRAMEWORK,
                release="v15.1",
                payload=_attack_bundle(t1110_description="v15.1 revised body."),
                activate=True,
            )
        )

    assert excinfo.value.code == "ATTACK_RELEASE_PROJECTION_INVALID_BINDING"
    assert await _active_release(session_factory) == "v14.1"
    assert "v14.1 body." in await _served_t1110(session_factory)

async def test_attack_stale_profile_coverage_refused_against_real_postgres(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Covered by the wrong space: the cutover must compare against ACTIVE.

    v14.1 activates (creating ACTIVE profile A). v15.1 stages its revised T1110
    -- also under A. Profile A is then retired and a fresh profile B activated,
    and v15.1's T1110 embeddings are moved to B: exactly the state an abandoned
    half-reindex leaves behind. Every chunk is embedded, so the old check (any
    full coverage) passed; vector retrieval filters on A, so it would serve
    nothing for the version.

    Activation must refuse with INCOMPLETE, with v14.1 still ACTIVE, its mirror
    intact, and the T1110 pointer untouched.
    """
    import uuid as _uuid
    from datetime import datetime as _datetime

    handler, _ = _attack_setup(session_factory)

    await handler.import_release(
        ImportAttackRelease(
            framework=FRAMEWORK,
            release="v14.1",
            payload=_attack_bundle(t1110_description="v14.1 body."),
            activate=True,
        )
    )
    await handler.import_release(
        ImportAttackRelease(
            framework=FRAMEWORK,
            release="v15.1",
            payload=_attack_bundle(t1110_description="v15.1 revised body."),
            activate=False,
        )
    )

    async with session_factory() as session:
        profiles = SqlAlchemyEmbeddingProfileRepository(session)
        active_a = await profiles.get_active()
        assert active_a is not None
        now = _datetime(2026, 9, 14, 10, 0, 0)
        await session.execute(
            update(EmbeddingProfileRow)
            .where(EmbeddingProfileRow.id == active_a.id)
            .values(status="RETIRED", retired_at=now)
        )
        profile_b_id = _uuid.uuid4()
        await profiles.add(
            profile=EmbeddingProfileRecord(
                id=profile_b_id,
                provider=active_a.provider,
                model_id="text-embedding-v2",
                dimension=active_a.dimension,
                distance_metric=active_a.distance_metric,
                normalization=active_a.normalization,
                profile_version=active_a.profile_version + 1,
                status="ACTIVE",
                created_at=now,
                retired_at=None,
            )
        )
        projections = SqlAlchemyAttackReleaseProjectionRepository(session)
        t1110 = next(
            binding
            for binding in await projections.list_for_release(
                framework=FRAMEWORK, source_release="v15.1"
            )
            if binding.technique_id == "T1110"
        )
        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        for content_chunk in await chunks.list_content_chunks(
            document_version_id=t1110.document_version_id,
            generation=CHUNK_GENERATION_INITIAL,
        ):
            await session.execute(
                update(KnowledgeChunkEmbeddingRow)
                .where(
                    KnowledgeChunkEmbeddingRow.content_chunk_id == content_chunk.id
                )
                .values(embedding_profile_id=profile_b_id)
            )
        await session.commit()

    # The cutover directly: a full re-import would re-stage first and trip
    # the generic handler's retired-profile refusal before the cutover runs.
    # The unit under test is the cutover's own validation, which runs here
    # against real SQL (advisory lock, mirror, pointer moves).
    with pytest.raises(AttackReleaseProjectionIncompleteError) as excinfo:
        await handler._cutover(
            ImportAttackRelease(
                framework=FRAMEWORK,
                release="v15.1",
                payload=_attack_bundle(t1110_description="v15.1 revised body."),
                activate=True,
            )
        )

    assert excinfo.value.code == "ATTACK_RELEASE_PROJECTION_INCOMPLETE"
    assert await _active_release(session_factory) == "v14.1"
    assert "v14.1 body." in await _served_t1110(session_factory)
    async with session_factory() as session:
        techniques = SqlAlchemyAttackTechniqueRepository(session)
        rows = await techniques.list_for_release(
            framework=FRAMEWORK, source_release="v14.1"
        )
        assert rows and all(row.active for row in rows)
        staged = await techniques.list_for_release(
            framework=FRAMEWORK, source_release="v15.1"
        )
        assert staged and all(not row.active for row in staged)
