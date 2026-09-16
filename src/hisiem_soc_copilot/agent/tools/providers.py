"""Provider-neutral contracts for the unified Tool Provider architecture.

The agent sees only the existing ``ToolCandidate``/``ToolResult`` contracts. The
provider contracts in this module are an internal boundary used by the executor;
external protocol objects, credentials, and raw provider exceptions never cross it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ...contracts.tools.types import ToolCandidate

ProviderFailureCode = Literal[
    "TIMEOUT", "UNAVAILABLE", "AUTH_FAILURE", "RATE_LIMITED",
    "REMOTE_TOOL_ERROR", "PROTOCOL_ERROR", "UNSUPPORTED_INTERACTION",
    "SCHEMA_MISMATCH", "INVALID_RESULT", "RESULT_TOO_LARGE", "PROVIDER_ERROR",
]
ProviderResultStatus = Literal["SUCCESS", "NO_DATA", "REJECTED", "UNAVAILABLE"]
TenantScope = Literal["GLOBAL_READ_ONLY", "TENANT_SCOPED"]
RiskClass = Literal["READ_ONLY", "HIGH_RISK", "WRITE"]


@dataclass(frozen=True)
class ResultBounds:
    """Hard rejection limits; no limit is a truncation instruction."""

    timeout_seconds: float = 30.0
    max_items: int = 100
    max_serialized_bytes: int = 256_000
    max_text_chars: int = 32_000
    max_depth: int = 8

    def validate(self, global_bounds: ResultBounds | None = None) -> None:
        if self.timeout_seconds <= 0 or self.max_items < 1 or self.max_serialized_bytes < 1:
            raise ValueError("result bounds must be positive")
        if self.max_text_chars < 1 or self.max_depth < 1:
            raise ValueError("result bounds must be positive")
        if global_bounds is not None:
            for field_name in (
                "timeout_seconds", "max_items", "max_serialized_bytes",
                "max_text_chars", "max_depth",
            ):
                if getattr(self, field_name) > getattr(global_bounds, field_name):
                    raise ValueError(f"per-capability {field_name} exceeds global bound")


@dataclass(frozen=True)
class ProviderIdentity:
    """Safe provider metadata. ``server_id`` is trusted only from config."""

    provider_type: Literal["native", "mcp"]
    server_id: str | None = None
    server_category: str = "internal"


@dataclass(frozen=True)
class ProviderCapability:
    """A raw, untrusted capability observed during provider discovery."""

    external_name: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    provider: ProviderIdentity
    description: str = ""
    annotations: dict[str, Any] = field(default_factory=dict)
    schema_fingerprint: str = ""

    def with_fingerprint(self) -> ProviderCapability:
        return ProviderCapability(
            external_name=self.external_name,
            input_schema=self.input_schema,
            output_schema=self.output_schema,
            provider=self.provider,
            description=self.description,
            annotations=self.annotations,
            schema_fingerprint=external_schema_fingerprint(
                self.external_name, self.input_schema, self.output_schema
            ),
        )


@dataclass(frozen=True)
class AdmissionEntry:
    """Trusted internal contract for one explicitly admitted capability."""

    internal_name: str
    description: str
    server_id: str
    external_name: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    result_schema: dict[str, Any] | None = None
    expected_external_schema_fingerprint: str = ""
    risk: RiskClass = "READ_ONLY"
    tenant_scope: TenantScope = "GLOBAL_READ_ONLY"
    model_selectable: bool = True
    result_bounds: ResultBounds = field(default_factory=ResultBounds)
    result_type: Literal["structured", "text"] = "structured"

    @property
    def is_model_selectable(self) -> bool:
        return self.model_selectable and self.risk == "READ_ONLY"


@dataclass(frozen=True)
class ProviderInvocationContext:
    """Trusted runtime context injected by the executor, never model input."""

    tenant_id: str
    investigation_id: str | None
    tool_call_id: str
    source_alert_ref: Mapping[str, str]
    # A graph-scheduled call has already reserved one budget slot. ``None`` is
    # used by direct callers that do not participate in graph budgeting.
    budget_remaining: int | None = None
    budget_already_reserved: bool = False


@dataclass(frozen=True)
class ProviderFailure:
    code: ProviderFailureCode
    safe_message: str = "provider operation failed"


@dataclass(frozen=True)
class ProviderInvocationResult:
    """Bounded, provider-neutral result handed to ``ToolExecutor``."""

    tool_name: str
    status: ProviderResultStatus
    data: dict[str, object] = field(default_factory=dict)
    failure: ProviderFailure | None = None
    provider: ProviderIdentity = field(
        default_factory=lambda: ProviderIdentity(provider_type="native")
    )
    schema_fingerprint: str | None = None


class ToolProvider(Protocol):
    """Discovery/invocation adapter; not a policy or authorization authority."""

    @property
    def identity(self) -> ProviderIdentity: ...

    async def discover(self) -> list[ProviderCapability]: ...

    async def invoke(
        self,
        *,
        admission: AdmissionEntry,
        candidate: ToolCandidate,
        context: ProviderInvocationContext,
    ) -> ProviderInvocationResult: ...


def canonical_json(value: object) -> str:
    """Canonical JSON representation used only for schema fingerprints."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def external_schema_fingerprint(
    external_name: str,
    input_schema: Mapping[str, object],
    output_schema: Mapping[str, object] | None,
) -> str:
    """SHA-256 over the exact external schema representation and absent marker."""
    payload = {
        "external_tool_name": external_name,
        "input_schema": input_schema,
        "output_schema": output_schema if output_schema is not None else {"__absent__": True},
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def json_depth(value: object, current: int = 0) -> int:
    if isinstance(value, Mapping):
        return max([current] + [json_depth(v, current + 1) for v in value.values()])
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return max([current] + [json_depth(v, current + 1) for v in value])
    return current


def serialized_size(value: object) -> int:
    return len(canonical_json(value).encode("utf-8"))


def item_count(value: object) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in ("items", "hits", "results"):
            nested = value.get(key)
            if isinstance(nested, list):
                return len(nested)
    return 1
