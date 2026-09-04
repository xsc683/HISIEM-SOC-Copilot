"""Minimal domain event base.

Aggregate methods emit immutable event objects describing what happened. They are
not persisted directly by the aggregate; the application layer maps them to the
``domain_event`` outbox/event table (append-only, not event sourcing).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .identifiers import new_uuid, utc_now


@dataclass(frozen=True)
class AggregateEvent:
    """Base class for aggregate-raised domain events."""

    event_type: str
    aggregate_type: str
    aggregate_id: Any
    version: int = 1
    occurred_at: datetime = field(default_factory=utc_now)
    event_id: Any = field(default_factory=new_uuid)

    @property
    def payload(self) -> dict[str, Any]:
        return {}
