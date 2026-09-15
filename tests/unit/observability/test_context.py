from __future__ import annotations

import pytest

from hisiem_soc_copilot.infrastructure.observability import context

_VALID_TRACEPARENT = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"


def test_traceparent_validation_rejects_malformed_and_zero_ids() -> None:
    assert context.validate_traceparent(_VALID_TRACEPARENT) == _VALID_TRACEPARENT
    assert context.validate_traceparent("01" + _VALID_TRACEPARENT[2:]) is None
    assert context.validate_traceparent("00-" + "0" * 32 + "-0123456789abcdef-01") is None
    zero_span = "00-0123456789abcdef0123456789abcdef-" + "0" * 16 + "-01"
    assert context.validate_traceparent(zero_span) is None
    assert context.validate_traceparent(None) is None


def test_trace_link_ignores_missing_or_invalid_context() -> None:
    assert context.trace_link(None) is None
    assert context.trace_link("not-a-traceparent") is None
    assert context.trace_link(_VALID_TRACEPARENT) is not None


def test_log_context_is_allowlisted_and_scoped() -> None:
    assert context.current_log_context() == {}
    with context.bind_log_context(
        investigation_id="inv-1",
        correlation_id="corr-1",
        authorization="secret",
    ):
        assert context.current_log_context() == {
            "investigation_id": "inv-1",
            "correlation_id": "corr-1",
        }
    assert context.current_log_context() == {}


def test_span_creation_failure_does_not_change_business_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable_tracer(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("telemetry offline")

    monkeypatch.setattr(context.trace, "get_tracer", unavailable_tracer)
    with context.start_span("investigation.run"), pytest.raises(
        ValueError, match="business failure"
    ):
            raise ValueError("business failure")


def test_current_operation_is_not_read_from_non_public_span_name() -> None:
    with context.start_span("tool.execute"):
        _, _, operation = context.current_span_context()
        assert operation == "tool.execute"
