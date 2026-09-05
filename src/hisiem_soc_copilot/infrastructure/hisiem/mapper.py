"""HISIEM read-model mappers.

Convert raw HISIEM JSON into bounded, tenant-scoped read models. Field mapping is
verified against the reference SIEM platform: the alert detail endpoint returns
the ES ``_source`` with keys flattened under ``alert.*`` / ``rule.*`` plus the
injected ``_id``. Only a bounded subset is carried into Copilot context — never a
full SIEM data copy (persistence-schema.md §29).
"""

from __future__ import annotations

from typing import Any

from ...application.errors import ExternalServiceError
from ...application.ports.hisiem import (
    DetectionRuleContext,
    HisiemAlertData,
    LogEventHit,
)


def _opt(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None


def _opt_num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def map_alert_detail(payload: Any, *, tenant_id: str | None = None) -> HisiemAlertData:
    """Map a HISIEM ``GET /api/alerts/{id}`` JSON object to a HisiemAlertData.

    ``alert_id`` (the addressable ES ``_id``) is read STRICTLY from ``_id`` — it is
    NEVER inferred from ``alert.id`` (a business identifier, not the addressing id;
    see hisiem-integration-contract.md). ``alert.id`` may only populate the optional
    ``business_id`` display field. A payload whose ``_id`` is missing yields an empty
    ``alert_id``: the caller treats that as an invalid/absent provider response.
    """
    if not isinstance(payload, dict):
        raise ValueError("alert detail payload must be a JSON object")

    def pick(*paths: str) -> Any:
        for path in paths:
            # Real HISIEM writes promoted fields as flat dotted keys
            # (e.g. ``source.ip``) but ``alert.*``/``rule.*`` as nested maps;
            # accept both shapes.
            direct = payload.get(path)
            if direct is not None:
                return direct
            node: Any = payload
            for part in path.split("."):
                if not isinstance(node, dict):
                    node = None
                    break
                node = node.get(part)
            if node is not None:
                return node
        return None

    raw_id = payload.get("_id")
    if raw_id is None or str(raw_id) == "":
        # The addressable ES ``_id`` is mandatory and may NEVER be inferred from
        # ``alert.id`` (a business identifier). A payload without it is an invalid
        # provider response, not a usable alert.
        raise ExternalServiceError(
            "HISIEM alert response is missing the addressing _id",
            service="hisiem",
            code="INVALID_RESPONSE",
        )
    alert_id = str(raw_id)
    tenant = tenant_id or _opt(payload.get("tenant_id"))
    alert = payload.get("alert")
    if isinstance(alert, dict):
        severity = alert.get("severity")
        status = alert.get("status")
    else:
        severity = pick("severity")
        status = pick("status")

    source_ip = _opt(payload.get("source.ip"))
    if source_ip is None and isinstance(alert, dict):
        source_ip = _opt(alert.get("source_ip"))

    rule_tags: list[str] = []
    rule = payload.get("rule")
    if isinstance(rule, dict):
        tags = rule.get("tags")
        if isinstance(tags, list):
            rule_tags = [str(t) for t in tags]

    related = _opt_list(payload.get("related_events"))
    return HisiemAlertData(
        alert_id=alert_id,
        tenant_id=tenant or "",
        business_id=_opt(alert.get("id") if isinstance(alert, dict) else None),
        alert_uuid=_opt(payload.get("alert_uuid")),
        rule_id=_opt(pick("alert.rule_id", "rule_id")),
        rule_name=_opt(pick("alert.rule_name", "rule_name")),
        rule_type=_opt(pick("alert.type", "rule.type", "rule_type")),
        severity=_opt(severity),
        description=_opt(pick("alert.description", "description")),
        status=_opt(status),
        detected_at=_opt(pick("alert.created_at", "detected_at")),
        risk_score=_opt_num(pick("alert.risk_score", "risk_score")),
        entity=_opt(pick("alert.entity", "entity")),
        case_id=_opt(pick("alert.case_id", "case_id")),
        rule_tags=rule_tags,
        source_ip=source_ip,
        user_name=_opt(pick("user.name", "user_name")),
        host_name=_opt(pick("host.name", "host_name")),
        event_category=_opt(pick("event.category", "event_category")),
        event_action=_opt(pick("event.action", "event_action")),
        log_source_id=_opt(pick("log.source_id", "log_source_id")),
        event_count=_int(pick("alert.deduplicated_count", "event_count")),
        related_events=[e for e in related if isinstance(e, dict)],
        raw=dict(payload),
    )


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def map_log_search_hit(item: Any) -> LogEventHit | None:
    """Map one ``POST /api/log-search`` item to a bounded LogEventHit.

    The provider returns the full ES ``_source`` merged with ``_id``/``_index``;
    only a bounded subset is carried into context.
    """
    if not isinstance(item, dict):
        return None
    document_id = str(item.get("_id") or item.get("id") or "")
    if not document_id:
        return None
    return LogEventHit(
        document_id=document_id,
        index=str(item.get("_index") or ""),
        timestamp=_opt(item.get("@timestamp")),
        event_category=_opt(item.get("event.category")),
        event_action=_opt(item.get("event.action")),
        event_outcome=_opt(item.get("event.outcome")),
        source_ip=_opt(item.get("source.ip")),
        destination_ip=_opt(item.get("destination.ip")),
        user_name=_opt(item.get("user.name")),
        host_name=_opt(item.get("host.name")),
        log_source_id=_opt(item.get("log.source_id")),
        message=_opt(item.get("message")),
        fields=dict(item),
    )


def map_log_search_response(payload: Any) -> list[LogEventHit]:
    """Map a ``POST /api/log-search`` response object to LogEventHits."""
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [hit for item in items if (hit := map_log_search_hit(item)) is not None]


def map_detection_rule(payload: Any, *, rule_id: str) -> DetectionRuleContext | None:
    """Map a ``GET /api/detection-rules/{id}`` YAML-map response.

    Rules are returned as the YAML map as-is; only a bounded subset is carried
    into Copilot. Never treats rule body as executable content.
    """
    if not isinstance(payload, dict):
        return None
    enabled = payload.get("enabled")
    return DetectionRuleContext(
        rule_id=_opt(payload.get("id")) or rule_id,
        name=_opt(payload.get("name")),
        category=_opt(payload.get("category")),
        rule_type=_opt(payload.get("type")),
        severity=_opt(payload.get("severity")),
        enabled=enabled if isinstance(enabled, bool) else None,
        status=_opt(payload.get("status")),
        version=_opt(payload.get("version")),
        tags=[str(t) for t in _opt_list(payload.get("tags"))],
        description=_opt(payload.get("description")),
        logic_summary=_condition_summary(payload.get("condition")),
    )


def _condition_summary(condition: Any) -> str | None:
    if isinstance(condition, dict):
        parts = [f"{k}={v}" for k, v in condition.items()]
        if parts:
            return "; ".join(parts)
    return None
