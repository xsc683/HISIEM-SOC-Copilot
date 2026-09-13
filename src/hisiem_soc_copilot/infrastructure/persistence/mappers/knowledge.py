"""Explicit mappers for the knowledge persistence rows (brief sections 12/13).

ORM rows are translated to/from pure domain dataclasses here — no
``from_attributes`` magic (python-package-boundary.md §18). Field-by-field is
deliberate: a new column cannot silently appear on a domain object, and a domain
field cannot be silently dropped on the way to the database.

Two conversions are easy to get wrong and are therefore done in exactly one place:

* enum fields are stored as RAW strings (the schema's CHECK constraints are the
  arbiter), so loading converts with ``SourceKind(value)`` and writes ``.value``;
  a stored value outside the enum raises ``ValueError`` rather than being coerced,
  because a corrupt row is a bug to surface, never to paper over;
* ``knowledge_document_version.metadata`` is a column literally NAMED ``metadata``
  while the ORM attribute is ``doc_metadata`` (``metadata`` is taken by
  ``DeclarativeBase``), so the rename lives here and nowhere else.

``row_to_document`` builds the aggregate DIRECTLY. It must never call
``KnowledgeDocument.create()``: that factory appends a
``knowledge_document_created`` domain event, and re-loading a row is not a new
business fact — replaying it would duplicate the audit ledger.
"""

from __future__ import annotations

from ....application.ports.knowledge import (
    AttackReleaseProjectionRecord,
    AttackReleaseRecord,
    AttackTechniqueRecord,
    ChunkEmbeddingRecord,
    EmbeddingProfileRecord,
    KnowledgeContentChunkRecord,
)
from ....domain.knowledge.entities import (
    KnowledgeContentChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
)
from ....domain.knowledge.enums import DocumentStatus, SourceKind, Visibility
from ..orm.knowledge import (
    AttackReleaseProjectionRow,
    AttackReleaseRow,
    AttackTechniqueRow,
    EmbeddingProfileRow,
    KnowledgeChunkEmbeddingRow,
    KnowledgeContentChunkRow,
    KnowledgeDocumentRow,
    KnowledgeDocumentVersionRow,
)


def document_to_row(document: KnowledgeDocument) -> KnowledgeDocumentRow:
    """Translate the aggregate into a NEW ORM row (insert path)."""
    return KnowledgeDocumentRow(
        id=document.id,
        source_kind=document.source_kind.value,
        external_key=document.external_key,
        visibility=document.visibility.value,
        tenant_id=document.tenant_id,
        title=document.title,
        status=document.status.value,
        active_version_id=document.active_version_id,
        revision=document.revision,
        lock_version=document.lock_version,
        created_at=document.created_at,
        retired_at=document.retired_at,
    )


def row_to_document(row: KnowledgeDocumentRow) -> KnowledgeDocument:
    """Rebuild the aggregate from its row WITHOUT emitting domain events."""
    return KnowledgeDocument(
        id=row.id,
        source_kind=SourceKind(row.source_kind),
        external_key=row.external_key,
        visibility=Visibility(row.visibility),
        tenant_id=row.tenant_id,
        title=row.title,
        status=DocumentStatus(row.status),
        active_version_id=row.active_version_id,
        lock_version=row.lock_version,
        revision=row.revision,
        created_at=row.created_at,
        retired_at=row.retired_at,
    )


def version_to_row(version: KnowledgeDocumentVersion) -> KnowledgeDocumentVersionRow:
    """Translate an immutable version into a NEW ORM row (append-only)."""
    return KnowledgeDocumentVersionRow(
        id=version.id,
        document_id=version.document_id,
        version=version.version,
        content_hash=version.content_hash,
        title=version.title,
        normalized_content=version.normalized_content,
        language=version.language,
        source_version=version.source_version,
        doc_metadata=dict(version.metadata),
        ingested_at=version.ingested_at,
        effective_at=version.effective_at,
    )


