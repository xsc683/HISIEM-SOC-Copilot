"""Event publisher port — feeds the transactional outbox."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID


class EventPublisherPort(Protocol):
    """Persists a domain event + outbox message in the current transaction.

    Called from within a UnitOfWork; the infrastructure implementation appends to
    ``domain_event`` and ``outbox_message`` so publish + aggregate commit are atomic.
    """

    async def publish(
        self,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        event_type: str,
        event_version: int,
        payload: dict[str, Any],
        tenant_id: str,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        actor_subject_id: str | None = None,
    ) -> None: ...
