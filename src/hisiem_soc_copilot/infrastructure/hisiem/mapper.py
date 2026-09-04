"""HISIEM read-model mapper.

Converts raw HISIEM JSON into a bounded, tenant-scoped alert read model. Only a
bounded subset of HISIEM fields is carried into Copilot context — never a full
SIEM data copy (persistence-schema.md §29 forbids alert/event copies).
"""

from __future__ import annotations

from typing import Any

from ...application.ports.hisiem import HisiemAlertData


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def map_alert_detail(payload: Any) -> HisiemAlertData:
    """Map a HISIEM ``GET /api/alerts/{id}`` JSON object to a HisiemAlertData.

    ``alert_id``/``tenant_id`` resolve from the payload; a missing id yields an
    empty id, which the caller treats as invalid/absent authority data.
    """
    if not isinstance(payload, dict):
        raise ValueError("alert detail payload must be a JSON object")
    return HisiemAlertData(
        alert_id=_as_optional_str(payload.get("id")) or "",
        tenant_id=_as_optional_str(payload.get("tenant_id")) or "",
        title=_as_optional_str(payload.get("title")),
        severity=_as_optional_str(payload.get("severity")),
        status=_as_optional_str(payload.get("status")),
        rule_name=_as_optional_str(payload.get("rule_name")),
        detected_at=_as_optional_str(payload.get("detected_at")),
        raw=dict(payload),
    )
