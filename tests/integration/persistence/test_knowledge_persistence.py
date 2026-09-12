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

Skipped when the pgvector-capable PostgreSQL on 127.0.0.1:5434 is unreachable, so
the suite stays green on a dev machine without it. Requires the schema already
migrated to head (``ed6af82d9b13``).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hisiem_soc_copilot.application.ports.knowledge import (
    AttackTechniqueRecord,
    ChunkProjectionState,
    EmbeddingProfileRecord,
    KnowledgeChunkRecord,
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
from hisiem_soc_copilot.domain.knowledge.value_objects import CHUNKER_VERSION
from hisiem_soc_copilot.domain.shared.errors import OptimisticConcurrencyError
from hisiem_soc_copilot.infrastructure.persistence.orm.knowledge import (
    KnowledgeChunkRow,
    KnowledgeDocumentRow,
)
from hisiem_soc_copilot.infrastructure.persistence.repositories.knowledge import (
    SqlAlchemyAttackTechniqueRepository,
    SqlAlchemyEmbeddingProfileRepository,
    SqlAlchemyKnowledgeChunkRepository,
    SqlAlchemyKnowledgeDocumentRepository,
)

_TRUNCATE = (
    "knowledge_chunk",
    "knowledge_document_version",
    "knowledge_document",
    "embedding_profile",
    "attack_technique",
)

_DATABASE_URL = "postgresql+psycopg://copilot:copilot@127.0.0.1:5434/copilot"

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

_TECHNIQUE_1 = UUID("00000000-0000-4000-8000-000000000040")
_TECHNIQUE_2 = UUID("00000000-0000-4000-8000-000000000041")

# Dyadic values only: the column is pgvector ``vector`` (float4), so a value like
# 0.1 would not survive the round-trip exactly and the assertion would be a lie.
_V_AXIS_X: tuple[float, ...] = (1.0, 0.0, 0.0, 0.0)
_V_AXIS_Y: tuple[float, ...] = (0.0, 1.0, 0.0, 0.0)
_V_BOTH: tuple[float, ...] = (0.5, 0.5, 0.0, 0.0)


def _settings() -> Settings:
    settings = Settings()
    settings.database.database_url = _DATABASE_URL
    return settings


def _db_reachable() -> bool:
    """Sync reachability probe so the module-level skipif can decide at import."""
    try:
        import psycopg
        from sqlalchemy.engine import make_url

        url = make_url(_DATABASE_URL)
        conn = psycopg.connect(
            host=url.host,
            port=url.port,
            user=url.username,
            password=url.password,
            dbname=url.database,
            connect_timeout=2,
        )
        conn.execute("SELECT 1")
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_reachable(),
    reason="pgvector PostgreSQL not reachable on 127.0.0.1:5434",
)


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(_DATABASE_URL)

    async def _clean() -> None:
        """Clear ONLY the five knowledge tables (never anything else)."""
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
    embedding: tuple[float, ...],
    embedding_profile_id: UUID,
    chunker_version: str = CHUNKER_VERSION,
) -> KnowledgeChunkRecord:
    return KnowledgeChunkRecord(
        id=chunk_id,
        document_id=document_id,
        document_version_id=document_version_id,
        ordinal=ordinal,
        heading_path="Authentication",
        content=content,
        content_hash=_hash(content),
        token_count=len(content.split()),
        language="en",
        embedding_profile_id=embedding_profile_id,
        embedding=embedding,
        created_at=_T0,
        chunker_version=chunker_version,
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
                await chunks.add_many(
                    chunks=[
                        _chunk(
                            chunk_id=chunk_id,
                            document_id=document_id,
                            document_version_id=seed.version_id,
                            ordinal=ordinal,
                            content=chunk_content,
                            embedding=vector,
                            embedding_profile_id=profile_id,
                        )
                        for ordinal, (chunk_id, chunk_content, vector) in enumerate(
                            seed.chunks
                        )
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
        # The pgvector values themselves survive (float4, so dyadic inputs only).
        row = await session.get(KnowledgeChunkRow, _CHUNK_1)
        assert row is not None
        assert [float(value) for value in row.embedding] == [1.0, 0.0, 0.0, 0.0]
        assert row.chunker_version == CHUNKER_VERSION
        assert row.lexical_document is not None  # GENERATED by the database

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
        await chunks.add_many(
            chunks=[
                _chunk(
                    chunk_id=_CHUNK_1,
                    document_id=_DOC_GLOBAL,
                    document_version_id=_VERSION_G1,
                    ordinal=0,
                    content="identical direction",
                    embedding=_V_AXIS_X,
                    embedding_profile_id=_PROFILE_1,
                ),
                _chunk(
                    chunk_id=_CHUNK_2,
                    document_id=_DOC_GLOBAL,
                    document_version_id=_VERSION_G1,
                    ordinal=1,
                    content="orthogonal direction",
                    embedding=_V_AXIS_Y,
                    embedding_profile_id=_PROFILE_1,
                ),
                # Same direction as the query, but indexed in ANOTHER space: it must
                # never be returned, because distances across profiles are not
                # comparable numbers.
                _chunk(
                    chunk_id=_CHUNK_3,
                    document_id=_DOC_GLOBAL,
                    document_version_id=_VERSION_G1,
                    ordinal=2,
                    content="other space",
                    embedding=_V_AXIS_X,
                    embedding_profile_id=_PROFILE_2,
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
        state = await chunks.projection_state(document_version_id=_VERSION_G1)
        assert state == ChunkProjectionState(None, None, 0)
        assert state.is_empty is True

        # An unknown version has no projection either -- reported, never raised.
        unknown = await chunks.projection_state(document_version_id=_VERSION_A2)
        assert unknown.is_empty is True

        # add_many(()) is a no-op, not an empty INSERT statement.
        await chunks.add_many(chunks=[])
        await chunks.add_many(
            chunks=[
                _chunk(
                    chunk_id=_CHUNK_1,
                    document_id=_DOC_GLOBAL,
                    document_version_id=_VERSION_G1,
                    ordinal=0,
                    content="projection chunk one",
                    embedding=_V_AXIS_X,
                    embedding_profile_id=_PROFILE_1,
                ),
                _chunk(
                    chunk_id=_CHUNK_2,
                    document_id=_DOC_GLOBAL,
                    document_version_id=_VERSION_G1,
                    ordinal=1,
                    content="projection chunk two",
                    embedding=_V_AXIS_Y,
                    embedding_profile_id=_PROFILE_1,
                ),
            ]
        )
        await session.commit()

    async with session_factory() as session:
        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        populated = await chunks.projection_state(document_version_id=_VERSION_G1)
        assert populated.is_empty is False
        assert populated.embedding_profile_id == _PROFILE_1
        assert populated.chunker_version == CHUNKER_VERSION
        assert populated.chunk_count == 2
        assert await chunks.count_for_version(document_version_id=_VERSION_G1) == 2

        # A rebuild drops ONLY the projection; deleting it is what makes the version
        # rebuildable under a new profile or a new chunker version.
        await chunks.delete_for_version(document_version_id=_VERSION_G1)
        await session.commit()

    async with session_factory() as session:
        documents = SqlAlchemyKnowledgeDocumentRepository(session)
        chunks = SqlAlchemyKnowledgeChunkRepository(session)
        assert (await chunks.projection_state(document_version_id=_VERSION_G1)).is_empty
        assert await chunks.count_for_version(document_version_id=_VERSION_G1) == 0
        # The immutable version itself is untouched by a projection rebuild.
        assert (
            await documents.get_version(
                tenant_id="tenant-a", document_version_id=_VERSION_G1
            )
            is not None
        )


# ---------------------------------------------------------------------------
# 14. ATT&CK techniques
# ---------------------------------------------------------------------------


async def test_attack_techniques_are_unique_per_release(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
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


def test_truncate_targets_only_knowledge_tables() -> None:
    """Guard-rail: this module may never clear a table outside the five it owns."""
    assert set(_TRUNCATE) == {
        "knowledge_chunk",
        "knowledge_document_version",
        "knowledge_document",
        "embedding_profile",
        "attack_technique",
    }
