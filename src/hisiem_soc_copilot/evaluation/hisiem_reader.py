"""Bounded, tenant-scoped reader over HISIEM's real control/read API (E1-B.3).

This is the evaluation-only read surface for the GP-01 materializer. It talks to
the SAME endpoints as the production ``HisiemHttpAdapter`` (``GET
/api/alerts/{id}``, ``POST /api/log-search``, ``GET /api/detection-rules/{id}``,
``GET /api/alerts``) but returns local frozen dataclasses that PRESERVE the exact
provider identity the resolver requires:

- the ES document ``_id`` as ``FoundEvent.document_id`` / ``FoundAlert.address_id``
  (address_id is NEVER derived from ``alert.id`` / business_id / hashes);
- the real ``_index`` on events and on alert-related-event refs;
- the bounded ECS fields + ``message`` the resolver correlates on.

The materializer resolution path therefore never needs direct Elasticsearch/Kafka
access (E1-B.3 §2, §13, §14).

Scope: read-only; every method is tenant-scoped via ``X-Tenant-ID`` + Bearer.
Never stores secrets, raw log bodies, or full provider documents beyond the
bounded fields mapped here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from ..config import HisiemSettings
from .contracts import (
    AlertNotStableError,
    AlertResolutionTimeout,
    AmbiguousEventError,
    AmbiguousSourceAlertError,
    EventResolutionTimeout,
    RelatedEventRef,
    ResolvedAlert,
    ResolvedEvent,
    sha256_hex,
)

_PROVIDER = "hisiem"

# Reachability probe endpoint (E1-B.3 §10.1).
_READY_PATH = "/api/health"

# Bounded page size for log-search and list-alerts (list endpoint max 200).
_PAGE_SIZE = 200

# The committed GP-01 detection rule the materializer resolves against.
_GP01_RULE_ID = "rule-ssh-brute-force-001"


def _opt(value: Any) -> str | None:
    """Map a raw field to ``None`` or a non-empty trimmed string."""
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None


def _first(raw: dict[str, Any], *paths: str) -> str | None:
    """Read the first non-empty dotted path from a flattened HISIEM source."""
    for path in paths:
        node: Any = raw
        for part in path.split("."):
            if not isinstance(node, dict):
                break
            node = node.get(part)
            if node is None:
                break
        if isinstance(node, dict):
            continue
        value = _opt(node)
        if value is not None:
            return value
    return None


def _int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _rfc3339_utc(value: datetime | str) -> str:
    """Normalize a from/to bound to the RFC3339 UTC form HISIEM returns/accepts."""
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        # Naive datetime is ambiguous about zone; assume UTC explicitly.
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Local bounded read models (defined here — never in contracts.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FoundEvent:
    """One HISIEM event hit with exact provider identity preserved.

    ``index``/``document_id`` are the EXACT ``_index``/``_id`` returned by HISIEM
    — never derived from logical roles or hashes (E1-B.3 §7). ``message`` is kept
    so the resolver can prove the rendered line's correlation fingerprint.
    """

    document_id: str
    index: str
    timestamp: str | None = None
    event_category: str | None = None
    event_action: str | None = None
    event_outcome: str | None = None
    source_ip: str | None = None
    user_name: str | None = None
    host_name: str | None = None
    log_source_id: str | None = None
    message: str | None = None

    @property
    def message_fingerprint(self) -> str | None:
        """Materializer-only correlation aid (resolver aid, never identity)."""
        return sha256_hex(self.message) if self.message is not None else None


@dataclass(frozen=True)
class RelatedEvent:
    """A stable provider reference to an alert-correlated event."""

    index: str
    document_id: str


@dataclass(frozen=True)
class FoundAlert:
    """One HISIEM alert preserving the real addressing id + bounded context.

    ``address_id`` is the ES ``_id`` that the alert detail API actually addresses
    with; it is NEVER inferred from ``alert.id`` / ``business_id``. ``business_id``
    carries the provider's own ``alert.id`` as display/correlation metadata only
    (hisiem-integration-contract.md §4).
    """

    address_id: str
    business_id: str | None = None
    rule_id: str | None = None
    rule_name: str | None = None
    severity: str | None = None
    status: str | None = None
    created_at: str | None = None
    risk_score: float | None = None
    entity: str | None = None
    source_ip: str | None = None
    user_name: str | None = None
    host_name: str | None = None
    event_count: int | None = None
    related_events: list[RelatedEvent] = field(default_factory=list)

    @property
    def fingerprint(self) -> tuple[str, ...]:
        """Stability-barrier fingerprint (E1-B.3 §15): address_id, rule_id,
        source/entity, event_count, related-event identity set, status."""
        return (
            self.address_id,
            str(self.rule_id or ""),
            str(self.entity or self.source_ip or ""),
            str(self.event_count or 0),
            ",".join(sorted(f"{r.index}:{r.document_id}" for r in self.related_events)),
            str(self.status or ""),
        )


@dataclass(frozen=True)
class RuleContract:
    """Bounded detection-rule preflight contract.

    NOT the production :class:`DetectionRuleContext` (whose mapper DISCARDS
    ``threshold``/``windowMinutes``/``keyField``/``condition``); this reader reads
    the raw rule payload itself and keeps every field GP-01's preflight needs.
    """

    rule_id: str
    name: str | None = None
    enabled: bool | None = None
    rule_type: str | None = None
    severity: str | None = None
    status: str | None = None
    key_field: str | None = None
    window_minutes: int | None = None
    threshold: int | None = None
    condition_action: str | None = None


def map_rule_contract(payload: Any) -> RuleContract | None:
    """Map the raw rule YAML-map payload to a :class:`RuleContract`."""
    if not isinstance(payload, dict):
        return None
    condition = payload.get("condition")
    action: str | None = None
    if isinstance(condition, dict):
        value = condition.get("value")
        if value is not None:
            action = str(value)
    enabled = payload.get("enabled")
    return RuleContract(
        rule_id=str(payload.get("id") or ""),
        name=_opt(payload.get("name")),
        enabled=enabled if isinstance(enabled, bool) else None,
        rule_type=_opt(payload.get("type")),
        severity=_opt(payload.get("severity")),
        status=_opt(payload.get("status")),
        key_field=_opt(payload.get("keyField")),
        window_minutes=_int(payload.get("windowMinutes")),
        threshold=_int(payload.get("threshold")),
        condition_action=action,
    )


def map_found_alert(raw: dict[str, Any]) -> FoundAlert | None:
    """Map one HISIEM alert item/source to a :class:`FoundAlert`.

    ``address_id`` is read STRICTLY from ``_id``; a payload without it is not a
    usable alert and maps to ``None`` (never silently addressed by ``alert.id``).
    Accepts both the flattened alert-source shape and the list-item envelope.
    """
    nested = raw.get("_source")
    payload: dict[str, Any] = nested if isinstance(nested, dict) else raw
    address_id = str(payload.get("_id") or raw.get("_id") or "")
    if address_id == "":
        return None
    related: list[RelatedEvent] = []
    related_raw = payload.get("related_events")
    if isinstance(related_raw, list):
        for entry in related_raw:
            if not isinstance(entry, dict):
                continue
            index = str(entry.get("_index") or entry.get("index") or "")
            doc_id = str(entry.get("_id") or entry.get("id") or "")
            if index and doc_id:
                related.append(RelatedEvent(index=index, document_id=doc_id))
    return FoundAlert(
        address_id=address_id,
        business_id=_first(payload, "alert.id") or _opt(payload.get("business_id")),
        rule_id=_first(payload, "alert.rule_id", "rule_id"),
        rule_name=_first(payload, "alert.rule_name", "rule_name"),
        severity=_first(payload, "alert.severity", "severity"),
        status=_first(payload, "alert.status", "status"),
        created_at=_first(payload, "alert.created_at", "created_at"),
        risk_score=_float(_first(payload, "alert.risk_score", "risk_score")),
        entity=_first(payload, "alert.entity", "entity"),
        source_ip=_first(payload, "source.ip", "source_ip"),
        user_name=_first(payload, "user.name", "user_name"),
        host_name=_first(payload, "host.name", "host_name"),
        event_count=_int(_first(payload, "alert.deduplicated_count", "event_count")),
        related_events=related,
    )


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class HisiemEvaluationReader:
    """Owns one httpx.AsyncClient; tenant-scoped bounded reads over HISIEM.

    Not a production ``HisiemPort`` implementation — the evaluation package may
    depend on production public contracts/adapters, never the reverse.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        settings: HisiemSettings | None = None,
        base_url: str | None = None,
        bearer_token: str | None = None,
        timeout_seconds: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id must be a non-empty string")
        self._tenant_id = tenant_id
        resolved_url = base_url or (settings.base_url if settings else None) or ""
        if resolved_url == "":
            resolved_url = HisiemSettings().base_url
        self._base_url = resolved_url.rstrip("/")
        resolved_timeout = timeout_seconds or (settings.timeout_seconds if settings else None)
        timeout = resolved_timeout or 10.0
        if bearer_token is not None:
            bearer = bearer_token
        elif settings is not None:
            bearer = settings.bearer_token
        else:
            bearer = ""
        headers: dict[str, str] = {}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout),
            headers=headers,
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    @property
    def base_url(self) -> str:
        return self._base_url

    async def ping(self) -> bool:
        """Reachability probe against the HISIEM control surface (E1-B.3 §10.1)."""
        try:
            response = await self._client.get(
                _READY_PATH, headers={"X-Tenant-ID": self._tenant_id}
            )
        except httpx.HTTPError:
            return False
        return response.status_code < 500

    async def get_rule_contract(self, rule_id: str) -> RuleContract | None:
        """Read the RAW rule payload and keep threshold/window/keyField/condition.

        Uses a dedicated request rather than the production ``DetectionRuleContext``
        read, whose mapper DISCARDS those fields. A 404 returns ``None``;
        transport/HTTP failures raise ``ExternalServiceError``.
        """
        payload = await self._request("GET", f"/api/detection-rules/{rule_id}")
        if payload is None:
            return None
        contract = map_rule_contract(payload)
        return contract if contract is not None and contract.rule_id else None

    async def search_events(
        self,
        *,
        from_: datetime | str,
        to: datetime | str,
        conditions: list[dict[str, object]],
        size: int = _PAGE_SIZE,
    ) -> list[FoundEvent]:
        """Bounded HISIEM log-search returning exact ``_index``/``_id`` events."""
        response = await self._request(
            "POST",
            "/api/log-search",
            json={
                "from": _rfc3339_utc(from_),
                "to": _rfc3339_utc(to),
                "page": 0,
                "size": size,
                "sort": "desc",
                "logic": "AND",
                "conditions": conditions,
            },
        )
        items = response.get("items") if response is not None else None
        if not isinstance(items, list):
            return []
        found: list[FoundEvent] = []
        for item in items:
            hit = _map_found_event(item)
            if hit is not None:
                found.append(hit)
        return found

    async def list_alerts(
        self,
        *,
        status: str | None = None,
        size: int = _PAGE_SIZE,
    ) -> list[FoundAlert]:
        """List alerts via the HISIEM list endpoint, client-side filtered.

        The list endpoint only supports status+size server-side filtering (no
        rule/entity/time filter), so candidates are returned and the caller
        filters by rule_id + attack entity + bounded time (E1-B.3 §14).
        """
        params: dict[str, str | int] = {"size": min(size, 200)}
        if status is not None and status != "":
            params["status"] = status
        response = await self._request("GET", "/api/alerts", params=params)
        items = response.get("items") if response is not None else None
        if not isinstance(items, list):
            return []
        alerts: list[FoundAlert] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            alert = map_found_alert(item)
            if alert is not None:
                alerts.append(alert)
        return alerts

    async def get_alert(self, address_id: str) -> FoundAlert | None:
        """Read one alert by its real ES addressing ``_id`` (E1-B.3 §14)."""
        payload = await self._request("GET", f"/api/alerts/{address_id}")
        if payload is None:
            return None
        return map_found_alert(payload)

    async def wait_for_event(
        self,
        *,
        logical_role: str,
        from_: datetime,
        to: datetime,
        conditions: list[dict[str, object]],
        deadline: datetime,
        interval: float = 2.0,
        size: int = 20,
    ) -> ResolvedEvent:
        """Bounded poll for EXACTLY one matching event; 0/1/>1 semantics.

        Returns a contracts-level :class:`ResolvedEvent` preserving the provider
        ``_index``/``_id``. ``>1`` matches raise ``AmbiguousEventError``; the
        deadline expiring with 0 matches raises ``EventResolutionTimeout``.
        """
        while True:
            found = await self.search_events(
                from_=from_, to=to, conditions=conditions, size=size
            )
            if len(found) > 1:
                raise AmbiguousEventError(
                    f"log-search for logical role {logical_role!r} returned "
                    f"{len(found)} matches; expected exactly one"
                )
            if len(found) == 1:
                return _to_resolved_event(logical_role, found[0])
            if _now() >= deadline:
                raise EventResolutionTimeout(
                    f"event for logical role {logical_role!r} did not resolve "
                    "within the bounded deadline"
                )
            await asyncio.sleep(interval)

    async def wait_for_alert(
        self,
        *,
        attack_source_ip: str,
        deadline: datetime,
        interval: float = 2.0,
        stable_reads: int = 3,
    ) -> ResolvedAlert:
        """Bounded poll for a STABLE, unambiguous HISIEM alert (E1-B.3 §14, §15).

        The list API is read and candidates filtered client-side by rule + attack
        entity; a single candidate is re-read by its real ``_id``. Ambiguity is
        NEVER resolved by newest/highest-risk/first.

        Stability barrier (§15): the first visible alert may still be dedup-updating
        inside the detection pipeline, so the SAME candidate must present an
        IDENTICAL fingerprint for ``stable_reads`` consecutive observations (1s
        apart). If stability cannot be established before the deadline the run is
        refused with ``AlertNotStableError`` — the dataset is NOT verified/sealed.
        """
        observed: list[FoundAlert] = []
        consecutive = 0
        while True:
            alerts = await self.list_alerts(size=_PAGE_SIZE)
            candidates = [
                a
                for a in alerts
                if a.rule_id == _GP01_RULE_ID
                and (a.entity == attack_source_ip or a.source_ip == attack_source_ip)
            ]
            if len(candidates) > 1:
                raise AmbiguousSourceAlertError(
                    f"alert resolution found {len(candidates)} candidate alerts for "
                    f"attack source {attack_source_ip}; expected exactly one"
                )
            if len(candidates) == 1:
                detail = await self.get_alert(candidates[0].address_id)
                if detail is None:
                    detail = candidates[0]
                if not observed or detail.fingerprint == observed[-1].fingerprint:
                    consecutive += 1
                else:
                    consecutive = 1
                observed.append(detail)
                if consecutive >= stable_reads:
                    return _to_resolved_alert(detail)
            else:
                consecutive = 0
                observed = []
            if _now() >= deadline:
                if observed:
                    raise AlertNotStableError(
                        f"alert for attack source {attack_source_ip} never became "
                        f"stable ({consecutive}/{stable_reads} identical reads before "
                        "the deadline); refusing to verify an unstable alert"
                    )
                raise AlertResolutionTimeout(
                    "no candidate HISIEM alert resolved for the GP-01 attack source "
                    "within the bounded deadline"
                )
            await asyncio.sleep(interval)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any] | None:
        from ..application.errors import ExternalServiceError

        headers = {"X-Tenant-ID": self._tenant_id}
        try:
            response = await self._client.request(
                method, url, headers=headers, json=json, params=params
            )
        except httpx.HTTPError as exc:
            raise ExternalServiceError(
                f"HISIEM request failed: {exc.__class__.__name__}",
                service="hisiem",
            ) from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"HISIEM returned HTTP {response.status_code}",
                service="hisiem",
                code=f"HTTP_{response.status_code}",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ExternalServiceError(
                "HISIEM returned a non-JSON body",
                service="hisiem",
                code="INVALID_RESPONSE",
            ) from exc
        if not isinstance(body, dict):
            raise ExternalServiceError(
                "HISIEM returned a non-object JSON body",
                service="hisiem",
                code="INVALID_RESPONSE",
            )
        return body


