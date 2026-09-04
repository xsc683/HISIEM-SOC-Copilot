"""Threat intelligence port."""

from __future__ import annotations

from typing import Protocol


class ThreatIntelPort(Protocol):
    async def lookup(self, *, tenant_id: str, value: str) -> dict[str, object]: ...
