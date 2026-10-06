# Stage B — Observability Foundation Implementation Spec

**Goal:** Establish the production observability substrate before MCP implementation so all later external capability work is instrumented from day one.

## 1. Objectives

Deliver:

- OpenTelemetry foundation
- context propagation
- stable runtime span taxonomy
- low-cardinality metrics foundation
- structured-log correlation
- Collector baseline
- first instrumentation for FastAPI, HTTP, PostgreSQL, Investigation Run, and Tool Execution

Do not attempt a full observability product in this stage.

## 2. Architecture constraints

- Telemetry != business truth
- Observability outage must not break Investigation/Response flows
- No raw prompt/model response/full ToolResult/full Alert/Event payload in telemetry by default
- No secrets/tokens/credentials
- Do not make Domain/Application depend on a vendor backend
- Do not use LangGraph internal node names as durable public telemetry contracts

## 3. Initial instrumentation scope

### 3.1 FastAPI

Instrument inbound request lifecycle and propagate W3C trace context.

Safe attributes may include route template, method, status class, service component, and approved correlation IDs. Avoid raw URL/query strings when they may contain sensitive data.

### 3.2 HTTP clients

Instrument HISIEM outbound calls and future provider calls through common HTTP instrumentation where possible.

### 3.3 PostgreSQL

Use standard DB instrumentation. Do not capture sensitive SQL bind values. Ensure SQL text capture policy is explicitly decided and safe by default.

### 3.4 Investigation runtime

Create semantic top-level spans such as `investigation.run`, `graph.invoke`, `investigation.persist` without exposing chain-of-thought or raw LangGraph checkpoints.

### 3.5 Tool execution

Instrument `tool.execute` with low-risk attributes:

- tool_name
- provider_type=native
- result category
- safe error category
- retry/attempt count where available

## 4. Context propagation

### 4.1 HTTP

Use W3C Trace Context.

### 4.2 Durable dispatcher

Design and implement explicit trace continuation or span-link semantics across persisted async work. Do not assume thread-local context survives dispatch.

The durable message remains business truth; trace context is optional diagnostic context.

## 5. Metrics foundation

Implement a small stable baseline rather than dozens of ad-hoc metrics.

Required initial metrics:

- investigation duration/total/failure
- tool duration/calls/errors/retries
- LLM duration/calls/errors/token counts if the current provider boundary allows clean instrumentation
- retrieval duration/hit_count/errors/retrieval_mode if Knowledge path is in scope without redesign
- durable queue depth/retry/dead-letter where existing durable APIs expose these safely
- response attention_required counter if available without coupling

### 5.1 Cardinality policy

Metrics labels may include:

- tool_name
- tool_provider
- model_provider
- model_family
- retrieval_mode
- result
- error_category
- response_state
- operation

Metrics labels must not include:

- investigation_id
- trace_id/span_id
- tenant_id
- user_id
- alert_id
- tool_invocation_id
- execution_command_id
- request_id

Add tests or validation utilities that make accidental high-cardinality dimensions difficult to introduce.

## 6. Structured logging

Do not rewrite all logging.

Add common structured context/correlation support so existing Python logs can carry:

- trace_id/span_id where present
- approved business correlation IDs in logs only
- operation/component
- safe error category

Ensure sensitive-data redaction remains explicit.

## 7. Collector baseline

Provide a minimal OTel Collector configuration that can:

- receive OTLP
- batch
- apply safe filtering/redaction if needed
- export to a configurable backend target

Do not bind business code to a particular observability vendor.

## 8. Backend decision

This stage may select a pragmatic development backend, but the code contract must remain OTel-native. Selection criteria:

- trace search
- metrics queries
- log search/correlation if logs included
- basic dashboards
- local/dev operability
- retention controls

Phoenix/LangSmith remain optional and cannot replace the OTel baseline.

## 9. Failure behavior

- exporter unavailable -> business flow continues
- Collector unavailable -> business flow continues
- telemetry serialization error -> fail safely without changing business outcome
- instrumentation must not mutate domain state

## 10. Tests

Focused tests/acceptance should cover:

- incoming trace context accepted/continued
- outgoing HISIEM HTTP spans created
- investigation.run span exists
- tool.execute span exists
- async durable work has continuation/link semantics
- metrics labels remain low-cardinality
- raw prompts/results/secrets absent from captured attributes
- telemetry backend outage does not break core workflow
- structured logs contain correlation when trace exists

## 11. Deliverables

- common OTel bootstrap/config module
- context propagation utilities
- base instrumentation
- metrics definitions
- structured-log correlation hook
- Collector config/docs
- focused tests
- Operations note: how to enable/disable/export telemetry without secrets

## 12. Acceptance

Stage B passes when one Investigation can be followed through API -> runtime -> Tool/HISIEM/DB with trace context, key metrics are emitted with safe cardinality, logs correlate to traces, and disabling/breaking telemetry does not alter the business result.