def row_to_version(row: KnowledgeDocumentVersionRow) -> KnowledgeDocumentVersion:
    """Rebuild an immutable version, renaming the ``metadata`` column back."""
    return KnowledgeDocumentVersion(
        id=row.id,
        document_id=row.document_id,
        version=row.version,
        content_hash=row.content_hash,
        title=row.title,
        normalized_content=row.normalized_content,
        language=row.language,
        source_version=row.source_version,
        metadata=dict(row.doc_metadata or {}),
        ingested_at=row.ingested_at,
        effective_at=row.effective_at,
    )


def content_chunk_to_row(
    chunk: KnowledgeContentChunkRecord,
) -> KnowledgeContentChunkRow:
    """Translate an immutable content chunk into a NEW ORM row.

    ``lexical_document`` is a GENERATED column, so it is deliberately absent here:
    the database derives it, and no code path can make it drift from the content.

    The row is built through the DOMAIN entity first, so the
    ``content_hash == SHA-256(content)`` invariant is enforced on the write path
    too and not only on the read path (brief section 8). A caller that hands over
    a mismatched pair fails here, before the row is ever added to a session,
    instead of persisting a citation target that cannot resolve.
    """
    entity = KnowledgeContentChunk(
        id=chunk.id,
        document_id=chunk.document_id,
        document_version_id=chunk.document_version_id,
        generation=chunk.generation,
        ordinal=chunk.ordinal,
        heading_path=chunk.heading_path,
        content=chunk.content,
        content_hash=chunk.content_hash,
        token_count=chunk.token_count,
        language=chunk.language,
        chunker_version=chunk.chunker_version,
        created_at=chunk.created_at,
    )
    return KnowledgeContentChunkRow(
        id=entity.id,
        document_id=entity.document_id,
        document_version_id=entity.document_version_id,
        generation=entity.generation,
        ordinal=entity.ordinal,
        heading_path=entity.heading_path,
        content=entity.content,
        content_hash=entity.content_hash,
        token_count=entity.token_count,
        language=entity.language,
        chunker_version=entity.chunker_version,
        created_at=entity.created_at,
    )


def row_to_content_chunk(
    row: KnowledgeContentChunkRow,
) -> KnowledgeContentChunkRecord:
    """Rebuild an immutable content chunk, likewise through the domain entity.

    Loading is where a tampered row would otherwise enter the system unnoticed:
    the entity recomputes the hash from the stored content, so a chunk whose text
    or hash was edited out-of-band raises ``InvalidKnowledgeChunkError`` rather
    than being served as if it were intact (brief sections 3.4/8).
    """
    entity = KnowledgeContentChunk(
        id=row.id,
        document_id=row.document_id,
        document_version_id=row.document_version_id,
        generation=row.generation,
        ordinal=row.ordinal,
        heading_path=row.heading_path,
        content=row.content,
        content_hash=row.content_hash,
        token_count=row.token_count,
        language=row.language,
        chunker_version=row.chunker_version,
        created_at=row.created_at,
    )
    return KnowledgeContentChunkRecord(
        id=entity.id,
        document_id=entity.document_id,
        document_version_id=entity.document_version_id,
        generation=entity.generation,
        ordinal=entity.ordinal,
        heading_path=entity.heading_path,
        content=entity.content,
        content_hash=entity.content_hash,
        token_count=entity.token_count,
        language=entity.language,
        chunker_version=entity.chunker_version,
        created_at=entity.created_at,
    )


def embedding_to_row(embedding: ChunkEmbeddingRecord) -> KnowledgeChunkEmbeddingRow:
    """Translate a rebuildable embedding projection into a NEW ORM row."""
    return KnowledgeChunkEmbeddingRow(
        id=embedding.id,
        content_chunk_id=embedding.content_chunk_id,
        embedding_profile_id=embedding.embedding_profile_id,
        # The domain carries an immutable tuple; pgvector's column is a list.
        embedding=list(embedding.embedding),
        indexed_at=embedding.indexed_at,
    )


def release_to_row(release: AttackReleaseRecord) -> AttackReleaseRow:
    """Translate an ATT&CK release record into a NEW ORM row."""
    return AttackReleaseRow(
        id=release.id,
        framework=release.framework,
        source_release=release.source_release,
        content_fingerprint=release.content_fingerprint,
        status=release.status,
        technique_count=release.technique_count,
        created_at=release.created_at,
        activated_at=release.activated_at,
    )


