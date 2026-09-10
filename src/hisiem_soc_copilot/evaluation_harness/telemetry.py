"""E1-C2 model telemetry artifact (evaluation-only sidecar).

One real-model evaluation execution also persists a bounded operational sidecar in
the SAME execution directory:

    <executions_dir>/gp-01/<dataset_run_id>/<execution_id>/
        execution.json          (schema evaluation-execution/v2 — unchanged)
        model-telemetry.json    (schema evaluation-model-telemetry/v1)

The telemetry artifact is NOT the execution record and does NOT change
``evaluation-execution/v2``. It owns the E1-C2 provider gate result SEPARATELY
from ``execution_status`` (§6): the production runtime intentionally degrades a
provider outage/refusal/invalid output into deterministic fallback, so an
Investigation can be COMPLETED + INCONCLUSIVE while the E1-C2 real-provider gate is
FAIL (all model calls failed). model-telemetry.json records the REAL bounded usage
so the two are never conflated.

The payload is strictly bounded — never an API key, Authorization header, prompt,
raw completion/response body, evidence payload, raw alert, oracle, F1-F5/S1/W1
labels, chain-of-thought, exception message, environment dump, or a credential-
bearing DSN. ``usage_records`` come from the provider's immutable usage snapshot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .record import atomic_write_json

TELEMETRY_SCHEMA_VERSION = "evaluation-model-telemetry/v1"
_TELEMETRY_ARTIFACT = "model-telemetry.json"

# Operations the E1-C2 gate must each have >= 1 successful validated model call for.
REQUIRED_OPERATIONS: tuple[str, ...] = ("plan", "decide", "assess", "verdict")
# Structured-output modes that count as "resolved" for the E1-C2 gate.
_ALLOWED_RESOLVED_MODES: frozenset[str] = frozenset(
    {"json_schema", "json_object", "json_only"}
)

# The EXACT E1-C2 provider contract the telemetry gate must confirm for PASS. A
# PASS means the full contract below holds — never merely "four operations ran".
EXPECTED_PROVIDER_ADAPTER = "openai_compatible"
EXPECTED_PROVIDER = "command_code"
EXPECTED_PROTOCOL = "openai_compatible_chat_completions"
EXPECTED_MODEL = "deepseek/deepseek-v4-flash"
EXPECTED_CONFIGURED_MODE = "auto"

# Hard bound for any externally supplied (provider-supplied) string persisted.
MAX_FIELD_LEN = 200

# The ONLY usage-record fields this artifact may persist. Everything else the
# provider snapshot may carry (api_key, Authorization, prompt, messages,
# raw_response, headers, environment, ...) is dropped — a real allowlist boundary,
# never a pass-through of ``dict(record)``.
_USAGE_ALLOWLIST: tuple[str, ...] = (
    "provider",
    "protocol",
    "model",
    "operation",
    "provider_request_id",
    "latency_ms",
    "attempt_count",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "outcome",
    "error_category",
)

GATE_PASS = "PASS"
GATE_FAIL = "FAIL"


class ModelTelemetrySchemaError(ValueError):
    """A persisted telemetry payload carries an unsupported/legacy schema version."""


def model_telemetry_path(
    executions_dir: str | Path, dataset_run_id: str, execution_id: str
) -> Path:
    """The ``model-telemetry.json`` sidecar beside ``execution.json`` (E1-C2 §5)."""
    return (
        Path(executions_dir)
        / "gp-01"
        / dataset_run_id
        / execution_id
        / _TELEMETRY_ARTIFACT
    )


@dataclass(frozen=True)
class ModelTelemetry:
    """Bounded real-model telemetry + the E1-C2 provider gate (mutable-free)."""

    schema_version: str = TELEMETRY_SCHEMA_VERSION
    execution_id: str = ""
    dataset_run_id: str = ""
    provider_adapter: str = ""
    provider: str = ""
    protocol: str = ""
    model: str = ""
    zdr_enabled: bool = True
    configured_structured_output_mode: str = ""
    resolved_structured_output_mode: str | None = None
    usage_records: tuple[dict[str, Any], ...] = ()
    required_operations: tuple[str, ...] = REQUIRED_OPERATIONS
    successful_operations: tuple[str, ...] = ()
    gate_status: str = GATE_FAIL
    gate_failures: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "dataset_run_id": self.dataset_run_id,
            "provider_adapter": self.provider_adapter,
            "provider": self.provider,
            "protocol": self.protocol,
            "model": self.model,
            "zdr_enabled": self.zdr_enabled,
            "configured_structured_output_mode": self.configured_structured_output_mode,
            "resolved_structured_output_mode": self.resolved_structured_output_mode,
            "usage_records": list(self.usage_records),
            "required_operations": list(self.required_operations),
            "successful_operations": list(self.successful_operations),
            "gate_status": self.gate_status,
            "gate_failures": list(self.gate_failures),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ModelTelemetry:
        schema = payload.get("schema_version")
        if schema != TELEMETRY_SCHEMA_VERSION:
            raise ModelTelemetrySchemaError(
                f"unsupported telemetry schema {schema!r}; this reader only accepts "
                f"{TELEMETRY_SCHEMA_VERSION}"
            )
        records = payload.get("usage_records") or []
        return cls(
            execution_id=_bounded_str(payload.get("execution_id", "")),
            dataset_run_id=_bounded_str(payload.get("dataset_run_id", "")),
            provider_adapter=_bounded_str(payload.get("provider_adapter", "")),
            provider=_bounded_str(payload.get("provider", "")),
            protocol=_bounded_str(payload.get("protocol", "")),
            model=_bounded_str(payload.get("model", "")),
            zdr_enabled=bool(payload.get("zdr_enabled", True)),
            configured_structured_output_mode=_bounded_str(
                payload.get("configured_structured_output_mode", "")
            ),
            resolved_structured_output_mode=_optional_bounded_str(
                payload.get("resolved_structured_output_mode")
            ),
            # Re-apply the allowlist on read-back: a tampered/legacy payload can
            # never smuggle an arbitrary field back into the in-memory artifact.
            usage_records=tuple(_allowlist_record(r) for r in records),
            required_operations=tuple(
                _bounded_str(o)
                for o in (payload.get("required_operations") or REQUIRED_OPERATIONS)
            ),
            successful_operations=tuple(
                _bounded_str(o) for o in (payload.get("successful_operations") or [])
            ),
            gate_status=_bounded_str(payload.get("gate_status", GATE_FAIL)),
            gate_failures=tuple(
                _bounded_str(f) for f in (payload.get("gate_failures") or [])
            ),
        )


def _bounded_str(value: Any) -> str:
    """Coerce to a bounded string (never persist an unbounded provider string)."""
    return str(value)[:MAX_FIELD_LEN]


def _optional_bounded_str(value: Any) -> str | None:
    return None if value is None else str(value)[:MAX_FIELD_LEN]


def _safe_scalar(value: Any) -> Any:
    """Keep only bounded scalars; drop anything structured (never leak a dict/list)."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_FIELD_LEN]
    return None


