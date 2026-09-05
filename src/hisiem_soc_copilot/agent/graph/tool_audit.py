"""Tool-invocation audit helpers used by the graph nodes.

A read-tool call is audited as two short, INDEPENDENT transactions around the
actual execution (persistence-schema.md §28 + §31):

    BEGIN  add_started(RUNNING)  COMMIT      (before the tool runs)
    [ tool executes — no DB transaction open ]
    BEGIN  finish(SUCCEEDED/FAILED)  COMMIT  (after the tool returns)

The tool itself therefore never runs inside a database transaction. Rows are
addressed by ``(investigation_id, idempotency_key)`` — deterministic for a given
candidate — so a crashed/replayed node reuses the same audit row (the UNIQUE
constraint makes ``add_started`` a no-op on conflict) instead of duplicating it.

Only bounded arguments + result metadata (status/counts/error) are stored; full
tool results never reach the audit table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from ...application.ports.durable import ToolInvocationRecord
from ...application.ports.unit_of_work import UnitOfWork


async def record_started(
    uow: UnitOfWork,
    *,
    tenant_id: str,
    investigation_id: UUID,
    invocation_id: UUID,
    tool_name: str,
    idempotency_key: str,
    arguments: dict[str, Any],
    provider_request_id: str | None = None,
) -> None:
    """Insert a RUNNING audit row with a STABLE invocation id.

    ``invocation_id`` is the single identity that threads through the audit row
    (``tool_invocation.id``), the executor's ``tool_call_id``, and
    ``Evidence.source_tool_invocation_id``. It is deterministic for a given
    (investigation, tool, candidate) so a crashed/replayed node reuses the same id.
    """
    await uow.tool_invocations.add_started(
        tenant_id=tenant_id,
        record=ToolInvocationRecord(
            id=invocation_id,
            investigation_id=investigation_id,
            tool_name=tool_name,
            idempotency_key=idempotency_key,
            status="RUNNING",
            started_at=datetime.now(UTC),
            arguments=arguments,
            provider_request_id=provider_request_id,
        ),
    )


async def record_finished(
    uow: UnitOfWork,
    *,
    tenant_id: str,
    investigation_id: UUID,
    idempotency_key: str,
    status: str,
    error_code: str | None = None,
    safe_error_message: str | None = None,
    result_metadata: dict[str, Any] | None = None,
) -> None:
    await uow.tool_invocations.finish(
        tenant_id=tenant_id,
        investigation_id=investigation_id,
        idempotency_key=idempotency_key,
        status=status,
        finished_at=datetime.now(UTC),
        error_code=error_code,
        safe_error_message=safe_error_message,
        result_metadata=result_metadata,
    )
