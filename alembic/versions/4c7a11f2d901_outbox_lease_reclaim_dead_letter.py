"""outbox lease reclaim + dead-letter terminal state

Revision ID: 4c7a11f2d901
Revises: d4bf8eed9e09
Create Date: 2026-09-05

Widens the ``outbox_message.status`` CHECK to admit ``DEAD_LETTER`` (a terminal
permanent-failure state that is never re-claimed) and adds an index that lets the
dispatcher reclaim ``PROCESSING`` rows whose lease (``locked_at``) has expired.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "4c7a11f2d901"
down_revision: Union[str, None] = "d4bf8eed9e09"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The ``copilot`` schema NAMING_CONVENTION renders this ORM CheckConstraint's name
# as ``ck_%(table_name)s_%(constraint_name)s``. The initial migration created it
# with that FINAL name (op.f). Alembic's standalone op.* functions re-apply the
# convention to bare strings, so here we execute explicit DDL against the final
# name to avoid a doubled ``ck_outbox_message_ck_outbox_message_...`` prefix.
_STATUS_CHECK = (
    "status IN ('PENDING','PROCESSING','PUBLISHED','FAILED','DEAD_LETTER')"
)
_CONSTRAINT = "ck_outbox_message_outbox_message_status_valid"


def upgrade() -> None:
    op.execute(
        text(f"ALTER TABLE outbox_message DROP CONSTRAINT {_CONSTRAINT}")
    )
    op.execute(
        text(
            f"ALTER TABLE outbox_message ADD CONSTRAINT {_CONSTRAINT} "
            f"CHECK ({_STATUS_CHECK})"
        )
    )
    op.create_index(
        "ix_outbox_lease_reclaim",
        "outbox_message",
        ["status", "locked_at"],
        unique=False,
        postgresql_where="status = 'PROCESSING'",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outbox_lease_reclaim",
        table_name="outbox_message",
        postgresql_where="status = 'PROCESSING'",
    )
    op.execute(
        text(f"ALTER TABLE outbox_message DROP CONSTRAINT {_CONSTRAINT}")
    )
    op.execute(
        text(
            f"ALTER TABLE outbox_message ADD CONSTRAINT {_CONSTRAINT} "
            "CHECK (status IN ('PENDING','PROCESSING','PUBLISHED','FAILED'))"
        )
    )
