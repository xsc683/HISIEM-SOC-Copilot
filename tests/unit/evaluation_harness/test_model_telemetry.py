"""E1-C2 model-telemetry sidecar unit tests (§5/§6/§16).

The telemetry artifact owns the E1-C2 provider gate SEPARATELY from
``execution_status``: a runtime can COMPLETE (INCONCLUSIVE fallback) while the gate
FAILs because no real validated model call succeeded. These tests cover gate
semantics, boundedness (secret + oracle firewall), the atomic sidecar persistence,
and the schema version guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hisiem_soc_copilot.evaluation_harness.telemetry import (
    GATE_FAIL,
    GATE_PASS,
    REQUIRED_OPERATIONS,
    TELEMETRY_SCHEMA_VERSION,
    ModelTelemetry,
    ModelTelemetrySchemaError,
    build_model_telemetry,
    model_telemetry_path,
    read_model_telemetry,
    write_model_telemetry,
)


class _IntrospectingProvider:
    """A provider exposing ONLY the public bounded introspection surface."""

    provider_name = "command_code"
    protocol_name = "openai_compatible_chat_completions"
    model_name = "deepseek/deepseek-v4-flash"
    zdr_enabled = True
    configured_structured_output_mode = "auto"

    def __init__(
        self, *, resolved: str | None, records: list[dict[str, object]]
    ) -> None:
        self._resolved = resolved
        self._records = list(records)

    @property
    def resolved_structured_output_mode(self) -> str | None:
        return self._resolved

    def usage_snapshot(self) -> tuple[dict[str, object], ...]:
        return tuple(self._records)


def _ok(operation: str) -> dict[str, object]:
    return {
        "provider": "command_code",
        "protocol": "openai_compatible_chat_completions",
        "model": "deepseek/deepseek-v4-flash",
        "operation": operation,
        "provider_request_id": "chatcmpl-123",
        "latency_ms": 320,
        "attempt_count": 1,
        "input_tokens": 500,
        "output_tokens": 90,
        "total_tokens": 590,
        "outcome": "ok",
        "error_category": None,
    }


def _all_ok() -> list[dict[str, object]]:
    return [_ok(op) for op in REQUIRED_OPERATIONS]


def _build(resolved: str | None, records: list[dict[str, object]]) -> ModelTelemetry:
    return build_model_telemetry(
        execution_id="exec-1",
        dataset_run_id="run-1",
        provider_adapter="openai_compatible",
        provider=_IntrospectingProvider(resolved=resolved, records=records),
    )


def test_all_four_operations_gate_passes() -> None:
    telemetry = _build(resolved="json_schema", records=_all_ok())
    assert telemetry.gate_status == GATE_PASS
    assert telemetry.successful_operations == REQUIRED_OPERATIONS
    assert telemetry.gate_failures == ()
    assert telemetry.provider == "command_code"
    assert telemetry.protocol == "openai_compatible_chat_completions"
    assert telemetry.resolved_structured_output_mode == "json_schema"


def test_missing_operation_gate_fails() -> None:
    records = _all_ok()[:-1]  # drop verdict
    telemetry = _build(resolved="json_schema", records=records)
    assert telemetry.gate_status == GATE_FAIL
    assert telemetry.successful_operations == ("plan", "decide", "assess")
    assert "MISSING_SUCCESSFUL_VERDICT" in telemetry.gate_failures


def test_resolved_mode_missing_gate_fails() -> None:
    telemetry = _build(resolved=None, records=_all_ok())
    assert telemetry.gate_status == GATE_FAIL
    assert "STRUCTURED_OUTPUT_MODE_UNRESOLVED" in telemetry.gate_failures


def test_model_configuration_failure_gate_fails_even_when_all_ops_ok() -> None:
    records = _all_ok() + [
        {
            "provider": "command_code",
            "protocol": "openai_compatible_chat_completions",
            "model": "deepseek/deepseek-v4-flash",
            "operation": "plan",
            "provider_request_id": None,
            "latency_ms": None,
            "attempt_count": 1,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "outcome": "error",
            "error_category": "MODEL_CONFIGURATION",
        }
    ]
    telemetry = _build(resolved="json_schema", records=records)
    assert telemetry.gate_status == GATE_FAIL
    assert "MODEL_CONFIGURATION_FAILURE" in telemetry.gate_failures


def test_runtime_completed_with_no_successes_is_gate_fail() -> None:
    """§6: Investigation/execution COMPLETED is NOT E1-C2 success. All model calls
    failed (errors only) → successful_operations empty → gate FAIL."""
    records = [
        {
            "operation": op,
            "outcome": "error",
            "error_category": "MODEL_UNAVAILABLE",
        }
        for op in REQUIRED_OPERATIONS
    ]
    telemetry = _build(resolved=None, records=records)
    assert telemetry.gate_status == GATE_FAIL
    assert telemetry.successful_operations == ()
    assert telemetry.gate_failures


def test_payload_round_trip() -> None:
    telemetry = _build(resolved="json_object", records=_all_ok())
    restored = ModelTelemetry.from_payload(telemetry.to_payload())
    assert restored.to_payload() == telemetry.to_payload()
    assert restored.gate_status == GATE_PASS


def test_unknown_schema_rejected() -> None:
    payload = _build(resolved="json_schema", records=_all_ok()).to_payload()
    payload["schema_version"] = "evaluation-model-telemetry/v99"
    with pytest.raises(ModelTelemetrySchemaError):
        ModelTelemetry.from_payload(payload)


def test_telemetry_never_leaks_secrets_or_oracle(tmp_path: Path) -> None:
    records = _all_ok()
    telemetry = _build(resolved="json_schema", records=records)
    text = json.dumps(telemetry.to_payload())
    for forbidden in (
        "api_key",
        "CMD_API_KEY",
        "authorization",
        "Bearer",
        "password",
        "hsiem_dev_password",
        "postgresql://",
        "oracle",
        "expected_verdict",
        "required_evidence_roles",
        "S1",
        "W1",
        "F1",
        "prompt",
        "chain-of-thought",
    ):
        assert forbidden not in text, f"telemetry leaked {forbidden!r}"


def test_telemetry_atomic_write_and_read(tmp_path: Path) -> None:
    telemetry = _build(resolved="json_schema", records=_all_ok())
    path = model_telemetry_path(tmp_path, "run-1", "exec-1")
    write_model_telemetry(path, telemetry)
    assert path.is_file()
    restored = read_model_telemetry(path)
    assert restored.to_payload() == telemetry.to_payload()
    assert restored.gate_status == GATE_PASS
    # Atomic replace leaves no temp artifacts behind.
    assert sorted(p.name for p in path.parent.iterdir()) == ["model-telemetry.json"]


def test_schema_version_constant() -> None:
    assert TELEMETRY_SCHEMA_VERSION == "evaluation-model-telemetry/v1"
    assert ModelTelemetry().schema_version == TELEMETRY_SCHEMA_VERSION


def test_usage_missing_fields_are_null_not_guessed() -> None:
    records = [_ok("plan")]
    records[0].update(
        provider_request_id=None,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
    )
    telemetry = _build(resolved="json_only", records=records)
    record = telemetry.usage_records[0]
    assert record["provider_request_id"] is None
    assert record["input_tokens"] is None


def test_provider_adapter_is_openai_compatible() -> None:
    telemetry = _build(resolved="json_schema", records=_all_ok())
    assert telemetry.provider_adapter == "openai_compatible"
