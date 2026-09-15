"""Optional OpenTelemetry lifecycle and privacy-safe standard instrumentation."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from threading import RLock
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from opentelemetry.propagate import set_global_textmap
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ...config import ObservabilitySettings
from .log_correlation import install_log_correlation, remove_log_correlation

logger = logging.getLogger(__name__)

# HTTP instrumentation populates these fields from request scopes. Their values may
# contain queries, resource identifiers, or caller network data; replace them at the
# server hook before they can reach a processor or exporter.
_REDACTED_HTTP_ATTRIBUTES = (
    "url.full",
    "url.original",
    "url.path",
    "url.query",
    "http.url",
    "http.target",
    "http.request.header.authorization",
    "http.request.header.cookie",
    "net.peer.ip",
    "network.peer.address",
    "client.address",
)


def _scrub_http_span(span: Any) -> None:
    """Remove URL and sensitive network attributes from an HTTP span."""
    try:
        for key in _REDACTED_HTTP_ATTRIBUTES:
            with suppress(Exception):
                span.set_attribute(key, "")
    except Exception:
        return


def _sanitize_server_request(span: Any, scope: Any) -> None:
    del scope  # Do not inspect or serialize raw request fields.
    _scrub_http_span(span)


def _sanitize_client_request(span: Any, request: Any) -> None:
    del request  # Do not inspect or serialize raw request fields.
    _scrub_http_span(span)


def _sanitize_client_response(span: Any, request: Any, response: Any) -> None:
    del request, response  # Do not inspect or serialize raw response fields.
    _scrub_http_span(span)


async def _sanitize_async_client_request(span: Any, request: Any) -> None:
    _sanitize_client_request(span, request)


async def _sanitize_async_client_response(
    span: Any, request: Any, response: Any
) -> None:
    _sanitize_client_response(span, request, response)


@contextmanager
def _disable_httpx_header_capture() -> Iterator[None]:
    """Force HTTPX instrumentation's environment-controlled capture lists empty."""
    names = (
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_REQUEST",
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_RESPONSE",
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS",
    )
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ[name] = ""
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _safe_http_url(value: str) -> str:
    """Keep only the outbound origin; discard path, query, and user-info."""
    try:
        parts = urlsplit(str(value))
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return ""
        host = parts.hostname
        if ":" in host:
            host = f"[{host}]"
        if parts.port is not None:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, "/", "", ""))
    except (TypeError, ValueError):
        return ""


@dataclass
class _TelemetryRuntime:
    """Process-scoped SDK resources shared by application lifespans."""

    tracer_provider: Any
    meter_provider: Any
    instrumentors: list[Any] = field(default_factory=list)
    log_handlers: tuple[logging.Handler, ...] = ()
    active_handles: int = 0


@dataclass
class TelemetryHandle:
    """Instrumentation owned by one application lifespan.

    OpenTelemetry providers are process-global by design. Keep them alive when a
    lifespan closes so an ASGI host can start the same process again without trying
    to replace the SDK's write-once global providers.
    """

    enabled: bool = False
    app: Any = None
    tracer_provider: Any = None
    meter_provider: Any = None
    instrumentors: list[Any] = field(default_factory=list)
    log_handlers: tuple[logging.Handler, ...] = ()
    _runtime: _TelemetryRuntime | None = field(default=None, repr=False)
    _fastapi_instrumented: bool = field(default=False, repr=False)
    _shutdown: bool = field(default=False, repr=False)

    def shutdown(self) -> None:
        """Stop this lifespan's instrumentation without replacing global providers."""
        if self._shutdown:
            return
        self._shutdown = True
        runtime = self._runtime
        if runtime is None:
            remove_log_correlation(self.log_handlers)
            return

        if self._fastapi_instrumented:
            with suppress(Exception):
                from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

                FastAPIInstrumentor.uninstrument_app(self.app)

        with _RUNTIME_LOCK:
            runtime.active_handles = max(0, runtime.active_handles - 1)
            if runtime.active_handles:
                return

            for instrumentor in reversed(runtime.instrumentors):
                with suppress(Exception):
                    if instrumentor.is_instrumented_by_opentelemetry:
                        instrumentor.uninstrument()
            remove_log_correlation(runtime.log_handlers)
            runtime.log_handlers = ()
            # Do not call provider.shutdown(): providers are process-scoped and
            # OpenTelemetry forbids replacing the global provider in this process.
            for provider in (runtime.meter_provider, runtime.tracer_provider):
                with suppress(Exception):
                    provider.force_flush()


_RUNTIME: _TelemetryRuntime | None = None
_RUNTIME_LOCK = RLock()


