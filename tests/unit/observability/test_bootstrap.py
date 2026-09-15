from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import opentelemetry.exporter.otlp.proto.grpc.metric_exporter as metric_exporter
import opentelemetry.exporter.otlp.proto.grpc.trace_exporter as trace_exporter
import opentelemetry.sdk.metrics.export as metrics_export
import opentelemetry.sdk.trace.export as trace_export
from fastapi import FastAPI
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine, text

from hisiem_soc_copilot.config import ObservabilitySettings
from hisiem_soc_copilot.infrastructure.observability.bootstrap import (
    _REDACTED_HTTP_ATTRIBUTES,
    _safe_http_url,
    _sanitize_client_request,
    _sanitize_client_response,
    _sanitize_server_request,
    setup_telemetry,
)


async def test_enabled_runtime_uses_real_instrumentors_and_preserves_context(
    monkeypatch,
) -> None:
    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()
    monkeypatch.setattr(
        trace_export, "BatchSpanProcessor", SimpleSpanProcessor
    )
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", lambda endpoint: span_exporter)
    monkeypatch.setattr(
        metric_exporter, "OTLPMetricExporter", lambda endpoint: object()
    )
    monkeypatch.setattr(
        metrics_export,
        "PeriodicExportingMetricReader",
        lambda exporter, export_interval_millis: metric_reader,
    )

    propagated: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            propagated.append(self.headers.get("traceparent"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    target_url = f"http://127.0.0.1:{server.server_port}/private?token=secret"
    app = FastAPI()

    @app.get("/probe")
    async def probe() -> dict[str, bool]:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                target_url, headers={"Authorization": "Bearer secret"}
            )
        return {"ok": response.is_success}

    settings = ObservabilitySettings(
        tracing_enabled=True,
        otlp_endpoint="http://collector.test:4317",
        metric_export_interval_millis=3_600_000,
    )
    handle = setup_telemetry(app, settings)
    try:
        assert handle.enabled is True
        inbound = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/probe", headers={"traceparent": inbound})
        assert response.json() == {"ok": True}

        engine = create_engine("sqlite://")
        with engine.connect() as connection:
            connection.execute(text("select 1"))
        engine.dispose()

        spans = span_exporter.get_finished_spans()
        names = {span.name for span in spans}
        assert "GET /probe" in names
        assert "GET" in names
        assert "connect" in names
        assert propagated and propagated[0]
        assert propagated[0].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
        values = [str(value) for span in spans for value in span.attributes.values()]
        assert not any(
            secret in value
            for secret in ("token=secret", "Bearer secret")
            for value in values
        )
    finally:
        handle.shutdown()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_disabled_telemetry_returns_noop_handle() -> None:
    handle = setup_telemetry(object(), ObservabilitySettings(tracing_enabled=False))
    assert handle.enabled is False
    handle.shutdown()


def test_invalid_export_endpoint_fails_closed_without_starting_sdk() -> None:
    settings = ObservabilitySettings(tracing_enabled=True, otlp_endpoint="  ")
    handle = setup_telemetry(object(), settings)
    assert handle.enabled is False
    handle.shutdown()


def test_enabled_setup_is_idempotent_across_lifespans(caplog) -> None:
    settings = ObservabilitySettings(
        tracing_enabled=True,
        otlp_endpoint="http://127.0.0.1:4317",
        metric_export_interval_millis=3_600_000,
    )
    app = FastAPI()

    with caplog.at_level(logging.WARNING):
        first = setup_telemetry(app, settings)
        duplicate = setup_telemetry(app, settings)
        duplicate.shutdown()
        first.shutdown()

        restarted = setup_telemetry(FastAPI(), settings)
        restarted.shutdown()

    assert first.enabled is True
    assert duplicate.enabled is True
    assert restarted.enabled is True
    assert "already instrumented" not in caplog.text
    assert "Overriding of current" not in caplog.text


def test_outbound_http_url_keeps_only_origin() -> None:
    assert _safe_http_url("https://user:secret@example.test:8443/path?q=token") == (
        "https://example.test:8443/"
    )
    assert _safe_http_url("file:///etc/passwd") == ""


def test_client_hooks_scrub_sensitive_fields_without_reading_requests() -> None:
    class Span:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

    span = Span()
    _sanitize_client_request(span, object())
    _sanitize_client_response(span, object(), object())
    assert set(span.attributes) == set(_REDACTED_HTTP_ATTRIBUTES)
    assert all(value == "" for value in span.attributes.values())


def test_server_hook_scrubs_sensitive_fields_without_reading_scope() -> None:
    class Span:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

    span = Span()
    _sanitize_server_request(span, {"url": "https://example.test/private?token=secret"})
    assert set(span.attributes) == set(_REDACTED_HTTP_ATTRIBUTES)
    assert all(value == "" for value in span.attributes.values())
