"""Typed security-knowledge contract (brief sections 4, 20, 39, 40, 50, 51).

This module REPLACES the old loose ``dict`` contract. It is still internal: the
two catalog tools ``knowledge.retrieve_security_guidance`` and
``knowledge.resolve_attack_technique`` remain in ``FUTURE_CATALOG_TOOLS`` and are
NOT registered with the ToolRegistry, so no model can reach any of this yet
(brief sections 4/88).

Typed retrieval travels as frozen dataclasses rather than ``list[dict]`` so a
future tool layer cannot invent fields, and so the excerpt/citation boundary is a
compile-time fact instead of a convention.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ...domain.knowledge.entities import KnowledgeDocument, KnowledgeDocumentVersion
from ...domain.knowledge.enums import SourceKind, Visibility

DEFAULT_RESULT_LIMIT = 5
MAX_RESULT_LIMIT = 5

#: The frozen production retrieval profile (brief section 50). Mode variants of
#: the SAME frozen configuration are named ``<mode>-v1`` so an artifact always
#: records which candidate generators actually ran.
RETRIEVAL_PROFILE_ID = "hybrid-v1"
LEXICAL_PROFILE_ID = "lexical-v1"
VECTOR_PROFILE_ID = "vector-v1"


@dataclass(frozen=True)
class KnowledgeQuery:
    """A caller's retrieval request, already bounded and normalized."""

    topic: str
    context_terms: tuple[str, ...] = ()
    limit: int = DEFAULT_RESULT_LIMIT


@dataclass(frozen=True)
class RetrievalProfile:
    """The frozen knobs a result was produced under.

    Recording this on every result is what makes a ranking reproducible: the same
    corpus snapshot plus the same profile yields the same order (section 48).
    """

    profile_id: str
    lexical_candidate_limit: int
    vector_candidate_limit: int
    rrf_k: int
    max_chunks_per_document: int
    chunker_version: str
    embedding_profile_id: str
    embedding_model_id: str

    @property
    def is_hybrid(self) -> bool:
        return self.profile_id == RETRIEVAL_PROFILE_ID


@dataclass(frozen=True)
class KnowledgeHit:
    """One ranked retrieval result, carrying a resolvable citation handle.

    ``excerpt`` is bounded, human-readable DATA. It is never an instruction, and
    it confers no authority -- the citation id is only a handle the resolver
    re-validates against the database (sections 24/51).
    """

    citation_id: str
    document_id: UUID
    document_version_id: UUID
    chunk_id: UUID
    source_kind: SourceKind
    title: str
    excerpt: str
    language: str
    source_version: str | None
    retrieved_at: datetime


@dataclass(frozen=True)
class KnowledgeSearchResult:
    """The typed result of one retrieval."""

    hits: tuple[KnowledgeHit, ...] = ()
    truncated: bool = False
    retrieval_profile: RetrievalProfile | None = None

    @property
    def profile_id(self) -> str | None:
        return self.retrieval_profile.profile_id if self.retrieval_profile else None


@dataclass(frozen=True)
class CitationResolution:
    """The outcome of re-validating a citation handle against the database.

    Resolution proves PROVENANCE: this chunk, of this immutable version, with
    this content hash, exists and is readable by this tenant. It proves nothing
    about correctness and grants nothing.
    """

    citation_id: str
    resolved: bool
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    chunk_id: UUID | None = None
    source_kind: SourceKind | None = None
    title: str | None = None
    language: str | None = None
    source_version: str | None = None
    excerpt: str | None = None
    document_status: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class KnowledgeChunkView:
    """A chunk joined with the document/version facts retrieval must not guess.

    Repositories return this so tenant/visibility/active-version filtering happens
    in SQL, never by loading the corpus and filtering in Python (section 44).
    """

    chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    ordinal: int
    heading_path: str
    content: str
    content_hash: str
    title: str
    source_kind: SourceKind
    language: str
    source_version: str | None
    document_status: str
    visibility: Visibility
    tenant_id: str | None