def _instrument_standard_libraries(
    instrumentors: list[Any],
    *,
    tracer_provider: Any,
    meter_provider: Any,
) -> None:
    """Instrument HTTPX, SQLAlchemy, and psycopg once per process."""
    httpx_instrumentor, sqlalchemy_instrumentor, psycopg_instrumentor = instrumentors
    if not httpx_instrumentor.is_instrumented_by_opentelemetry:
        # HTTPX instrumentation 0.65b0 reads header capture configuration from
        # environment variables rather than instrument() kwargs. Blank those
        # variables for the setup call, and scrub URL attributes in hooks for
        # versions that record them before the hook.
        with _disable_httpx_header_capture():
            httpx_instrumentor.instrument(
                tracer_provider=tracer_provider,
                meter_provider=meter_provider,
                request_hook=_sanitize_client_request,
                response_hook=_sanitize_client_response,
                async_request_hook=_sanitize_async_client_request,
                async_response_hook=_sanitize_async_client_response,
            )
    if not sqlalchemy_instrumentor.is_instrumented_by_opentelemetry:
        sqlalchemy_instrumentor.instrument(
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
            enable_commenter=False,
            capture_parameters=False,
        )
    if not psycopg_instrumentor.is_instrumented_by_opentelemetry:
        psycopg_instrumentor.instrument(
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
            capture_parameters=False,
        )


def _uninstrument_standard_libraries(instrumentors: list[Any]) -> None:
    for instrumentor in reversed(instrumentors):
        with suppress(Exception):
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()


def setup_telemetry(app: Any, settings: ObservabilitySettings) -> TelemetryHandle:
    """Initialize optional SDK instrumentation and degrade to a no-op on failure."""
    global _RUNTIME

    handle = TelemetryHandle(app=app)
    if not settings.tracing_enabled:
        return handle
    endpoint = settings.otlp_endpoint.strip()
    if not endpoint:
        logger.warning("OpenTelemetry setup unavailable (ValueError)")
        return handle

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

        with _RUNTIME_LOCK:
            runtime = _RUNTIME
            created = runtime is None
            if runtime is None:
                resource = Resource.create({"service.name": settings.service_name})
                tracer_provider = TracerProvider(
                    resource=resource,
                    sampler=ParentBased(TraceIdRatioBased(settings.trace_sample_ratio)),
                )
                tracer_provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
                )
                metric_reader = PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=endpoint),
                    export_interval_millis=settings.metric_export_interval_millis,
                )
                meter_provider = MeterProvider(
                    resource=resource, metric_readers=[metric_reader]
                )
                runtime = _TelemetryRuntime(
                    tracer_provider=tracer_provider,
                    meter_provider=meter_provider,
                    instrumentors=[
                        HTTPXClientInstrumentor(),
                        SQLAlchemyInstrumentor(),
                        PsycopgInstrumentor(),
                    ],
                )
            else:
                tracer_provider = runtime.tracer_provider
                meter_provider = runtime.meter_provider

            try:
                if not getattr(app, "_is_instrumented_by_opentelemetry", False):
                    FastAPIInstrumentor.instrument_app(
                        app,
                        tracer_provider=tracer_provider,
                        meter_provider=meter_provider,
                        server_request_hook=_sanitize_server_request,
                        http_capture_headers_server_request=[],
                        http_capture_headers_server_response=[],
                        http_capture_headers_sanitize_fields=[],
                    )
                    handle._fastapi_instrumented = True
                    handle.instrumentors.append(FastAPIInstrumentor())

                _instrument_standard_libraries(
                    runtime.instrumentors,
                    tracer_provider=tracer_provider,
                    meter_provider=meter_provider,
                )
                if created:
                    trace.set_tracer_provider(tracer_provider)
                    metrics.set_meter_provider(meter_provider)
                    set_global_textmap(TraceContextTextMapPropagator())
                    _RUNTIME = runtime
                runtime.active_handles += 1
                handle._runtime = runtime
                handle.tracer_provider = tracer_provider
                handle.meter_provider = meter_provider
                runtime.log_handlers = tuple(
                    dict.fromkeys(
                        runtime.log_handlers
                        + install_log_correlation(settings.service_name)
                    )
                )
                handle.log_handlers = runtime.log_handlers
                handle.instrumentors.extend(runtime.instrumentors)
                handle.enabled = True
                return handle
            except Exception:
                if handle._fastapi_instrumented:
                    with suppress(Exception):
                        FastAPIInstrumentor.uninstrument_app(app)
                if created:
                    _uninstrument_standard_libraries(runtime.instrumentors)
                    with suppress(Exception):
                        runtime.meter_provider.shutdown()
                    with suppress(Exception):
                        runtime.tracer_provider.shutdown()
                    _RUNTIME = None
                raise
    except Exception as exc:
        logger.warning("OpenTelemetry setup unavailable (%s)", type(exc).__name__)
        return handle
