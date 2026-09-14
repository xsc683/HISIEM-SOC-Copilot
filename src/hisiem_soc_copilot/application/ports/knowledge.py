"""Typed security-knowledge contract (brief sections 4, 20, 39, 40, 50, 51).

This module REPLACES the old loose ``dict`` contract. The retrieval and ATT&CK
catalog capabilities are exposed through the bounded Agent tool surface; tenant
scope remains an explicit trusted-runtime argument, never a model argument.

Typed retrieval travels as frozen dataclasses rather than ``list[dict]`` so a
tool layer cannot invent fields, and so the excerpt/citation boundary is a
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
from ...domain.knowledge.value_objects import CHUNK_GENERATION_INITIAL

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
    content_hash: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class KnowledgeChunkView:
    """A chunk joined with the document/version facts retrieval must not guess.

    Repositories return this so tenant/visibility/active-version filtering happens
    in SQL, never by loading the corpus and filtering in Python (section 44).

    ``chunk_id`` is the IMMUTABLE content-chunk id -- the citation target -- not
    an embedding-projection row, so it stays valid when the projection is rebuilt
    (brief section 3). The remaining fields beyond the content are provenance and
    *ranking* metadata: they make the deterministic tie-break explainable in
    business terms instead of by database-generated UUID (brief section 5.1/5.2).
    They confer no authority.
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
    #: The document's natural key inside its scope, e.g. ``mitre-attack:T1110``.
    external_key: str = ""
    #: The immutable version's ordinal (1, 2, ...) within the document.
    document_version_number: int = 1
    #: Which chunk generation of that version this chunk belongs to.
    chunk_generation: int = CHUNK_GENERATION_INITIAL


def stable_ranking_key(
    view: KnowledgeChunkView,
) -> tuple[str, str, str, str, int, int, int]:
    """The ONE deterministic tie-break order for ranked knowledge (brief section 5.1).

    Ranking ties are broken by SEMANTIC business identity -- what the knowledge
    IS -- and never by a database-generated surrogate::

        (source_kind, visibility, tenant_id, external_key,
         document_version_number, chunk_generation, ordinal)

    A random UUID tie-break would make two runs over the same corpus disagree
    whenever a tie occurred, which is precisely what a reproducible baseline
    cannot tolerate. Every component here is stable under re-ingestion into a
    fresh database: the same document at the same version, chunked by the same
    chunker, always produces the same key.

    ``visibility``/``tenant_id`` are part of the key because a GLOBAL document and
    a TENANT document may share an ``external_key`` (the two partial unique
    indexes allow exactly that), so without the scope the order would not be
    total. A GLOBAL row has no tenant; ``tenant_id`` is folded to ``""`` for
    ordering, which is safe because the fold is only ever applied WITHIN one
    visibility value, where the tenant is uniform.

    The SQL side of this order lives in the repository's ``_stable_order_columns``;
    the two MUST stay identical, so the SQL uses ``COLLATE "C"`` (byte order),
    which is exactly this function's ``str`` comparison order for UTF-8 text.
    """
    return (
        view.source_kind.value,
        view.visibility.value,
        view.tenant_id or "",
        view.external_key,
        view.document_version_number,
        view.chunk_generation,
        view.ordinal,
    )


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
class KnowledgeContentChunkRecord:
    """An IMMUTABLE content chunk as it is persisted -- the citation target.

    Content identity and retrieval machinery are deliberately separate rows
    (brief section 3.1). This half is written once per ``(document_version_id,
    generation, ordinal)`` and is never rewritten, so a citation into it survives
    re-embedding, projection rebuilds, rechunking, restarts, retirement, and
    later versions. ``content_hash`` is derived from ``content`` by the one
    domain hash function, never supplied by a caller.
    """

    id: UUID
    document_id: UUID
    document_version_id: UUID
    generation: int
    ordinal: int
    heading_path: str
    content: str
    content_hash: str
    token_count: int
    language: str
    chunker_version: str
    created_at: datetime


