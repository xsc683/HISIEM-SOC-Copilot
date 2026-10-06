# Observability Foundation

The application uses OpenTelemetry APIs and OTLP for diagnostic traces and metrics. Telemetry is disabled by default and is never an input to authorization, durable retry/idempotency, investigation state, response decisions, or business outcomes.

## Application configuration

Set the following environment variables to enable the SDK and standard FastAPI, HTTPX, SQLAlchemy, and psycopg instrumentation:

```dotenv
OBS_TRACING_ENABLED=true
OBS_SERVICE_NAME=hisiem-soc-copilot
OBS_OTLP_ENDPOINT=http://127.0.0.1:4317
OBS_TRACE_SAMPLE_RATIO=1.0
OBS_METRIC_EXPORT_INTERVAL_MILLIS=60000
```

`OBS_OTLP_ENDPOINT` is the OTLP/gRPC receiver address. `OBS_TRACE_SAMPLE_RATIO` is constrained to `0..1`; the default is `1.0`. Keep the setting false to disable SDK setup and instrumentation. If exporter or instrumentation setup fails, the application falls back to disabled telemetry and continues its business lifecycle. Each application lifespan removes its own instrumentation and force-flushes shared providers when the final active lifespan closes. Providers remain process-scoped rather than being shut down, so a later lifespan can reuse the OpenTelemetry global providers without a forbidden provider replacement.

## Collector

`infra/otel-collector/collector.yaml` is a minimal vendor-neutral Collector configuration. It accepts OTLP/gRPC and OTLP/HTTP, batches telemetry, removes sensitive trace fields and high-cardinality metric dimensions, and exports to an OTLP target supplied at runtime:

```text
OTEL_EXPORTER_OTLP_ENDPOINT=<collector-or-backend-host>:4317
```

Provide authentication material through the Collector's deployment secret mechanism if the selected target requires it; do not commit credentials or put them in application telemetry settings. Point the application at the Collector receiver, and point the Collector exporter at a distinct upstream target to avoid exporting back into its own receiver. Bind the Collector ports to a trusted network boundary and apply the organization's access controls.

## Data and metric policy

The SDK does not capture HTTP headers, SQL bind parameters, SQL comments, prompt/response bodies, alert or event payloads, or embedding vectors. Outbound HTTP URLs are reduced to origin; inbound URL/query, credentials/cookies, and peer-address attributes are scrubbed before span export. The Collector repeats sensitive-field deletion as defense in depth.

Metrics are created from a fixed registry. Only explicitly allowlisted names, keys, and bounded values are accepted; an observation containing an unknown dimension is dropped. Investigation, tenant, trace/span, request, alert, user, tool-invocation, and execution-command identifiers are not metric labels. Durable queue depth is not emitted because the current outbox API does not expose a safe aggregate measurement.

The stable semantic spans are `investigation.run`, `graph.invoke`, `tool.execute`, `knowledge.retrieve`, `investigation.persist`, `llm.call`, `response.submit`, `response.observe`, and `durable.dispatch`. Durable outbox rows may carry one validated W3C `traceparent`; a worker starts a new trace and links to the producer context. Missing or malformed context is ignored. Business event payloads and `correlation_id` semantics are unchanged.