# ---------------------------------------------------------------------------
# Private mapping helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _map_found_event(item: object) -> FoundEvent | None:
    """Map one log-search item (flat source + ``_id``/``_index``) to FoundEvent."""
    if not isinstance(item, dict):
        return None
    document_id = str(item.get("_id") or item.get("id") or "")
    if not document_id:
        return None
    return FoundEvent(
        document_id=document_id,
        index=str(item.get("_index") or ""),
        timestamp=_opt(item.get("@timestamp")),
        event_category=_first(item, "event.category", "event_category"),
        event_action=_first(item, "event.action", "event_action"),
        event_outcome=_first(item, "event.outcome", "event_outcome"),
        source_ip=_first(item, "source.ip", "source_ip"),
        user_name=_first(item, "user.name", "user_name"),
        host_name=_first(item, "host.name", "host_name"),
        log_source_id=_first(item, "log.source_id", "log_source_id"),
        message=_first(item, "message"),
    )


def _to_resolved_event(logical_role: str, hit: FoundEvent) -> ResolvedEvent:
    """Build the contracts ResolvedEvent, preserving index/_id + ECS fields."""
    return ResolvedEvent(
        logical_role=logical_role,
        provider=_PROVIDER,
        index=hit.index,
        document_id=hit.document_id,
        timestamp=hit.timestamp or "",
        event_category=hit.event_category or "",
        event_action=hit.event_action or "",
        event_outcome=hit.event_outcome,
        source_ip=hit.source_ip or "",
        user_name=hit.user_name or "",
        host_name=hit.host_name or "",
        log_source_id=hit.log_source_id,
        message_fingerprint=hit.message_fingerprint,
    )


def _to_resolved_alert(alert: FoundAlert) -> ResolvedAlert:
    """Build the contracts ResolvedAlert; address_id is the REAL ES ``_id``."""
    return ResolvedAlert(
        provider=_PROVIDER,
        address_id=alert.address_id,
        business_id=alert.business_id,
        rule_id=alert.rule_id or "",
        rule_name=alert.rule_name,
        entity=alert.entity or alert.source_ip,
        created_at=alert.created_at or "",
        event_count=alert.event_count or 0,
        status=alert.status or "",
        related_event_refs=[
            RelatedEventRef(index=r.index, document_id=r.document_id)
            for r in alert.related_events
        ],
    )


__all__ = [
    "FoundAlert",
    "FoundEvent",
    "HisiemEvaluationReader",
    "RelatedEvent",
    "RuleContract",
    "map_found_alert",
    "map_rule_contract",
]
