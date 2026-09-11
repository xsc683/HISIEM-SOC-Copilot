"""HISIEM SOAR HTTP adapter implementing the SoarPort.

Transport-only over HISIEM's INTERNAL server-to-server SOAR boundary
(``POST/GET /api/internal/soar/executions``). The tenant is sent as
``X-Tenant-ID`` (HISIEM derives it server-side for the caller — never from a
browser), the action is a bounded typed contract, and ``Idempotency-Key`` is the
stable submission key so a retry can never double-execute.

Errors map to :class:`ExternalServiceError`; raw upstream bodies never leak. The
service credential is read from settings, never logged or persisted.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx

from ...application.errors import ExternalServiceError
from ...application.ports.soar import SoarExecutionResult, SoarPort
from ...config import SoarSettings
from ...domain.investigation.value_objects import ExternalResourceRef

_PROVIDER = "hisiem"

# HISIEM SOAR execution statuses → the Copilot execution-ref status vocabulary.
# ``waiting``/``waiting_human`` are non-terminal (still RUNNING from our view).
_STATUS_MAP: dict[str, str] = {
    "pending": "QUEUED",
    "running": "RUNNING",
    "waiting": "RUNNING",
    "waiting_human": "RUNNING",
    "success": "SUCCEEDED",
    "failed": "FAILED",
    "cancelled": "FAILED",
}

# Typed action → the HISIEM request fragment it maps to. Only actions in the V1
# executable allowlist reach here (validated earlier); an unknown key is a
# programming error, never a silent no-op.
_ACTION_PLAYBOOK_KEY = "playbook_id"


class HisiemSoarAdapter(SoarPort):
    """implements the SoarPort over HISIEM's internal SOAR API."""

    def __init__(
        self,
        *,
        settings: SoarSettings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        token = settings.bearer_token.strip()
        if not token:
            # Fail closed: never build a SOAR client without a service credential.
            raise ExternalServiceError(
                "SOAR service credential is not configured",
                service=_PROVIDER,
                code="SOAR_CONFIGURATION",
            )
        self._base_url = settings.base_url.rstrip("/")
        self._token = token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(settings.timeout_seconds),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def submit_execution(
        self,
        *,
        tenant_id: str,
        proposal_id: UUID,
        submission_key: str,
        action_key: str,
        parameters: dict[str, object],
        target_ref: ExternalResourceRef,
    ) -> SoarExecutionResult:
        playbook_id = str(parameters.get(_ACTION_PLAYBOOK_KEY, "")).strip()
        payload = await self._request(
            "POST",
            "/api/internal/soar/executions",
            tenant_id=tenant_id,
            idempotency_key=submission_key,
            json={
                "action_key": action_key,
                "playbook_id": playbook_id,
                "target": {
                    "provider": target_ref.provider,
                    "resource_type": target_ref.resource_type,
                    "address_id": target_ref.address_id,
                },
            },
        )
        return _to_result(payload)

    async def get_execution_status(
        self, *, tenant_id: str, execution_id: str
    ) -> SoarExecutionResult:
        payload = await self._request(
            "GET",
            f"/api/internal/soar/executions/{execution_id}",
            tenant_id=tenant_id,
            idempotency_key=None,
            json=None,
        )
        return _to_result(payload)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        tenant_id: str,
        idempotency_key: str | None,
        json: dict[str, Any] | None,
    ) -> dict[str, Any]:
        headers = {
            "X-Tenant-ID": tenant_id,
            "Authorization": f"Bearer {self._token}",
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = await self._client.request(
                method, url, headers=headers, json=json
            )
        except httpx.HTTPError as exc:
            raise ExternalServiceError(
                f"HISIEM SOAR request failed: {exc.__class__.__name__}",
                service=_PROVIDER,
            ) from exc

        if response.status_code == 404:
            raise ExternalServiceError(
                "HISIEM SOAR execution not found",
                service=_PROVIDER,
                code="SOAR_NOT_FOUND",
            )
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"HISIEM SOAR returned HTTP {response.status_code}",
                service=_PROVIDER,
                code=f"HTTP_{response.status_code}",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ExternalServiceError(
                "HISIEM SOAR returned a non-JSON body",
                service=_PROVIDER,
                code="INVALID_RESPONSE",
            ) from exc
        if not isinstance(body, dict):
            raise ExternalServiceError(
                "HISIEM SOAR returned a non-object JSON body",
                service=_PROVIDER,
                code="INVALID_RESPONSE",
            )
        return body


def _to_result(payload: dict[str, Any]) -> SoarExecutionResult:
    """Map a bounded HISIEM SOAR JSON body to a safe execution result.

    Only whitelisted fields are read; result/error are bounded summaries — never
    the raw upstream object.
    """
    execution_id = str(payload.get("execution_id", "")).strip()
    if not execution_id:
        raise ExternalServiceError(
            "HISIEM SOAR response is missing execution_id",
            service=_PROVIDER,
            code="INVALID_RESPONSE",
        )
    raw_status = str(payload.get("status", "")).strip().lower()
    status = _STATUS_MAP.get(raw_status, "RUNNING")
    safe_result: dict[str, object] = {}
    result = payload.get("result")
    if isinstance(result, dict):
        safe_result = {
            str(k): v
            for k, v in result.items()
            if isinstance(v, (str, int, float, bool)) and len(str(v)) <= 200
        }
    error_code = payload.get("error_code")
    error_message = payload.get("error_message")
    return SoarExecutionResult(
        execution_id=execution_id,
        status=status,
        safe_result=safe_result,
        safe_error_code=str(error_code) if error_code else None,
        safe_error_message=(str(error_message)[:500] if error_message else None),
    )
