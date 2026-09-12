"""add versioned security knowledge and hybrid retrieval

Revision ID: ed6af82d9b13
Revises: 979070495d4f
Create Date: 2026-09-12

P3-A introduces the knowledge subsystem's persistence:

``knowledge_document``
    One externally-identified document. Identity is
    ``(source_kind, external_key)`` inside a scope; ``source_kind``,
    ``external_key`` and ``visibility`` are immutable and the only lifecycle move
    is ACTIVE -> RETIRED. The scope invariant is a CHECK constraint rather than a
    convention, so "GLOBAL with a tenant" is unrepresentable. Two PARTIAL unique
    indexes (not one) enforce identity, because ``NULL`` never conflicts in a
    plain unique index and a single index over ``(source_kind, external_key)``
    would silently allow duplicate global documents.

``knowledge_document_version``
    Immutable content. A change APPENDS a version; nothing ever UPDATEs one.
    ``UNIQUE(document_id, version)`` and ``UNIQUE(document_id, content_hash)``
    make "re-ingesting identical bytes cannot create a second version" a database
    fact, which is what makes concurrent ingestion converge instead of racing.

``embedding_profile``
    Which vector space the chunks were indexed in. At most ONE row may be ACTIVE,
    enforced by a partial unique index on ``status`` -- not by application code,
    which could not survive two concurrent activations.

``knowledge_chunk``
    A rebuildable retrieval projection, deliberately NOT a domain aggregate. The
    ``embedding`` column is the UNTYPED ``vector`` type: no dimension is baked
    into the schema, so a second embedding model of a different size needs no
    migration, and the dimension contract is enforced by the active embedding
    profile. There is deliberately NO HNSW and NO IVFFlat index -- P3-A ranks
    exactly, and an ANN index would silently change which neighbours are found.
    ``lexical_document`` is a GENERATED column, so the full-text projection can
    never drift from the content it describes.

``attack_technique``
    Canonical technique rows for a pinned release. Re-importing the same release
    cannot duplicate a technique; a new release adds new rows and old releases are
    never deleted.

pgvector is an infrastructure prerequisite, and this migration does NOT assume
superuser rights: it verifies the extension is present, tries to create it if it
is not, and otherwise fails EXPLICITLY with an actionable message before any
table exists. A silent failure here would surface much later as an unexplained
missing column.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects import postgresql

revision: str = "ed6af82d9b13"
down_revision: Union[str, None] = "979070495d4f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# CHECK names were created by the metadata naming convention, which prefixes the
# short name with ``ck_<table>_``. ``op.f`` marks a name as already final so the
# convention is not applied a second time -- the same pattern as the migrations
# that introduced the earlier tables.
_CK_DOCUMENT_SOURCE_KIND = "ck_knowledge_document_knowledge_document_source_kind_valid"
_CK_DOCUMENT_VISIBILITY = "ck_knowledge_document_knowledge_document_visibility_valid"
_CK_DOCUMENT_STATUS = "ck_knowledge_document_knowledge_document_status_valid"
_CK_DOCUMENT_SCOPE = "ck_knowledge_document_knowledge_document_scope_coherent"
_CK_DOCUMENT_RETIREMENT = "ck_knowledge_document_knowledge_document_retirement_coherent"
_CK_DOCUMENT_REVISION = "ck_knowledge_document_knowledge_document_revision_valid"
_CK_DOCUMENT_LOCK_VERSION = "ck_knowledge_document_knowledge_document_lock_version_valid"

_CK_VERSION_NUMBER = "ck_knowledge_document_version_version_number_valid"
_CK_VERSION_HASH = "ck_knowledge_document_version_version_content_hash_valid"
_CK_VERSION_CONTENT = "ck_knowledge_document_version_version_content_non_empty"

_CK_PROFILE_DIMENSION = "ck_embedding_profile_embedding_profile_dimension_bounded"
_CK_PROFILE_METRIC = "ck_embedding_profile_embedding_profile_distance_metric_valid"
_CK_PROFILE_NORMALIZATION = "ck_embedding_profile_embedding_profile_normalization_valid"
_CK_PROFILE_STATUS = "ck_embedding_profile_embedding_profile_status_valid"
_CK_PROFILE_VERSION = "ck_embedding_profile_embedding_profile_version_valid"
_CK_PROFILE_RETIREMENT = "ck_embedding_profile_embedding_profile_retirement_coherent"

_CK_CHUNK_ORDINAL = "ck_knowledge_chunk_knowledge_chunk_ordinal_non_negative"
_CK_CHUNK_CONTENT = "ck_knowledge_chunk_knowledge_chunk_content_non_empty"
_CK_CHUNK_HASH = "ck_knowledge_chunk_knowledge_chunk_content_hash_valid"
_CK_CHUNK_TOKENS = "ck_knowledge_chunk_knowledge_chunk_token_count_valid"

_CK_TECHNIQUE_HASH = "ck_attack_technique_attack_technique_content_hash_valid"

_VECTOR_EXTENSION_MESSAGE = (
    "The PostgreSQL 'vector' extension (pgvector) is required by this migration "
    "and is not installed, and this role may not create it. Ask a database "
    "administrator to run "
    "`CREATE EXTENSION IF NOT EXISTS vector SCHEMA copilot;` in this database "
    "once, then re-run `alembic upgrade head`. Nothing was changed by this "
    "failed run."
)


def _vector_extension_schema() -> str | None:
    """Return the schema holding ``vector``, or None when it is not installed."""
    row = op.get_bind().execute(
        sa.text(
            "SELECT n.nspname FROM pg_extension e "
            "JOIN pg_namespace n ON n.oid = e.extnamespace "
            "WHERE e.extname = 'vector'"
        )
    ).first()
    return None if row is None else str(row[0])


def _require_vector_extension() -> None:
    """Ensure pgvector is installed AND reachable, failing loudly and early.

    Two distinct failures matter here, and lumping them together would produce an
    unactionable error:

    1. the extension is not installed at all -- a one-time database prerequisite;
    2. it IS installed, but in a schema that is not on this connection's
       ``search_path`` (the Copilot connection is pinned to ``copilot``, so an
       extension created in ``public`` is invisible). PostgreSQL would then fail
       with a bare ``type "vector" does not exist`` much later, on the first
       table that uses the column.

    Runs BEFORE any table is created: a half-applied migration would leave the
    schema in a state the operator has to reason about, and the fix is a one-time
    prerequisite either way.
    """
    bind = op.get_bind()
    installed_in = _vector_extension_schema()
    if installed_in is not None:
        visible = bind.execute(
            sa.text("SELECT to_regtype('vector') IS NOT NULL")
        ).scalar()
        if visible:
            return
        raise RuntimeError(
            f"The PostgreSQL 'vector' extension is installed in schema "
            f"'{installed_in}', which is not on this connection's search_path, so "
            f"the 'vector' type is unresolvable. Fix it once with "
            f"`ALTER EXTENSION vector SET SCHEMA copilot;` (or connect with a "
            f"search_path that includes '{installed_in}'), then re-run "
            f"`alembic upgrade head`. Nothing was changed by this failed run."
        )
    try:
        # Succeeds for a superuser / a role granted CREATE on the database. The
        # point is to avoid REQUIRING superuser, not to hide the requirement.
        # Installing into the connection's own schema keeps the type reachable
        # under the pinned ``search_path``.
        schema = bind.execute(sa.text("SELECT current_schema()")).scalar()
        bind.execute(sa.text(f'CREATE EXTENSION IF NOT EXISTS vector SCHEMA "{schema}"'))
    except sa.exc.DBAPIError as exc:
        raise RuntimeError(_VECTOR_EXTENSION_MESSAGE) from exc
    if _vector_extension_schema() is None:
        raise RuntimeError(_VECTOR_EXTENSION_MESSAGE)


def upgrade() -> None:
    _require_vector_extension()

    op.create_table(
        "knowledge_document",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("external_key", sa.String(length=512), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("active_version_id", sa.Uuid(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("retired_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "source_kind IN ('MITRE_ATTACK','CURATED_GUIDANCE','TENANT_RUNBOOK')",
            name=op.f(_CK_DOCUMENT_SOURCE_KIND),
        ),
        sa.CheckConstraint(
            "visibility IN ('GLOBAL','TENANT')", name=op.f(_CK_DOCUMENT_VISIBILITY)
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','RETIRED')", name=op.f(_CK_DOCUMENT_STATUS)
        ),
        sa.CheckConstraint(
            "(visibility = 'GLOBAL' AND tenant_id IS NULL) "
            "OR (visibility = 'TENANT' AND tenant_id IS NOT NULL)",
            name=op.f(_CK_DOCUMENT_SCOPE),
        ),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND retired_at IS NULL) "
            "OR (status = 'RETIRED' AND retired_at IS NOT NULL)",
            name=op.f(_CK_DOCUMENT_RETIREMENT),
        ),
        sa.CheckConstraint("revision >= 0", name=op.f(_CK_DOCUMENT_REVISION)),
        sa.CheckConstraint("lock_version >= 0", name=op.f(_CK_DOCUMENT_LOCK_VERSION)),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_document")),
    )
    op.create_index(
        "uq_knowledge_document_global_key",
        "knowledge_document",
        ["source_kind", "external_key"],
        unique=True,
        postgresql_where=sa.text("visibility = 'GLOBAL'"),
    )
    op.create_index(
        "uq_knowledge_document_tenant_key",
        "knowledge_document",
        ["tenant_id", "source_kind", "external_key"],
        unique=True,
        postgresql_where=sa.text("visibility = 'TENANT'"),
    )
    op.create_index(
        "ix_knowledge_document_tenant_status",
        "knowledge_document",
        ["tenant_id", "status"],
        unique=False,
    )

    op.create_table(
        "knowledge_document_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("normalized_content", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("source_version", sa.String(length=128), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("ingested_at", sa.DateTime(), nullable=False),
        sa.Column("effective_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("version >= 1", name=op.f(_CK_VERSION_NUMBER)),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name=op.f(_CK_VERSION_HASH)
        ),
        sa.CheckConstraint(
            "length(normalized_content) > 0", name=op.f(_CK_VERSION_CONTENT)
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_document.id"],
            name=op.f("fk_knowledge_document_version_document"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_document_version")),
    )
    op.create_index(
        "uq_knowledge_document_version_number",
        "knowledge_document_version",
        ["document_id", "version"],
        unique=True,
    )
    op.create_index(
        "uq_knowledge_document_version_content_hash",
        "knowledge_document_version",
        ["document_id", "content_hash"],
        unique=True,
    )

    op.create_table(
        "embedding_profile",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("distance_metric", sa.String(length=16), nullable=False),
        sa.Column("normalization", sa.String(length=16), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("retired_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "dimension >= 1 AND dimension <= 8192", name=op.f(_CK_PROFILE_DIMENSION)
        ),
        sa.CheckConstraint(
            "distance_metric IN ('COSINE')", name=op.f(_CK_PROFILE_METRIC)
        ),
        sa.CheckConstraint(
            "normalization IN ('NONE','L2')", name=op.f(_CK_PROFILE_NORMALIZATION)
        ),
        sa.CheckConstraint("status IN ('ACTIVE','RETIRED')", name=op.f(_CK_PROFILE_STATUS)),
        sa.CheckConstraint("profile_version >= 1", name=op.f(_CK_PROFILE_VERSION)),
        sa.CheckConstraint(
            "(status = 'ACTIVE' AND retired_at IS NULL) "
            "OR (status = 'RETIRED' AND retired_at IS NOT NULL)",
            name=op.f(_CK_PROFILE_RETIREMENT),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embedding_profile")),
    )
    op.create_index(
        "uq_embedding_profile_identity",
        "embedding_profile",
        [
            "provider",
            "model_id",
            "dimension",
            "distance_metric",
            "normalization",
            "profile_version",
        ],
        unique=True,
    )
    op.create_index(
        "uq_embedding_profile_single_active",
        "embedding_profile",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "knowledge_chunk",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("chunker_version", sa.String(length=64), nullable=False),
        sa.Column("embedding_profile_id", sa.Uuid(), nullable=False),
        sa.Column("embedding", VECTOR(), nullable=False),
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
        sa.CheckConstraint("ordinal >= 0", name=op.f(_CK_CHUNK_ORDINAL)),
        sa.CheckConstraint("length(content) > 0", name=op.f(_CK_CHUNK_CONTENT)),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name=op.f(_CK_CHUNK_HASH)
        ),
        sa.CheckConstraint("token_count >= 0", name=op.f(_CK_CHUNK_TOKENS)),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_document.id"],
            name=op.f("fk_knowledge_chunk_document"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["knowledge_document_version.id"],
            name=op.f("fk_knowledge_chunk_document_version"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["embedding_profile_id"],
            ["embedding_profile.id"],
            name=op.f("fk_knowledge_chunk_embedding_profile"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_chunk")),
    )
    op.create_index(
        "uq_knowledge_chunk_version_ordinal",
        "knowledge_chunk",
        ["document_version_id", "ordinal"],
        unique=True,
    )
    op.create_index("ix_knowledge_chunk_document_id", "knowledge_chunk", ["document_id"])
    op.create_index(
        "ix_knowledge_chunk_document_version_id",
        "knowledge_chunk",
        ["document_version_id"],
    )
    op.create_index(
        "ix_knowledge_chunk_embedding_profile_id",
        "knowledge_chunk",
        ["embedding_profile_id"],
    )
    op.create_index(
        "ix_knowledge_chunk_lexical",
        "knowledge_chunk",
        ["lexical_document"],
        unique=False,
        postgresql_using="gin",
    )

    op.create_table(
        "attack_technique",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("framework", sa.String(length=32), nullable=False),
        sa.Column("technique_id", sa.String(length=32), nullable=False),
        sa.Column("source_release", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("tactics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("platforms", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_stix_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name=op.f(_CK_TECHNIQUE_HASH)
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attack_technique")),
    )
    op.create_index(
        "uq_attack_technique_release",
        "attack_technique",
        ["framework", "technique_id", "source_release"],
        unique=True,
    )


def downgrade() -> None:
    """Remove the knowledge schema, newest dependency first.

    The ``vector`` extension is deliberately NOT dropped: it may predate P3-A and
    other schemas may depend on it, so removing it would be a destructive change
    far outside this migration's scope. Nothing from P1/P2/GP-01 is touched --
    this migration only ever owned the five tables above.
    """
    op.drop_index("uq_attack_technique_release", table_name="attack_technique")
    op.drop_table("attack_technique")

    op.drop_index("ix_knowledge_chunk_lexical", table_name="knowledge_chunk")
    op.drop_index(
        "ix_knowledge_chunk_embedding_profile_id", table_name="knowledge_chunk"
    )
    op.drop_index("ix_knowledge_chunk_document_version_id", table_name="knowledge_chunk")
    op.drop_index("ix_knowledge_chunk_document_id", table_name="knowledge_chunk")
    op.drop_index("uq_knowledge_chunk_version_ordinal", table_name="knowledge_chunk")
    op.drop_table("knowledge_chunk")

    op.drop_index("uq_embedding_profile_single_active", table_name="embedding_profile")
    op.drop_index("uq_embedding_profile_identity", table_name="embedding_profile")
    op.drop_table("embedding_profile")

    op.drop_index(
        "uq_knowledge_document_version_content_hash",
        table_name="knowledge_document_version",
    )
    op.drop_index(
        "uq_knowledge_document_version_number", table_name="knowledge_document_version"
    )
    op.drop_table("knowledge_document_version")

    op.drop_index("ix_knowledge_document_tenant_status", table_name="knowledge_document")
    op.drop_index("uq_knowledge_document_tenant_key", table_name="knowledge_document")
    op.drop_index("uq_knowledge_document_global_key", table_name="knowledge_document")
    op.drop_table("knowledge_document")
