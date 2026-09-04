"""HISIEM HTTP adapter implementing the HisiemPort.

Transport-only over HISIEM's control API. Endpoints follow the real HISIEM
contract (verified against the reference SIEM repo):
``GET /api/alerts/{id}``, ``POST /api/log-search``, ``GET /api/detection-rules/{id}``
and the X-Tenant-ID convention. Errors map to ExternalServiceError; upstream
bodies never leak.
"""

from __future__ import annotations

from typing import Any

import httpx

from ...application.errors import ExternalServiceError
from ...application.ports.hisiem import (
    DetectionRuleContext,
    EventSearchResult,
    HisiemAlertData,
    HisiemPort,
)
from ...config import HisiemSettings
from .mapper import (
    map_alert_detail,
    map_detection_rule,
    map_log_search_response,
)


class HisiemHttpAdapter(HisiemPort):
    """implements the HisiemPort over HISIEM's HTTP API."""

    def __init__(
        self,
        *,
        settings: HisiemSettings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = settings.base_url.rstrip("/")
        self._timeout = httpx.Timeout(settings.timeout_seconds)
        headers: dict[str, str] = {}
        if settings.bearer_token:
            headers["Authorization"] = f"Bearer {settings.bearer_token}"
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url, timeout=self._timeout, headers=headers
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_alert(
        self, *, tenant_id: str, alert_id: str
    ) -> HisiemAlertData | None:
        payload = await self._request(
            "GET",
            f"/api/alerts/{alert_id}",
            tenant_id=tenant_id,
        )
        if payload is None:
            return None
        return map_alert_detail(payload, tenant_id=tenant_id)

    async def search_events(
        self,
        *,
        tenant_id: str,
        from_: str,
        to: str,
        conditions: list[dict[str, object]],
        limit: int = 100,
        sort: str = "desc",
    ) -> EventSearchResult:
        payload = await self._request(
            "POST",
            "/api/log-search",
            tenant_id=tenant_id,
            json={
                "from": from_,
                "to": to,
                "logic": "AND",
                "conditions": conditions,
                "page": 0,
                "size": limit,
                "sort": sort,
            },
        )
        if payload is None:
            return EventSearchResult(
                items=[], total=0, returned=0, from_=from_, to=to, truncated=False
            )
        items = map_log_search_response(payload)
        total = _int(payload.get("total")) if isinstance(payload, dict) else 0
        return EventSearchResult(
            items=items,
            total=total or len(items),
            returned=len(items),
            from_=from_,
            to=to,
            took_ms=_int(payload.get("tookMs")) if isinstance(payload, dict) else None,
            truncated=(total or len(items)) > len(items),
        )

    async def get_detection_rule(
        self, *, tenant_id: str, rule_id: str
    ) -> DetectionRuleContext | None:
        payload = await self._request(
            "GET",
            f"/api/detection-rules/{rule_id}",
            tenant_id=tenant_id,
        )
        if payload is None:
            return None
        return map_detection_rule(payload, rule_id=rule_id)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        tenant_id: str,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        try:
            response = await self._client.request(
                method,
                url,
                headers={"X-Tenant-ID": tenant_id},
                json=json,
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


def _int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
