"""command_receipt scoped idempotency identity

Revision ID: 5a1e07c9b4f0
Revises: d7681ebf99cd
Create Date: 2026-09-05

The receipt's idempotency identity was previously ``idempotency_key PRIMARY KEY`` —
a GLOBAL key across the whole database. But the application's logical scope for an
Idempotency-Key is a Tenant + Command Type (two tenants may each use the same key,
and one tenant may use the same key for different command types, all independent
idempotency spaces).

This migration:
- adds a surrogate ``id UUID`` technical primary key;
- keeps ``command_id`` UNIQUE (one receipt per command);
- replaces the global-key primary key with a scoped unique constraint
  ``(tenant_id, command_type, idempotency_key)``.

Existing rows are backfilled with a generated UUID id, so the migration is data
compatible.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "5a1e07c9b4f0"
down_revision: Union[str, None] = "d7681ebf99cd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Names of the constraints as they currently exist (created by the initial schema
# migration, which used op.f()).
_OLD_PK = "pk_command_receipt"
_NEW_SCOPED_UQ = "uq_command_receipt_tenant_command_key"


def upgrade() -> None:
    bind = op.get_bind()
    # 1) Backfill a surrogate id for every existing row (data compatible).
    op.add_column(
        "command_receipt",
        sa.Column("id", sa.Uuid(), nullable=True),
    )
    op.execute(
        "UPDATE command_receipt SET id = gen_random_uuid() WHERE id IS NULL"
    )
    op.alter_column("command_receipt", "id", nullable=False)

    # 2) Drop the global-key primary key (was on idempotency_key).
    op.drop_constraint(_OLD_PK, "command_receipt", type_="primary")

    # 3) Make ``id`` the new primary key.
    op.create_primary_key("pk_command_receipt", "command_receipt", ["id"])

    # 4) Enforce the SCOPED idempotency identity: tenant + command_type + key.
    op.create_unique_constraint(
        _NEW_SCOPED_UQ,
        "command_receipt",
        ["tenant_id", "command_type", "idempotency_key"],
    )
    # command_id keeps its UNIQUE constraint (already present as
    # uq_command_receipt_command_id) — untouched by this migration.


def downgrade() -> None:
    # Reverse: drop the scoped unique, restore the global idempotency_key PK.
    op.drop_constraint(_NEW_SCOPED_UQ, "command_receipt", type_="unique")
    op.drop_constraint("pk_command_receipt", "command_receipt", type_="primary")
    # Restore the surrogate id column drop after demoting.
    op.drop_column("command_receipt", "id")
    op.create_primary_key(
        "pk_command_receipt", "command_receipt", ["idempotency_key"]
    )