def _record_dict(record: Any) -> dict[str, Any]:
    """Normalize ONE usage record to a raw dict (``ModelUsage.as_dict``) — UNFILTERED."""
    if isinstance(record, dict):
        return dict(record)
    as_dict = getattr(record, "as_dict", None)
    if callable(as_dict):
        return dict(as_dict())
    return {}


def _allowlist_record(record: Any) -> dict[str, Any]:
    """Reduce ONE usage record to the allowlisted bounded fields ONLY (§3).

    Any field not in :data:`_USAGE_ALLOWLIST` (api_key, Authorization, prompt,
    messages, raw_response, headers, environment, ...) is dropped, and every value
    is coerced to a bounded scalar. This is the security boundary — the artifact
    must never persist arbitrary provider-supplied fields.
    """
    raw = _record_dict(record)
    return {k: _safe_scalar(raw[k]) for k in _USAGE_ALLOWLIST if k in raw}


def build_model_telemetry(
    *,
    execution_id: str,
    dataset_run_id: str,
    provider_adapter: str,
    provider: Any,
) -> ModelTelemetry:
    """Build the E1-C2 telemetry from ONE real provider instance (public surface).

    ``provider`` must expose the bounded introspection API of
    :class:`OpenAICompatibleModelProvider`: ``provider_name``, ``protocol_name``,
    ``model_name``, ``zdr_enabled``, ``configured_structured_output_mode``,
    ``resolved_structured_output_mode``, and ``usage_snapshot()``. Only public safe
    metadata is read — never the API key/prompt/client internals; every usage record
    is reduced to the explicit :data:`_USAGE_ALLOWLIST` (§3).

    Gate semantics (§2/§6): PASS only when the COMPLETE E1-C2 provider contract
    holds — adapter ``openai_compatible``, provider ``command_code``, protocol
    ``openai_compatible_chat_completions``, model ``deepseek/deepseek-v4-flash``,
    ZDR enabled, configured structured-output mode ``auto``, a resolved mode in
    json_schema/json_object/json_only, >= 1 successful validated call for EVERY
    required operation (plan/decide/assess/verdict), and no MODEL_CONFIGURATION
    failure. The gate is INDEPENDENT of the Investigation/execution status.
    """
    provider_adapter = _bounded_str(provider_adapter)
    provider_name = _bounded_str(getattr(provider, "provider_name", "") or "")
    protocol = _bounded_str(getattr(provider, "protocol_name", "") or "")
    model = _bounded_str(getattr(provider, "model_name", "") or "")
    zdr = bool(getattr(provider, "zdr_enabled", False))
    configured = _bounded_str(
        getattr(provider, "configured_structured_output_mode", "") or ""
    )
    resolved = _optional_bounded_str(
        getattr(provider, "resolved_structured_output_mode", None)
    )

    snapshot = getattr(provider, "usage_snapshot", None)
    records: list[dict[str, Any]] = []
    if callable(snapshot):
        for record in snapshot():
            d = _allowlist_record(record)
            if d:
                records.append(d)

    successes = {
        op: any(
            r.get("operation") == op and r.get("outcome") == "ok" for r in records
        )
        for op in REQUIRED_OPERATIONS
    }
    successful = tuple(op for op in REQUIRED_OPERATIONS if successes[op])

    gate_failures: list[str] = []
    if provider_adapter != EXPECTED_PROVIDER_ADAPTER:
        gate_failures.append("UNEXPECTED_PROVIDER_ADAPTER")
    if provider_name != EXPECTED_PROVIDER:
        gate_failures.append("UNEXPECTED_PROVIDER")
    if protocol != EXPECTED_PROTOCOL:
        gate_failures.append("UNEXPECTED_PROTOCOL")
    if model != EXPECTED_MODEL:
        gate_failures.append("UNEXPECTED_MODEL")
    if not zdr:
        gate_failures.append("ZDR_DISABLED")
    if configured != EXPECTED_CONFIGURED_MODE:
        gate_failures.append("UNEXPECTED_STRUCTURED_OUTPUT_CONFIG")
    if resolved not in _ALLOWED_RESOLVED_MODES:
        gate_failures.append("STRUCTURED_OUTPUT_MODE_UNRESOLVED")
    for op in REQUIRED_OPERATIONS:
        if not successes[op]:
            gate_failures.append(f"MISSING_SUCCESSFUL_{op.upper()}")
    if any(r.get("error_category") == "MODEL_CONFIGURATION" for r in records):
        gate_failures.append("MODEL_CONFIGURATION_FAILURE")
    gate_status = GATE_PASS if not gate_failures else GATE_FAIL

    return ModelTelemetry(
        execution_id=_bounded_str(execution_id),
        dataset_run_id=_bounded_str(dataset_run_id),
        provider_adapter=provider_adapter,
        provider=provider_name,
        protocol=protocol,
        model=model,
        zdr_enabled=zdr,
        configured_structured_output_mode=configured,
        resolved_structured_output_mode=resolved,
        usage_records=tuple(records),
        successful_operations=successful,
        gate_status=gate_status,
        gate_failures=tuple(gate_failures),
    )


def write_model_telemetry(path: str | Path, telemetry: ModelTelemetry) -> None:
    """Atomically persist the telemetry sidecar (never sealed)."""
    atomic_write_json(path, telemetry.to_payload())


def read_model_telemetry(path: str | Path) -> ModelTelemetry:
    """Load + validate a persisted model-telemetry artifact."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return ModelTelemetry.from_payload(payload)
