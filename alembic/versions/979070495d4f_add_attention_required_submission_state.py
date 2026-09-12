"""add the attention-required submission state

Revision ID: 979070495d4f
Revises: 5b85af6841f2
Create Date: 2026-09-12

The automatic SUBMIT retry budget can run out while every failure was still
TRANSIENT/UNCERTAIN (timeout, transport error, 408/425/429, 5xx). Up to now that
left an incoherent pair: the outbox delivery reached DEAD_LETTER while
``response_submission.status`` still said ``RETRYING``, so the workspace kept
telling the analyst a retry was in flight and kept polling forever.

``ATTENTION_REQUIRED`` is the missing terminal LOCAL state. It deliberately does
not reuse ``FAILED_DEFINITIVE``: that one asserts the provider REFUSED the
submission, and after only transient failures we cannot assert any such thing —
we do not even know whether an execution exists. This state asserts exactly what
is true: automatic retrying has stopped and a human has to look.

The column ``attention_required_at`` records when that happened; the status CHECK
is widened to accept the new value. No historical migration is modified.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "979070495d4f"
down_revision: Union[str, None] = "5b85af6841f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The table was created with an explicit, convention-expanded name; DROP must use
# that exact name (``op.f`` marks a name as already final), while CREATE lets the
# metadata naming convention expand the short name — the same pattern as the
# migration that introduced the table.
_STATUS_CHECK = "response_submission_status_valid"
_STATUS_CHECK_FULL = "ck_response_submission_response_submission_status_valid"
_OLD_STATUSES = "('PENDING','RETRYING','SUBMITTED','FAILED_DEFINITIVE')"
_NEW_STATUSES = (
    "('PENDING','RETRYING','SUBMITTED','FAILED_DEFINITIVE','ATTENTION_REQUIRED')"
)


def upgrade() -> None:
    op.add_column(
        "response_submission",
        sa.Column("attention_required_at", sa.DateTime(), nullable=True),
    )
    op.drop_constraint(
        op.f(_STATUS_CHECK_FULL), "response_submission", type_="check"
    )
    op.create_check_constraint(
        _STATUS_CHECK, "response_submission", f"status IN {_NEW_STATUSES}"
    )


def downgrade() -> None:
    # Rows already in ATTENTION_REQUIRED cannot stay under the narrower CHECK and
    # have no faithful narrower meaning; they are reported as still-retrying so the
    # record says "not settled" rather than inventing a provider verdict.
    op.execute(
        sa.text(
            "UPDATE response_submission SET status = 'RETRYING', "
            "attention_required_at = NULL "
            "WHERE status = 'ATTENTION_REQUIRED'"
        )
    )
    op.drop_constraint(
        op.f(_STATUS_CHECK_FULL), "response_submission", type_="check"
    )
    op.create_check_constraint(
        _STATUS_CHECK, "response_submission", f"status IN {_OLD_STATUSES}"
    )
    op.drop_column("response_submission", "attention_required_at")
