"""Machine-readable trace and approved business-ID fields for Python logs."""

from __future__ import annotations

import logging

from .context import current_log_context, current_span_context


class TraceCorrelationFilter(logging.Filter):
    """Attach trace/span IDs and approved log-only context to each record."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def filter(self, record: logging.LogRecord) -> bool:
        trace_id, span_id, operation = current_span_context()
        record.otel_trace_id = trace_id
        record.otel_span_id = span_id
        record.otel_service_name = self._service_name
        record.otel_operation = operation
        for key, value in current_log_context().items():
            setattr(record, key, value)
        return True


def install_log_correlation(service_name: str) -> tuple[logging.Handler, ...]:
    """Attach one correlation filter to existing root handlers, idempotently."""
    root = logging.getLogger()
    installed: list[logging.Handler] = []
    for handler in root.handlers:
        if any(isinstance(item, TraceCorrelationFilter) for item in handler.filters):
            continue
        handler.addFilter(TraceCorrelationFilter(service_name))
        installed.append(handler)
    return tuple(installed)


def remove_log_correlation(handlers: tuple[logging.Handler, ...]) -> None:
    for handler in handlers:
        for item in tuple(handler.filters):
            if isinstance(item, TraceCorrelationFilter):
                handler.removeFilter(item)