@dataclass(frozen=True)
class ChunkEmbeddingRecord:
    """The REBUILDABLE embedding projection of one content chunk.

    Carries no content and therefore no identity worth citing: it can be dropped
    and recreated (a new embedding profile, a re-index) without invalidating a
    single citation. Exactly one row per ``(content_chunk_id,
    embedding_profile_id)``.
    """

    id: UUID
    content_chunk_id: UUID
    embedding_profile_id: UUID
    embedding: tuple[float, ...]
    indexed_at: datetime


@dataclass(frozen=True)
class ChunkProjectionState:
    """What a document version's retrieval projection currently looks like.

    ``is_empty`` and the profile/chunker comparison are what let ingestion decide
    between a true no-op, a re-embed, and a rechunk. The version row is immutable
    either way: only the rebuildable projection is ever regenerated.

    ``generation`` is the CURRENT (highest) content-chunk generation: a chunker
    change appends generation N+1 rather than deleting N, which is what keeps an
    older chunker's chunks -- and any citation into them -- resolvable (section
    3.3).

    ``embedding_profile_id`` names the ONE embedding space that FULLY covers the
    current generation, and is ``None`` when none does or when more than one does.
    That definition is what makes a re-embedding decision terminate: an ambiguous
    or partial projection always reads as "not embedded", so the caller re-embeds
    instead of oscillating between two spaces. ``embedding_count`` is the raw
    number of projections, reported for operators and diagnostics.
    """

    embedding_profile_id: UUID | None
    chunker_version: str | None
    chunk_count: int
    generation: int = CHUNK_GENERATION_INITIAL
    embedding_count: int = 0

    @property
    def is_empty(self) -> bool:
        return self.chunk_count == 0

    @property
    def is_embedded(self) -> bool:
        """True when one embedding space fully covers the current generation."""
        return self.chunk_count > 0 and self.embedding_profile_id is not None


@dataclass(frozen=True)
class CorpusChunkFact:
    """The canonical SEMANTIC facts about one eligible corpus chunk.

    This is the unit the corpus fingerprint is built from (brief section 5.5). It
    deliberately contains no database-generated UUID, no timestamp, no host, no
    credential and no embedding vector: two independently ingested databases must
    produce the same fingerprint for the same corpus, and only facts that are
    reproducible from the corpus itself can do that.
    """

    visibility: Visibility
    tenant_id: str | None
    source_kind: SourceKind
    external_key: str
    document_version_number: int
    document_content_hash: str
    chunk_generation: int
    ordinal: int
    chunk_content_hash: str


@dataclass(frozen=True)
class CorpusSnapshot:
    """The tenant-visible ACTIVE eligible corpus as reproducible facts.

    "Eligible" is the retrieval definition, not a new one: an ACTIVE document,
    visible to the tenant, whose active version has at least one content chunk in
    its current generation. Repositories build this in SQL so an evaluation
    precondition cannot read rows the tenant may not see (brief sections 5.3/44).
    """

    chunks: tuple[CorpusChunkFact, ...] = ()
    document_count: int = 0
    version_count: int = 0

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


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
class AttackReleaseRecord:
    """One imported ATT&CK release, with its authority state and fingerprint.

    Authority lives HERE, at release granularity, not on the technique rows: the
    question "which release is authoritative for this framework" is one fact
    about one release, and modelling it per technique is what let two releases be
    authoritative at once (brief section 2.1). The database enforces "at most one
    ACTIVE release per framework" with a partial unique index, so this record's
    ``status`` cannot disagree with the constraint.

    ``content_fingerprint`` is the deterministic fingerprint over the release's
    canonical technique collection (section 2.2). It is ``None`` only for a
    release adopted by the migration from rows that predate fingerprinting; a
    release imported by the handler always carries one.
    """

    id: UUID
    framework: str
    source_release: str
    content_fingerprint: str | None
    status: str
    technique_count: int
    created_at: datetime
    activated_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.status == ATTACK_RELEASE_STATUS_ACTIVE


