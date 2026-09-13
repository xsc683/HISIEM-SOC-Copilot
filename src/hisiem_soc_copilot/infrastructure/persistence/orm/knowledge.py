"""ORM models for the knowledge subsystem (brief sections 2, 3, 10-16, 36-38).

Seven live tables, four different kinds of thing, plus one retired leftover:

``knowledge_document`` / ``knowledge_document_version``
    The authoritative knowledge truth. A document is identified by
    ``(source_kind, external_key)`` inside a scope, and its content lives in
    immutable versions that are only ever appended.

``knowledge_content_chunk``
    The IMMUTABLE citation target: one piece of a version's content, written once
    and never rewritten, addressable across re-embedding, projection rebuilds,
    rechunking, restarts, retirement, and later versions.

``knowledge_chunk_embedding``
    A REBUILDABLE retrieval projection of a content chunk. It can be dropped and
    recreated at any time, because a citation names the content chunk and not this
    row -- which is why re-indexing no longer breaks historical citations.

``embedding_profile`` / ``attack_release`` / ``attack_technique``
    Supporting records: which vector space the corpus was indexed in (at most one
    ACTIVE), which ATT&CK release is authoritative per framework (at most one
    ACTIVE, by partial unique index), and the canonical technique rows of a pinned
    release.

``knowledge_chunk``
    The PRE-CLOSURE chunk table, declared but never read or written: content and
    embedding used to share a row here, which is what made an old citation die
    with the index. The upgrade copies every row into the pair above and leaves
    the table standing, and this class exists so ``alembic check`` does not
    report that deliberate survival as drift.

The database is the final arbiter for the invariants the domain also checks: the
scope CHECK constraints make "GLOBAL with a tenant" unrepresentable, and the
PARTIAL unique indexes make a duplicated document, a second ACTIVE embedding
profile, and a second authoritative ATT&CK release impossible without relying on
NULL-uniqueness semantics (``NULL`` never conflicts in a plain unique index, so a
single ``(source_kind, external_key)`` index would silently allow duplicate
global documents).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from .base import CopilotBase

_SOURCE_KINDS = "'MITRE_ATTACK','CURATED_GUIDANCE','TENANT_RUNBOOK'"
_VISIBILITIES = "'GLOBAL','TENANT'"
_DOCUMENT_STATUSES = "'ACTIVE','RETIRED'"
_PROFILE_STATUSES = "'ACTIVE','RETIRED'"
_RELEASE_STATUSES = "'ACTIVE','INACTIVE'"
_DISTANCE_METRICS = "'COSINE'"
_NORMALIZATIONS = "'NONE','L2'"


class KnowledgeDocumentRow(CopilotBase):
    """One externally-identified knowledge document (mutable lifecycle only)."""

    __tablename__ = "knowledge_document"
    __table_args__ = (
        CheckConstraint(
            f"source_kind IN ({_SOURCE_KINDS})",
            name="knowledge_document_source_kind_valid",
        ),
        CheckConstraint(
            f"visibility IN ({_VISIBILITIES})",
            name="knowledge_document_visibility_valid",
        ),
        CheckConstraint(
            f"status IN ({_DOCUMENT_STATUSES})",
            name="knowledge_document_status_valid",
        ),
        # The scope invariant, enforced by the database rather than trusted to
        # application code: GLOBAL means "no tenant", TENANT means "exactly one".
        CheckConstraint(
            "(visibility = 'GLOBAL' AND tenant_id IS NULL) "
            "OR (visibility = 'TENANT' AND tenant_id IS NOT NULL)",
            name="knowledge_document_scope_coherent",
        ),
        CheckConstraint(
            "(status = 'ACTIVE' AND retired_at IS NULL) "
            "OR (status = 'RETIRED' AND retired_at IS NOT NULL)",
            name="knowledge_document_retirement_coherent",
        ),
        CheckConstraint("revision >= 0", name="knowledge_document_revision_valid"),
        CheckConstraint(
            "lock_version >= 0", name="knowledge_document_lock_version_valid"
        ),
        # Two PARTIAL unique indexes, not one: a plain index over
        # (source_kind, external_key) would let every GLOBAL row duplicate the
        # last one, because NULL tenant_id never conflicts in PostgreSQL.
        Index(
            "uq_knowledge_document_global_key",
            "source_kind",
            "external_key",
            unique=True,
            postgresql_where=text("visibility = 'GLOBAL'"),
        ),
        Index(
            "uq_knowledge_document_tenant_key",
            "tenant_id",
            "source_kind",
            "external_key",
            unique=True,
            postgresql_where=text("visibility = 'TENANT'"),
        ),
        Index("ix_knowledge_document_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    external_key: Mapped[str] = mapped_column(String(512), nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # A POINTER to the current immutable version. It is written in the SAME
    # transaction that persists that version and its chunks, so it can never
    # reference a version whose retrieval projection is missing. It is
    # deliberately not a foreign key: the two tables reference each other, and a
    # deferred circular constraint would buy nothing the transaction does not
    # already guarantee.
    active_version_id: Mapped[UUID | None] = mapped_column(nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(nullable=True)


class KnowledgeDocumentVersionRow(CopilotBase):
    """One immutable document version. A change APPENDS; it never updates."""

    __tablename__ = "knowledge_document_version"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_number_valid"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="version_content_hash_valid",
        ),
        CheckConstraint(
            "length(normalized_content) > 0",
            name="version_content_non_empty",
        ),
        # The dedup guarantee: re-ingesting identical content into a document can
        # never create a second version, whatever the caller does.
        Index(
            "uq_knowledge_document_version_number",
            "document_id",
            "version",
            unique=True,
        ),
        Index(
            "uq_knowledge_document_version_content_hash",
            "document_id",
            "content_hash",
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "knowledge_document.id",
            ondelete="RESTRICT",
            name="fk_knowledge_document_version_document",
        ),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_content: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    doc_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    ingested_at: Mapped[datetime] = mapped_column(nullable=False)
    effective_at: Mapped[datetime | None] = mapped_column(nullable=True)


class EmbeddingProfileRow(CopilotBase):
    """One embedding space. At most one row may be ACTIVE at a time."""

    __tablename__ = "embedding_profile"
    __table_args__ = (
        CheckConstraint(
            "dimension >= 1 AND dimension <= 8192",
            name="embedding_profile_dimension_bounded",
        ),
        CheckConstraint(
            f"distance_metric IN ({_DISTANCE_METRICS})",
            name="embedding_profile_distance_metric_valid",
        ),
        CheckConstraint(
            f"normalization IN ({_NORMALIZATIONS})",
            name="embedding_profile_normalization_valid",
        ),
        CheckConstraint(
            f"status IN ({_PROFILE_STATUSES})",
            name="embedding_profile_status_valid",
        ),
        CheckConstraint(
            "profile_version >= 1", name="embedding_profile_version_valid"
        ),
        CheckConstraint(
            "(status = 'ACTIVE' AND retired_at IS NULL) "
            "OR (status = 'RETIRED' AND retired_at IS NOT NULL)",
            name="embedding_profile_retirement_coherent",
        ),
        Index(
            "uq_embedding_profile_identity",
            "provider",
            "model_id",
            "dimension",
            "distance_metric",
            "normalization",
            "profile_version",
            unique=True,
        ),
        # "At most one ACTIVE profile" is a database fact, not an application
        # convention: a second activation fails at COMMIT instead of silently
        # producing two comparable-in-name-only vector spaces.
        Index(
            "uq_embedding_profile_single_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    distance_metric: Mapped[str] = mapped_column(String(16), nullable=False)
    normalization: Mapped[str] = mapped_column(String(16), nullable=False)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(nullable=True)


class KnowledgeChunkRow(CopilotBase):
    """The PRE-CLOSURE chunk table, kept so the upgrade is non-destructive.

    ``knowledge_chunk`` was content and embedding in one row, which is why a
    re-index used to break historical citations. It is no longer read or written
    by any P3-A path: retrieval reads ``knowledge_content_chunk`` and its
    ``knowledge_chunk_embedding`` projection, and ingestion writes only those.

    It is still DECLARED, for two load-bearing reasons. The new migration leaves
    every pre-existing row exactly where it was rather than dropping the table,
    so ``downgrade -1`` restores the pre-closure schema byte for byte instead of
    reconstructing rows it might not be able to represent (a content chunk with
    no embedding row yet has no faithful ``knowledge_chunk`` form). And a table
    that exists in the database but not in the metadata is permanent
    ``alembic check`` drift, which would mask the NEXT real drift.

    ``knowledge doctor`` reports this table and its row count, so an operator who
    finds it in the catalog knows it is superseded residue rather than live
    schema.
    """

    __tablename__ = "knowledge_chunk"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="knowledge_chunk_ordinal_non_negative"),
        CheckConstraint("length(content) > 0", name="knowledge_chunk_content_non_empty"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="knowledge_chunk_content_hash_valid"
        ),
        CheckConstraint("token_count >= 0", name="knowledge_chunk_token_count_valid"),
        Index(
            "uq_knowledge_chunk_version_ordinal",
            "document_version_id",
            "ordinal",
            unique=True,
        ),
        Index("ix_knowledge_chunk_lexical", "lexical_document", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "knowledge_document.id",
            ondelete="RESTRICT",
            name="fk_knowledge_chunk_document",
        ),
        nullable=False,
        index=True,
    )
    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "knowledge_document_version.id",
            ondelete="RESTRICT",
            name="fk_knowledge_chunk_document_version",
        ),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    chunker_version: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "embedding_profile.id",
            ondelete="RESTRICT",
            name="fk_knowledge_chunk_embedding_profile",
        ),
        nullable=False,
        index=True,
    )
    embedding: Mapped[list[float]] = mapped_column(VECTOR(), nullable=False)
    lexical_document: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(heading_path, '') || ' ' || content)",
            persisted=True,
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class KnowledgeContentChunkRow(CopilotBase):
    """One IMMUTABLE content chunk -- the identity a citation points at.

    Split out from the embedding projection (brief section 3.1) because the two
    have opposite lifetimes. This row is written once and never rewritten: it is
    the citation target, so a re-embedding, a retrieval-projection rebuild, a
    process restart, a retirement or a later version must all leave it intact.

    ``generation`` makes a chunker change non-destructive: rechunking the same
    immutable version appends generation N+1 and leaves N in place, so a citation
    into the older chunking still resolves. There is deliberately no
    ``delete_for_version`` anywhere in this subsystem -- immutable historical
    knowledge content is not deletable through P3-A.
    """

    __tablename__ = "knowledge_content_chunk"
    __table_args__ = (
        # Short names on purpose. The naming convention prefixes ``ck_<table>_``,
        # and PostgreSQL truncates an identifier at 63 bytes -- so repeating the
        # table name inside the constraint name silently produced a DIFFERENT
        # constraint than the metadata declared, and ``alembic check`` reported
        # permanent drift. The prefix already says which table these belong to.
        CheckConstraint("ordinal >= 0", name="ordinal_valid"),
        CheckConstraint("generation >= 1", name="generation_valid"),
        CheckConstraint("length(content) > 0", name="content_non_empty"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_valid"),
        CheckConstraint("token_count >= 0", name="token_count_valid"),
        CheckConstraint("length(chunker_version) > 0", name="chunker_version_valid"),
        # Ordinal is TOTAL inside one generation, which is what lets the stable
        # semantic ranking key be a total order (brief section 5.1).
        Index(
            "uq_knowledge_content_chunk_generation_ordinal",
            "document_version_id",
            "generation",
            "ordinal",
            unique=True,
        ),
        # Full-text search over the chunk's own text. The vector is COMPUTED, so
        # the lexical projection can never drift from the content it describes --
        # there is no code path that can forget to update it.
        Index(
            "ix_knowledge_content_chunk_lexical",
            "lexical_document",
            postgresql_using="gin",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "knowledge_document.id",
            ondelete="RESTRICT",
            name="fk_knowledge_content_chunk_document",
        ),
        nullable=False,
        index=True,
    )
    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "knowledge_document_version.id",
            ondelete="RESTRICT",
            name="fk_knowledge_content_chunk_document_version",
        ),
        nullable=False,
        index=True,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    # Which chunker produced this generation. Persisted so ingestion can tell
    # whether the CURRENT generation matches the frozen chunker configuration
    # instead of silently serving a projection built by different code (section 22).
    chunker_version: Mapped[str] = mapped_column(String(64), nullable=False)
    lexical_document: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(heading_path, '') || ' ' || content)",
            persisted=True,
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class KnowledgeChunkEmbeddingRow(CopilotBase):
    """The REBUILDABLE embedding projection of one content chunk.

    Holds no content, so it is safe to drop and recreate: a new embedding
    profile, a re-index, or a full vector rebuild replaces rows here and breaks no
    citation, because a citation names a ``knowledge_content_chunk`` id.

    ``UNIQUE(content_chunk_id, embedding_profile_id)`` makes re-embedding
    idempotent: the same chunk in the same space has exactly one vector.
    """

    __tablename__ = "knowledge_chunk_embedding"
    __table_args__ = (
        Index(
            "uq_knowledge_chunk_embedding_content_profile",
            "content_chunk_id",
            "embedding_profile_id",
            unique=True,
        ),
        Index("ix_knowledge_chunk_embedding_profile", "embedding_profile_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    content_chunk_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "knowledge_content_chunk.id",
            ondelete="RESTRICT",
            name="fk_knowledge_chunk_embedding_content_chunk",
        ),
        nullable=False,
    )
    embedding_profile_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "embedding_profile.id",
            ondelete="RESTRICT",
            name="fk_knowledge_chunk_embedding_embedding_profile",
        ),
        nullable=False,
    )
    # UNTYPED ``vector``: no fixed dimension is baked into the schema, so a second
    # embedding model of a different size does not need a migration. The
    # dimension contract is enforced by the embedding profile plus exact
    # comparison inside one ACTIVE profile (brief section 16).
    embedding: Mapped[list[float]] = mapped_column(VECTOR(), nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(nullable=False)


class AttackReleaseRow(CopilotBase):
    """One imported ATT&CK release and its authority state.

    Authority lives at RELEASE granularity because "which release is
    authoritative for this framework" is one fact about one release. The
    per-framework PARTIAL unique index below makes "at most one authoritative
    release" a database fact rather than an application convention -- the bug it
    replaces was exactly a model in which two releases could both claim to be
    current (brief section 2.1).

    ``content_fingerprint`` makes a pinned release IMMUTABLE: re-importing the
    same release with different content is refused rather than absorbed. It is
    NULL only for a release adopted by the migration from rows that predate this
    table; such a release is verified against what is stored and pinned on the
    next import of the same bytes, and a NULL fingerprint never counts as a match
    (brief section 2.2).
    """

    __tablename__ = "attack_release"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_RELEASE_STATUSES})", name="attack_release_status_valid"
        ),
        CheckConstraint(
            "content_fingerprint IS NULL OR content_fingerprint ~ '^[0-9a-f]{64}$'",
            name="attack_release_fingerprint_valid",
        ),
        CheckConstraint(
            "technique_count >= 0", name="attack_release_technique_count_valid"
        ),
        Index(
            "uq_attack_release_framework_source_release",
            "framework",
            "source_release",
            unique=True,
        ),
        # THE rule of brief section 2.1, expressed as a constraint: a second
        # ACTIVE release for a framework fails at COMMIT instead of silently
        # producing two authoritative releases.
        Index(
            "uq_attack_release_single_active",
            "framework",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    framework: Mapped[str] = mapped_column(String(32), nullable=False)
    source_release: Mapped[str] = mapped_column(String(32), nullable=False)
    content_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    technique_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(nullable=True)


class AttackTechniqueRow(CopilotBase):
    """One canonical ATT&CK technique for a pinned source release.

    This is the canonical technique fact. A MITRE KnowledgeDocument generated for
    the same technique is RELATED but is not the same authority: the document is
    retrieval content, this row is the technique record.

    ``active`` MIRRORS the owning release's authority and never decides for
    itself whether it is authoritative; the foreign key makes a technique row
    with no registered release unrepresentable, so a technique can never be the
    place where "which release is current" is answered (brief section 2.3).
    """

    __tablename__ = "attack_technique"
    __table_args__ = (
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="attack_technique_content_hash_valid",
        ),
        # Re-importing the same release cannot duplicate a technique; a NEW
        # release is a new set of rows, and old releases are never deleted.
        Index(
            "uq_attack_technique_release",
            "framework",
            "technique_id",
            "source_release",
            unique=True,
        ),
        ForeignKeyConstraint(
            ["framework", "source_release"],
            ["attack_release.framework", "attack_release.source_release"],
            ondelete="RESTRICT",
            name="fk_attack_technique_attack_release_release",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    framework: Mapped[str] = mapped_column(String(32), nullable=False)
    technique_id: Mapped[str] = mapped_column(String(32), nullable=False)
    source_release: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tactics: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    platforms: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    source_stix_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class AttackReleaseProjectionRow(CopilotBase):
    """The binding of one release's technique to the version it staged.

    The table exists because two facts that look like one are genuinely two.
    "Release v15.1 carries this technique's content" and "the immutable version
    row this release projects is V" are different claims: v14.1 and v15.1 may
    carry byte-identical content and therefore share V, in which case a single
    version row cannot record which release staged it. ``source_version`` on the
    version records only which release CREATED it, which misattributes the other.

    The binding is written at STAGE time and never re-derived, so re-activating an
    older release restores the exact version that release staged rather than a
    version reconstructed from whatever currently matches (brief section 2.6).

    Nothing here confers authority. Which release is authoritative is
    ``attack_release.status``; this row says only which version a release's
    projection IS, which is what the atomic cutover moves the document pointers
    to (brief section 2.10).
    """

    __tablename__ = "attack_release_projection"
    __table_args__ = (
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="content_hash_valid",
        ),
        # One projection per (release, technique): the natural key, and the
        # conflict target that makes a retried stage converge (brief section 2.8).
        Index(
            "uq_attack_release_projection_release_technique",
            "framework",
            "source_release",
            "technique_id",
            unique=True,
        ),
        # A release may not claim two different projections of one document.
        Index(
            "uq_attack_release_projection_release_document",
            "framework",
            "source_release",
            "document_id",
            unique=True,
        ),
        Index(
            "ix_attack_release_projection_document",
            "document_id",
        ),
        ForeignKeyConstraint(
            ["framework", "source_release"],
            ["attack_release.framework", "attack_release.source_release"],
            ondelete="RESTRICT",
            name="fk_attack_release_projection_release",
        ),
        ForeignKeyConstraint(
            ["framework", "technique_id", "source_release"],
            [
                "attack_technique.framework",
                "attack_technique.technique_id",
                "attack_technique.source_release",
            ],
            ondelete="RESTRICT",
            name="fk_attack_release_projection_technique",
        ),
        # RESTRICT, not CASCADE: a projection that silently vanished with its
        # version would leave a release authoritative for content retrieval can
        # no longer return.
        ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_document.id"],
            ondelete="RESTRICT",
            name="fk_attack_release_projection_document",
        ),
        ForeignKeyConstraint(
            ["document_version_id"],
            ["knowledge_document_version.id"],
            ondelete="RESTRICT",
            name="fk_attack_release_projection_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    framework: Mapped[str] = mapped_column(String(32), nullable=False)
    source_release: Mapped[str] = mapped_column(String(32), nullable=False)
    technique_id: Mapped[str] = mapped_column(String(32), nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

