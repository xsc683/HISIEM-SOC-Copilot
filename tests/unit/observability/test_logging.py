from __future__ import annotations

import logging

from opentelemetry.context import attach, detach
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from hisiem_soc_copilot.infrastructure.observability.context import bind_log_context
from hisiem_soc_copilot.infrastructure.observability.log_correlation import (
    TraceCorrelationFilter,
)


def test_log_correlation_works_without_an_active_span() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)

    assert TraceCorrelationFilter("service").filter(record) is True
    assert record.otel_trace_id == ""
    assert record.otel_span_id == ""
    assert record.otel_service_name == "service"
    assert record.otel_operation == ""


def test_log_correlation_adds_active_ids_and_approved_context() -> None:
    span = NonRecordingSpan(
        SpanContext(
            trace_id=0x0123456789ABCDEF0123456789ABCDEF,
            span_id=0x0123456789ABCDEF,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    )
    token = attach(__import__("opentelemetry").trace.set_span_in_context(span))
    try:
        with bind_log_context(investigation_id="inv-1", correlation_id="corr-1"):
            record = logging.LogRecord(
                "test", logging.INFO, __file__, 1, "message", (), None
            )
            assert TraceCorrelationFilter("service").filter(record) is True
    finally:
        detach(token)

    assert record.otel_trace_id == "0123456789abcdef0123456789abcdef"
    assert record.otel_span_id == "0123456789abcdef"
    assert record.otel_service_name == "service"
    assert record.otel_operation == ""
    assert record.investigation_id == "inv-1"
    assert record.correlation_id == "corr-1"
