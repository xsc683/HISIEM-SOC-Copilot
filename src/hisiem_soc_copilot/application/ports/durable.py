"""Durable runtime ports — events, outbox, receipts, audit, binding.

These Protocols describe what the Application layer needs to make a business
command durable, auditable, and idempotent. Infrastructure provides SQLAlchemy
implementations; fakes provide in-memory ones for unit tests.

Every method here is called INSIDE an existing UnitOfWork transaction, so the
rows they insert commit (or roll back) atomically with the domain rows they
describe (persistence-schema.md §31).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from ...domain.investigation.events import InvestigationEvent


@dataclass(frozen=True)
class OrchestrationBinding:
    """Domain Investigation ↔ LangGraph thread (distinct identities)."""

    investigation_id: UUID
    thread_id: str
    graph_name: str
    graph_version: str
    state_schema_version: int


@dataclass(frozen=True)
class ToolInvocationRecord:
    """Operational audit row for one tool call (never stores raw results)."""

    id: UUID
    investigation_id: UUID
    tool_name: str
    idempotency_key: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    arguments: dict[str, Any] | None = None
    tool_version: str | None = None
    provider_request_id: str | None = None
    error_code: str | None = None
    safe_error_message: str | None = None
    result_metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class DurableCommand:
    """A business command stamped with idempotency metadata."""

    command_id: UUID
    command_type: str
    idempotency_key: str
    tenant_id: str
    aggregate_type: str
    aggregate_id: UUID
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    actor_subject_id: str | None = None
    # Optional result the caller returns on a duplicate (never raw/sensitive).
    safe_result: dict[str, Any] | None = None
    # Bounded fingerprint of the business request so a replayed key bound to a
    # DIFFERENT request is a deterministic conflict (not a silent wrong replay).
    request_fingerprint: str | None = None


@dataclass(frozen=True)
class CommandReceiptRecord:
    """A persisted command_receipt read back for idempotent replay resolution."""

    idempotency_key: str
    command_type: str
    tenant_id: str
    aggregate_id: UUID | None
    request_fingerprint: str | None = None
    safe_result: dict[str, Any] | None = None


@dataclass(frozen=True)
class DomainEventEnvelope:
    """A persisted domain_event row read back for outbox dispatch/audit."""

    event_id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    tenant_id: str
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    actor_subject_id: str | None = None
    payload: dict[str, Any] | None = None
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class OutboxRecord:
    """A claimable outbox message for the dispatcher.

    ``lease_token`` is the fencing token written at claim time. Every settlement
    (published / failed / dead-letter) and every lease renewal must present the
    SAME token; a worker whose lease was lost to a reclaim (another worker took the
    row after the lease expired) therefore can never settle or renew it — its stale
    token simply no longer matches.
    """

    id: UUID
    event_id: UUID
    destination: str
    status: str
    attempt_count: int
    available_at: datetime
    lease_token: str
    locked_at: datetime | None = None
    locked_by: str | None = None
    last_error_code: str | None = None


class EventLedger(Protocol):
    """Append-only domain-event + outbox persistence inside one transaction."""

    async def append(self, event: InvestigationEvent, *, aggregate_revision: int) -> None: ...

    async def get(self, *, event_id: UUID) -> DomainEventEnvelope | None: ...


class CommandReceiptStore(Protocol):
    """Command idempotency record persistence/check.

    All lookups are scoped to the receipt's logical identity
    ``(tenant_id, command_type, idempotency_key)`` — an Idempotency-Key is never a
    global key. There is deliberately NO unscoped ``exists(key)`` /
    ``get_safe_result(key)`` read path, so a caller cannot accidentally treat two
    tenants' same key as the same idempotency space.
    """

    async def exists(
        self, *, tenant_id: str, command_type: str, idempotency_key: str
    ) -> bool: ...

    async def record(self, receipt: DurableCommand) -> None: ...

    async def find(
        self, *, tenant_id: str, command_type: str, idempotency_key: str
    ) -> CommandReceiptRecord | None:
        """Resolve a receipt by its logical identity (tenant + type + key).

        Used by request-idempotency replay to recover the ORIGINAL aggregate id
        (even when that investigation is terminal) and to compare the replayed
        request's fingerprint.
        """

    async def get_safe_result(
        self, *, tenant_id: str, command_type: str, idempotency_key: str
    ) -> dict[str, Any] | None: ...


class OutboxStore(Protocol):
    """Transactional outbox read/claim/mark for the dispatcher.

    Claiming and state transitions each run in their OWN transaction — never in
    the same transaction that executes a graph/LLM/network call.

    Lease + fencing semantics: a claim moves a ready message to ``PROCESSING`` and
    writes a fresh ``lease_token`` + ``locked_at``/``locked_by``. A worker whose
    lease expires and is reclaimed by another worker keeps a STALE token: its
    later settlement/renewal attempts carry that stale token, match no row
    (``rowcount == 0``) and are rejected — so a late stale worker can never mark a
    row it no longer owns. Claim is atomic — two workers can never hold the same
    live lease.

    ``locked_at`` is the timestamp of the LAST successful claim or renewal (never
    written into the future). A lease is expired when
    ``locked_at <= now - lease_timeout_seconds``; renewing re-arms that window
    from the renewal instant.

    Lease renewal: a legitimately long-running worker renews its lease (resets
    ``locked_at`` to the renewal time) while the run is in flight, always
    presenting its own token. Renewal must match ``lease_token`` so a stale worker
    cannot keep extending a lease it lost. The runner renews periodically so a
    normal long investigation (up to the runtime duration budget) is never stolen
    by a fixed 60s lease. When the worker stops renewing (crash), the message
    becomes reclaimable once ``locked_at + lease_timeout`` passes.

    Terminal states: ``PUBLISHED`` (delivered) and ``DEAD_LETTER`` (permanent
    failure — a dead letter is never re-claimed). ``FAILED`` is always retryable.
    """

    async def claim_batch(
        self,
        *,
        worker: str,
        limit: int,
        available_before: datetime,
        lease_timeout_seconds: int = 60,
    ) -> list[OutboxRecord]: ...

    async def renew_lease(
        self,
        *,
        outbox_id: UUID,
        lease_token: str,
        lease_timeout_seconds: int,
        now: datetime,
    ) -> bool: ...

    async def mark_published(
        self,
        *,
        outbox_id: UUID,
        lease_token: str,
        published_at: datetime,
    ) -> bool: ...

    async def mark_failed(
        self,
        *,
        outbox_id: UUID,
        lease_token: str,
        error_code: str,
        next_available_at: datetime,
        attempt_count: int,
    ) -> bool: ...

    async def mark_dead_letter(
        self, *, outbox_id: UUID, lease_token: str, error_code: str
    ) -> bool: ...


class OrchestrationBindingStore(Protocol):
    """Lookup/persist the investigation↔thread binding (tenant-scoped reads)."""

    async def get(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> OrchestrationBinding | None: ...

    async def get_by_thread_id(self, *, thread_id: str) -> OrchestrationBinding | None: ...

    async def put(self, binding: OrchestrationBinding) -> None: ...


class ToolInvocationStore(Protocol):
    """Operational tool audit (RUNNING → SUCCEEDED / FAILED).

    Rows are addressed by ``(investigation_id, idempotency_key)`` (the schema's
    unique constraint) so a crashed/replayed graph node re-uses the same audit row
    instead of duplicating it. Only bounded arguments + result metadata are ever
    stored — never raw tool results (persistence-schema.md §28).
    """

    async def add_started(
        self,
        *,
        tenant_id: str,
        record: ToolInvocationRecord,
    ) -> None:
        """Insert a RUNNING audit row; a duplicate key is a no-op (replay-safe)."""

    async def finish(
        self,
        *,
        tenant_id: str,
        investigation_id: UUID,
        idempotency_key: str,
        status: str,
        finished_at: datetime,
        error_code: str | None = None,
        safe_error_message: str | None = None,
        result_metadata: dict[str, Any] | None = None,
    ) -> None: ...

    async def find_by_key(
        self,
        *,
        tenant_id: str,
        investigation_id: UUID,
        idempotency_key: str,
    ) -> ToolInvocationRecord | None: ...
