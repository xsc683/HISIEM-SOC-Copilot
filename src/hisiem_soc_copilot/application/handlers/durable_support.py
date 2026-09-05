"""Shared durable-command support for the Investigation handlers.

Every mutating command must leave behind, in ONE transaction:
    domain rows + domain_event row(s) + (when orchestration-triggering) an outbox
    row + a command_receipt (persistence-schema.md §31, §25).

``run_idempotent_command`` wraps that contract: it loads the aggregate once and
hands it to ``apply``, short-circuits when the command's ``idempotency_key`` was
already executed (crash-replay of a node whose commit succeeded but whose
checkpoint was lost), otherwise runs ``apply``, flushes pending events, records
the receipt, and lets the caller commit. ``apply`` mutates the aggregate + child
rows and returns the public result; ``replay`` reconstructs that result from the
already-persisted state (using the bounded ids stored on the receipt).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from ...domain.investigation.aggregate import Investigation
from ..errors import NotFoundError
from ..ports.durable import DurableCommand
from ..ports.unit_of_work import UnitOfWork

# Domain entity types a command result may carry; only these are mirrored onto the
# receipt so a duplicate call can be answered without re-running the command.
_SAFE_RESULT_TYPES = frozenset(
    {
        "Investigation",
        "InvestigationResult",
        "PlanRevision",
        "Hypothesis",
        "Evidence",
        "Finding",
        "HypothesisAssessment",
    }
)


def _audit_only_key(
    *, investigation_id: UUID, command_type: str, command_id: UUID
) -> str:
    """Receipt key when the caller did not supply one (unique-per-run audit).

    Legacy/direct handler call sites (unit tests) never set an ``idempotency_key``;
    giving them a unique key per ``command_id`` means the receipt is recorded for
    the audit trail but can never collide with a later, logically-identical call.
    """
    return f"investigation:{investigation_id}:{command_type}:{command_id}"


async def load_investigation(
    uow: UnitOfWork, *, tenant_id: str, investigation_id: UUID
) -> Investigation:
    investigation = await uow.investigations.get(
        tenant_id=tenant_id, investigation_id=investigation_id
    )
    if investigation is None:
        raise NotFoundError(
            "investigation not found",
            resource_type="investigation",
            resource_id=str(investigation_id),
        )
    return investigation


async def flush_events(uow: UnitOfWork, investigation: Investigation) -> None:
    """Persist the aggregate's pending events (as domain_event rows) atomically.

    Outbox rows are created by the EventLedger only for orchestration-triggering
    event types; ordinary audit events never enqueue a delivery.
    """
    for event in investigation.pending_events:
        await uow.events.append(event, aggregate_revision=investigation.revision)
    investigation.clear_events()


async def run_idempotent_command[T](
    *,
    uow: UnitOfWork,
    tenant_id: str,
    investigation_id: UUID,
    command_type: str,
    command_id: UUID,
    idempotency_key: str | None,
    correlation_id: UUID | None,
    causation_id: UUID | None,
    actor_subject_id: str | None,
    apply: Callable[[Investigation], Awaitable[T]],
    replay: Callable[[Investigation], Awaitable[T]],
) -> T:
    """Execute one durable command with exactly-once semantics.

    The aggregate is loaded once and passed to both callbacks. On a receipt hit the
    command body is never re-run, so a crashed-but-committed node cannot duplicate
    domain rows or events. ``apply`` mutates the aggregate and child rows and
    returns the public result; ``replay`` reconstructs it from persisted state.
    """
    investigation = await load_investigation(
        uow, tenant_id=tenant_id, investigation_id=investigation_id
    )
    key = idempotency_key or _audit_only_key(
        investigation_id=investigation_id,
        command_type=command_type,
        command_id=command_id,
    )
    if await uow.command_receipts.exists(
        tenant_id=tenant_id, command_type=command_type, idempotency_key=key
    ):
        return await replay(investigation)

    outcome = await apply(investigation)
    await flush_events(uow, investigation)
    await uow.command_receipts.record(
        DurableCommand(
            command_id=command_id,
            command_type=command_type,
            idempotency_key=key,
            tenant_id=tenant_id,
            aggregate_type="investigation",
            aggregate_id=investigation_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            actor_subject_id=actor_subject_id,
            safe_result=_safe_outcome(outcome),
        )
    )
    return outcome


def _safe_outcome(outcome: object) -> dict[str, Any] | None:
    """Bounded ids from a command result, for duplicate short-circuits.

    Maps the domain entity type name → its created ids. Only stable identifiers
    are stored (never raw payloads/results) (persistence-schema.md §25).
    """
    safe: dict[str, Any] = {}
    items = outcome if isinstance(outcome, tuple) else (outcome,)
    for item in items:
        if item is None:
            continue
        kind = type(item).__name__
        if kind not in _SAFE_RESULT_TYPES:
            continue
        obj_id = getattr(item, "id", None)
        if isinstance(obj_id, UUID):
            safe.setdefault(kind, []).append(str(obj_id))
    return safe or None
