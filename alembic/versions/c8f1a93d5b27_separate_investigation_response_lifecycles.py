"""separate the investigation and response lifecycles

Revision ID: c8f1a93d5b27
Revises: b7c2d41a90ef
Create Date: 2026-09-11

The Investigation state machine used to declare ``RUNNING → WAITING_APPROVAL →
EXECUTING_RESPONSE → COMPLETED``, but the real P2 workflow never enters those
statuses: the investigation graph finalizes its result and completes the
investigation, and the response workflow (proposal → approval → SOAR execution)
then runs in an INDEPENDENT post-completion aggregate lifecycle. The two response
statuses were therefore dead/unreachable, and their presence in the CHECK
constraint and in the partial-unique active-alert index contradicted the model the
code actually implements (spec §5).

This migration narrows the persisted model to the investigation-ANALYSIS lifecycle
only: ``CREATED → RUNNING → COMPLETED | FAILED | CANCELLED``.

Historical-row strategy (explicit, not silent):

* The only two statuses being removed both meant "the analysis had already
  finalized and the response workflow took over", so they normalize to
  ``COMPLETED``. Rows are only touched when they actually carry a removed value.
* ``finished_at`` is backfilled ONLY where it is NULL, so a recorded finish time is
  never overwritten.
* ``termination_reason`` values ``COMPLETED_AFTER_APPROVAL`` /
  ``COMPLETED_AFTER_REJECTION`` no longer exist in the domain enum; a row still
  carrying one would fail to load. Rather than assert a completion reason that may
  be false, they are set to NULL ("not recorded").
* The partial-unique index KEEPS ITS NAME (``uq_investigation_active_alert``):
  the unit of work translates an IntegrityError on that exact name into a
  deterministic 409, so renaming it would silently degrade concurrent-start
  conflicts into HTTP 500s. Only its predicate narrows — which must be done in the
  same transaction as the normalization, otherwise two rows could collide on the
  index before being normalized.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8f1a93d5b27"
down_revision: Union[str, None] = "b7c2d41a90ef"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUS_CHECK = "investigation_status_valid"
_STATUS_CHECK_FULL = "ck_investigation_investigation_status_valid"
_NEW_STATUSES = "('CREATED','RUNNING','COMPLETED','FAILED','CANCELLED')"
_OLD_STATUSES = (
    "('CREATED','RUNNING','WAITING_APPROVAL','EXECUTING_RESPONSE',"
    "'COMPLETED','FAILED','CANCELLED')"
)
_ACTIVE_PREDICATE = "status IN ('CREATED','RUNNING')"


def upgrade() -> None:
    # 1. Drop the legacy CHECK first: it forbade nothing we need to write, but
    #    recreating it at the end is what pins the narrowed status set.
    op.drop_constraint(
        op.f(_STATUS_CHECK_FULL), "investigation", type_="check"
    )
    # 2. Drop the partial unique index whose predicate encoded the legacy active set.
    op.drop_index("uq_investigation_active_alert", table_name="investigation")

    # 3. Normalize only rows that actually carry a removed value.
    op.execute(
        sa.text(
            "UPDATE investigation "
            "SET status = 'COMPLETED', "
            "    finished_at = COALESCE(finished_at, now()) "
            "WHERE status IN ('WAITING_APPROVAL','EXECUTING_RESPONSE')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE investigation SET termination_reason = NULL "
            "WHERE termination_reason IN "
            "('COMPLETED_AFTER_APPROVAL','COMPLETED_AFTER_REJECTION')"
        )
    )

    # 4. Re-create the CHECK with the investigation-analysis-only status set.
    op.create_check_constraint(
        _STATUS_CHECK, "investigation", f"status IN {_NEW_STATUSES}"
    )
    # 5. Re-create the active-alert index with the narrowed predicate (same NAME).
    op.create_index(
        "uq_investigation_active_alert",
        "investigation",
        ["tenant_id", "source_provider", "source_resource_type", "source_address_id"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_PREDICATE),
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f(_STATUS_CHECK_FULL), "investigation", type_="check"
    )
    op.drop_index("uq_investigation_active_alert", table_name="investigation")
    # Rows that were normalized to COMPLETED are NOT moved back: the removed
    # statuses have no reversible meaning under the separated lifecycle (which
    # status a completed investigation "was waiting on" is not recoverable).
    op.create_check_constraint(
        _STATUS_CHECK, "investigation", f"status IN {_OLD_STATUSES}"
    )
    op.create_index(
        "uq_investigation_active_alert",
        "investigation",
        ["tenant_id", "source_provider", "source_resource_type", "source_address_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('CREATED','RUNNING','WAITING_APPROVAL','EXECUTING_RESPONSE')"
        ),
    )
