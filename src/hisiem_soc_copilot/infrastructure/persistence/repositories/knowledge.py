"""SQLAlchemy knowledge repositories implementing the application ports.

Three rules from the frozen knowledge contract drive every method here (brief
sections 12/13/44/45):

1. **Scope is a SQL WHERE clause, never a Python post-filter.** Visibility decides
   which tenant may read a row at all; loading the corpus and filtering in Python
   would both leak rows the caller may not see and not scale. Every read takes
   ``tenant_id`` as a required keyword argument, so there is no scope-less path.
2. **The database is the final arbiter.** The scope CHECK constraints, the two
   PARTIAL unique indexes on the document key, and the version uniqueness indexes
   are what actually enforce the invariants; the repository relies on them rather
   than re-implementing them in application code.
3. **Candidate generation reads the ACTIVE version only.** Lexical and vector
   retrieval join to the document and require
   ``active_version_id = knowledge_document_version.id``, so a superseded version's
   chunks can never surface. Citation RESOLUTION is the deliberate exception: it is
   scope-filtered only, so a citation to a still-existing historical version of a
   RETIRED document stays resolvable (brief section 51).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    CursorResult,
    RowMapping,
    Select,
    Text,
    and_,
    delete,
    func,
    insert,
    or_,
    select,
    update,
)

# Aliased: ``typing.cast`` (used for the rowcount result) and SQLAlchemy's SQL
# ``cast`` expression are different functions with the same name.
from sqlalchemy import cast as sa_cast
from sqlalchemy.ext.asyncio import AsyncSession

from ....application.ports.knowledge import (
    AttackTechniqueRecord,
    AttackTechniqueRepository,
    ChunkProjectionState,
    EmbeddingProfileRecord,
    EmbeddingProfileRepository,
    KnowledgeChunkRecord,
    KnowledgeChunkRepository,
    KnowledgeChunkView,
    KnowledgeDocumentRepository,
    LexicalCandidate,
    VectorCandidate,
)
from ....domain.knowledge.entities import KnowledgeDocument, KnowledgeDocumentVersion
from ....domain.knowledge.enums import (
    DocumentStatus,
    EmbeddingProfileStatus,
    SourceKind,
    Visibility,
)
from ....domain.shared.errors import OptimisticConcurrencyError
from ..mappers.knowledge import (
    chunk_to_row,
    document_to_row,
    profile_to_row,
    row_to_document,
    row_to_profile,
    row_to_technique,
    row_to_version,
    technique_to_row,
    version_to_row,
)
from ..orm.knowledge import (
    AttackTechniqueRow,
    EmbeddingProfileRow,
    KnowledgeChunkRow,
    KnowledgeDocumentRow,
    KnowledgeDocumentVersionRow,
)

#: The text-search configuration used by the chunk's GENERATED ``lexical_document``
#: column. It MUST stay identical here: ``ts_rank`` against a vector built with a
#: different configuration silently returns a meaningless score instead of failing.
_TS_CONFIG = "simple"

#: The result shape of :func:`_chunk_view_select`. Written out so the shared column
#: projection stays type-checked end to end (mypy strict, no ``Any``).
_ChunkViewSelect = Select[
    tuple[
        UUID,
        UUID,
        UUID,
        int,
        str,
        str,
        str,
        str,
        str,
        str,
        str | None,
        str,
        str,
        str | None,
    ]
]


def _scope_predicate(tenant_id: str) -> ColumnElement[bool]:
    """The one scope rule: GLOBAL rows plus this tenant's own TENANT rows.

    This mirrors :meth:`KnowledgeDocument.is_visible_to` exactly. It is a SCOPE,
    not a permission model -- it decides whose retrieval may read the row, and
    confers no authority.
    """
    return or_(
        KnowledgeDocumentRow.visibility == Visibility.GLOBAL.value,
        and_(
            KnowledgeDocumentRow.visibility == Visibility.TENANT.value,
            KnowledgeDocumentRow.tenant_id == tenant_id,
        ),
    )


def _chunk_view_select() -> _ChunkViewSelect:
    """The shared chunk + version + document projection, columns listed explicitly.

    Whole rows are deliberately not loaded: retrieval needs a handful of facts
    from three tables, and naming them keeps the SQL honest about what it reads.
    """
    return (
        select(
            KnowledgeChunkRow.id.label("chunk_id"),
            KnowledgeDocumentRow.id.label("document_id"),
            KnowledgeDocumentVersionRow.id.label("document_version_id"),
            KnowledgeChunkRow.ordinal.label("ordinal"),
            KnowledgeChunkRow.heading_path.label("heading_path"),
            KnowledgeChunkRow.content.label("content"),
            KnowledgeChunkRow.content_hash.label("content_hash"),
            KnowledgeDocumentVersionRow.title.label("title"),
            KnowledgeDocumentRow.source_kind.label("source_kind"),
            KnowledgeDocumentVersionRow.language.label("language"),
            KnowledgeDocumentVersionRow.source_version.label("source_version"),
            KnowledgeDocumentRow.status.label("document_status"),
            KnowledgeDocumentRow.visibility.label("visibility"),
            KnowledgeDocumentRow.tenant_id.label("tenant_id"),
        )
        .join(
            KnowledgeDocumentVersionRow,
            KnowledgeDocumentVersionRow.id == KnowledgeChunkRow.document_version_id,
        )
        .join(
            KnowledgeDocumentRow,
            KnowledgeDocumentRow.id == KnowledgeDocumentVersionRow.document_id,
        )
    )


def _view_from_mapping(row: RowMapping) -> KnowledgeChunkView:
    """Build the typed view from a labeled mapping.

    ``title``/``language``/``source_version`` come from the VERSION row: a citation
    names an immutable version, so its provenance title must be the version's, not
    whatever the (mutable) document title happens to be now.
    """
    return KnowledgeChunkView(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        document_version_id=row["document_version_id"],
        ordinal=row["ordinal"],
        heading_path=row["heading_path"],
        content=row["content"],
        content_hash=row["content_hash"],
        title=row["title"],
        source_kind=SourceKind(row["source_kind"]),
        language=row["language"],
        source_version=row["source_version"],
        document_status=row["document_status"],
        visibility=Visibility(row["visibility"]),
        tenant_id=row["tenant_id"],
    )


def _chunk_values(chunk: KnowledgeChunkRecord) -> dict[str, object]:
    """One bulk-insert parameter set for a chunk.

    The field mapping itself lives in the mapper (one definition, so bulk and
    single-row writes can never diverge). Only the GENERATED ``lexical_document``
    column is dropped: the database derives it, and an INSERT may not name it.
    """
    row = chunk_to_row(chunk)
    return {
        column.key: getattr(row, column.key)
        for column in KnowledgeChunkRow.__table__.columns
        if column.computed is None
    }


def _technique_values(technique: AttackTechniqueRecord) -> dict[str, object]:
    """One bulk-insert parameter set for an ATT&CK technique row (mapper-driven)."""
    row = technique_to_row(technique)
    return {
        column.key: getattr(row, column.key)
        for column in AttackTechniqueRow.__table__.columns
    }


def _or_tsquery(search_terms: Sequence[str]) -> ColumnElement[bool]:
    """OR together one ``plainto_tsquery`` per term.

    PostgreSQL's ``plainto_tsquery`` ANDs the words it is given, so passing a whole
    query string would require EVERY word to appear in one chunk and collapse
    lexical recall. The OR is assembled from the NUMBER of terms -- never from their
    text -- and each term reaches the database as its own BOUND PARAMETER, so no
    caller-supplied string is ever interpolated into SQL or parsed as an operator
    expression. ``to_tsquery`` is never used: it would let caller text become query
    syntax (brief section 43).
    """
    query_expr: ColumnElement[bool] | None = None
    for term in search_terms:
        term_query = func.plainto_tsquery(_TS_CONFIG, term)
        query_expr = term_query if query_expr is None else query_expr.op("||")(term_query)
    if query_expr is None:
        raise ValueError("search_terms must contain at least one term")
    return query_expr


class SqlAlchemyKnowledgeDocumentRepository(KnowledgeDocumentRepository):
    """Tenant-scoped document/version persistence with an optimistic-lock save."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, *, tenant_id: str, document_id: UUID
    ) -> KnowledgeDocument | None:
        """Load one document the tenant may read (GLOBAL, or its own TENANT row)."""
        result = await self._session.execute(
            select(KnowledgeDocumentRow).where(
                KnowledgeDocumentRow.id == document_id,
                _scope_predicate(tenant_id),
            )
        )
        row = result.scalar_one_or_none()
        return row_to_document(row) if row is not None else None

    async def find_by_external_key(
        self,
        *,
        tenant_id: str | None,
        source_kind: SourceKind,
        external_key: str,
        visibility: Visibility,
    ) -> KnowledgeDocument | None:
        """Find the document for ``(source_kind, external_key)`` in an exact scope.

        The lookup matches on the SCOPE, not only on the key: the same
        ``(source_kind, external_key)`` may legitimately exist once GLOBALLY and once
        per tenant. The predicates mirror the two PARTIAL unique indexes exactly --
        GLOBAL filters ``tenant_id IS NULL`` (the passed value is ignored, because
        a GLOBAL row has no tenant by definition), TENANT filters ``tenant_id ==
        tenant_id``. Any other tenant's document is not found.
        """
        if visibility is Visibility.GLOBAL:
            scope: ColumnElement[bool] = KnowledgeDocumentRow.tenant_id.is_(None)
        else:
            scope = KnowledgeDocumentRow.tenant_id == tenant_id
        result = await self._session.execute(
            select(KnowledgeDocumentRow).where(
                KnowledgeDocumentRow.source_kind == source_kind.value,
                KnowledgeDocumentRow.external_key == external_key,
                KnowledgeDocumentRow.visibility == visibility.value,
                scope,
            )
        )
        row = result.scalar_one_or_none()
        return row_to_document(row) if row is not None else None

    async def add(self, *, document: KnowledgeDocument) -> None:
        self._session.add(document_to_row(document))

    async def save(self, *, document: KnowledgeDocument) -> None:
        """CAS-update the MUTABLE lifecycle columns.

        The WHERE clause carries the expected ``lock_version``; a 0-row update means
        another writer committed first, so this raises rather than silently
        last-write-wins. Identity columns (``source_kind``, ``external_key``,
        ``visibility``, ``tenant_id``, ``created_at``, ``id``) are deliberately never
        in the SET list: they are immutable for the life of the document, and a
        caller bug must not be able to rewrite identity through a lifecycle save.
        """
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                update(KnowledgeDocumentRow)
                .where(
                    KnowledgeDocumentRow.id == document.id,
                    KnowledgeDocumentRow.lock_version == document.lock_version,
                )
                .values(
                    title=document.title,
                    status=document.status.value,
                    active_version_id=document.active_version_id,
                    revision=document.revision,
                    retired_at=document.retired_at,
                    lock_version=document.lock_version + 1,
                )
            ),
        )
        if result.rowcount == 0:
            raise OptimisticConcurrencyError(
                aggregate_type="knowledge_document",
                aggregate_id=str(document.id),
            )
        document.lock_version += 1

    async def next_version_number(self, *, document_id: UUID) -> int:
        """The next version number for a document -- a HINT, not a reservation.

        Two concurrent ingests can read the same value; ``UNIQUE(document_id,
        version)`` is the real arbiter and the loser retries. Handing out a number
        here (rather than a sequence) is what makes the numbering dense per
        document instead of global.
        """
        result = await self._session.execute(
            select(
                func.coalesce(func.max(KnowledgeDocumentVersionRow.version), 0) + 1
            ).where(KnowledgeDocumentVersionRow.document_id == document_id)
        )
        return int(result.scalar_one())

    async def add_version(self, *, version: KnowledgeDocumentVersion) -> None:
        """Append an immutable version. A change appends; it never updates."""
        self._session.add(version_to_row(version))

    async def find_version_by_content_hash(
        self, *, document_id: UUID, content_hash: str
    ) -> KnowledgeDocumentVersion | None:
        """The existing version with this content hash, if any.

        Not tenant-filtered by design: the caller has already loaded the document
        through a scoped read, and ``UNIQUE(document_id, content_hash)`` makes this
        the dedup lookup that stops identical content creating a second version.
        """
        result = await self._session.execute(
            select(KnowledgeDocumentVersionRow).where(
                KnowledgeDocumentVersionRow.document_id == document_id,
                KnowledgeDocumentVersionRow.content_hash == content_hash,
            )
        )
        row = result.scalar_one_or_none()
        return row_to_version(row) if row is not None else None

    async def get_version(
        self, *, tenant_id: str, document_version_id: UUID
    ) -> KnowledgeDocumentVersion | None:
        """Load one immutable version by joining to its document for scope.

        Deliberately does NOT filter on document status or active version: history
        stays readable so an older citation can still be re-validated.
        """
        result = await self._session.execute(
            select(KnowledgeDocumentVersionRow)
            .join(
                KnowledgeDocumentRow,
                KnowledgeDocumentRow.id == KnowledgeDocumentVersionRow.document_id,
            )
            .where(
                KnowledgeDocumentVersionRow.id == document_version_id,
                _scope_predicate(tenant_id),
            )
        )
        row = result.scalar_one_or_none()
        return row_to_version(row) if row is not None else None

    async def list_versions(
        self, *, tenant_id: str, document_id: UUID
    ) -> tuple[KnowledgeDocumentVersion, ...]:
        """Every version of a document the tenant may read, oldest first."""
        result = await self._session.execute(
            select(KnowledgeDocumentVersionRow)
            .join(
                KnowledgeDocumentRow,
                KnowledgeDocumentRow.id == KnowledgeDocumentVersionRow.document_id,
            )
            .where(
                KnowledgeDocumentVersionRow.document_id == document_id,
                _scope_predicate(tenant_id),
            )
            .order_by(KnowledgeDocumentVersionRow.version.asc())
        )
        return tuple(row_to_version(row) for row in result.scalars().all())


