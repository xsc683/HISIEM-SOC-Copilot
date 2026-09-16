"""Bounded OTel metrics with centrally enforced label cardinality."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from opentelemetry import metrics

_COUNTERS = frozenset(
    {
        "investigation.total",
        "investigation.failure",
        "agent.steps",
        "agent.tool_calls",
        "agent.retry_count",
        "agent.loop_count",
        "llm.calls",
        "llm.errors",
        "tool.calls",
        "tool.errors",
        "tool.retries",
        "retrieval.calls",
        "retrieval.errors",
        "durable.retry_count",
        "durable.dead_letter_count",
        "response.attention_required",
        "mcp.calls",
        "mcp.errors",
    }
)
_HISTOGRAMS = frozenset(
    {
        "investigation.duration",
        "llm.duration",
        "llm.input_tokens",
        "llm.output_tokens",
        "tool.duration",
        "retrieval.duration",
        "retrieval.hit_count",
        "response.submit_duration",
        "response.observe_duration",
        "mcp.duration",
    }
)
_ALLOWED_LABEL_VALUES: dict[str, frozenset[str]] = {
    "tool_name": frozenset(
        {
            "hisiem.search_events",
            "hisiem.get_detection_rule",
            "knowledge.retrieve_security_guidance",
            "knowledge.resolve_attack_technique",
        }
    ),
    "tool_provider": frozenset({"native", "mcp"}),
    "server_category": frozenset({"internal", "external"}),
    "model_provider": frozenset({"command_code", "scripted", "openai_compatible"}),
    "retrieval_mode": frozenset({"HYBRID", "LEXICAL_ONLY", "VECTOR_ONLY", "UNKNOWN"}),
    "result": frozenset({"success", "no_data", "partial", "rejected", "unavailable", "error"}),
    "error_category": frozenset(
        {
            "TIMEOUT",
            "UNAVAILABLE",
            "AUTH_FAILURE",
            "SCHEMA_MISMATCH",
            "RATE_LIMITED",
            "REMOTE_TOOL_ERROR",
            "UNSUPPORTED_INTERACTION",
            "INVALID_RESULT",
            "RESULT_TOO_LARGE",
            "PROVIDER_ERROR",
            "POLICY_REJECTED",
            "MODEL_CONFIGURATION",
            "MODEL_REFUSAL",
        }
    ),
    "operation": frozenset(
        {
            "investigation.run",
            "graph.invoke",
            "tool.execute",
            "llm.call",
            "llm.plan",
            "llm.decide",
            "llm.assess",
            "llm.verdict",
            "knowledge.retrieve",
            "response.submit",
            "response.observe",
            "investigation.graph.run",
            "response.execution.submit",
            "response.execution.observe",
        }
    ),
    "response_state": frozenset(
        {"APPROVED", "SUBMITTED", "SUCCEEDED", "FAILED", "ATTENTION_REQUIRED"}
    ),
}
_INSTRUMENTS: dict[tuple[str, str], Any] = {}
_METER = metrics.get_meter_provider().get_meter("hisiem_soc_copilot")


def sanitize_metric_attributes(
    attributes: Mapping[str, object] | None,
) -> dict[str, str] | None:
    """Validate all metric labels; reject the observation if any label is unsafe."""
    if not attributes:
        return {}
    safe: dict[str, str] = {}
    for key, value in attributes.items():
        allowed = _ALLOWED_LABEL_VALUES.get(key)
        if allowed is None or not isinstance(value, str) or value not in allowed:
            return None
        safe[key] = value
    return safe


def record_counter(
    name: str, value: int = 1, attributes: Mapping[str, object] | None = None
) -> None:
    if name not in _COUNTERS or not isinstance(value, int) or value <= 0:
        return
    try:
        safe = sanitize_metric_attributes(attributes)
        if safe is None:
            return
        key = ("counter", name)
        instrument = _INSTRUMENTS.get(key)
        if instrument is None:
            instrument = _METER.create_counter(name=name, unit="1")
            _INSTRUMENTS[key] = instrument
        instrument.add(value, safe)
    except Exception:
        # Metric recording is best-effort; never affect an application operation.
        return


def record_histogram(
    name: str, value: float, attributes: Mapping[str, object] | None = None
) -> None:
    if name not in _HISTOGRAMS or not isinstance(value, (int, float)):
        return
    if not isfinite(value) or value < 0:
        return
    try:
        safe = sanitize_metric_attributes(attributes)
        if safe is None:
            return
        key = ("histogram", name)
        instrument = _INSTRUMENTS.get(key)
        if instrument is None:
            unit = "s" if name.endswith("duration") else "1"
            instrument = _METER.create_histogram(name=name, unit=unit)
            _INSTRUMENTS[key] = instrument
        instrument.record(value, safe)
    except Exception:
        return
