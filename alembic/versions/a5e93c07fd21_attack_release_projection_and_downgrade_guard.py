"""ATT&CK release projection binding, and a fail-closed downgrade guard

Revision ID: a5e93c07fd21
Revises: c41f7b2e9d08
Create Date: 2026-09-13

This revision does two things a review of the previous closure found missing.

``attack_release_projection``
    The binding between a release's canonical technique and the immutable
    knowledge document version that release staged. It exists because release
    identity and knowledge content identity are genuinely different facts: two
    releases may carry byte-identical technique content and therefore share one
    ``knowledge_document_version``, and ``source_version`` records only which
    release happened to CREATE that row. Without an explicit binding there is no
    fact that says "v15.1's projection of T1110 IS this version", so re-activating
    v14.1 could not restore its own projection and a staged release could move
    what retrieval serves.

``downgrade`` refuses to run when the database holds state this revision's
predecessor cannot represent
    ``c41f7b2e9d08`` dropped ``knowledge_content_chunk``, ``knowledge_chunk_embedding``
    and ``attack_release`` unconditionally on the way down, and stopped updating the
    legacy ``knowledge_chunk`` table on the way up. Once the application has written
    anything through the new tables, ``downgrade -1`` therefore destroyed it
    silently. A migration may not do that: it must be lossless or it must refuse.
    The guard below runs BEFORE the first ``op.drop_*``, so "nothing was changed by
    this failed run" is true of the run itself rather than a property of the
    caller's transaction configuration -- the same discipline ``c41f7b2e9d08``
    already applies to its pgvector precondition.

    It is placed here rather than inside ``c41f7b2e9d08`` because that revision is
    frozen and must not be edited. The consequence, recorded plainly: this guard
    runs first for any downgrade that STARTS at this head (``downgrade -1`` and
    ``downgrade <older-revision>`` both execute this function before the
    predecessor's), so it intercepts the operator's real command. A database left
    sitting at ``c41f7b2e9d08`` from before this revision existed is not covered by
    it; the operations doc says so and tells the operator to ``upgrade head``
    first.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a5e93c07fd21"
down_revision: Union[str, None] = "c41f7b2e9d08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


class P3ADowngradeUnsafeError(RuntimeError):
    """Raised when a downgrade would destroy state the old schema cannot hold.

    Carries the ``P3A_DOWNGRADE_UNSAFE`` code so an operator (and the test suite)
    can recognise it without matching prose.
    """

    code = "P3A_DOWNGRADE_UNSAFE"


#: Each entry is ``(category, count_sql, why)``. The queries are deliberately
#: narrow: a false positive would make the SAFE round trip impossible, which is a
#: worse bug than the one being fixed, so every predicate here is chosen to be
#: zero on a database that was upgraded and then not written to.
#:
#: In particular, "``attack_release`` is not empty" is WRONG as a guard: the
#: upgrade ADOPTS one row per ``(framework, source_release)`` found in
#: ``attack_technique``, so a clean upgrade always leaves rows behind. Adopted
#: rows are the only ones that ever carry a NULL fingerprint -- every import path
#: pins one -- so the fingerprint is what distinguishes "the migration adopted
#: what was already there" from "an operator imported a release".
_LOSS_CHECKS: tuple[tuple[str, str, str], ...] = (
    (
        "NEW_CONTENT_CHUNKS",
        """
        SELECT count(*) FROM knowledge_content_chunk c
         WHERE NOT EXISTS (SELECT 1 FROM knowledge_chunk k WHERE k.id = c.id)
        """,
        "content chunk(s) with no pre-closure row; the old schema has nowhere to "
        "put them",
    ),
    (
        "MULTIPLE_CHUNK_GENERATIONS",
        """
        SELECT count(*) FROM knowledge_content_chunk WHERE generation <> 1
        """,
        "content chunk(s) in a generation the old schema has no column for, so "
        "the stale generation-1 rows would be re-presented as current",
    ),
    (
        "PROJECTION_CHANGED",
        """
        SELECT count(*) FROM knowledge_chunk_embedding e
         LEFT JOIN knowledge_chunk k ON k.id = e.content_chunk_id
         WHERE k.id IS NULL
            OR k.embedding_profile_id <> e.embedding_profile_id
            OR k.embedding::text <> e.embedding::text
        """,
        "retrieval projection row(s) the old single-vector row cannot represent; "
        "restoring the stale vector as current would be wrong rather than merely "
        "lossy",
    ),
    (
        "MUTATED_CONTENT",
        """
        SELECT count(*) FROM knowledge_content_chunk c
          JOIN knowledge_chunk k ON k.id = c.id
         WHERE k.content IS DISTINCT FROM c.content
            OR k.content_hash IS DISTINCT FROM c.content_hash
            OR k.ordinal IS DISTINCT FROM c.ordinal
            OR k.document_version_id IS DISTINCT FROM c.document_version_id
        """,
        "pre-closure chunk(s) whose stored content no longer matches; the old "
        "schema would serve the stale bytes as current",
    ),
    (
        "PINNED_ATTACK_RELEASE",
        """
        SELECT count(*) FROM attack_release WHERE content_fingerprint IS NOT NULL
        """,
        "pinned ATT&CK release(s); the release fingerprint is the immutability "
        "claim and the old schema has no column for it",
    ),
    (
        "ATTACK_PROJECTION_BINDING",
        """
        SELECT count(*) FROM attack_release_projection
        """,
        "release-to-knowledge projection binding(s); the old schema cannot express "
        "which version a release staged",
    ),
)


def _refuse_unsafe_downgrade() -> None:
    """Fail closed unless ``ed6af82d9b13`` can hold everything this database has.

    Runs BEFORE the first ``op.drop_*`` so a refusal is observationally "nothing
    happened", independent of whether the caller wraps migrations in a
    transaction (``alembic downgrade --sql`` does not). The message names the
    categories and their counts -- never any content.
    """
    bind = op.get_bind()
    found: list[str] = []
    for category, sql, why in _LOSS_CHECKS:
        count = int(bind.execute(sa.text(sql)).scalar_one())
        if count:
            found.append(f"  {category}: {count} -- {why}")
    if not found:
        return
    raise P3ADowngradeUnsafeError(
        "P3A_DOWNGRADE_UNSAFE: this database holds state that revision "
        "ed6af82d9b13 cannot represent, so downgrading would destroy it. "
        "Refusing before any schema change; this failed run changed nothing.\n"
        + "\n".join(found)
        + "\nTo move below this revision, take a physical backup first, or "
        "deliberately remove the state listed above."
    )


def upgrade() -> None:
    """Add the release -> knowledge projection binding.

    No backfill: a database being upgraded has no staged projections, because
    before this revision an import projected documents WITHOUT recording which
    release staged them. Inferring bindings from content hashes would be exactly
    the ambiguous derivation the table exists to replace, so the correct answer
    for an existing database is "no bindings yet" -- and the next import of a
    release writes them (idempotently) before it can activate anything.
    """
    op.create_table(
        "attack_release_projection",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("framework", sa.String(length=32), nullable=False),
        sa.Column("source_release", sa.String(length=32), nullable=False),
        sa.Column("technique_id", sa.String(length=32), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="content_hash_valid",
        ),
        sa.ForeignKeyConstraint(
            ["framework", "source_release"],
            ["attack_release.framework", "attack_release.source_release"],
            name="fk_attack_release_projection_release",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["framework", "technique_id", "source_release"],
            [
                "attack_technique.framework",
                "attack_technique.technique_id",
                "attack_technique.source_release",
            ],
            name="fk_attack_release_projection_technique",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["knowledge_document.id"],
            name="fk_attack_release_projection_document",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["knowledge_document_version.id"],
            name="fk_attack_release_projection_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # The natural key, and the conflict target that makes a retried stage
    # converge instead of duplicating (brief section 2.8).
    op.create_index(
        "uq_attack_release_projection_release_technique",
        "attack_release_projection",
        ["framework", "source_release", "technique_id"],
        unique=True,
    )
    # A release may not claim two different projections of one document.
    op.create_index(
        "uq_attack_release_projection_release_document",
        "attack_release_projection",
        ["framework", "source_release", "document_id"],
        unique=True,
    )
    op.create_index(
        "ix_attack_release_projection_document",
        "attack_release_projection",
        ["document_id"],
    )


def downgrade() -> None:
    """Drop the binding table -- but only when nothing else would be lost.

    The guard is deliberately broader than this revision's own table: the step
    that actually destroys data is ``c41f7b2e9d08``'s, and it is reached by the
    same operator command, so refusing here is what stops the operator before
    that step rather than after it.
    """
    _refuse_unsafe_downgrade()

    op.drop_index(
        "ix_attack_release_projection_document",
        table_name="attack_release_projection",
    )
    op.drop_index(
        "uq_attack_release_projection_release_document",
        table_name="attack_release_projection",
    )
    op.drop_index(
        "uq_attack_release_projection_release_technique",
        table_name="attack_release_projection",
    )
    op.drop_table("attack_release_projection")