def row_to_release(row: AttackReleaseRow) -> AttackReleaseRecord:
    """Rebuild an ATT&CK release record from its row."""
    return AttackReleaseRecord(
        id=row.id,
        framework=row.framework,
        source_release=row.source_release,
        content_fingerprint=row.content_fingerprint,
        status=row.status,
        technique_count=row.technique_count,
        created_at=row.created_at,
        activated_at=row.activated_at,
    )


def profile_to_row(profile: EmbeddingProfileRecord) -> EmbeddingProfileRow:
    """Translate an embedding profile record into a NEW ORM row."""
    return EmbeddingProfileRow(
        id=profile.id,
        provider=profile.provider,
        model_id=profile.model_id,
        dimension=profile.dimension,
        distance_metric=profile.distance_metric,
        normalization=profile.normalization,
        profile_version=profile.profile_version,
        status=profile.status,
        created_at=profile.created_at,
        retired_at=profile.retired_at,
    )


def row_to_profile(row: EmbeddingProfileRow) -> EmbeddingProfileRecord:
    """Rebuild an embedding profile record from its row.

    ``status`` stays a plain ``str``: it is a lifecycle token owned by the schema's
    CHECK constraint and the single-ACTIVE partial index, not a domain enum here.
    """
    return EmbeddingProfileRecord(
        id=row.id,
        provider=row.provider,
        model_id=row.model_id,
        dimension=row.dimension,
        distance_metric=row.distance_metric,
        normalization=row.normalization,
        profile_version=row.profile_version,
        status=row.status,
        created_at=row.created_at,
        retired_at=row.retired_at,
    )


def technique_to_row(technique: AttackTechniqueRecord) -> AttackTechniqueRow:
    """Translate an ATT&CK technique record into a NEW ORM row."""
    return AttackTechniqueRow(
        id=technique.id,
        framework=technique.framework,
        technique_id=technique.technique_id,
        source_release=technique.source_release,
        name=technique.name,
        description=technique.description,
        tactics=list(technique.tactics),
        platforms=list(technique.platforms),
        source_stix_id=technique.source_stix_id,
        content_hash=technique.content_hash,
        active=technique.active,
        created_at=technique.created_at,
    )


def row_to_technique(row: AttackTechniqueRow) -> AttackTechniqueRecord:
    """Rebuild an ATT&CK technique record, restoring the tuple JSONB lists."""
    return AttackTechniqueRecord(
        id=row.id,
        framework=row.framework,
        technique_id=row.technique_id,
        source_release=row.source_release,
        name=row.name,
        description=row.description,
        tactics=tuple(row.tactics or ()),
        platforms=tuple(row.platforms or ()),
        source_stix_id=row.source_stix_id,
        content_hash=row.content_hash,
        active=row.active,
        created_at=row.created_at,
    )

def row_to_projection(row: AttackReleaseProjectionRow) -> AttackReleaseProjectionRecord:
    """Rebuild a release-projection binding from its row.

    A pure translation: the binding is authoritative in the database and is never
    re-derived from content, which is what keeps re-activating an older release
    restoring the version that release staged (brief section 2.6).
    """
    return AttackReleaseProjectionRecord(
        id=row.id,
        framework=row.framework,
        source_release=row.source_release,
        technique_id=row.technique_id,
        document_id=row.document_id,
        document_version_id=row.document_version_id,
        content_hash=row.content_hash,
        created_at=row.created_at,
    )


def projection_values(
    projection: AttackReleaseProjectionRecord,
) -> dict[str, object]:
    """Translate a projection record into INSERT values."""
    return {
        "id": projection.id,
        "framework": projection.framework,
        "source_release": projection.source_release,
        "technique_id": projection.technique_id,
        "document_id": projection.document_id,
        "document_version_id": projection.document_version_id,
        "content_hash": projection.content_hash,
        "created_at": projection.created_at,
    }

