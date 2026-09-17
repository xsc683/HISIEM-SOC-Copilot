"""E4 telemetry capture harness: real Stage B telemetry, observed in-process.

E4 must measure the REAL observability pipeline, so this module does not fake it:
it installs the production ``setup_telemetry`` bootstrap and only swaps the
**export destination** for an in-process exporter (the same technique the Stage B
bootstrap tests already use). Everything else — the tracer provider, the span
processors, ``start_span`` / ``linked_worker_span``, ``sanitize_metric_attributes``,
``bind_log_context`` — is the production code path.

Test-support only: nothing here is production code, and nothing in ``src`` imports it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from hisiem_soc_copilot.config import ObservabilitySettings
from hisiem_soc_copilot.evaluation_harness.cross_plane_observability import SpanFact
from hisiem_soc_copilot.infrastructure.observability import bootstrap as bootstrap_module
from hisiem_soc_copilot.infrastructure.observability import metrics as metrics_module
from hisiem_soc_copilot.infrastructure.observability.bootstrap import setup_telemetry

#: The status word a finished span carries when the SDK left it unset.
_STATUS_WORDS = {0: "unset", 1: "ok", 2: "error"}


def _status_category(span: Any) -> str:
    status = getattr(span, "status", None)
    code = getattr(status, "status_code", None)
    if code is None:
        return "unset"
    value = getattr(code, "value", code)
    return _STATUS_WORDS.get(int(value), "unset")


@contextmanager
def span_capture(monkeypatch: Any) -> Iterator[InMemorySpanExporter]:
    """Install the REAL telemetry bootstrap and capture its spans in-process.

    Only the exporter/processor classes are substituted, exactly as the Stage B
    bootstrap test does; the pipeline that decides *what* a span is remains
    production code. The SDK's providers are process-global and write-once, so
    when a provider is already installed the capture attaches its span processor to
    that provider rather than trying to replace it.
    """
    import opentelemetry.exporter.otlp.proto.grpc.trace_exporter as trace_exporter
    import opentelemetry.sdk.metrics.export as metrics_export
    import opentelemetry.sdk.trace.export as trace_export
    from opentelemetry import trace as trace_api
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()
    monkeypatch.setattr(trace_export, "BatchSpanProcessor", SimpleSpanProcessor)
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", lambda endpoint: exporter)
    monkeypatch.setattr(
        metrics_export,
        "PeriodicExportingMetricReader",
        lambda exporter, export_interval_millis: metric_reader,
    )

    # The SDK's providers are process-global AND write-once, so a capture must both
    # claim a fresh install and hand the process back a PRISTINE, uninstalled state:
    # otherwise a later test in the same process (the Stage B bootstrap test) could
    # never install its own provider. This is test-local SDK state manipulation, not
    # a production path.
    import opentelemetry.metrics._internal as metrics_internal
    from opentelemetry.util._once import Once

    def _reset_to_uninstalled() -> None:
        trace_api._TRACER_PROVIDER = None
        trace_api._TRACER_PROVIDER_SET_ONCE = Once()
        metrics_internal._METER_PROVIDER = None
        metrics_internal._METER_PROVIDER_SET_ONCE = Once()
        bootstrap_module._RUNTIME = None

    _reset_to_uninstalled()
    settings = ObservabilitySettings(
        tracing_enabled=True,
        otlp_endpoint="http://collector.invalid:4317",
        metric_export_interval_millis=3_600_000,
    )
    setup_telemetry(FastAPI(), settings)
    runtime = bootstrap_module._RUNTIME
    provider = trace_api.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield exporter
    finally:
        # Library instrumentation is process-global and sticky too: leaving httpx /
        # SQLAlchemy / psycopg instrumented against a provider this capture discarded
        # would silently starve any later test that installs its own pipeline.
        if runtime is not None:
            bootstrap_module._uninstrument_standard_libraries(list(runtime.instrumentors))
        _reset_to_uninstalled()


@contextmanager
def span_capture_in_process(monkeypatch: Any) -> Iterator[InMemorySpanExporter]:
    """Alias kept explicit: the E4 capture is always in-process."""
    with span_capture(monkeypatch) as exporter:
        yield exporter


@contextmanager
def metric_label_capture(monkeypatch: Any) -> Iterator[list[dict[str, str]]]:
    """Record the label sets production hands to the REAL metric sanitizer.

    The wrapper DELEGATES to the production sanitizer, so behaviour is unchanged;
    it only observes what production asked to record. Measuring those captured sets
    is what makes the cardinality gate a measurement of production rather than a
    restatement of the allowlist.
    """
    captured: list[dict[str, str]] = []
    real = metrics_module.sanitize_metric_attributes

    def _observing(attributes: Mapping[str, object] | None) -> dict[str, str] | None:
        if attributes:
            captured.append({str(key): str(value) for key, value in attributes.items()})
        return real(attributes)

    monkeypatch.setattr(metrics_module, "sanitize_metric_attributes", _observing)
    monkeypatch.setattr(metrics_module, "_INSTRUMENTS", {})
    monkeypatch.setattr(metrics_module, "_METER", _RecordingMeter())
    yield captured


class _RecordingMeter:
    """A meter that accepts instruments so ``record_*`` runs its real logic."""

    def create_counter(self, *, name: str, unit: str) -> _RecordingInstrument:
        del name, unit
        return _RecordingInstrument()

    def create_histogram(self, *, name: str, unit: str) -> _RecordingInstrument:
        del name, unit
        return _RecordingInstrument()


class _RecordingInstrument:
    def add(self, value: int, attributes: dict[str, str]) -> None:
        del value, attributes

    def record(self, value: float, attributes: dict[str, str]) -> None:
        del value, attributes


def project_spans(spans: Sequence[Any]) -> tuple[SpanFact, ...]:
    """Project finished spans into BOUNDED facts (E4 §21).

    Only the operation name, a status *category*, parent/link presence, and the
    attribute **keys** survive. No attribute value, no span event, and no payload
    ever leaves this function.
    """
    facts: list[SpanFact] = []
    for span in spans:
        context = getattr(span, "context", None)
        valid = context is not None and context.is_valid
        trace_id = f"{context.trace_id:032x}" if valid else ""
        span_id = f"{context.span_id:016x}" if valid else ""
        parent = getattr(span, "parent", None)
        attributes = getattr(span, "attributes", None) or {}
        facts.append(
            SpanFact(
                operation=str(getattr(span, "name", "")),
                kind=str(getattr(getattr(span, "kind", None), "name", "INTERNAL")),
                status_category=_status_category(span),
                parent_present=parent is not None and getattr(parent, "is_valid", False),
                link_count=len(getattr(span, "links", ()) or ()),
                trace_id=trace_id,
                span_id=span_id,
                attribute_keys=tuple(sorted(str(key) for key in attributes)),
            )
        )
    return tuple(facts)


def telemetry_surfaces(
    *,
    spans: Sequence[SpanFact],
    metric_label_sets: Sequence[Mapping[str, str]],
) -> dict[str, str]:
    """Bounded, transient telemetry surfaces for the oracle/secret scans.

    The surfaces carry the KEYS (never the values) plus the real metric label
    key/value pairs — the two places a stray evaluation identity or a credential
    could reach telemetry. They are scanned and discarded; only marker spellings
    survive into a measurement.
    """
    surfaces: dict[str, str] = {}
    for span in spans:
        surfaces[f"span:{span.operation}"] = " ".join(span.attribute_keys)
    for index, label_set in enumerate(metric_label_sets):
        surfaces[f"metric_labels:{index}"] = " ".join(
            f"{key}={value}" for key, value in sorted(label_set.items())
        )
    return surfaces


__all__ = [
    "metric_label_capture",
    "project_spans",
    "span_capture",
    "span_capture_in_process",
    "telemetry_surfaces",
]
