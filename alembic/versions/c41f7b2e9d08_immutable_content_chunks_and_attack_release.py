"""immutable content chunks and ATT&CK release authority

Revision ID: c41f7b2e9d08
Revises: ed6af82d9b13
Create Date: 2026-09-13

The P3-A closure fixes two things the previous revision got wrong, and this is
the schema half of both.

``knowledge_content_chunk``
    The IMMUTABLE citation target. ``knowledge_chunk`` was the chunk's content
    AND its embedding at once, so a re-index deleted the very rows a citation
    pointed at and every historical ``kcit:`` handle died with them. Content now
    lives here, written once and never rewritten: it survives re-embedding, a
    retrieval-projection rebuild, a rechunk (a new ``generation`` for the same
    version, appended and never replaced), a restart, a retirement and a later
    document version. The full-text index stays on THIS table, because lexical
    retrieval reads content, not vectors, so the lexical channel outlives any
    vector-space change.

``knowledge_chunk_embedding``
    The REBUILDABLE projection. It holds no content, so re-embedding the corpus
    is a delete-and-reinsert of rows here and breaks nothing: a citation names a
    content chunk, never a row of this table.

``attack_release``
    ATT&CK authority at RELEASE granularity, with a per-framework partial unique
    index on the ACTIVE status. ``attack_technique.active`` could not express "at
    most one authoritative release per framework" at all -- it is
    technique-per-row, so no unique index over it can state the rule, and two
    releases could both claim to be current. ``content_fingerprint`` makes a
    pinned release immutable. It is NULL for a release this migration ADOPTS,
    because the rows it adopts were never fingerprinted; such a release is
    verified against what is stored and pinned on the next import of the same
    bytes, and a NULL fingerprint is never treated as a match.

The data backfill is id-preserving and additive: every existing
``knowledge_chunk`` row keeps its ``id``, so a historical ``kcit:`` handle that
names an old chunk still resolves to the immutable row now holding that content,
and every existing ``attack_technique`` row stays exactly where it was. The
legacy ``knowledge_chunk`` table is deliberately KEPT so ``downgrade`` can put
the pre-closure rows back byte for byte; P3-A never reads or writes it again.

The one thing this migration will not do is GUESS. Where the pre-existing
``active`` flags claim more than one release of a framework, those flags cannot
say which release is authoritative, and picking one would re-create the exact
defect the release table exists to prevent. Such a framework is left with NO
ACTIVE release, every one of its releases is registered INACTIVE, and the
upgrade reports ``ATTACK_RELEASE_AUTHORITY_AMBIGUOUS`` together with the
statement that resolves it. A framework whose legacy rows agree is unaffected:
deriving its single ACTIVE release only states what the data already said.
"""

from __future__ import annotations

from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Row

revision: str = "c41f7b2e9d08"
down_revision: Union[str, None] = "ed6af82d9b13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# CHECK names are produced by the metadata naming convention, which prefixes the
# short name with ``ck_<table>_``. ``op.f`` marks a name as already final so the
# convention is not applied a second time -- the same pattern as every earlier
# migration that created tables from this ORM metadata.
# Short, because PostgreSQL truncates an identifier at 63 bytes and the naming
# convention already prefixes ``ck_<table>_``: spelling the table name twice
# silently produced a DIFFERENT constraint than the metadata declares.
_CK_CONTENT_ORDINAL = "ck_knowledge_content_chunk_ordinal_valid"
_CK_CONTENT_GENERATION = "ck_knowledge_content_chunk_generation_valid"
_CK_CONTENT_NON_EMPTY = "ck_knowledge_content_chunk_content_non_empty"
_CK_CONTENT_HASH = "ck_knowledge_content_chunk_content_hash_valid"
_CK_CONTENT_TOKENS = "ck_knowledge_content_chunk_token_count_valid"
_CK_CONTENT_CHUNKER = "ck_knowledge_content_chunk_chunker_version_valid"

