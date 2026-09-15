"""Persist optional W3C context for durable outbox delivery.

Revision ID: b6c2a4d19f30
Revises: a5e93c07fd21
Create Date: 2026-09-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6c2a4d19f30"
down_revision: Union[str, None] = "a5e93c07fd21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "outbox_message",
        sa.Column(
            "traceparent",
            sa.Text(),
            nullable=True,
            comment="Optional diagnostic W3C context; not business correlation.",
        ),
    )


def downgrade() -> None:
    op.drop_column("outbox_message", "traceparent")
