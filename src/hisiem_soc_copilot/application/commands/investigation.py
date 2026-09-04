"""Investigation commands — expressions of intent to change business facts.

Commands are immutable messages. They never carry authoritative tenant/actor
values from clients: those are bound by the caller from the authenticated context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4


@dataclass(frozen=True)
class StartAlertInvestigation:
    """Create (or return existing active) Investigation for one HISIEM Alert.

    ``tenant_id`` and ``initiated_by`` are populated by the authenticated request
    context (never from the request body / model).
    """

    tenant_id: str
    source_alert_id: str
    initiated_by_subject: str
    initiated_by_display_name: str | None = None
    command_id: UUID = field(default_factory=uuid4)
    correlation_id: UUID | None = None


@dataclass(frozen=True)
class CancelInvestigation:
    tenant_id: str
    investigation_id: UUID
    initiated_by_subject: str
    command_id: UUID = field(default_factory=uuid4)
    correlation_id: UUID | None = None


# placeholder union for future typed command dispatch
InvestigationCommand = (
    StartAlertInvestigation | CancelInvestigation
)