#: The two release authority states. There is deliberately no third STAGING
#: state: a half-activated release is what the partial unique index exists to
#: make impossible (brief section 2.1).
ATTACK_RELEASE_STATUS_ACTIVE = "ACTIVE"
ATTACK_RELEASE_STATUS_INACTIVE = "INACTIVE"

#: Fingerprint of a release adopted from pre-fingerprint rows is UNKNOWN until an
#: import pins it: ``None`` means exactly that, and is never treated as "matches".


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
class AttackReleaseProjectionRecord:
    """One technique's staged knowledge projection for ONE release.

    Release identity and knowledge content identity are DIFFERENT facts. Two
    releases may carry byte-identical technique content, in which case they share
    one immutable ``KnowledgeDocumentVersion`` -- reuse is correct, because the
    content is the same. What must not be shared is the CLAIM that a given release
    projected a given version.

    ``source_version`` on the version cannot carry that claim: it records which
    release happened to CREATE the row, so on a shared version it names one
    release and silently misattributes the other. Re-deriving the binding from
    content hashes is likewise insufficient -- it yields the set of releases whose
    content matches, not which projection a release actually staged, and the row
    ids are random so no derivation can reconstruct the one a caller observed.

    This row is therefore the binding of record. Because it names the exact
    ``document_version_id`` at stage time, re-activating an older release restores
    the version it staged instead of re-deriving one (brief section 2.6).
    """

    id: UUID
    framework: str
    source_release: str
    technique_id: str
    document_id: UUID
    document_version_id: UUID
    content_hash: str
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
    #: The release's authority state AFTER the import, and the fingerprint that
    #: was pinned (or confirmed) for it. Reported so an operator can see which
    #: release is authoritative instead of inferring it from a row count.
    release_active: bool = False
    content_fingerprint: str | None = None
    release_created: bool = False


class KnowledgeDocumentRepository(Protocol):
    """Tenant-scoped document/version persistence.

    Every read takes ``tenant_id`` (brief section 40): there is no scope-less
    variant, so an Agent path cannot accidentally query the global corpus.
    """

    async def get(
        self, *, tenant_id: str | None, document_id: UUID
    ) -> KnowledgeDocument | None: ...

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
        self, *, tenant_id: str | None, document_version_id: UUID
    ) -> KnowledgeDocumentVersion | None: ...

    async def list_versions(
        self, *, tenant_id: str, document_id: UUID
    ) -> tuple[KnowledgeDocumentVersion, ...]: ...


