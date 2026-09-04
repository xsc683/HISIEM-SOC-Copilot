"""HISIEM HTTP adapter implementing the HisiemPort.

Transport-only over HISIEM's control API (``GET /api/alerts/{id}`` and the
X-Tenant-ID convention). Errors map to ExternalServiceError; upstream bodies never
leak. Real HISIEM response shape discovered from the SIEM control-api module:
the detail endpoint returns a JSON object with string ids and optional fields.
"""

from __future__ import annotations

import httpx

from ...application.errors import ExternalServiceError
from ...application.ports.hisiem import HisiemAlertData, HisiemPort
from ...config import HisiemSettings
from .mapper import map_alert_detail


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
        return map_alert_detail(payload)

    async def search_events(
        self,
        *,
        tenant_id: str,
        query: str,
        time_range_minutes: int = 60,
        size: int = 100,
    ) -> list[dict[str, object]]:
        payload = await self._request(
            "POST",
            "/api/events/search",
            tenant_id=tenant_id,
            json={
                "query": query,
                "time_range_minutes": time_range_minutes,
                "size": size,
            },
        )
        if payload is None:
            return []
        items = payload if isinstance(payload, list) else []
        return [item for item in items if isinstance(item, dict)]

    async def _request(
        self,
        method: str,
        url: str,
        *,
        tenant_id: str,
        json: dict[str, object] | None = None,
    ) -> object | None:
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
            return response.json()  # type: ignore[no-any-return]
        except ValueError as exc:
            raise ExternalServiceError(
                "HISIEM returned a non-JSON body",
                service="hisiem",
                code="INVALID_RESPONSE",
            ) from exc