@dataclass(frozen=True)
class LexicalCandidate:
    """A full-text candidate plus its PostgreSQL rank (higher is better)."""

    view: KnowledgeChunkView
    rank: float


@dataclass(frozen=True)
class VectorCandidate:
    """An exact nearest-neighbour candidate plus its distance (lower is better)."""

    view: KnowledgeChunkView
    distance: float


@dataclass(frozen=True)
class KnowledgeChunkRecord:
    """A chunk as it is persisted: a REBUILDABLE retrieval projection.

    Chunks are not a domain aggregate. They can always be regenerated from the
    immutable version, so they carry no business authority of their own.
    """

    id: UUID
    document_id: UUID
    document_version_id: UUID
    ordinal: int
    heading_path: str
    content: str
    content_hash: str
    token_count: int
    language: str
    embedding_profile_id: UUID
    embedding: tuple[float, ...]
    created_at: datetime
    #: The chunker that produced this projection. Persisted so ingestion can tell
    #: whether a version's chunks are stale (older chunker, older embedding
    #: profile) and must be rebuilt, instead of silently serving a projection that
    #: no longer matches the frozen chunker configuration (brief section 22).
    chunker_version: str


@dataclass(frozen=True)
class ChunkProjectionState:
    """What a document version's retrieval projection currently looks like.

    ``is_empty`` and the profile/chunker comparison are what let ingestion decide
    between a true no-op and a rebuild. The version row is immutable either way:
    only the rebuildable projection is ever regenerated.
    """

    embedding_profile_id: UUID | None
    chunker_version: str | None
    chunk_count: int

    @property
    def is_empty(self) -> bool:
        return self.chunk_count == 0


@dataclass(frozen=True)
class EmbeddingProfileRecord:
    """One embedding space as persisted, with its ACTIVE/RETIRED lifecycle."""

    id: UUID
    provider: str
    model_id: str
    dimension: int
    distance_metric: str
    normalization: str
    profile_version: int
    status: str
    created_at: datetime
    retired_at: datetime | None = None

    @property
    def identifier(self) -> str:
        return f"{self.provider}:{self.model_id}:{self.dimension}:{self.profile_version}"


@dataclass(frozen=True)
class AttackTechniqueRecord:
    """One canonical ATT&CK technique row for a pinned release."""

    id: UUID
    framework: str
    technique_id: str
    source_release: str
    name: str
    description: str
    tactics: tuple[str, ...]
    platforms: tuple[str, ...]
    source_stix_id: str
    content_hash: str
    active: bool
    created_at: datetime


@dataclass(frozen=True)
class AttackImportOutcome:
    """What a release import actually changed (idempotent by construction)."""

    release: str
    techniques_parsed: int
    techniques_created: int
    documents_created: int
    versions_ingested: int
    unchanged: int
    skipped: tuple[str, ...] = field(default=())


class KnowledgeDocumentRepository(Protocol):
    """Tenant-scoped document/version persistence.

    Every read takes ``tenant_id`` (brief section 40): there is no scope-less
    variant, so an Agent path cannot accidentally query the global corpus.
    """

    async def get(self, *, tenant_id: str, document_id: UUID) -> KnowledgeDocument | None: ...

    async def find_by_external_key(
        self,
        *,
        tenant_id: str | None,
        source_kind: SourceKind,
        external_key: str,
        visibility: Visibility,
    ) -> KnowledgeDocument | None:
        """Find a document by its natural key inside an exact scope.

        ``tenant_id`` is ``None`` for a GLOBAL document and the owner id for a
        TENANT one -- the same pairing the database CHECK enforces. The lookup
        must match on the SCOPE, not just the key: the same ``(source_kind,
        external_key)`` may legitimately exist once globally and once per tenant.
        """

    async def add(self, *, document: KnowledgeDocument) -> None: ...

    async def save(self, *, document: KnowledgeDocument) -> None: ...

    async def next_version_number(self, *, document_id: UUID) -> int: ...

    async def add_version(self, *, version: KnowledgeDocumentVersion) -> None: ...

    async def find_version_by_content_hash(
        self, *, document_id: UUID, content_hash: str
    ) -> KnowledgeDocumentVersion | None: ...

    async def get_version(
        self, *, tenant_id: str, document_version_id: UUID
    ) -> KnowledgeDocumentVersion | None: ...

    async def list_versions(
        self, *, tenant_id: str, document_id: UUID
    ) -> tuple[KnowledgeDocumentVersion, ...]: ...


