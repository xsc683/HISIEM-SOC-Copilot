"""ORM models for the knowledge subsystem (brief sections 10-16, 36-38).

Five tables, three different kinds of thing:

``knowledge_document`` / ``knowledge_document_version``
    The authoritative knowledge truth. A document is identified by
    ``(source_kind, external_key)`` inside a scope, and its content lives in
    immutable versions that are only ever appended.

``knowledge_chunk``
    A REBUILDABLE retrieval projection, not a domain aggregate. Chunks can be
    regenerated from an immutable version at any time, so nothing authoritative
    depends on them.

``embedding_profile`` / ``attack_technique``
    Supporting records: which vector space the chunks were indexed in (at most
    one ACTIVE), and the canonical ATT&CK technique rows for a pinned release.

The database is the final arbiter for the invariants the domain also checks: the
scope CHECK constraints make "GLOBAL with a tenant" unrepresentable, and the two
PARTIAL unique indexes make a duplicated document impossible without relying on
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
    """A rebuildable retrieval projection of one immutable version's content."""

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
        # Full-text search over the chunk's own text. The vector is COMPUTED, so
        # the lexical projection can never drift from the content it describes --
        # there is no code path that can forget to update it.
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
    # Which chunker produced this projection. Without it, a chunker configuration
    # change would leave undocumented stale projections in place and there would
    # be no way to notice (brief section 22).
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
    # UNTYPED ``vector``: no fixed dimension is baked into the schema, so a second
    # embedding model of a different size does not need a migration. The
    # dimension contract is enforced by the embedding profile plus exact
    # comparison inside one ACTIVE profile (brief section 16).
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


class AttackTechniqueRow(CopilotBase):
    """One canonical ATT&CK technique for a pinned source release.

    This is the canonical technique fact. A MITRE KnowledgeDocument generated for
    the same technique is RELATED but is not the same authority: the document is
    retrieval content, this row is the technique record.
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
