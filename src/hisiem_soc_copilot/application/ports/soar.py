"""SOAR execution boundary (HISIEM-owned truth).

HISIEM SOAR is the source of truth for executions. Copilot persists only a
``ResponseExecutionRef`` projection and never implements a second workflow engine.
The adapter maps an approved typed action to a HISIEM request and normalizes the
response into a bounded, secret-free :class:`SoarExecutionResult` — raw upstream
bodies never cross this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from ...domain.investigation.value_objects import ExternalResourceRef


@dataclass(frozen=True)
class SoarExecutionResult:
    """Bounded, secret-free view of one HISIEM SOAR execution.

    ``status`` is a ``ResponseExecutionStatus`` value (QUEUED/RUNNING/SUCCEEDED/
    FAILED). ``safe_result``/``safe_error_code``/``safe_error_message`` are bounded
    summaries — never a raw upstream body, token, or platform credential.
    """

    execution_id: str
    status: str
    safe_result: dict[str, object] = field(default_factory=dict)
    safe_error_code: str | None = None
    safe_error_message: str | None = None


class SoarPort(Protocol):
    """Submit/observe SOAR executions for an approved response action."""

    async def submit_execution(
        self,
        *,
        tenant_id: str,
        proposal_id: UUID,
        submission_key: str,
        action_key: str,
        parameters: dict[str, object],
        target_ref: ExternalResourceRef,
    ) -> SoarExecutionResult:
        """Submit one approved action. Idempotent by ``submission_key``."""
        ...

    async def get_execution_status(
        self, *, tenant_id: str, execution_id: str
    ) -> SoarExecutionResult:
        """Fetch the current status/result of a previously submitted execution."""
        ...
