"""HISIEM platform read/authority port + read models.

V1 only hydrates authoritative alert context plus bounded event/entity/rule
search. Write/response capabilities are deliberately excluded: side effects flow
through SOAR only after human approval.

Read-model shapes follow the real HISIEM control API contract (verified against
the reference SIEM repo): the alert detail endpoint returns the ES ``_source``
flattened under ``alert.*``/``rule.*`` plus the injected ``_id``; log-search is
``POST /api/log-search`` and rules are ``GET /api/detection-rules/{id}``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class HisiemAlertData:
    """Bounded alert hydration read model (source: HISIEM authority).

    ``alert_id`` is the ES ``_id`` used to address the alert. Only a bounded
    subset of HISIEM fields enters Copilot context — never a full SIEM data copy.
    """

    alert_id: str
    tenant_id: str
    alert_uuid: str | None = None
    rule_id: str | None = None
    rule_name: str | None = None
    rule_type: str | None = None
    severity: str | None = None
    description: str | None = None
    status: str | None = None
    detected_at: str | None = None
    risk_score: float | None = None
    entity: str | None = None
    case_id: str | None = None
    rule_tags: list[str] = field(default_factory=list)
    source_ip: str | None = None
    user_name: str | None = None
    host_name: str | None = None
    event_category: str | None = None
    event_action: str | None = None
    log_source_id: str | None = None
    event_count: int | None = None
    related_events: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] | None = None

    @property
    def display_title(self) -> str | None:
        return self.description or self.rule_name


@dataclass(frozen=True)
class LogEventHit:
    """A single normalized event from ``POST /api/log-search``."""

    document_id: str
    index: str
    timestamp: str | None = None
    event_category: str | None = None
    event_action: str | None = None
    event_outcome: str | None = None
    source_ip: str | None = None
    destination_ip: str | None = None
    user_name: str | None = None
    host_name: str | None = None
    log_source_id: str | None = None
    message: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EventSearchResult:
    """Bounded result of one HISIEM event search."""

    items: list[LogEventHit]
    total: int
    returned: int
    from_: str
    to: str
    took_ms: int | None = None
    truncated: bool = False


@dataclass(frozen=True)
class DetectionRuleContext:
    """Bounded detection-rule context (never executable code or platform config)."""

    rule_id: str
    name: str | None = None
    category: str | None = None
    rule_type: str | None = None
    severity: str | None = None
    enabled: bool | None = None
    status: str | None = None
    version: str | None = None
    tags: list[str] = field(default_factory=list)
    description: str | None = None
    logic_summary: str | None = None


class HisiemPort(Protocol):
    """Read-only HISIEM access used to build authoritative Investigation context."""

    async def get_alert(
        self, *, tenant_id: str, alert_id: str
    ) -> HisiemAlertData | None: ...

    async def search_events(
        self,
        *,
        tenant_id: str,
        from_: str,
        to: str,
        conditions: list[dict[str, object]],
        limit: int = 100,
        sort: str = "desc",
    ) -> EventSearchResult: ...

    async def get_detection_rule(
        self, *, tenant_id: str, rule_id: str
    ) -> DetectionRuleContext | None: ...