class KnowledgeChunkRepository(Protocol):
    """Immutable content-chunk + rebuildable embedding-projection persistence.

    Candidate generation MUST filter in SQL: a tenant/visibility/active-version
    restriction applied after loading the corpus would read rows the caller is
    not allowed to see, and would not scale (brief sections 44/45).

    There is deliberately NO ``delete_for_version``: P3-A offers no path that
    destroys immutable historical chunk content, because that is exactly what
    would make a historical citation unresolvable (brief section 3.2). The only
    deletion available is of the REBUILDABLE embedding rows.
    """

    async def add_content_chunks(
        self, *, chunks: Sequence[KnowledgeContentChunkRecord]
    ) -> None: ...

    async def add_embeddings(self, *, embeddings: Sequence[ChunkEmbeddingRecord]) -> None: ...

    async def count_for_version(self, *, document_version_id: UUID) -> int: ...

    async def projection_state(
        self, *, document_version_id: UUID
    ) -> ChunkProjectionState: ...

    async def list_content_chunks(
        self, *, document_version_id: UUID, generation: int
    ) -> tuple[KnowledgeContentChunkRecord, ...]:
        """Read ONE generation's immutable chunks, in ordinal order.

        Used to RE-EMBED a version that already has content but no projection in
        the ACTIVE embedding space. Re-indexing reuses the very same content
        chunks -- that is the point of separating content identity from the
        projection -- so their ids, and every citation into them, survive the
        rebuild unchanged (brief section 3.1).
        """
        ...

    async def delete_embeddings_for_version_generation(
        self, *, document_version_id: UUID, generation: int
    ) -> int:
        """Delete one generation's EMBEDDING rows; return how many were removed.

        The content chunks of that generation are untouched: re-embedding under a
        new profile replaces the projection, never the cited identity.
        """
        ...

    async def delete_embeddings_for_profile(
        self, *, document_version_id: UUID, embedding_profile_id: UUID
    ) -> int: ...

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
    ) -> KnowledgeChunkView | None:
        """Read one content chunk for citation resolution.

        Deliberately applies NO chunk-generation predicate: an older generation is
        still legitimate history, and a citation into it must keep resolving
        (brief section 3.3). Scope IS still enforced -- ``tenant_id`` is
        mandatory and applied in SQL -- so a citation cannot cross tenants.
        """
        ...

    async def corpus_snapshot(self, *, tenant_id: str) -> CorpusSnapshot:
        """Return the tenant-visible ACTIVE eligible corpus as reproducible facts.

        Used as the sealed-evaluation precondition (brief section 5.3): the driver
        compares this against the fingerprint it expects from the fixture BEFORE
        any metric runs, so an ambient or mutated corpus fails as
        ``CORPUS_PRECONDITION_FAILED`` instead of producing a misleading score.
        """
        ...


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


class AttackReleaseRepository(Protocol):
    """Release-level ATT&CK authority: at most one ACTIVE release per framework.

    The single-active rule is a database fact (a per-framework partial unique
    index), not an application convention, so ``activate`` cannot produce two
    authoritative releases even under concurrency (brief section 2.1).
    """

    async def find(
        self, *, framework: str, source_release: str
    ) -> AttackReleaseRecord | None: ...

    async def get_active(self, *, framework: str) -> AttackReleaseRecord | None: ...

    async def list_for_framework(
        self, *, framework: str
    ) -> tuple[AttackReleaseRecord, ...]: ...

    async def list_active(self) -> tuple[AttackReleaseRecord, ...]:
        """Every ACTIVE release, across every framework.

        The authority invariant is per framework, so the diagnostic that verifies
        it needs the whole picture rather than one framework at a time: grouping
        here is what turns "one active per framework" into a checkable fact
        (brief sections 2.1/9.2).
        """
        ...

    async def add(self, *, release: AttackReleaseRecord) -> None: ...

    async def pin_fingerprint(
        self, *, framework: str, source_release: str, content_fingerprint: str
    ) -> None:
        """Record the fingerprint of an adopted (pre-fingerprint) release.

        Only ever called after the stored rows were re-derived through the SAME
        canonical function and matched, so pinning records a fact that was
        verified rather than one that was assumed.
        """
        ...

    async def activate(self, *, framework: str, source_release: str, now: datetime) -> None:
        """Make this release the framework's only ACTIVE one, in one statement.

        Deactivates every other release of the framework and activates this one.
        The partial unique index makes a second ACTIVE release fail at COMMIT.
        """
        ...


class AttackTechniqueRepository(Protocol):
    """Canonical ATT&CK technique persistence for a pinned release."""

    async def find(
        self, *, framework: str, technique_id: str, source_release: str
    ) -> AttackTechniqueRecord | None: ...

    async def add_many(self, *, techniques: Sequence[AttackTechniqueRecord]) -> None: ...

    async def count_for_release(self, *, framework: str, source_release: str) -> int: ...

    async def list_for_release(
        self, *, framework: str, source_release: str
    ) -> tuple[AttackTechniqueRecord, ...]:
        """Read a release's canonical rows, in a total deterministic order.

        Used to re-derive an adopted release's fingerprint from what is actually
        stored, so legacy adoption is verified against the database rather than
        assumed from the incoming bundle (brief section 2.2).
        """
        ...

    async def set_active_for_release(
        self, *, framework: str, source_release: str, active: bool
    ) -> int:
        """Set the ``active`` flag on every row of ONE release; return rows changed.

        Technique rows carry the flag because a technique is the unit a future
        tool would read, but the flag is a MIRROR of the release's authority --
        never an independent source of it (brief section 2.3). Rows are never
        deleted, so an older release stays readable by its own ``source_release``.
        """
        ...