_CK_RELEASE_STATUS = "ck_attack_release_attack_release_status_valid"
_CK_RELEASE_FINGERPRINT = "ck_attack_release_attack_release_fingerprint_valid"
_CK_RELEASE_COUNT = "ck_attack_release_attack_release_technique_count_valid"

#: The generation a backfilled chunk is registered in. It is the same initial
#: generation that new ingestion writes (``CHUNK_GENERATION_INITIAL`` in
#: ``domain/knowledge/value_objects.py``), because a backfilled chunk IS the
#: first and only chunking of its version.
_BACKFILL_GENERATION = 1

#: Reported by the upgrade, and greppable in ``docs/p3/p3-a-operations.md`` and in
#: the ``knowledge doctor`` output, so an operator who sees it once can find the
#: remediation without re-running the migration.
_AMBIGUOUS_CODE = "ATTACK_RELEASE_AUTHORITY_AMBIGUOUS"

#: Per-release facts derived PURELY from the pre-existing technique rows, shared
#: by the insert and the ambiguity report so the two can never disagree about
#: which release claims authority. ``claims_authority`` is ``bool_or`` because the
#: legacy flag lived per technique row: a release claims authority when ANY of its
#: rows still says so, which is the conservative reading (a release with mixed
#: flags is treated as claiming, and therefore as ambiguous when it is not alone,
#: rather than being silently written off).
_PER_RELEASE_CTE = """
WITH per_release AS (
    SELECT framework,
           source_release,
           count(*) AS technique_count,
           min(created_at) AS created_at,
           bool_or(active) AS claims_authority
    FROM attack_technique
    GROUP BY framework, source_release
),
claiming AS (
    SELECT framework, count(*) AS claimants
    FROM per_release
    WHERE claims_authority
    GROUP BY framework
)
"""


def _require_vector_type() -> None:
    """Fail with an actionable message when ``vector`` is no longer resolvable.

    ``ed6af82d9b13`` owns the one-time EXTENSION install: it attempts the
    ``CREATE EXTENSION``, and explains exactly what to run when the role may not
    do it. This migration creates a second ``vector`` column, so all it needs is
    the knowledge that the type is reachable -- but it needs that CHECKED, not
    assumed. Without it, PostgreSQL's bare ``type "vector" does not exist`` would
    come out of a ``CREATE TABLE`` in this revision while the fix lives in the
    previous one.

    Runs before any DDL, so "nothing was changed" is true of this failed run
    whether or not the surrounding migration transaction is rolled back.
    """
    resolvable = op.get_bind().execute(
        sa.text("SELECT to_regtype('vector') IS NOT NULL")
    ).scalar()
    if resolvable:
        return
    raise RuntimeError(
        "The PostgreSQL 'vector' type is not resolvable on this connection, so "
        "the rebuildable embedding projection (knowledge_chunk_embedding) cannot "
        "be created. Install the extension once with "
        "`CREATE EXTENSION IF NOT EXISTS vector SCHEMA copilot;`, then re-run "
        "`alembic upgrade head`. Nothing was changed by this failed run."
    )


