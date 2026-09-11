"""response execution lifecycle fields

Revision ID: b7c2d41a90ef
Revises: 5a1e07c9b4f0
Create Date: 2026-09-11

Extends ``response_execution_ref`` into a first-class execution fact (P2): rename
``last_observed_status`` → ``status`` and add the bounded lifecycle fields the
Workspace/audit projection needs (started_at / finished_at / safe_result /
safe_error_code / safe_error_message). No new table — the existing execution-ref
table already models one execution per proposal; only these columns were missing.

The table carried no data before this slice (no repository/mapper existed), so the
rename is safe.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b7c2d41a90ef"
down_revision: Union[str, None] = "5a1e07c9b4f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "response_execution_ref", "last_observed_status", new_column_name="status"
    )
    op.add_column(
        "response_execution_ref", sa.Column("started_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "response_execution_ref", sa.Column("finished_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "response_execution_ref",
        sa.Column("safe_result", JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "response_execution_ref",
        sa.Column("safe_error_code", sa.Text(), nullable=True),
    )
    op.add_column(
        "response_execution_ref",
        sa.Column("safe_error_message", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "response_execution_status_valid",
        "response_execution_ref",
        "status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "response_execution_status_valid", "response_execution_ref", type_="check"
    )
    op.drop_column("response_execution_ref", "safe_error_message")
    op.drop_column("response_execution_ref", "safe_error_code")
    op.drop_column("response_execution_ref", "safe_result")
    op.drop_column("response_execution_ref", "finished_at")
    op.drop_column("response_execution_ref", "started_at")
    op.alter_column(
        "response_execution_ref", "status", new_column_name="last_observed_status"
    )