class KnowledgeChunkRepository(Protocol):
    """Chunk (retrieval projection) persistence and candidate generation.

    Candidate generation MUST filter in SQL: a tenant/visibility/active-version
    restriction applied after loading the corpus would read rows the caller is
    not allowed to see, and would not scale (brief sections 44/45).
    """

    async def add_many(self, *, chunks: Sequence[KnowledgeChunkRecord]) -> None: ...

    async def count_for_version(self, *, document_version_id: UUID) -> int: ...

    async def projection_state(
        self, *, document_version_id: UUID
    ) -> ChunkProjectionState: ...

    async def delete_for_version(self, *, document_version_id: UUID) -> None: ...

    async def lexical_candidates(
        self, *, tenant_id: str, search_terms: Sequence[str], limit: int
    ) -> tuple[LexicalCandidate, ...]: ...

    async def vector_candidates(
        self,
        *,
        tenant_id: str,
        embedding_profile_id: UUID,
        query_vector: Sequence[float],
        limit: int,
    ) -> tuple[VectorCandidate, ...]: ...

    async def get_chunk_view(
        self, *, tenant_id: str, chunk_id: UUID
    ) -> KnowledgeChunkView | None: ...


class EmbeddingProfileRepository(Protocol):
    """Persistence for the embedding spaces the corpus was indexed in."""

    async def get_active(self) -> EmbeddingProfileRecord | None: ...

    async def find_by_identity(
        self,
        *,
        provider: str,
        model_id: str,
        dimension: int,
        distance_metric: str,
        normalization: str,
        profile_version: int,
    ) -> EmbeddingProfileRecord | None: ...

    async def add(self, *, profile: EmbeddingProfileRecord) -> None: ...

    async def retire_active(self, *, retired_at: datetime) -> None: ...


class AttackTechniqueRepository(Protocol):
    """Canonical ATT&CK technique persistence for a pinned release."""

    async def find(
        self, *, framework: str, technique_id: str, source_release: str
    ) -> AttackTechniqueRecord | None: ...

    async def add_many(self, *, techniques: Sequence[AttackTechniqueRecord]) -> None: ...

    async def count_for_release(self, *, framework: str, source_release: str) -> int: ...

    async def deactivate_except(self, *, framework: str, source_release: str) -> int:
        """Mark every OTHER release's rows inactive; return how many changed.

        This is the explicit release switch of section 38. Rows are never
        deleted -- an older release stays readable by its own ``source_release``
        -- so "which release is authoritative" is a reversible flag rather than a
        destructive act. Returns the number of rows actually flipped so an
        operator can see the switch happened instead of assuming it did.
        """
        ...


class KnowledgeRetrievalCatalogPort(Protocol):
    """The narrow, typed surface a FUTURE knowledge tool will call.

    Not registered anywhere yet (brief sections 4/88). It exists so the eventual
    tool layer has exactly one entry point per capability instead of reaching
    into repositories.
    """

    async def retrieve_security_guidance(
        self, *, tenant_id: str, query: KnowledgeQuery
    ) -> KnowledgeSearchResult: ...

    async def resolve_attack_technique(
        self, *, tenant_id: str, technique_id: str, framework: str = "mitre-attack"
    ) -> AttackTechniqueRecord | None: ...