def _create_content_chunk_table() -> None:
    """The immutable citation target, plus the lexical index that outlives vectors."""
    op.create_table(
        "knowledge_content_chunk",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("chunker_version", sa.String(length=64), nullable=False),
        sa.Column(
            "lexical_document",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple', coalesce(heading_path, '') || ' ' || content)",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name=op.f(_CK_CONTENT_ORDINAL)),
        sa.CheckConstraint("generation >= 1", name=op.f(_CK_CONTENT_GENERATION)),
        sa.CheckConstraint("length(content) > 0", name=op.f(_CK_CONTENT_NON_EMPTY)),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name=op.f(_CK_CONTENT_HASH)
        ),
        sa.CheckConstraint("token_count >= 0", name=op.f(_CK_CONTENT_TOKENS)),
        sa.CheckConstraint(
            "length(chunker_version) > 0", name=op.f(_CK_CONTENT_CHUNKER)
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_document.id"],
            name=op.f("fk_knowledge_content_chunk_document"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["knowledge_document_version.id"],
            name=op.f("fk_knowledge_content_chunk_document_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_content_chunk")),
    )
    # Ordinal is TOTAL inside one generation, which is what makes the stable
    # semantic ranking key of section 5.1 a total order rather than a partial one.
    op.create_index(
        "uq_knowledge_content_chunk_generation_ordinal",
        "knowledge_content_chunk",
        ["document_version_id", "generation", "ordinal"],
        unique=True,
    )
    op.create_index(
        "ix_knowledge_content_chunk_document_id",
        "knowledge_content_chunk",
        ["document_id"],
    )
    op.create_index(
        "ix_knowledge_content_chunk_document_version_id",
        "knowledge_content_chunk",
        ["document_version_id"],
    )
    op.create_index(
        "ix_knowledge_content_chunk_lexical",
        "knowledge_content_chunk",
        ["lexical_document"],
        unique=False,
        postgresql_using="gin",
    )


def _create_embedding_table() -> None:
    """The rebuildable projection: no content, so it is safe to drop and rebuild."""
    op.create_table(
        "knowledge_chunk_embedding",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("content_chunk_id", sa.Uuid(), nullable=False),
        sa.Column("embedding_profile_id", sa.Uuid(), nullable=False),
        sa.Column("embedding", VECTOR(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["content_chunk_id"],
            ["knowledge_content_chunk.id"],
            name=op.f("fk_knowledge_chunk_embedding_content_chunk"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["embedding_profile_id"],
            ["embedding_profile.id"],
            name=op.f("fk_knowledge_chunk_embedding_embedding_profile"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_chunk_embedding")),
    )
    op.create_index(
        "uq_knowledge_chunk_embedding_content_profile",
        "knowledge_chunk_embedding",
        ["content_chunk_id", "embedding_profile_id"],
        unique=True,
    )
    op.create_index(
        "ix_knowledge_chunk_embedding_profile",
        "knowledge_chunk_embedding",
        ["embedding_profile_id"],
    )


def _backfill_chunks() -> None:
    """Copy every pre-existing chunk into the split pair, preserving its ``id``.

    The ``id`` is copied rather than regenerated, and that is the whole point of
    the backfill: a ``kcit:`` handle minted before this revision names a
    ``knowledge_chunk.id``, so keeping the id is what lets that handle resolve to
    the immutable row that now holds its content. A regenerated id would have
    turned every historical citation into a dangling reference at the moment it
    was most needed.

    ``generation`` is the initial one because a backfilled chunk is the first
    chunking of its version. The uniqueness of ``(document_version_id,
    generation, ordinal)`` therefore holds by construction: the legacy table
    already enforced ``(document_version_id, ordinal)``.

    The embedding rows get fresh surrogate ids. That is safe precisely because
    this table is the REBUILDABLE projection -- no citation, no semantic ranking
    key and no fingerprint ever names a row of it. The embedding, its profile and
    its timestamp are copied unchanged, so the vector channel keeps working
    across the upgrade without re-embedding anything.
    """
    bind = op.get_bind()
    bind.execute(
        sa.text(
            f"""
            INSERT INTO knowledge_content_chunk (
                id, document_id, document_version_id, generation, ordinal,
                heading_path, content, content_hash, token_count, language,
                chunker_version, created_at
            )
            SELECT id, document_id, document_version_id, {_BACKFILL_GENERATION},
                   ordinal, heading_path, content, content_hash, token_count,
                   language, chunker_version, created_at
            FROM knowledge_chunk
            """
        )
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO knowledge_chunk_embedding (
                id, content_chunk_id, embedding_profile_id, embedding, indexed_at
            )
            SELECT gen_random_uuid(), id, embedding_profile_id, embedding,
                   created_at
            FROM knowledge_chunk
            """
        )
    )


def _create_release_table() -> None:
    """ATT&CK authority at release granularity, with the single-ACTIVE rule in SQL."""
    op.create_table(
        "attack_release",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("framework", sa.String(length=32), nullable=False),
        sa.Column("source_release", sa.String(length=32), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("technique_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('ACTIVE','INACTIVE')", name=op.f(_CK_RELEASE_STATUS)
        ),
        sa.CheckConstraint(
            "content_fingerprint IS NULL OR content_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f(_CK_RELEASE_FINGERPRINT),
        ),
        sa.CheckConstraint("technique_count >= 0", name=op.f(_CK_RELEASE_COUNT)),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attack_release")),
    )
    op.create_index(
        "uq_attack_release_framework_source_release",
        "attack_release",
        ["framework", "source_release"],
        unique=True,
    )
    # THE rule of section 2.1 as a constraint: a second ACTIVE release for one
    # framework fails at COMMIT instead of silently producing two authorities.
    # SQL rather than application code, because application code cannot survive
    # two concurrent activations.
    op.create_index(
        "uq_attack_release_single_active",
        "attack_release",
        ["framework"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )


def _adopt_releases() -> None:
    """Register one release row per (framework, source_release) already stored.

    ``content_fingerprint`` is NULL for every adopted release, and deliberately
    so: the pre-existing rows were written before releases were fingerprinted, so
    no fingerprint was ever computed for them. Recomputing one here would put a
    SECOND definition of the release fingerprint in a migration, where it could
    never be kept in step with the canonical function. The handler instead
    re-derives the fingerprint from what is STORED (through
    ``AttackTechniqueRepository.list_for_release``) and pins it on the next import
    of the same release, which is what makes the pin a verified fact rather than
    an assumption. A NULL fingerprint never counts as a match.

    Authority is derived, never invented: a framework with exactly one claiming
    release gets that release ACTIVE, which only restates what the legacy flags
    said. A framework whose rows claim two or more releases has no derivable
    answer, so every one of its releases is INACTIVE and the operator is told.
    """
    bind = op.get_bind()
    bind.execute(
        sa.text(
            _PER_RELEASE_CTE
            + """
            INSERT INTO attack_release (
                id, framework, source_release, content_fingerprint, status,
                technique_count, created_at, activated_at
            )
            SELECT gen_random_uuid(),
                   r.framework,
                   r.source_release,
                   NULL,
                   CASE WHEN r.claims_authority AND coalesce(c.claimants, 0) = 1
                        THEN 'ACTIVE' ELSE 'INACTIVE' END,
                   r.technique_count,
                   r.created_at,
                   CASE WHEN r.claims_authority AND coalesce(c.claimants, 0) = 1
                        THEN r.created_at ELSE NULL END
            FROM per_release r
            LEFT JOIN claiming c ON c.framework = r.framework
            """
        )
    )

    ambiguous_rows = bind.execute(
        sa.text(
            _PER_RELEASE_CTE
            + """
            SELECT r.framework,
                   string_agg(r.source_release, ', ' ORDER BY r.source_release)
            FROM per_release r
            JOIN claiming c ON c.framework = r.framework
            WHERE r.claims_authority AND c.claimants > 1
            GROUP BY r.framework
            ORDER BY r.framework
            """
        )
    ).all()
    if ambiguous_rows:
        _report_ambiguous(ambiguous_rows)

    # ``active`` on the technique snapshot MIRRORS the owning release's authority
    # and never decides it. Where the legacy flags agreed this is a no-op by
    # construction; where they disagreed the mirror follows the release, because
    # a stale ``active = true`` on a non-authoritative release is exactly the
    # state the release table was introduced to make unrepresentable.
    bind.execute(
        sa.text(
            """
            UPDATE attack_technique AS t
            SET active = (r.status = 'ACTIVE')
            FROM attack_release AS r
            WHERE r.framework = t.framework
              AND r.source_release = t.source_release
            """
        )
    )

    # Last, so the FK cannot be violated by the adoption above: a technique row
    # whose release is unregistered becomes unrepresentable from here on, which
    # is what stops a technique from ever being the place where "which release is
    # current" gets answered.
    op.create_foreign_key(
        op.f("fk_attack_technique_attack_release_release"),
        "attack_technique",
        "attack_release",
        ["framework", "source_release"],
        ["framework", "source_release"],
        ondelete="RESTRICT",
    )




def _report_ambiguous(ambiguous: Sequence[Row[Any]]) -> None:
    """Say which frameworks could not be resolved, and how to resolve them.

    Printed rather than raised. A database that cannot be upgraded at all is a
    worse outcome than one whose ATT&CK authority is explicitly unset: the
    operator cannot fix a pre-closure flag once the pre-closure schema is gone,
    and refusing to CHOOSE is the part that matters. Either release is one
    statement away once a human says which one is intended.

    The remediation names the re-import rather than an activation command,
    because there is no separate "activate" verb and there should not be one: the
    import path is what re-derives the stored rows through the canonical
    fingerprint function and pins them, so it is the only path that can make a
    release authoritative without inventing a fingerprint.
    """
    lines = [
        "",
        f"{_AMBIGUOUS_CODE}: the pre-existing technique rows claim more than one "
        "ATT&CK release for the framework(s) below, so no single authoritative "
        "release could be derived.",
        "  Every release of those frameworks was registered INACTIVE. No technique "
        "row and no knowledge content was lost.",
        "  Re-import the INTENDED release with --activate, which re-derives its "
        "fingerprint from the stored rows, pins it, and makes it the framework's "
        "only authoritative release:",
    ]
    for framework, releases in ambiguous:
        lines.append(f"    framework {framework}: {releases}")
        for release in str(releases).split(", "):
            lines.append(
                "      python -m hisiem_soc_copilot.knowledge.cli import-attack "
                f"--file <THE {release} STIX BUNDLE> --release {release} --activate"
            )
    lines.append(
        "  Re-run `knowledge doctor` afterwards: it reports the same code until "
        "exactly one release per framework is ACTIVE. See "
        "docs/p3/p3-a-operations.md."
    )
    lines.append("")
    print("\n".join(lines))


def upgrade() -> None:
    _require_vector_type()
    _create_content_chunk_table()
    _create_embedding_table()
    _backfill_chunks()
    _create_release_table()
    _adopt_releases()


def downgrade() -> None:
    """Undo this revision, and only this revision.

    ``knowledge_chunk`` is left standing with every pre-closure row intact: it was
    created by ``ed6af82d9b13`` and is that revision's to remove, so
    ``downgrade -1`` followed by ``upgrade head`` converges instead of losing the
    rows it would have to backfill from.

    One value cannot be restored: a legacy ``attack_technique.active`` that
    contradicted its own release. The upgrade refused to treat such a framework's
    rows as authority, so there is no authority to put back -- and re-deriving it
    from a flag the closure exists to retire would restore the ambiguity rather
    than the information.
    """
    op.drop_constraint(
        op.f("fk_attack_technique_attack_release_release"),
        "attack_technique",
        type_="foreignkey",
    )

    op.drop_index("uq_attack_release_single_active", table_name="attack_release")
    op.drop_index(
        "uq_attack_release_framework_source_release", table_name="attack_release"
    )
    op.drop_table("attack_release")

    op.drop_index(
        "ix_knowledge_chunk_embedding_profile", table_name="knowledge_chunk_embedding"
    )
    op.drop_index(
        "uq_knowledge_chunk_embedding_content_profile",
        table_name="knowledge_chunk_embedding",
    )
    op.drop_table("knowledge_chunk_embedding")

    op.drop_index(
        "ix_knowledge_content_chunk_lexical", table_name="knowledge_content_chunk"
    )
    op.drop_index(
        "ix_knowledge_content_chunk_document_version_id",
        table_name="knowledge_content_chunk",
    )
    op.drop_index(
        "ix_knowledge_content_chunk_document_id", table_name="knowledge_content_chunk"
    )
    op.drop_index(
        "uq_knowledge_content_chunk_generation_ordinal",
        table_name="knowledge_content_chunk",
    )
    op.drop_table("knowledge_content_chunk")
