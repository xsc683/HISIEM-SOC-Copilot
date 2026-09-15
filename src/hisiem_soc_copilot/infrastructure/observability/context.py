"""W3C context helpers for process and durable boundaries.

Only ``traceparent`` is persisted with an outbox delivery. Baggage and business
correlation identifiers are deliberately separate and never become propagation data.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar, Token
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.propagate import get_global_textmap
from opentelemetry.trace import Link, Span, SpanContext, SpanKind

_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_LOG_FIELDS = frozenset(
    {
        "investigation_id",
        "response_proposal_id",
        "tool_invocation_id",
        "execution_command_id",
        "correlation_id",
    }
)
_log_context: ContextVar[Mapping[str, str] | None] = ContextVar("otel_log_context", default=None)
_active_operation: ContextVar[str] = ContextVar("otel_active_operation", default="")


def validate_traceparent(value: object) -> str | None:
    """Return a well-formed W3C version-00 traceparent, or ignore it."""
    if not isinstance(value, str) or len(value) != 55:
        return None
    match = _TRACEPARENT_RE.fullmatch(value)
    if match is None or match.group(1) == "0" * 32 or match.group(2) == "0" * 16:
        return None
    return value


def capture_traceparent() -> str | None:
    """Capture the active span context for optional durable diagnostic metadata."""
    try:
        carrier: dict[str, str] = {}
        get_global_textmap().inject(carrier)
        return validate_traceparent(carrier.get("traceparent"))
    except Exception:
        return None


def trace_link(traceparent: str | None) -> Link | None:
    """Build a span link from validated durable context; invalid context is ignored."""
    valid = validate_traceparent(traceparent)
    if valid is None:
        return None
    try:
        context = get_global_textmap().extract({"traceparent": valid})
        span_context = trace.get_current_span(context).get_span_context()
        if not isinstance(span_context, SpanContext) or not span_context.is_valid:
            return None
        return Link(span_context)
    except Exception:
        return None


@contextmanager
def start_span(
    name: str,
    *,
    attributes: Mapping[str, str] | None = None,
    links: tuple[Link, ...] = (),
    kind: SpanKind | None = None,
    context: Context | None = None,
) -> Iterator[Span]:
    """Start a span without allowing telemetry setup/processor errors to escape."""
    operation_token = _active_operation.set(name)
    manager: Any = None
    try:
        try:
            tracer = trace.get_tracer("hisiem_soc_copilot")
            manager = tracer.start_as_current_span(
                name,
                attributes=dict(attributes or {}),
                links=links,
                kind=kind or SpanKind.INTERNAL,
                context=context,
                record_exception=False,
                set_status_on_exception=False,
            )
            span = manager.__enter__()
        except Exception:
            yield trace.get_current_span()
            return

        try:
            yield span
        except BaseException as exc:
            with suppress(Exception):
                manager.__exit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            # Span/export failures are diagnostic only and must not alter the caller.
            with suppress(Exception):
                manager.__exit__(None, None, None)
    finally:
        _active_operation.reset(operation_token)


def set_span_attribute(span: Span, key: str, value: str) -> None:
    """Set one diagnostic attribute without allowing SDK failures to escape."""
    try:
        span.set_attribute(key, value)
    except Exception:
        return


@contextmanager
def linked_worker_span(
    name: str,
    *,
    traceparent: str | None,
    attributes: Mapping[str, str] | None = None,
) -> Iterator[Span]:
    """Start a new worker trace linked to the producer instead of a long child span."""
    link = trace_link(traceparent)
    links = (link,) if link is not None else ()
    # A durable worker is a new root even if a framework happens to run it under an
    # unrelated ambient context. The source relationship is represented by a link.
    with start_span(
        name,
        attributes=attributes,
        links=links,
        kind=SpanKind.CONSUMER,
        context=Context(),
    ) as span:
        yield span


@contextmanager
def bind_log_context(**values: str | None) -> Iterator[None]:
    """Bind approved business IDs to logs only; never to spans or metric labels."""
    safe = dict(_log_context.get() or {})
    for key, value in values.items():
        if key not in _LOG_FIELDS:
            continue
        if isinstance(value, str) and value and len(value) <= 128:
            safe[key] = value
    token: Token[Mapping[str, str] | None] = _log_context.set(safe)
    try:
        yield
    finally:
        _log_context.reset(token)


def current_log_context() -> Mapping[str, str]:
    """Return the current allowlisted log-only correlation fields."""
    return _log_context.get() or {}


def current_span_context() -> tuple[str, str, str]:
    """Return trace id, span id, and operation for structured log enrichment."""
    try:
        span = trace.get_current_span()
        span_context = span.get_span_context()
        if not span_context.is_valid:
            return "", "", _active_operation.get()
        trace_id = f"{span_context.trace_id:032x}"
        span_id = f"{span_context.span_id:016x}"
        return trace_id, span_id, _active_operation.get()
    except Exception:
        return "", "", ""
