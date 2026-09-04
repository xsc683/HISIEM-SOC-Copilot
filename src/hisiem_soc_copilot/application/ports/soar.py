"""SOAR execution boundary (HISIEM-owned truth)."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class SoarPort(Protocol):
    """Submit/observe SOAR executions.

    HISIEM SOAR is the source of truth for executions. Copilot persists only a
    ResponseExecutionRef projection and never implements a second workflow engine.
    """

    async def submit_execution(
        self,
        *,
        tenant_id: str,
        proposal_id: UUID,
        submission_key: str,
        action_key: str,
    ) -> str:  # returns provider execution_id
        ...

    async def get_execution_status(
        self, *, tenant_id: str, execution_id: str
    ) -> str: ...
