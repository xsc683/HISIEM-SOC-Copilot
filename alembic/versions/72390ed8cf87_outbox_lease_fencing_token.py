"""outbox lease fencing token

Revision ID: 72390ed8cf87
Revises: 4c7a11f2d901
Create Date: 2026-09-05

Adds ``outbox_message.lease_token`` — the fencing token a claim writes and every
settlement / renewal must present. A worker whose lease was reclaimed holds a stale
token, so its late ``mark_published`` / ``mark_failed`` / ``mark_dead_letter`` /
``renew_lease`` matches 0 rows and is rejected (true lease ownership, not just a
PROCESSING status check).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "72390ed8cf87"
down_revision: Union[str, None] = "4c7a11f2d901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("outbox_message", sa.Column("lease_token", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("outbox_message", "lease_token")
