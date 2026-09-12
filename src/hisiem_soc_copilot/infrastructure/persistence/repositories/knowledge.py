"""SQLAlchemy knowledge repositories implementing the application ports.

Four rules from the frozen knowledge contract drive every method here (brief
sections 3/12/13/44/45):

1. **Scope is a SQL WHERE clause, never a Python post-filter.** Visibility decides
   which tenant may read a row at all; loading the corpus and filtering in Python
   would both leak rows the caller may not see and not scale. Every read takes
   ``tenant_id`` as a required keyword argument, so there is no scope-less path.
2. **The database is the final arbiter.** The scope CHECK constraints, the two
   PARTIAL unique indexes on the document key, the single-ACTIVE embedding-profile
   index, and the per-framework single-ACTIVE ATT&CK release index are what
   actually enforce the invariants; the repository relies on them rather than
   re-implementing them in application code.
3. **Candidate generation reads the ACTIVE version's CURRENT chunk generation
   only.** Lexical and vector retrieval join to the document and require
   ``active_version_id = knowledge_document_version.id`` and
   ``generation = MAX(generation)`` for that version, so a superseded version's
   chunks -- and an older chunking of the current version -- can never surface.
   Citation RESOLUTION is the deliberate exception: it is scope-filtered with no
   version and no generation predicate, so a citation into a still-existing
   historical chunk stays resolvable (brief sections 3.2/3.3).
4. **Ordering is semantic, never by surrogate key.** Ties are broken by
   :func:`stable_ranking_key`'s SQL twin, :func:`_stable_order_columns`, so two
   independently ingested databases rank the same corpus identically (section 5.1).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    CursorResult,
    RowMapping,
    ScalarSelect,
    Select,
    Text,
    and_,
    delete,
    distinct,
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
from sqlalchemy.orm import InstrumentedAttribute, aliased

from ....application.ports.knowledge import (
    ATTACK_RELEASE_STATUS_ACTIVE,
    ATTACK_RELEASE_STATUS_INACTIVE,
    AttackReleaseRecord,
    AttackReleaseRepository,
    AttackTechniqueRecord,
    AttackTechniqueRepository,
    ChunkEmbeddingRecord,
    ChunkProjectionState,
    CorpusChunkFact,
    CorpusSnapshot,
    EmbeddingProfileRecord,
    EmbeddingProfileRepository,
    KnowledgeChunkRepository,
    KnowledgeChunkView,
    KnowledgeContentChunkRecord,
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
    content_chunk_to_row,
    document_to_row,
    embedding_to_row,
    profile_to_row,
    release_to_row,
    row_to_content_chunk,
    row_to_document,
    row_to_profile,
    row_to_release,
    row_to_technique,
    row_to_version,
    technique_to_row,
    version_to_row,
)
from ..orm.knowledge import (
    AttackReleaseRow,
    AttackTechniqueRow,
    EmbeddingProfileRow,
    KnowledgeChunkEmbeddingRow,
    KnowledgeContentChunkRow,
    KnowledgeDocumentRow,
    KnowledgeDocumentVersionRow,
)

#: The text-search configuration used by the content chunk's GENERATED
#: ``lexical_document`` column. It MUST stay identical here: ``ts_rank`` against a
#: vector built with a different configuration silently returns a meaningless score
#: instead of failing.
_TS_CONFIG = "simple"

#: What the deterministic tie-break hands to ``order_by``: a collated expression
#: or a mapped column. SQLAlchemy's own annotation for order-by arguments is a
#: union of typing roles with no single public name, so the two shapes this helper
#: actually returns are spelled out instead of importing private symbols.
_OrderByClause = ColumnElement[Any] | InstrumentedAttribute[Any]

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
        str,
        int,
        int,
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


def _stable_order_columns() -> tuple[_OrderByClause, ...]:
    """The SQL twin of :func:`stable_ranking_key` -- the deterministic tie-break.

    Both the lexical and the vector candidate queries order by their own score
    first and then by EXACTLY these columns, so a score tie resolves the same way
    in SQL and in the Python RRF fusion, and two fresh databases ranking the same
    corpus produce the same order (brief sections 5.1/5.6).

    ``COLLATE "C"`` is not decoration: the database's default collation is
    locale-dependent and would order ``external_key`` differently on two machines,
    while ``C`` (byte order) is identical to Python's ``str`` comparison for UTF-8
    text -- which is what makes the SQL and the Python key provably the same order.
    ``coalesce(tenant_id, '')`` folds the NULL of a GLOBAL row; the fold only ever
    applies within one ``visibility`` value, where the tenant is uniform anyway.
    """
    return (
        KnowledgeDocumentRow.source_kind.collate("C"),
        KnowledgeDocumentRow.visibility.collate("C"),
        func.coalesce(KnowledgeDocumentRow.tenant_id, "").collate("C"),
        KnowledgeDocumentRow.external_key.collate("C"),
        KnowledgeDocumentVersionRow.version,
        KnowledgeContentChunkRow.generation,
        KnowledgeContentChunkRow.ordinal,
    )


def _generation_probe() -> ScalarSelect[int]:
    """``MAX(generation)`` for the version in scope, as a correlated subquery.

    The probe is an ALIAS of the chunk table on purpose. SQLAlchemy correlates a
    scalar subquery by removing any FROM entity that also appears in the enclosing
    query, so an unaliased probe would be silently correlated away and
    ``MAX(generation)`` would be evaluated over the row itself -- always true, and
    therefore useless. The alias makes the probe a distinct FROM entity that
    survives, while ``KnowledgeDocumentVersionRow.id`` still correlates outward.
    """
    probe = aliased(KnowledgeContentChunkRow, name="generation_probe")
    return (
        select(func.max(probe.generation))
        .where(probe.document_version_id == KnowledgeDocumentVersionRow.id)
        .scalar_subquery()
    )


def _generation_of_version(document_version_id: UUID) -> ScalarSelect[int]:
    """``MAX(generation)`` for ONE version id, independent of any outer query."""
    probe = aliased(KnowledgeContentChunkRow, name="generation_probe")
    return (
        select(func.max(probe.generation))
        .where(probe.document_version_id == document_version_id)
        .scalar_subquery()
    )


def _covering_profile_probe(document_version_id: UUID) -> ScalarSelect[str | None]:
    """The embedding profile that FULLY covers this version's current generation.

    "Fully covers" is the only definition that can drive a correct rebuild
    decision. A bare ``MIN(embedding_profile_id)`` over the projection would answer
    with a profile that happens to be present even when it covers only part of the
    generation, and the caller would then read a half-indexed corpus as indexed --
    leaving the chunks covered by some other profile unretrievable.

    So the probe groups the projections BY PROFILE and keeps only the groups whose
    distinct content-chunk count equals the generation's chunk count. ``MIN`` over
    what survives is a covering profile when one exists and ``NULL`` when none
    does. Both aliases are deliberate: an unaliased table appearing in the
    enclosing query would be correlated away (see
    :func:`_generation_probe`), and the probe is embedded in a query that reads
    both of these tables.
    """
    embedding = aliased(KnowledgeChunkEmbeddingRow, name="covering_embedding")
    chunk = aliased(KnowledgeContentChunkRow, name="covering_chunk")
    generation = _generation_of_version(document_version_id)
    generation_size = (
        select(func.count())
        .select_from(KnowledgeContentChunkRow)
        .where(
            KnowledgeContentChunkRow.document_version_id == document_version_id,
            KnowledgeContentChunkRow.generation == generation,
        )
        .scalar_subquery()
    )
    covering = (
        select(embedding.embedding_profile_id.label("profile_id"))
        .select_from(embedding)
        .join(chunk, chunk.id == embedding.content_chunk_id)
        .where(
            chunk.document_version_id == document_version_id,
            chunk.generation == generation,
        )
        .group_by(embedding.embedding_profile_id)
        .having(func.count(distinct(chunk.id)) == generation_size)
        .subquery()
    )
    return (
        select(func.min(sa_cast(covering.c.profile_id, Text)))
        .select_from(covering)
        .scalar_subquery()
    )


def _chunk_view_select(*, join_embeddings: bool = False) -> _ChunkViewSelect:
    """The shared content-chunk + version + document projection.

    Whole rows are deliberately not loaded: retrieval needs a handful of facts
    from three tables, and naming them keeps the SQL honest about what it reads.
    The three provenance columns at the end (``external_key``,
    ``document_version_number``, ``chunk_generation``) exist so the deterministic
    tie-break can be expressed in business terms rather than by surrogate UUID
    (brief sections 5.1/5.2); they confer no authority.

    ``join_embeddings`` adds the rebuildable projection's row, which only the
    VECTOR channel needs. It is a parameter rather than a second query builder so
    the column projection stays defined in exactly one place.
    """
    statement = (
        select(
            KnowledgeContentChunkRow.id.label("chunk_id"),
            KnowledgeDocumentRow.id.label("document_id"),
            KnowledgeDocumentVersionRow.id.label("document_version_id"),
            KnowledgeContentChunkRow.ordinal.label("ordinal"),
            KnowledgeContentChunkRow.heading_path.label("heading_path"),
            KnowledgeContentChunkRow.content.label("content"),
            KnowledgeContentChunkRow.content_hash.label("content_hash"),
            KnowledgeDocumentVersionRow.title.label("title"),
            KnowledgeDocumentRow.source_kind.label("source_kind"),
            KnowledgeDocumentVersionRow.language.label("language"),
            KnowledgeDocumentVersionRow.source_version.label("source_version"),
            KnowledgeDocumentRow.status.label("document_status"),
            KnowledgeDocumentRow.visibility.label("visibility"),
            KnowledgeDocumentRow.tenant_id.label("tenant_id"),
            KnowledgeDocumentRow.external_key.label("external_key"),
            KnowledgeDocumentVersionRow.version.label("document_version_number"),
            KnowledgeContentChunkRow.generation.label("chunk_generation"),
        )
        .join(
            KnowledgeDocumentVersionRow,
            KnowledgeDocumentVersionRow.id
            == KnowledgeContentChunkRow.document_version_id,
        )
        .join(
            KnowledgeDocumentRow,
            KnowledgeDocumentRow.id == KnowledgeDocumentVersionRow.document_id,
        )
    )
    if join_embeddings:
        statement = statement.join(
            KnowledgeChunkEmbeddingRow,
            KnowledgeChunkEmbeddingRow.content_chunk_id
            == KnowledgeContentChunkRow.id,
        )
    return statement


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
        external_key=row["external_key"],
        document_version_number=row["document_version_number"],
        chunk_generation=row["chunk_generation"],
    )


def _content_chunk_values(chunk: KnowledgeContentChunkRecord) -> dict[str, object]:
    """One bulk-insert parameter set for an immutable content chunk.

    The field mapping itself lives in the mapper (one definition, so bulk and
    single-row writes can never diverge). Only the GENERATED ``lexical_document``
    column is dropped: the database derives it, and an INSERT may not name it.
    """
    row = content_chunk_to_row(chunk)
    return {
        column.key: getattr(row, column.key)
        for column in KnowledgeContentChunkRow.__table__.columns
        if column.computed is None
    }


def _embedding_values(embedding: ChunkEmbeddingRecord) -> dict[str, object]:
    """One bulk-insert parameter set for a rebuildable embedding projection."""
    row = embedding_to_row(embedding)
    return {
        column.key: getattr(row, column.key)
        for column in KnowledgeChunkEmbeddingRow.__table__.columns
    }


def _technique_values(technique: AttackTechniqueRecord) -> dict[str, object]:
    """One bulk-insert parameter set for an ATT&CK technique row (mapper-driven)."""
    row = technique_to_row(technique)
    return {
        column.key: getattr(row, column.key)
        for column in AttackTechniqueRow.__table__.columns
    }
def _or_tsquery(search_terms: Sequence[str]) -> ColumnElement[bool]:
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
    """Immutable content chunks plus the REBUILDABLE embedding projection.

    The split is the whole of brief section 3: content identity is written once
    and never rewritten, while the retrieval projection beside it can be dropped
    and recreated at will. Every citation names a CONTENT chunk, so a
    re-embedding, a vector rebuild or a rechunk breaks no citation.

    There is deliberately no ``delete_for_version``: P3-A offers no path that
    destroys immutable historical chunk content, because that is exactly what
    would make a historical citation unresolvable (brief section 3.2).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_content_chunks(
        self, *, chunks: Sequence[KnowledgeContentChunkRecord]
    ) -> None:
        """Bulk-insert immutable content chunks. Empty input is a no-op, not a query."""
        if not chunks:
            return
        await self._session.execute(
            insert(KnowledgeContentChunkRow),
            [_content_chunk_values(chunk) for chunk in chunks],
        )

    async def add_embeddings(
        self, *, embeddings: Sequence[ChunkEmbeddingRecord]
    ) -> None:
        """Bulk-insert embedding projections. Empty input is a no-op, not a query."""
        if not embeddings:
            return
        await self._session.execute(
            insert(KnowledgeChunkEmbeddingRow),
            [_embedding_values(embedding) for embedding in embeddings],
        )

    async def count_for_version(self, *, document_version_id: UUID) -> int:
        """How many chunks the CURRENT generation of this version holds.

        Current generation, not "all generations": this is the retrieval
        projection's size, and an operator asking how big a version is means the
        chunking retrieval is actually serving.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(KnowledgeContentChunkRow)
            .where(
                KnowledgeContentChunkRow.document_version_id == document_version_id,
                KnowledgeContentChunkRow.generation
                == _generation_of_version(document_version_id),
            )
        )
        return int(result.scalar_one())

    async def list_content_chunks(
        self, *, document_version_id: UUID, generation: int
    ) -> tuple[KnowledgeContentChunkRecord, ...]:
        """Read ONE generation's immutable chunks, in ordinal order.

        The read side of a re-embedding: the caller reuses these very rows -- ids,
        ordinals and content untouched -- and writes only new embedding
        projections beside them. Rechunking here would be wrong even though it is
        possible, because it would move every citation target (brief section 3.1).

        No scope predicate: the caller already resolved the version through a
        scope-filtered ``get_version``, and an unordered generation is not a
        reachable handle on its own.
        """
        result = await self._session.execute(
            select(KnowledgeContentChunkRow)
            .where(
                KnowledgeContentChunkRow.document_version_id == document_version_id,
                KnowledgeContentChunkRow.generation == generation,
            )
            .order_by(KnowledgeContentChunkRow.ordinal.asc())
        )
        return tuple(row_to_content_chunk(row) for row in result.scalars().all())

    async def projection_state(
        self, *, document_version_id: UUID
    ) -> ChunkProjectionState:
        """What this version's retrieval projection currently is, in one round trip.

        Reported over the CURRENT generation only: a chunker change appends a new
        generation rather than replacing the old one, so the older generation's
        chunks and their embeddings still exist and must not be mistaken for the
        retrieval projection now being served.

        ``COUNT(DISTINCT chunk.id)`` rather than ``COUNT(chunk.id)`` because the
        LEFT JOIN multiplies a chunk by its embeddings -- a chunk embedded under two
        profiles must still count once, or "every chunk is embedded" would be
        reported from a corpus that is only half indexed in the ACTIVE space.
        ``MIN`` over the profile id (folded to text, because PostgreSQL has no
        ``min`` for ``uuid``) is the conservative answer when more than one profile
        is present: it cannot equal the ACTIVE profile id in that case, so the
        caller rebuilds rather than assuming a clean index.
        """
        result = await self._session.execute(
            select(
                func.count(distinct(KnowledgeContentChunkRow.id)).label("chunk_count"),
                func.min(KnowledgeContentChunkRow.chunker_version).label(
                    "chunker_version"
                ),
                func.max(KnowledgeContentChunkRow.generation).label("generation"),
                func.count(KnowledgeChunkEmbeddingRow.id).label("embedding_count"),
                _covering_profile_probe(document_version_id).label(
                    "embedding_profile_id"
                ),
            )
            .select_from(KnowledgeContentChunkRow)
            .outerjoin(
                KnowledgeChunkEmbeddingRow,
                KnowledgeChunkEmbeddingRow.content_chunk_id
                == KnowledgeContentChunkRow.id,
            )
            .where(
                KnowledgeContentChunkRow.document_version_id == document_version_id,
                KnowledgeContentChunkRow.generation
                == _generation_of_version(document_version_id),
            )
        )
        row = result.mappings().one()
        chunk_count = int(row["chunk_count"])
        if chunk_count == 0:
            return ChunkProjectionState(None, None, 0)
        profile_id = row["embedding_profile_id"]
        return ChunkProjectionState(
            embedding_profile_id=(
                UUID(str(profile_id)) if profile_id is not None else None
            ),
            chunker_version=row["chunker_version"],
            chunk_count=chunk_count,
            generation=int(row["generation"]),
            embedding_count=int(row["embedding_count"]),
        )

    async def delete_embeddings_for_version_generation(
        self, *, document_version_id: UUID, generation: int
    ) -> int:
        """Delete ONE generation's EMBEDDING rows; return how many were removed.

        Only the projection is deleted. The generation's content chunks -- the
        citation targets -- are untouched, which is what makes re-embedding a
        non-event for every historical citation.
        """
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                delete(KnowledgeChunkEmbeddingRow).where(
                    KnowledgeChunkEmbeddingRow.content_chunk_id.in_(
                        select(KnowledgeContentChunkRow.id).where(
                            KnowledgeContentChunkRow.document_version_id
                            == document_version_id,
                            KnowledgeContentChunkRow.generation == generation,
                        )
                    )
                )
            ),
        )
        return int(result.rowcount)

    async def delete_embeddings_for_profile(
        self, *, document_version_id: UUID, embedding_profile_id: UUID
    ) -> int:
        """Delete this version's embeddings in ONE embedding space.

        Scoped to a single profile on purpose: dropping the whole vector
        projection of a version to re-index one space would throw away vectors
        that are still valid in another, and the rebuild would then have to start
        from the embedding provider again.
        """
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                delete(KnowledgeChunkEmbeddingRow).where(
                    KnowledgeChunkEmbeddingRow.embedding_profile_id
                    == embedding_profile_id,
                    KnowledgeChunkEmbeddingRow.content_chunk_id.in_(
                        select(KnowledgeContentChunkRow.id).where(
                            KnowledgeContentChunkRow.document_version_id
                            == document_version_id
                        )
                    ),
                )
            ),
        )
        return int(result.rowcount)

    async def lexical_candidates(
        self, *, tenant_id: str, search_terms: Sequence[str], limit: int
    ) -> tuple[LexicalCandidate, ...]:
        """Full-text candidates from the ACTIVE version's CURRENT chunking.

        The query is an OR of one ``plainto_tsquery`` per term (see
        :func:`_or_tsquery`), so a multi-term query matches a chunk containing SOME
        of the terms instead of demanding all of them, while every term still
        travels as a bound parameter and the configuration stays ``simple`` -- the
        same one baked into the generated column (brief sections 43/52).

        The ORDER BY is ``rank`` first and then the semantic stable key, which is
        what makes the ranking reproducible across two independently ingested
        databases (brief sections 5.1/5.6).
        """
        if not search_terms:
            return ()
        query_expr = _or_tsquery(search_terms)
        rank = func.ts_rank(KnowledgeContentChunkRow.lexical_document, query_expr).label(
            "rank"
        )
        result = await self._session.execute(
            _chunk_view_select()
            .add_columns(rank)
            .where(
                _scope_predicate(tenant_id),
                KnowledgeDocumentRow.status == DocumentStatus.ACTIVE.value,
                KnowledgeDocumentRow.active_version_id
                == KnowledgeDocumentVersionRow.id,
                KnowledgeContentChunkRow.generation == _generation_probe(),
                KnowledgeContentChunkRow.lexical_document.bool_op("@@")(query_expr),
            )
            .order_by(rank.desc(), *_stable_order_columns())
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
        """EXACT nearest-neighbour candidates from the ACTIVE version's chunking.

        This is deliberately an exact scan, not an approximate index lookup: there
        is no HNSW/IVFFlat index on ``knowledge_chunk_embedding.embedding``, so the
        plan is a sequential scan ordered by cosine distance. That is the intended
        P3-A behaviour -- the corpus is small and a bounded, reproducible ranking
        matters more than index speed (brief sections 16/48).

        ``cosine_distance`` compiles to pgvector's ``<=>`` operator; the profile
        predicate keeps candidates inside ONE embedding space, because comparing
        distances across two spaces would be arithmetic on incomparable numbers.
        The join is to the REBUILDABLE projection, which is exactly why this
        result can be regenerated without touching what a citation points at.
        """
        distance = KnowledgeChunkEmbeddingRow.embedding.cosine_distance(
            list(query_vector)
        ).label("distance")
        result = await self._session.execute(
            _chunk_view_select(join_embeddings=True)
            .add_columns(distance)
            .where(
                _scope_predicate(tenant_id),
                KnowledgeDocumentRow.status == DocumentStatus.ACTIVE.value,
                KnowledgeDocumentRow.active_version_id
                == KnowledgeDocumentVersionRow.id,
                KnowledgeContentChunkRow.generation == _generation_probe(),
                KnowledgeChunkEmbeddingRow.embedding_profile_id
                == embedding_profile_id,
            )
            .order_by(distance.asc(), *_stable_order_columns())
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
        """Resolve ONE content chunk for citation re-validation, scope filter ONLY.

        There is deliberately no status, no active-version and no chunk-generation
        predicate here. A citation may name a historical version of a document that
        has since been retired or superseded, or an older chunking of it, and
        resolution must still prove the chunk exists and is readable by this tenant
        -- that is the whole point of citation resolution (brief sections 3.2/51).

        The resolver re-derives the content hash from the returned content rather
        than trusting what is stored, so returning the stored hash alongside the
        stored content is exactly what it needs. Only the scope rule applies.
        """
        result = await self._session.execute(
            _chunk_view_select().where(
                KnowledgeContentChunkRow.id == chunk_id,
                _scope_predicate(tenant_id),
            )
        )
        row = result.mappings().first()
        return _view_from_mapping(row) if row is not None else None

    async def corpus_snapshot(self, *, tenant_id: str) -> CorpusSnapshot:
        """The tenant-visible ACTIVE eligible corpus, as reproducible facts.

        "Eligible" is the RETRIEVAL definition and nothing new: an ACTIVE document,
        visible to this tenant, whose active version has content chunks in its
        current generation. Building it in SQL (rather than loading documents and
        filtering in Python) is what keeps the sealed-evaluation precondition from
        reading rows the tenant may not see (brief sections 5.3/44).

        The selection carries no UUID in the fact it returns: a fingerprint over
        database-generated ids could not be reproduced by a second, independently
        ingested database, which is the entire point of the fingerprint.
        """
        result = await self._session.execute(
            select(
                KnowledgeDocumentRow.id.label("document_id"),
                KnowledgeDocumentVersionRow.id.label("document_version_id"),
                KnowledgeDocumentRow.visibility.label("visibility"),
                KnowledgeDocumentRow.tenant_id.label("tenant_id"),
                KnowledgeDocumentRow.source_kind.label("source_kind"),
                KnowledgeDocumentRow.external_key.label("external_key"),
                KnowledgeDocumentVersionRow.version.label("document_version_number"),
                KnowledgeDocumentVersionRow.content_hash.label(
                    "document_content_hash"
                ),
                KnowledgeContentChunkRow.generation.label("chunk_generation"),
                KnowledgeContentChunkRow.ordinal.label("ordinal"),
                KnowledgeContentChunkRow.content_hash.label("chunk_content_hash"),
            )
            .select_from(KnowledgeContentChunkRow)
            .join(
                KnowledgeDocumentVersionRow,
                KnowledgeDocumentVersionRow.id
                == KnowledgeContentChunkRow.document_version_id,
            )
            .join(
                KnowledgeDocumentRow,
                KnowledgeDocumentRow.id == KnowledgeDocumentVersionRow.document_id,
            )
            .where(
                _scope_predicate(tenant_id),
                KnowledgeDocumentRow.status == DocumentStatus.ACTIVE.value,
                KnowledgeDocumentRow.active_version_id
                == KnowledgeDocumentVersionRow.id,
                KnowledgeContentChunkRow.generation == _generation_probe(),
            )
            .order_by(*_stable_order_columns())
        )
        rows = result.mappings().all()
        return CorpusSnapshot(
            chunks=tuple(
                CorpusChunkFact(
                    visibility=Visibility(row["visibility"]),
                    tenant_id=row["tenant_id"],
                    source_kind=SourceKind(row["source_kind"]),
                    external_key=row["external_key"],
                    document_version_number=row["document_version_number"],
                    document_content_hash=row["document_content_hash"],
                    chunk_generation=row["chunk_generation"],
                    ordinal=row["ordinal"],
                    chunk_content_hash=row["chunk_content_hash"],
                )
                for row in rows
            ),
            document_count=len({row["document_id"] for row in rows}),
            version_count=len({row["document_version_id"] for row in rows}),
        )
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




class SqlAlchemyAttackReleaseRepository(AttackReleaseRepository):
    """ATT&CK release authority: at most one ACTIVE release per framework.

    Every mutation here is written so the per-framework PARTIAL unique index on
    ``status = 'ACTIVE'`` stays the arbiter: a new release is inserted INACTIVE and
    activated by a separate UPDATE, so no single statement can ever present two
    ACTIVE rows to the constraint (brief section 2.1).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find(
        self, *, framework: str, source_release: str
    ) -> AttackReleaseRecord | None:
        result = await self._session.execute(
            select(AttackReleaseRow).where(
                AttackReleaseRow.framework == framework,
                AttackReleaseRow.source_release == source_release,
            )
        )
        row = result.scalar_one_or_none()
        return row_to_release(row) if row is not None else None

    async def get_active(self, *, framework: str) -> AttackReleaseRecord | None:
        result = await self._session.execute(
            select(AttackReleaseRow).where(
                AttackReleaseRow.framework == framework,
                AttackReleaseRow.status == ATTACK_RELEASE_STATUS_ACTIVE,
            )
        )
        row = result.scalar_one_or_none()
        return row_to_release(row) if row is not None else None

    async def list_for_framework(
        self, *, framework: str
    ) -> tuple[AttackReleaseRecord, ...]:
        """Every release of the framework, in a deterministic release-name order."""
        result = await self._session.execute(
            select(AttackReleaseRow)
            .where(AttackReleaseRow.framework == framework)
            .order_by(AttackReleaseRow.source_release.collate("C").asc())
        )
        return tuple(row_to_release(row) for row in result.scalars().all())

    async def list_active(self) -> tuple[AttackReleaseRecord, ...]:
        """Every ACTIVE release, across every framework.

        Exposed so the migration and the doctor can DETECT an ambiguous state in
        data written before the constraint existed, instead of assuming it away
        (brief section 9.2). Ordered by framework then release so the report reads
        the same way twice.
        """
        result = await self._session.execute(
            select(AttackReleaseRow)
            .where(AttackReleaseRow.status == ATTACK_RELEASE_STATUS_ACTIVE)
            .order_by(
                AttackReleaseRow.framework.collate("C").asc(),
                AttackReleaseRow.source_release.collate("C").asc(),
            )
        )
        return tuple(row_to_release(row) for row in result.scalars().all())

    async def add(self, *, release: AttackReleaseRecord) -> None:
        self._session.add(release_to_row(release))

    async def pin_fingerprint(
        self, *, framework: str, source_release: str, content_fingerprint: str
    ) -> None:
        """Pin the fingerprint of an ADOPTED release, once.

        The ``content_fingerprint IS NULL`` predicate is the guard: pinning is only
        ever the adoption of a pre-fingerprint release, and it must never be able
        to overwrite an already-pinned fingerprint. If the row was pinned by a
        concurrent import between the handler's read and this write, the UPDATE
        matches nothing -- and that is correct, because the value it would have
        written was derived from the same stored rows.
        """
        await self._session.execute(
            update(AttackReleaseRow)
            .where(
                AttackReleaseRow.framework == framework,
                AttackReleaseRow.source_release == source_release,
                AttackReleaseRow.content_fingerprint.is_(None),
            )
            .values(content_fingerprint=content_fingerprint)
            .execution_options(synchronize_session=False)
        )

    async def activate(
        self, *, framework: str, source_release: str, now: datetime
    ) -> None:
        """Make this release the framework's ONLY ACTIVE one.

        Two statements in one transaction, deactivation FIRST: the partial unique
        index is not deferrable, so activating before deactivating would make the
        constraint fire on a transient state that is never observable outside the
        transaction. ``activated_at`` describes the CURRENT authority rather than a
        log of past ones, so a deactivated release loses its timestamp and a
        re-activated one is stamped afresh -- which is what keeps "when did this
        become authoritative" from reporting a stale moment.
        """
        await self._session.execute(
            update(AttackReleaseRow)
            .where(
                AttackReleaseRow.framework == framework,
                AttackReleaseRow.source_release != source_release,
                AttackReleaseRow.status == ATTACK_RELEASE_STATUS_ACTIVE,
            )
            .values(status=ATTACK_RELEASE_STATUS_INACTIVE, activated_at=None)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(
            update(AttackReleaseRow)
            .where(
                AttackReleaseRow.framework == framework,
                AttackReleaseRow.source_release == source_release,
            )
            .values(status=ATTACK_RELEASE_STATUS_ACTIVE, activated_at=now)
            .execution_options(synchronize_session=False)
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

    async def add_many(self, *, techniques: Sequence[AttackTechniqueRecord]) -> None:
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

    async def list_for_release(
        self, *, framework: str, source_release: str
    ) -> tuple[AttackTechniqueRecord, ...]:
        """Read a release's canonical rows in a TOTAL deterministic order.

        Ordered by ``(technique_id, source_stix_id)`` with ``COLLATE "C"`` -- the
        same order the release fingerprint canonicalizes with. The order is what
        makes "re-derive the fingerprint from what is stored" a reproducible
        computation rather than a function of whatever order the plan returned, and
        the pair is unique per release because ``(framework, technique_id,
        source_release)`` is.
        """
        result = await self._session.execute(
            select(AttackTechniqueRow)
            .where(
                AttackTechniqueRow.framework == framework,
                AttackTechniqueRow.source_release == source_release,
            )
            .order_by(
                AttackTechniqueRow.technique_id.collate("C"),
                AttackTechniqueRow.source_stix_id.collate("C"),
            )
        )
        return tuple(row_to_technique(row) for row in result.scalars().all())

    async def set_active_for_release(
        self, *, framework: str, source_release: str, active: bool
    ) -> int:
        """Set the ``active`` flag on every row of ONE release; return rows changed.

        ``synchronize_session=False`` because the caller has no loaded rows of the
        release in this session: the update is a pure server-side mirror of the
        release's authority, and a stale in-memory copy is exactly what must not be
        consulted. Rows are never deleted, so an older release stays readable by its
        own ``source_release`` (brief section 2.3).
        """
        result = cast(
            "CursorResult[object]",
            await self._session.execute(
                update(AttackTechniqueRow)
                .where(
                    AttackTechniqueRow.framework == framework,
                    AttackTechniqueRow.source_release == source_release,
                )
                .values(active=active)
                .execution_options(synchronize_session=False)
            ),
        )
        return int(result.rowcount)
