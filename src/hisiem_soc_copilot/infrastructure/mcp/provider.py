"""Read-only MCP ToolProvider backed by the official high-level client.

MCP is an external capability adapter.  This module owns transport, discovery,
and safe provider-result normalization only; registry admission, policy, budget,
audit, and Evidence remain in the existing application path.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from typing import Any, cast
from urllib.parse import SplitResult, urlsplit

import httpx2
from mcp import Client, InputRequiredRoundsExceededError
from mcp.client.streamable_http import streamable_http_client

from ...agent.tools.providers import (
    AdmissionEntry,
    ProviderCapability,
    ProviderFailure,
    ProviderFailureCode,
    ProviderIdentity,
    ProviderInvocationContext,
    ProviderInvocationResult,
    ProviderResultStatus,
    ResultBounds,
    external_schema_fingerprint,
    item_count,
    json_depth,
    serialized_size,
)
from ...config import MCPAdmissionSettings, MCPResultBoundsSettings, MCPServerSettings
from ...contracts.tools.types import ToolCandidate

PRODUCTION_PROTOCOL = "2026-07-28"

# These names are runtime-owned.  A model may not use them to change routing,
# tenant scope, or credential selection.
_PROTECTED_ARGUMENTS = frozenset(
    {
        "tenant_id",
        "server_id",
        "endpoint",
        "scheme",
        "host",
        "port",
        "transport",
        "credential",
        "token",
        "password",
        "passphrase",
        "secret",
        "api_key",
        "access_token",
        "authorization",
        "bearer",
    }
)
_FAILURE_CODES = frozenset(
    {
        "TIMEOUT",
        "UNAVAILABLE",
        "AUTH_FAILURE",
        "RATE_LIMITED",
        "REMOTE_TOOL_ERROR",
        "PROTOCOL_ERROR",
        "UNSUPPORTED_INTERACTION",
        "SCHEMA_MISMATCH",
        "INVALID_RESULT",
        "RESULT_TOO_LARGE",
        "PROVIDER_ERROR",
    }
)


class MCPProviderError(RuntimeError):
    """Internal provider error with a safe typed category."""

    def __init__(self, code: str, message: str = "provider operation failed") -> None:
        super().__init__(message)
        self.code: ProviderFailureCode = _failure_code(code)


ClientFactory = Callable[[MCPServerSettings], Any]


def admission_from_settings(
    value: AdmissionEntry | MCPAdmissionSettings,
) -> AdmissionEntry:
    """Convert trusted pydantic settings to provider-neutral admission data."""
    if isinstance(value, AdmissionEntry):
        return value
    bounds = value.result_bounds
    return AdmissionEntry(
        internal_name=value.internal_name,
        description=value.description,
        server_id=value.server_id,
        external_name=value.external_name,
        input_schema=dict(value.input_schema),
        result_schema=(
            dict(value.result_schema) if value.result_schema is not None else None
        ),
        expected_external_schema_fingerprint=value.expected_external_schema_fingerprint,
        risk=value.risk,
        tenant_scope=value.tenant_scope,
        model_selectable=value.model_selectable,
        result_type=value.result_type,
        result_bounds=ResultBounds(
            timeout_seconds=bounds.timeout_seconds,
            max_items=bounds.max_items,
            max_serialized_bytes=bounds.max_serialized_bytes,
            max_text_chars=bounds.max_text_chars,
            max_depth=bounds.max_depth,
        ),
    )


def bounds_from_settings(
    value: ResultBounds | MCPResultBoundsSettings,
) -> ResultBounds:
    if isinstance(value, ResultBounds):
        return value
    return ResultBounds(
        timeout_seconds=value.timeout_seconds,
        max_items=value.max_items,
        max_serialized_bytes=value.max_serialized_bytes,
        max_text_chars=value.max_text_chars,
        max_depth=value.max_depth,
    )


class MCPToolProvider:
    """One configured MCP server and its explicit read-only admission manifest."""

    def __init__(
        self,
        *,
        server: MCPServerSettings,
        admissions: Iterable[AdmissionEntry | MCPAdmissionSettings],
        global_bounds: ResultBounds | MCPResultBoundsSettings,
        client_factory: ClientFactory | None = None,
    ) -> None:
        if server.protocol_version != PRODUCTION_PROTOCOL:
            raise ValueError("MCP legacy/unsupported protocol is disabled")

        self.server = server
        self._identity = ProviderIdentity(
            provider_type="mcp",
            server_id=server.server_id,
            server_category=server.server_category,
        )
        self._admissions = tuple(admission_from_settings(item) for item in admissions)
        self._admission_by_name = {
            admission.internal_name: admission for admission in self._admissions
        }
        if len(self._admission_by_name) != len(self._admissions):
            raise ValueError("duplicate MCP admission name")
        if any(
            admission.server_id != server.server_id
            for admission in self._admissions
        ):
            raise ValueError("MCP admission references a different configured server")

        self._global_bounds = bounds_from_settings(global_bounds)
        self._global_bounds.validate()
        for admission in self._admissions:
            admission.result_bounds.validate(self._global_bounds)
            if admission.risk != "READ_ONLY" and admission.model_selectable:
                raise ValueError(
                    "write/high-risk capability cannot be model-selectable"
                )

        self._client_factory = client_factory
        self._catalog: dict[str, ProviderCapability] = {}
        self._available: dict[str, ProviderCapability] = {}
        self._unavailable: dict[str, ProviderFailure] = {}
        self._discovery_failure: ProviderFailure | None = None
        self._validate_endpoint()

    @property
    def identity(self) -> ProviderIdentity:
        return self._identity

    @property
    def admissions(self) -> tuple[AdmissionEntry, ...]:
        return self._admissions

    @property
    def catalog(self) -> tuple[ProviderCapability, ...]:
        """The latest complete raw catalog; dynamic tools remain unadmitted."""
        return tuple(self._catalog.values())

    @property
    def unavailable(self) -> Mapping[str, ProviderFailure]:
        return dict(self._unavailable)

    @property
    def discovery_failure(self) -> ProviderFailure | None:
        return self._discovery_failure

    async def start(self) -> None:
        """Run mandatory startup discovery and admission revalidation."""
        await self.refresh()

    async def close(self) -> None:
        """Per-invocation clients own their transport resources."""
        return None

    async def refresh(self) -> list[ProviderCapability]:
        """Full rediscovery; never mutate the model-visible admission manifest."""
        try:
            discovered = await self.discover()
        except MCPProviderError as exc:
            failure = ProviderFailure(exc.code, "MCP capability discovery failed")
            self._discovery_failure = failure
            self._catalog = {}
            self._available = {}
            self._unavailable = {
                admission.internal_name: failure for admission in self._admissions
            }
            return []

        self._discovery_failure = None
        self._catalog = {cap.external_name: cap for cap in discovered}
        self._available = {}
        self._unavailable = {}
        for admission in self._admissions:
            observed = self._catalog.get(admission.external_name)
            if observed is None:
                self._unavailable[admission.internal_name] = ProviderFailure(
                    "UNAVAILABLE", "admitted capability is not currently exposed"
                )
                continue
            expected = admission.expected_external_schema_fingerprint
            if not expected or observed.schema_fingerprint != expected:
                self._unavailable[admission.internal_name] = ProviderFailure(
                    "SCHEMA_MISMATCH", "external capability schema changed"
                )
                continue
            self._available[admission.internal_name] = observed
        return discovered

    async def discover(self) -> list[ProviderCapability]:
        """Collect every page returned by ``list_tools``."""
        discovered: list[ProviderCapability] = []
        try:
            async with self._connected_client() as client:
                _require_protocol(client)
                cursor: str | None = None
                while True:
                    page = await client.list_tools(cursor=cursor)
                    if _is_input_required(page):
                        raise MCPProviderError(
                            "UNSUPPORTED_INTERACTION",
                            "interactive MCP discovery is unsupported",
                        )
                    tools = _attribute(page, "tools")
                    if not isinstance(tools, list):
                        raise MCPProviderError(
                            "PROTOCOL_ERROR", "malformed MCP tool catalog"
                        )
                    for raw_tool in tools:
                        discovered.append(_normalize_capability(raw_tool, self.identity))
                    next_cursor = _attribute(page, "nextCursor", "next_cursor")
                    if next_cursor is None:
                        break
                    if not isinstance(next_cursor, str) or not next_cursor:
                        raise MCPProviderError(
                            "PROTOCOL_ERROR", "malformed MCP pagination cursor"
                        )
                    cursor = next_cursor
        except MCPProviderError:
            raise
        except TimeoutError:
            raise MCPProviderError("TIMEOUT", "MCP operation timed out") from None
        except httpx2.TimeoutException:
            raise MCPProviderError("TIMEOUT", "MCP operation timed out") from None
        except httpx2.HTTPStatusError as exc:
            raise MCPProviderError(
                _status_code(exc.response.status_code),
                "MCP server rejected the request",
            ) from None
        except httpx2.HTTPError:
            raise MCPProviderError("UNAVAILABLE", "MCP server is unavailable") from None
        except Exception:
            raise MCPProviderError("PROVIDER_ERROR", "MCP discovery failed") from None
        return discovered

    async def invoke(
        self,
        *,
        admission: AdmissionEntry,
        candidate: ToolCandidate,
        context: ProviderInvocationContext,
    ) -> ProviderInvocationResult:
        """Invoke only a currently available explicitly admitted capability."""
        if (
            admission.server_id != self.server.server_id
            or admission.internal_name not in self._admission_by_name
        ):
            return self._failure(
                admission.internal_name,
                "PROTOCOL_ERROR",
                "untrusted provider admission",
            )
        if admission.internal_name not in self._available:
            failure = self._unavailable.get(
                admission.internal_name,
                ProviderFailure("UNAVAILABLE", "capability is unavailable"),
            )
            return self._result_failure(admission.internal_name, failure)

        try:
            external_arguments = self._build_external_arguments(
                admission, candidate.arguments, context
            )
            async with self._connected_client() as client:
                _require_protocol(client)
                timeout = min(
                    admission.result_bounds.timeout_seconds,
                    self._global_bounds.timeout_seconds,
                    self.server.timeout_seconds,
                )
                remote = await asyncio.wait_for(
                    client.call_tool(
                        admission.external_name,
                        external_arguments,
                        read_timeout_seconds=timeout,
                    ),
                    timeout=timeout,
                )
            return self._process_result(admission, remote)
        except MCPProviderError as exc:
            return self._failure(
                admission.internal_name, exc.code, "MCP provider operation failed"
            )
        except InputRequiredRoundsExceededError:
            return self._failure(
                admission.internal_name,
                "UNSUPPORTED_INTERACTION",
                "interactive MCP result is unsupported",
            )
        except TimeoutError:
            return self._failure(
                admission.internal_name, "TIMEOUT", "MCP operation timed out"
            )
        except httpx2.TimeoutException:
            return self._failure(
                admission.internal_name, "TIMEOUT", "MCP operation timed out"
            )
        except httpx2.HTTPStatusError as exc:
            return self._failure(
                admission.internal_name,
                _status_code(exc.response.status_code),
                "MCP server rejected the request",
            )
        except httpx2.HTTPError:
            return self._failure(
                admission.internal_name, "UNAVAILABLE", "MCP server is unavailable"
            )
        except (TypeError, ValueError):
            return self._failure(
                admission.internal_name,
                "INVALID_RESULT",
                "MCP request or result was invalid",
            )
        except Exception:
            return self._failure(
                admission.internal_name,
                "PROVIDER_ERROR",
                "MCP provider operation failed",
            )

    def _validate_endpoint(self) -> None:
        parsed = urlsplit(self.server.endpoint)
        if not parsed.hostname:
            raise ValueError("MCP endpoint must contain a host")
        if parsed.scheme != "https" and not self.server.trusted_internal_transport:
            raise ValueError("remote MCP endpoint must use HTTPS")
        allowed_hosts = set(self.server.allowed_hosts) or {parsed.hostname}
        if parsed.hostname not in allowed_hosts:
            raise ValueError("MCP endpoint host is outside the trusted allowlist")
        if parsed.username or parsed.password:
            raise ValueError("MCP endpoint must not contain credentials")

    def _build_external_arguments(
        self,
        admission: AdmissionEntry,
        arguments: Mapping[str, object],
        context: ProviderInvocationContext,
    ) -> dict[str, object]:
        if not isinstance(arguments, Mapping):
            raise MCPProviderError("INVALID_RESULT", "tool arguments were not an object")
        if any(key.lower() in _PROTECTED_ARGUMENTS for key in arguments):
            raise MCPProviderError(
                "PROVIDER_ERROR", "protected runtime argument supplied by model"
            )
        if admission.tenant_scope == "TENANT_SCOPED" and not context.tenant_id:
            raise MCPProviderError(
                "PROVIDER_ERROR", "trusted tenant context is required"
            )

        # The model-facing/internal schema is deliberately separate from the
        # observed external schema.  Validate model arguments against the former,
        # then inject tenant context only when the trusted remote schema requires
        # that protected field and validate the resulting wire arguments against
        # the latter.
        _validate_schema(arguments, admission.input_schema)
        external = dict(arguments)
        observed = self._available.get(admission.internal_name)
        external_schema = observed.input_schema if observed is not None else {}
        properties = external_schema.get("properties")
        required = external_schema.get("required", [])
        if (
            admission.tenant_scope == "TENANT_SCOPED"
            and isinstance(properties, Mapping)
            and isinstance(required, list)
            and "tenant_id" in required
            and "tenant_id" in properties
        ):
            external["tenant_id"] = context.tenant_id
        _validate_schema(external, external_schema)
        return external

    def _process_result(
        self, admission: AdmissionEntry, remote: object
    ) -> ProviderInvocationResult:
        # Check protocol/result shape before trusting any structured content.
        if _is_input_required(remote) or _is_task_result(remote):
            return self._failure(
                admission.internal_name,
                "UNSUPPORTED_INTERACTION",
                "interactive MCP result is unsupported",
            )
        if remote is None or not hasattr(remote, "is_error"):
            return self._failure(
                admission.internal_name, "PROTOCOL_ERROR", "malformed MCP result"
            )
        if bool(_attribute(remote, "is_error")):
            return self._failure(
                admission.internal_name,
                "REMOTE_TOOL_ERROR",
                "remote tool returned an error",
            )

        content = _attribute(remote, "content")
        if content is not None:
            if not isinstance(content, list):
                return self._failure(
                    admission.internal_name, "INVALID_RESULT", "malformed MCP content"
                )
            for item in content:
                kind = str(
                    _attribute(item, "type") or item.__class__.__name__
                ).lower()
                if kind not in {"text", "textcontent"}:
                    return self._failure(
                        admission.internal_name,
                        "INVALID_RESULT",
                        "unsupported MCP content type",
                    )
                text = _attribute(item, "text")
                if not isinstance(text, str):
                    return self._failure(
                        admission.internal_name,
                        "INVALID_RESULT",
                        "malformed text content",
                    )
                if len(text) > self._effective_bounds(admission).max_text_chars:
                    return self._failure(
                        admission.internal_name,
                        "RESULT_TOO_LARGE",
                        "MCP result exceeds bounds",
                    )

        structured = _attribute(remote, "structuredContent", "structured_content")
        if structured is not None:
            if admission.result_type != "structured" or not isinstance(
                structured, Mapping
            ):
                return self._failure(
                    admission.internal_name,
                    "INVALID_RESULT",
                    "structured result violates the admitted contract",
                )
            data: dict[str, object] = dict(structured)
            if admission.result_schema is not None:
                try:
                    _validate_schema(data, admission.result_schema)
                except (TypeError, ValueError):
                    return self._failure(
                        admission.internal_name,
                        "INVALID_RESULT",
                        "structured result violates the admitted contract",
                    )
            successful_status: ProviderResultStatus = "SUCCESS"
        else:
            if admission.result_type != "text":
                return self._failure(
                    admission.internal_name,
                    "INVALID_RESULT",
                    "structured result is required",
                )
            texts = []
            if isinstance(content, list):
                texts = [str(_attribute(item, "text")) for item in content]
            data = {"text": "\n".join(texts)}
            successful_status = "SUCCESS" if texts and any(texts) else "NO_DATA"

        bounds = self._effective_bounds(admission)
        try:
            if serialized_size(data) > bounds.max_serialized_bytes:
                raise MCPProviderError("RESULT_TOO_LARGE")
            if item_count(data) > bounds.max_items or json_depth(data) > bounds.max_depth:
                raise MCPProviderError("RESULT_TOO_LARGE")
        except (TypeError, ValueError):
            return self._failure(
                admission.internal_name, "INVALID_RESULT", "MCP result is not JSON data"
            )
        except MCPProviderError as exc:
            return self._failure(
                admission.internal_name, exc.code, "MCP result exceeds bounds"
            )

        observed = self._available.get(admission.internal_name)
        return ProviderInvocationResult(
            tool_name=admission.internal_name,
            status=successful_status,
            data=data,
            provider=self.identity,
            schema_fingerprint=(observed.schema_fingerprint if observed else None),
        )

    def _effective_bounds(self, admission: AdmissionEntry) -> ResultBounds:
        return ResultBounds(
            timeout_seconds=min(
                admission.result_bounds.timeout_seconds,
                self._global_bounds.timeout_seconds,
            ),
            max_items=min(admission.result_bounds.max_items, self._global_bounds.max_items),
            max_serialized_bytes=min(
                admission.result_bounds.max_serialized_bytes,
                self._global_bounds.max_serialized_bytes,
            ),
            max_text_chars=min(
                admission.result_bounds.max_text_chars,
                self._global_bounds.max_text_chars,
            ),
            max_depth=min(admission.result_bounds.max_depth, self._global_bounds.max_depth),
        )

    def _failure(
        self, tool_name: str, code: str, message: str
    ) -> ProviderInvocationResult:
        return self._result_failure(tool_name, ProviderFailure(_failure_code(code), message))

    def _result_failure(
        self, tool_name: str, failure: ProviderFailure
    ) -> ProviderInvocationResult:
        rejected = {
            "PROVIDER_ERROR",
            "INVALID_RESULT",
            "PROTOCOL_ERROR",
            "SCHEMA_MISMATCH",
            "UNSUPPORTED_INTERACTION",
            "REMOTE_TOOL_ERROR",
            "RESULT_TOO_LARGE",
            "AUTH_FAILURE",
        }
        status: ProviderResultStatus = (
            "REJECTED" if failure.code in rejected else "UNAVAILABLE"
        )
        return ProviderInvocationResult(
            tool_name=tool_name,
            status=status,
            failure=failure,
            provider=self.identity,
        )

    @asynccontextmanager
    async def _connected_client(self) -> AsyncIterator[Any]:
        if self._client_factory is not None:
            value = self._client_factory(self.server)
            if inspect.isawaitable(value):
                value = await value
            if hasattr(value, "__aenter__") and hasattr(value, "__aexit__"):
                async with value as client:
                    yield client
            else:
                yield value
            return

        token = (
            os.environ.get(self.server.auth_token_env, "")
            if self.server.auth_token_env
            else ""
        )
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        expected = urlsplit(self.server.endpoint)
        async with httpx2.AsyncClient(
            headers=headers,
            timeout=self.server.timeout_seconds,
            follow_redirects=True,
            event_hooks={"response": [_response_boundary(expected, self.server.allowed_hosts)]},
        ) as http_client:
            # The official helper is an async context manager that yields the
            # stream pair consumed by the high-level Client. Keep that resource
            # boundary intact while supplying the configured httpx2 client.
            transport = _streamable_http_transport(
                self.server.endpoint,
                http_client,
            )
            # Explicit mode prevents the SDK's automatic legacy negotiation from
            # silently admitting a pre-v1.1 protocol server.
            async with Client(
                transport,
                mode=PRODUCTION_PROTOCOL,
                read_timeout_seconds=self.server.timeout_seconds,
                # Stage C V1 never drives sampling or elicitation.  A returned
                # InputRequiredResult is therefore rejected immediately.
                input_required_max_rounds=0,
            ) as client:
                yield client


@asynccontextmanager
async def _streamable_http_transport(
    endpoint: str, http_client: httpx2.AsyncClient
) -> AsyncIterator[tuple[Any, Any]]:
    """Adapt the official Streamable HTTP helper to high-level Client."""
    async with streamable_http_client(endpoint, http_client=http_client) as streams:
        yield streams


def _normalize_capability(
    raw_tool: object, provider: ProviderIdentity
) -> ProviderCapability:
    name = _attribute(raw_tool, "name")
    input_schema = _attribute(raw_tool, "inputSchema", "input_schema")
    output_schema = _attribute(raw_tool, "outputSchema", "output_schema")
    if not isinstance(name, str) or not name:
        raise MCPProviderError("PROTOCOL_ERROR", "malformed MCP tool metadata")
    normalized_input = dict(input_schema) if isinstance(input_schema, Mapping) else {}
    normalized_output = (
        dict(output_schema) if isinstance(output_schema, Mapping) else None
    )
    return ProviderCapability(
        external_name=name,
        input_schema=normalized_input,
        output_schema=normalized_output,
        provider=provider,
        description=_safe_description(_attribute(raw_tool, "description")),
        # Annotations remain diagnostic data and never determine read-only status.
        annotations={},
        schema_fingerprint=external_schema_fingerprint(
            name, normalized_input, normalized_output
        ),
    )


def _require_protocol(client: object) -> None:
    negotiated = _attribute(client, "protocol_version", "protocolVersion")
    # High-level Client is constructed with an exact mode.  Older/fake clients may
    # not expose negotiated metadata, in which case the explicit mode remains the
    # admission boundary; any reported mismatch is rejected.
    if negotiated is not None and negotiated != PRODUCTION_PROTOCOL:
        raise MCPProviderError("PROTOCOL_ERROR", "unsupported MCP protocol")


def _attribute(value: object, *names: str) -> object:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _safe_description(value: object) -> str:
    return value.strip()[:512] if isinstance(value, str) else ""


def _origin(value: object) -> tuple[str, str, int | None]:
    parsed: Any = value if hasattr(value, "scheme") else urlsplit(str(value))
    scheme = str(parsed.scheme).lower()
    # ``urllib.parse.SplitResult`` exposes ``hostname`` while httpx2 ``URL``
    # exposes ``host``; normalize both without retaining URL objects.
    host = str(
        getattr(parsed, "hostname", None) or getattr(parsed, "host", "")
    ).lower()
    port = getattr(parsed, "port", None)
    if port is None and scheme == "https":
        port = 443
    elif port is None and scheme == "http":
        port = 80
    return scheme, host, port


def _response_boundary(
    expected: SplitResult, allowed_hosts: list[str]
) -> Callable[[httpx2.Response], Any]:
    trusted_hosts = set(allowed_hosts)
    if not trusted_hosts and expected.hostname:
        trusted_hosts.add(expected.hostname)
    expected_origin = _origin(expected)

    async def guard(response: httpx2.Response) -> None:
        actual_origin = _origin(response.url)
        actual_host = str(response.url.host or "").lower()
        if actual_host not in trusted_hosts or actual_origin != expected_origin:
            raise MCPProviderError("PROVIDER_ERROR", "MCP endpoint boundary changed")

    return guard


def _failure_code(code: str) -> ProviderFailureCode:
    return cast(ProviderFailureCode, code) if code in _FAILURE_CODES else "PROVIDER_ERROR"


def _status_code(status: int) -> ProviderFailureCode:
    if status in (401, 403):
        return "AUTH_FAILURE"
    if status == 429:
        return "RATE_LIMITED"
    if 500 <= status <= 599:
        return "UNAVAILABLE"
    return "PROVIDER_ERROR"


def _is_input_required(value: object) -> bool:
    kind = str(
        _attribute(value, "resultType", "result_type", "type")
        or value.__class__.__name__
    ).lower()
    return "inputrequired" in kind or "input_required" in kind


def _is_task_result(value: object) -> bool:
    return (
        _attribute(value, "task", "task_id", "taskId") is not None
        or "taskresult" in value.__class__.__name__.lower()
    )


def _validate_schema(value: object, schema: Mapping[str, object]) -> None:
    if not schema:
        return
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(value, Mapping):
            raise ValueError("schema requires an object")
        required = schema.get("required", [])
        if isinstance(required, list) and any(
            name not in value for name in required if isinstance(name, str)
        ):
            raise ValueError("required schema property is missing")
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            if schema.get("additionalProperties") is False and any(
                key not in properties for key in value
            ):
                raise ValueError("unexpected schema property")
            for key, child in properties.items():
                if key in value and isinstance(child, Mapping):
                    _validate_schema_type(value[key], child)
    elif schema_type == "array":
        if not isinstance(value, list):
            raise ValueError("schema requires an array")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for item in value:
                _validate_schema_type(item, items)
    elif schema_type:
        _validate_schema_type(value, schema)


def _validate_schema_type(value: object, schema: Mapping[str, object]) -> None:
    expected = schema.get("type")
    valid = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if isinstance(expected, str) and not valid.get(expected, True):
        raise ValueError("schema type mismatch")
    if expected in {"object", "array"}:
        _validate_schema(value, schema)
