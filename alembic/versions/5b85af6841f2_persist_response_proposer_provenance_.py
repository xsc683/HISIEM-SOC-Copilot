"""persist response proposer provenance and the local submission lifecycle

Revision ID: 5b85af6841f2
Revises: c8f1a93d5b27
Create Date: 2026-09-12

Two facts the response workflow was unable to answer from persisted state:

1. **Who proposed this response?** The proposer was already server-derived
   (``CreateResponseProposal.initiated_by_subject``) but never stored, so the
   audit trail could not name the proposer and the workspace had to render a
   proposal with no author. ``response_proposal.created_by_subject`` is now
   immutable provenance captured at creation.

   It is deliberately NOT derived from ``investigation.initiated_by``: that
   answers "who ran the investigation", a different question with a different
   answer as soon as anyone other than the investigator proposes a response.

   Rows that predate provenance capture are backfilled with the literal
   ``'unknown'`` — no identity is invented for them.

2. **Did the provider accept our submission?** A definitive provider rejection
   (400/404/409/422) left NO local trace: the proposal stayed ``APPROVED``, no
   provider execution ref was created (correctly — nothing was executed), and the
   workspace therefore kept claiming "approved / awaiting submission" forever.
   ``response_submission`` is the local lifecycle that distinguishes
   PENDING / RETRYING / SUBMITTED / FAILED_DEFINITIVE without ever fabricating a
   provider execution identity.

Both columns and the table are additive; no historical migration is modified.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "5b85af6841f2"
down_revision: Union[str, None] = "c8f1a93d5b27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Backfill for proposals created before provenance was captured. Deliberately a
#: self-describing sentinel rather than a plausible-looking subject id, so nobody
#: can mistake it for a real actor.
_LEGACY_PROPOSER = "unknown"


def upgrade() -> None:
    op.create_table(
        "response_submission",
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("submission_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("safe_error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("failed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING','RETRYING','SUBMITTED','FAILED_DEFINITIVE')",
            name=op.f("ck_response_submission_response_submission_status_valid"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_response_submission_response_submission_attempts_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["response_proposal.id"],
            name=op.f("fk_response_submission_proposal_id_response_proposal"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("proposal_id", name=op.f("pk_response_submission")),
        sa.UniqueConstraint(
            "submission_key", name="uq_response_submission_submission_key"
        ),
    )
    # Added nullable, backfilled, then tightened: an existing installation with
    # proposals must upgrade cleanly, and the invariant must hold afterwards.
    op.add_column(
        "response_proposal",
        sa.Column("created_by_subject", sa.Text(), nullable=True),
    )
    op.add_column(
        "response_proposal",
        sa.Column("created_by_display_name", sa.Text(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE response_proposal SET created_by_subject = :legacy "
            "WHERE created_by_subject IS NULL"
        ).bindparams(legacy=_LEGACY_PROPOSER)
    )
    op.alter_column(
        "response_proposal", "created_by_subject", existing_type=sa.Text(), nullable=False
    )


def downgrade() -> None:
    op.drop_column("response_proposal", "created_by_display_name")
    op.drop_column("response_proposal", "created_by_subject")
    op.drop_table("response_submission")
