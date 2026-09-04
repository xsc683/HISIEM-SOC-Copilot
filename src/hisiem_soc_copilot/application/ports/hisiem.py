"""HISIEM platform read/authority port + alert read model.

V1 only hydrates authoritative alert context (context loading) plus bounded
event/entity search. Write/response capabilities are deliberately excluded:
side effects flow through SOAR only after human approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class HisiemAlertData:
    """Bounded alert hydration read model (source: HISIEM authority).

    Only a bounded subset of HISIEM fields enters Copilot context — never a full
    SIEM data copy. `raw` is a bounded snapshot for provenance display.
    """

    alert_id: str
    tenant_id: str
    title: str | None = None
    severity: str | None = None
    status: str | None = None
    rule_name: str | None = None
    detected_at: str | None = None
    raw: dict[str, Any] | None = None


class HisiemPort(Protocol):
    """Read-only HISIEM access used to build authoritative Investigation context."""

    async def get_alert(
        self, *, tenant_id: str, alert_id: str
    ) -> HisiemAlertData | None: ...

    async def search_events(
        self,
        *,
        tenant_id: str,
        query: str,
        time_range_minutes: int = 60,
        size: int = 100,
    ) -> list[dict[str, object]]: ...