class AttackReleaseProjectionRepository(Protocol):
    """The release -> knowledge projection binding, and the atomic cutover.

    Staging and activation are separate acts. Staging only appends bindings and
    never touches a document pointer; activation is one transaction that validates
    completeness and then moves the whole framework's pointers at once. Splitting
    them is what makes "an inactive release cannot change what retrieval serves"
    true by construction rather than by ordering discipline (brief sections
    2.5/2.7).
    """

    async def record_many(
        self, *, projections: Sequence[AttackReleaseProjectionRecord]
    ) -> None:
        """Stage bindings idempotently.

        Conflicting on ``(framework, source_release, technique_id)`` and doing
        nothing, so a retry after a crash mid-projection converges instead of
        producing a duplicate or a false conflict (brief section 2.8).
        """
        ...

    async def list_for_release(
        self, *, framework: str, source_release: str
    ) -> tuple[AttackReleaseProjectionRecord, ...]:
        """Read a release's bindings in a total deterministic order."""
        ...

    async def count_for_release(self, *, framework: str, source_release: str) -> int:
        """How many techniques of this release have a staged projection."""
        ...

    async def missing_techniques(
        self, *, framework: str, source_release: str
    ) -> tuple[str, ...]:
        """Canonical techniques of this release that have NO staged projection.

        The completeness precondition for activation. Returns technique ids
        rather than a count, so a refusal can name what is missing without
        dumping any content.
        """
        ...

    async def lock_framework(self, *, framework: str) -> None:
        """Serialize cutovers for one framework for the rest of the transaction.

        A transaction-scoped advisory lock rather than ``SELECT ... FOR UPDATE``:
        the FIRST activation of a framework has no ACTIVE row to lock, so a row
        lock would leave exactly the case that matters unserialized. Released
        automatically at COMMIT or ROLLBACK (brief section 2.9).
        """
        ...

    async def diverged_documents(
        self, *, framework: str, source_release: str
    ) -> tuple[str, ...]:
        """Bound documents whose live pointer is NOT this release's projection.

        The diagnostic that makes the failure this closure fixes VISIBLE. An
        authoritative release whose documents serve some other version means
        canonical authority and retrieval disagree -- a state no write path can
        produce any more, and exactly the state an operator restoring a dump or
        applying revisions out of order can still reach. Returns external keys so
        the report names the documents without dumping content.
        """
        ...

    async def unusable_documents(
        self, *, framework: str, source_release: str
    ) -> tuple[str, ...]:
        """Bound documents of this release that normal retrieval cannot serve.

        A RETIRED document is terminal and excluded from normal search, so cutting
        a release over to it would leave the framework authoritative for content
        retrieval cannot return. Checked as part of the same precondition as
        completeness, before any mutation (brief section 2.7).
        """
        ...


class KnowledgeRetrievalCatalogPort(Protocol):
    """The narrow capability surface consumed by the Agent tool executor.

    Tenant scope is explicit and required; callers cannot substitute a model
    argument for this trusted runtime context.
    """

    async def retrieve_security_guidance(
        self, *, tenant_id: str, query: KnowledgeQuery
    ) -> KnowledgeSearchResult: ...

    async def resolve_attack_technique(
        self, *, tenant_id: str, technique_id: str, framework: str = "mitre-attack"
    ) -> AttackTechniqueRecord | None: ...
