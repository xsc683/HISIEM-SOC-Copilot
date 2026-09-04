"""Shared domain errors.

Domain layer raises only these (or aggregate-specific subclasses). They carry a
stable machine code used at the HTTP boundary; the message is analyst-facing.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base for all domain errors."""

    code = "DOMAIN_ERROR"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class StateTransitionError(DomainError):
    """Raised when a command would perform an illegal state transition."""

    code = "INVALID_STATE_TRANSITION"

    def __init__(
        self,
        *,
        aggregate_type: str,
        current_status: str,
        command: str,
        message: str | None = None,
    ) -> None:
        super().__init__(
            message
            or f"{aggregate_type} cannot transition from {current_status} via {command}",
            details={
                "aggregate_type": aggregate_type,
                "current_status": current_status,
                "command": command,
            },
        )


class OptimisticConcurrencyError(DomainError):
    """Raised when a versioned update affects zero rows."""

    code = "OPTIMISTIC_CONCURRENCY"

    def __init__(self, *, aggregate_type: str, aggregate_id: str) -> None:
        super().__init__(
            f"{aggregate_type} {aggregate_id} was concurrently modified",
            details={"aggregate_type": aggregate_type, "aggregate_id": aggregate_id},
        )