class SqlAlchemyKnowledgeChunkRepository(KnowledgeChunkRepository):
    """Rebuildable chunk projections plus scope- and active-version-filtered search."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_many(self, *, chunks: Sequence[KnowledgeChunkRecord]) -> None:
        """Bulk-insert chunk projections. An empty sequence is a no-op, not a query."""
        if not chunks:
            return
        await self._session.execute(
            insert(KnowledgeChunkRow),
            [_chunk_values(chunk) for chunk in chunks],
        )

    async def count_for_version(self, *, document_version_id: UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(KnowledgeChunkRow)
            .where(KnowledgeChunkRow.document_version_id == document_version_id)
        )
        return int(result.scalar_one())

    async def projection_state(
        self, *, document_version_id: UUID
    ) -> ChunkProjectionState:
        """What this version's retrieval projection currently is, in one round trip.

        Ingestion compares this against the frozen embedding profile and chunker
        version to choose between a true no-op and a rebuild. ``MIN`` is safe
        because a version's chunks are always written together under one profile
        and one chunker, so the values are uniform; if they ever stop being uniform
        the aggregate still reports something a caller can compare (and the
        projection is rebuildable anyway).
        """
        result = await self._session.execute(
            select(
                func.count().label("chunk_count"),
                # PostgreSQL has NO ``min``/``max`` aggregate for ``uuid``, so the
                # profile id is folded as text and parsed back. That keeps this a
                # SINGLE round trip; ``MIN`` over the chunker version (text) is
                # native.
                func.min(sa_cast(KnowledgeChunkRow.embedding_profile_id, Text)).label(
                    "embedding_profile_id"
                ),
                func.min(KnowledgeChunkRow.chunker_version).label("chunker_version"),
            ).where(KnowledgeChunkRow.document_version_id == document_version_id)
        )
        row = result.mappings().one()
        if int(row["chunk_count"]) == 0:
            return ChunkProjectionState(None, None, 0)
        return ChunkProjectionState(
            embedding_profile_id=UUID(str(row["embedding_profile_id"])),
            chunker_version=row["chunker_version"],
            chunk_count=int(row["chunk_count"]),
        )

    async def delete_for_version(self, *, document_version_id: UUID) -> None:
        """Drop a version's chunk projection so it can be REBUILT.

        The version row itself stays immutable: only the rebuildable retrieval
        projection is regenerated, which is what lets a projection be rebuilt after
        the embedding profile or the chunker version changed.
        """
        await self._session.execute(
            delete(KnowledgeChunkRow).where(
                KnowledgeChunkRow.document_version_id == document_version_id
            )
        )

    async def lexical_candidates(
        self, *, tenant_id: str, search_terms: Sequence[str], limit: int
    ) -> tuple[LexicalCandidate, ...]:
        """Full-text candidates from the ACTIVE version, ranked by ``ts_rank``.

        The query is an OR of one ``plainto_tsquery`` per term (see
        :func:`_or_tsquery`), so a multi-term query matches a chunk containing SOME
        of the terms instead of demanding all of them, while every term still
        travels as a bound parameter and the configuration stays ``simple`` -- the
        same one baked into the generated column (brief sections 43/52).
        """
        if not search_terms:
            return ()
        query_expr = _or_tsquery(search_terms)
        rank = func.ts_rank(KnowledgeChunkRow.lexical_document, query_expr).label("rank")
        result = await self._session.execute(
            _chunk_view_select()
            .add_columns(rank)
            .where(
                _scope_predicate(tenant_id),
                KnowledgeDocumentRow.status == DocumentStatus.ACTIVE.value,
                KnowledgeDocumentRow.active_version_id
                == KnowledgeDocumentVersionRow.id,
                KnowledgeChunkRow.lexical_document.bool_op("@@")(query_expr),
            )
            .order_by(
                rank.desc(),
                KnowledgeDocumentRow.id,
                KnowledgeDocumentVersionRow.id,
                KnowledgeChunkRow.ordinal,
            )
            .limit(limit)
        )
        return tuple(
            LexicalCandidate(view=_view_from_mapping(row), rank=float(row["rank"]))
            for row in result.mappings().all()
        )

    async def vector_candidates(
        self,
        *,
        tenant_id: str,
        embedding_profile_id: UUID,
        query_vector: Sequence[float],
        limit: int,
    ) -> tuple[VectorCandidate, ...]:
        """EXACT nearest-neighbour candidates from the ACTIVE version.

        This is deliberately an exact scan, not an approximate index lookup: there is
        no HNSW/IVFFlat index on ``knowledge_chunk.embedding``, so the plan is a
        sequential scan ordered by cosine distance. That is the intended P3-A
        behaviour -- the corpus is small and a bounded, reproducible ranking matters
        more than index speed (brief sections 16/48).

        ``cosine_distance`` compiles to pgvector's ``<=>`` operator; the profile
        predicate keeps candidates inside ONE embedding space, because comparing
        distances across two spaces would be arithmetic on incomparable numbers.
        """
        distance = KnowledgeChunkRow.embedding.cosine_distance(
            list(query_vector)
        ).label("distance")
        result = await self._session.execute(
            _chunk_view_select()
            .add_columns(distance)
            .where(
                _scope_predicate(tenant_id),
                KnowledgeDocumentRow.status == DocumentStatus.ACTIVE.value,
                KnowledgeDocumentRow.active_version_id
                == KnowledgeDocumentVersionRow.id,
                KnowledgeChunkRow.embedding_profile_id == embedding_profile_id,
            )
            .order_by(
                distance.asc(),
                KnowledgeDocumentRow.id,
                KnowledgeDocumentVersionRow.id,
                KnowledgeChunkRow.ordinal,
            )
            .limit(limit)
        )
        return tuple(
            VectorCandidate(
                view=_view_from_mapping(row), distance=float(row["distance"])
            )
            for row in result.mappings().all()
        )

    async def get_chunk_view(
        self, *, tenant_id: str, chunk_id: UUID
    ) -> KnowledgeChunkView | None:
        """Resolve ONE chunk for citation re-validation, scope filter ONLY.

        There is deliberately no status and no active-version predicate here. A
        citation may name a historical version of a document that has since been
        retired or superseded, and resolution must still prove the chunk exists and
        is readable by this tenant -- that is the whole point of citation
        resolution (brief section 51). Only the scope rule applies.
        """
        result = await self._session.execute(
            _chunk_view_select().where(
                KnowledgeChunkRow.id == chunk_id,
                _scope_predicate(tenant_id),
            )
        )
        row = result.mappings().first()
        return _view_from_mapping(row) if row is not None else None


class SqlAlchemyEmbeddingProfileRepository(EmbeddingProfileRepository):
    """Embedding-space persistence; the single-ACTIVE rule is a database fact."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active(self) -> EmbeddingProfileRecord | None:
        result = await self._session.execute(
            select(EmbeddingProfileRow).where(
                EmbeddingProfileRow.status == EmbeddingProfileStatus.ACTIVE.value
            )
        )
        row = result.scalar_one_or_none()
        return row_to_profile(row) if row is not None else None

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
        """Find a profile by its full identity, whatever its ACTIVE/RETIRED status."""
        result = await self._session.execute(
            select(EmbeddingProfileRow).where(
                EmbeddingProfileRow.provider == provider,
                EmbeddingProfileRow.model_id == model_id,
                EmbeddingProfileRow.dimension == dimension,
                EmbeddingProfileRow.distance_metric == distance_metric,
                EmbeddingProfileRow.normalization == normalization,
                EmbeddingProfileRow.profile_version == profile_version,
            )
        )
        row = result.scalar_one_or_none()
        return row_to_profile(row) if row is not None else None

    async def add(self, *, profile: EmbeddingProfileRecord) -> None:
        self._session.add(profile_to_row(profile))

    async def retire_active(self, *, retired_at: datetime) -> None:
        """Retire whatever profile is ACTIVE, in a single predicate-only UPDATE.

        The ``status = 'ACTIVE'`` predicate is the whole point: reading the ACTIVE
        row first and then updating it by id would race a concurrent activation and
        could retire a row that had already been superseded. One UPDATE over a
        status predicate keeps the single-ACTIVE partial index authoritative.
        """
        await self._session.execute(
            update(EmbeddingProfileRow)
            .where(EmbeddingProfileRow.status == EmbeddingProfileStatus.ACTIVE.value)
            .values(
                status=EmbeddingProfileStatus.RETIRED.value,
                retired_at=retired_at,
            )
        )


