"""command_receipt request fingerprint

Revision ID: d7681ebf99cd
Revises: 72390ed8cf87
Create Date: 2026-09-05

Adds ``command_receipt.request_fingerprint`` — a bounded fingerprint of the business
request (for StartAlertInvestigation, the source_alert_ref). A replayed
Idempotency-Key bound to a DIFFERENT request is then a deterministic idempotency
conflict instead of silently returning the original (wrong) logical result.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d7681ebf99cd"
down_revision: Union[str, None] = "72390ed8cf87"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "command_receipt", sa.Column("request_fingerprint", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("command_receipt", "request_fingerprint")