class SqlAlchemyAttackTechniqueRepository(AttackTechniqueRepository):
    """Canonical ATT&CK technique persistence, pinned per source release."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find(
        self, *, framework: str, technique_id: str, source_release: str
    ) -> AttackTechniqueRecord | None:
        result = await self._session.execute(
            select(AttackTechniqueRow).where(
                AttackTechniqueRow.framework == framework,
                AttackTechniqueRow.technique_id == technique_id,
                AttackTechniqueRow.source_release == source_release,
            )
        )
        row = result.scalar_one_or_none()
        return row_to_technique(row) if row is not None else None

    async def add_many(
        self, *, techniques: Sequence[AttackTechniqueRecord]
    ) -> None:
        """Bulk-insert techniques. An empty sequence is a no-op, not a query."""
        if not techniques:
            return
        await self._session.execute(
            insert(AttackTechniqueRow),
            [_technique_values(technique) for technique in techniques],
        )

    async def count_for_release(self, *, framework: str, source_release: str) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(AttackTechniqueRow)
            .where(
                AttackTechniqueRow.framework == framework,
                AttackTechniqueRow.source_release == source_release,
            )
        )
        return int(result.scalar_one())

    async def deactivate_except(self, *, framework: str, source_release: str) -> int:
        """Flip every other release's rows to inactive in ONE statement.

        ``synchronize_session=False`` because the caller has no loaded rows of
        those releases in this session: the update is a pure server-side state
        switch, and a stale in-memory copy is exactly what we must not consult.
        """
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                update(AttackTechniqueRow)
                .where(
                    AttackTechniqueRow.framework == framework,
                    AttackTechniqueRow.source_release != source_release,
                    AttackTechniqueRow.active.is_(True),
                )
                .values(active=False)
                .execution_options(synchronize_session=False)
            ),
        )
        return int(result.rowcount)
