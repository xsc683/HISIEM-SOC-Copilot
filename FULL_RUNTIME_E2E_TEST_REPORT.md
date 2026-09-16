# HISIEM + HISIEM-SOC-Copilot Full Runtime E2E Test Report

- Date: 2026-09-16 (first run) / 2026-09-17 (closure run) - local, Asia/Shanghai
- Tester: Claude Code
- HISIEM branch: `add_frame`
- HISIEM HEAD: `bce6586977055876675d052b03af8d58066aa2e6`
- HISIEM remote SHA: `origin/add_frame` = `bce6586977055876675d052b03af8d58066aa2e6` (in sync)
- Copilot branch: `capability-mcp`
- Copilot HEAD: `09487a72cf46a368fe82a797b7e8dabf7638e07d`
- Copilot remote SHA: `origin/capability-mcp` = `09487a72cf46a368fe82a797b7e8dabf7638e07d` (in sync)
- Environment: Windows 11, Docker Desktop, JDK 21.0.12, Node 22, Python 3.13.14, `.venv` editable install
- Test namespace: `e2e-<epoch>` (per-run unique marker in log lines, users, and source IPs)

> Note on the prompt's premise: HISIEM had **no** uncommitted Stage D changes at the start of this run —
> the Stage D frontend/docs work was already committed and pushed as `bce6586`
> ("feat(copilot-ui): make the workspace answer \"what now\" and keep authority legible").
> Nothing was reset, restored, checked out, or cleaned.

## Final Status

- HISIEM RUNTIME E2E: **PASS**
- COPILOT RUNTIME E2E: **PASS**
- CROSS-PROJECT E2E: **PASS**
- PRE-SEAL SYSTEM GATE: **PASS**

The first run reported `FAIL` because ten matrix rows had not been executed and Spotless was
failing. The 2026-09-17 closure run executed every one of those rows against the same real runtime
(no restart from zero, no mock substitution), fixed `DEFECT-004` with a regression test, resolved
the Maven Spotless gate, and found one further defect (`DEFECT-006`) which it also fixed and
verified. The gate is now claimed from the completed matrix, not from the earlier partial one. See
**Closure Run (2026-09-17)** for the per-row evidence.

The original FAIL rationale follows, for the record: the gate was withheld because several required
scenarios in the matrix were **not executed** in the first run (KNOWLEDGE, MCP, OTEL, DURABILITY,
ATTENTION, RULE, CASE, RISK, SIEM-SOAR, TENANT cross-read), and because the repository still carried
a **pre-existing** Spotless violation. Per §37.2 the gate may only be claimed when the required
coverage is complete, so it was reported FAIL rather than PASS.
## Runtime Topology

| Component | Runtime | Endpoint/Port | Real/Mock | Result |
|---|---|---|---|---|
| HISIEM PostgreSQL | docker `siem-postgres` (postgres:16.4) | 5432 | Real | UP (healthy) |
| Elasticsearch | docker `siem-elasticsearch` (8.14.0) | 9200 | Real | UP (yellow, 1 node) |
| Kafka | docker `siem-kafka` (apache/kafka:3.8.0) | 9092→9094 | Real | UP (healthy) |
| Logstash | docker `siem-logstash` (8.14.0) | 5000–5007/9600 | Real | UP (healthy) |
| Flink JobManager | docker `siem-flink-jobmanager` (flink:2.1-java21) | 8081 | Real | UP (healthy) |
| Flink TaskManager | docker `siem-flink-taskmanager` | — | Real | UP |
| Flink detection job | `SIEM Detection Engine`, jobID `180b93e13e7cf6c52111b911d8c284f7` | — | Real | RUNNING (6 rules enabled) |
| HISIEM control-api | host JVM, `hsiem-platform.jar` | 8080 | Real | UP (`/actuator/health` → `{"status":"UP"}`) |
| detection-controller | not started | — | — | N/A (not required for this matrix) |
| SOAR worker | host JVM, `hsiem-soar-worker.jar` | — | Real | RUNNING (after fixes, see DEFECT-001..003) |
| HISIEM Vue | Vite dev server | 5173 | Real | UP (HTTP 200) |
| Copilot PostgreSQL | docker `copilot-postgres` | 5433 | Real | UP (Alembic migrated) |
| Copilot FastAPI | host uvicorn `hisiem_soc_copilot.main` | 8000 | Real | UP (`/healthz` → `{"status":"ok"}`) |
| Deterministic model server | temporary OpenAI-compatible HTTP server | 8099 | **Test double for the external LLM only** | UP |
| Local MCP server | official-SDK Streamable HTTP server (host python) | 8100 | Real | UP — `e2e_lookup` admitted, schema fingerprint `182c8c08...`; a drifted server used for `SCHEMA_MISMATCH` |
| OTel Collector | docker `e2e-otel-collector` (otel/opentelemetry-collector-contrib 0.115.1) → `e2e-otel-sink` | 4317/4318 | Real | UP — repo `infra/otel-collector/collector.yaml`, only the local exporter endpoint and `tls.insecure` changed |

The deterministic model server replaces **only** the external LLM. HISIEM, both databases, the BFF,
tool execution, Evidence persistence, the durable dispatcher, and the SOAR bridge are all real.

## Scenario Matrix

| ID | Scenario | Runtime Path | Result | Durable Evidence | External Evidence | UI Evidence | Notes |
|---|---|---|---|---|---|---|---|
| ENV | Runtime health | all containers + 5 host processes | **PASS** | — | `pg_isready`, ES `_cluster/health`, Kafka topics, Flink `list` RUNNING, `/actuator/health`, `/healthz`, Vite 200 | — | Readiness judged by semantics, not port-listen |
| AUTH | Auth/BFF/service trust | Browser→HISIEM; HISIEM→Copilot | **PASS** | real session tokens, runtime users | 18/18 checks | login page exercised in browser | 428 password-rotation gate observed |
| INGEST | Real log ingestion | TCP 5000 → Logstash → ES + Kafka | **PASS** | 6 ECS docs `siem-events-*` | `@timestamp`, `host.name`, `user.name` (marker), `source.ip`, `event.action/outcome/category`, `pipeline` | — | unique `e2e-<epoch>` marker |
| DETECT | Kafka → Flink → Alert | Logstash→Kafka→Flink→ES | **PASS** | 2 alerts in `siem-alerts` | Kafka offsets (21/6), DLQ empty, job RUNNING | — | window rule `event_count=5` + single-event rule |
| RULE | Rule lifecycle | HISIEM API → `infra/rules/*.yaml` | **PASS** | new rule `rule-e2e-runtime-001` read back byte-identical via `GET /api/detection-rules/{id}` | create `201`; four invalid-DSL writes → `400 INVALID_ARGUMENT` (`unsupported rule condition: field_between`, `condition.field is not a valid ECS field`, `condition.conditions has an invalid size`, `unsupported rule condition: nope`); the rejected rule is `404`; the previous valid rule is byte-identical after all four rejections | — | The temporary rule was deleted afterwards. `RuleLintTest` asserts the published-rule count is exactly 6 and passes |
| ALERT | Alert workflow | ES ↔ HISIEM API | **PASS** | alert `86701bf9…`, `c66f003e…` | identical `alert.id`/rule/severity/`event_count` in ES and API; detail by `_id` | alert detail page in browser | see DEFECT-004 |
| CASE | Case workflow | 2 open alerts → case → status → timeline → resolve | **PASS** | case `case-20260917-ff78`, `aggregation=manual`, two `alert_ids`, entities `203.0.113.9` / `198.51.100.142`, status `open`→`investigating`→`resolved(true_positive)`, timeline 10 real ES events, alert cascade → `closed` | negatives: unknown case `404`, invalid status `400`, unknown alert `404`, single-alert manual aggregation `400`, resolve without verdict `400`, append to a resolved case `400` | — | Adding an alert already in the case is an idempotent `200 {"added":[]}`, not an error. Manual aggregation requires ≥2 open alerts by design |
| RISK | Entity criticality → recalculation → audit | `/api/settings/criticality` → recalc task → `entity-risk.py` → ES projection | **PASS** | controlled A/B on the real entity `198.51.100.142`: `asset.criticality` `1.0`→`2.0`, `risk_score` `186`→372`, projection `@timestamp` rewritten; then restored and verified back to `1.0` / `186` | audit rows in `audit_logs` for `criticality.set`, `criticality.recalc` (×2) and `criticality.delete`, each with actor and timestamp; the recalc task reached `succeeded` / `progress=100` | — | No direct DB insert. Negatives: invalid level, invalid type and malformed IP → `400`; deleting an unknown asset → `404` |
| COPILOT-LAUNCH | Alert → Investigation | Browser/BFF→AlertController→AgentLaunchService→Copilot | **PASS** | `investigation_id`, investigation row in Copilot PG | `POST /api/alerts/{id}/agent-investigation` → 200 + `redirectUrl` | workspace route resolves | idempotent per (tenant, alert) |
| COPILOT-RUN | Real investigation | Copilot→real HISIEM adapter→control-api→ES | **PASS** | 20 Evidence (19 `HISIEM_LOG_SEARCH` + 1 `SYSTEM`), 1 Finding, 1 Hypothesis SUPPORTED, verdict **MALICIOUS 0.9** | `hisiem.get_detection_rule` SUCCEEDED, `hisiem.search_events` SUCCEEDED | workspace renders verdict + evidence + finding | No FakeHisiem / MockTransport |
| KNOWLEDGE | Knowledge runtime | real Copilot PG (pgvector) + `knowledge.retrieve_security_guidance` through the real Agent | **PASS** | document `f5ffb06f-9172-4aec-86c3-09521c538962` v1, 4 chunks, ACTIVE profile `deterministic/deterministic-test-v1 dim=64 COSINE`; 3 `KNOWLEDGE` Evidence rows carrying a real `citation_identity`; the Finding cited knowledge evidence and **the verdict was downgraded to `INCONCLUSIVE` (confidence 0.0, "No platform-grounded finding supported the model's proposed verdict")** | `doctor` OK; hybrid `search` → 4 hits; `resolve-citation kcit:7d8419ba-...:57b183f82358` → **resolved**; retrieval provenance truthfully reports `LEXICAL_ONLY` (the Copilot process has no embedding provider configured, so HYBRID degrades instead of claiming a mode it did not use) | browser: `平台事实 0 · 支持性上下文 3`; knowledge evidence is never labelled a platform fact | No FakeRepository: ingestion went through `knowledge.cli ingest-file`. ATT&CK `import-attack` was not re-run (no operator-supplied STIX bundle is available locally and runtime fetching is forbidden by design); canonical ATT&CK resolution was covered by the Stage A validation |
| MCP | Real local MCP | Copilot `MCPToolProvider` → Streamable HTTP `:8100` | **PASS** | admitted `mcp.e2e.lookup` invoked by the Agent and persisted as real Evidence; the `mcp.call` span nests under `tool.execute` | fingerprint match `182c8c08...`; boundary cases: unadmitted `e2e_dynamic_only` stayed non-selectable, remote error typed, `RESULT_TOO_LARGE` without truncation, prompt-injection content kept as DATA, schema drift → `SCHEMA_MISMATCH` fail-closed | workspace renders the MCP-derived evidence | One classification limitation recorded as `DEFECT-005` |
| RESPONSE | Response proposal | Browser→BFF→Copilot→Policy | **PASS** | proposal `56f525a8…`, `WAITING_APPROVAL`, policy `REQUIRE_APPROVAL` | target derived server-side from the persisted alert | response tab render | browser supplies no target/tenant/actor |
| APPROVE | Human approve | BFF→Copilot→durable command | **PASS** | `decision=APPROVE`, `execution_queued=true` | exact `expected_revision`+`expected_content_hash` | approve button + confirmation | TOCTOU: stale/wrong → 409, zero execution |
| REJECT | Human reject | BFF→Copilot | **PASS** | proposal `REJECTED`, `execution_queued=false` | HISIEM executions 15 → 15 | response tab shows 已驳回 | **zero durable execution command** |
| SOAR | Copilot → HISIEM SOAR | Copilot→`/api/internal/soar/executions`→SoarService→SOAR worker | **PASS** | Copilot observed `exec-eec71561-4b62-4d44-b9d0-f764250eaf58` QUEUED→**SUCCEEDED** | HISIEM API: status `success`, playbook `pb-a3b539a1…`, trigger `MANUAL`, nodeRuns `start`+`end` success | workspace execution block | observed via `GET /api/internal/soar/executions/{id}` |
| IDEMPOTENCY | Duplicate/retry convergence | repeat identical approve | **PASS** | same proposal, no new command | HISIEM executions 12 → 12 (no duplicate side effect) | — | — |
| DURABILITY | Restart recovery | Copilot restarted with a pending durable command | **PASS** | the pending command resumed after restart and produced **exactly one** HISIEM execution (23 → 24); no duplicate | no database and no volume was deleted | — | — |
| FAILURE | Failure/recovery | HISIEM unavailable to Copilot; Copilot down; MCP unreachable | **PASS** | bounded errors, no partial rows, no fabricated Evidence | HISIEM → Copilot with Copilot down gave a bounded `503`, HISIEM stayed fully usable and recovered the moment Copilot returned; MCP unreachable gave a typed fail-closed failure with **zero Evidence** | — | The MCP-unreachable classification limitation is recorded as `DEFECT-005` |
| ATTENTION | ATTENTION_REQUIRED | approved command → transient `503` → retry budget exhausted | **PASS** | submission `status=ATTENTION_REQUIRED`, `attempt_count=10`, `last_error_code=HTTP_503`, `attention_required_at` set, `submitted_at=null`, `execution=null` | the failing SOAR endpoint counted **10** submit attempts and **0 further attempts over the following 180 s**; no provider-rejected claim and no fabricated execution id anywhere in the payload | 14/14 browser assertions green (summary banner plus the 従应 tab detail), no non-2xx API call, no console errors | Reached in real time (~8 min). **No production retry semantics were changed** |
| TENANT | Tenant isolation | a real second tenant | **PASS** | non-member tenant → `403`; a missing tenant header does not widen scope; a real second tenant's resources are invisible to the default tenant | cross-tenant investigation read and cross-tenant approve both refused | — | The creating admin is auto-added as **owner** of the new tenant (`/api/tenants/mine` confirms the membership), so that access is legitimate membership rather than a leak. Full ES data-plane multi-tenancy is **not** claimed: it is outside the current contract |
| SECURITY | Security negative | real HTTP boundary | **PASS** | — | 5/5 internal-SOAR negatives refused (missing bearer, invalid bearer, wrong scheme, cross-service token, tenant mismatch) with sanitized errors; MCP endpoint/host policy refuses non-configured and SSRF-shaped endpoints; plus missing/invalid session, role denial, tenant scoping, approval TOCTOU, prompt injection and search-span policy | — | No authentication, tenant, approval, idempotency or security boundary was weakened at any point in this validation |
| UI | True browser E2E | Vue→BFF→backend, **no mocks** | **PASS** | real investigation + alert rendered | no `page.route`/`route.fulfill`; browser never called `:8000` | 6/6 Playwright runtime specs | narrow viewport, refresh reconstruction, no leakage |
| OTEL | Observability | Copilot → real Collector (repo config) → sink | **PASS** | 2,536 spans collected at the sink; required spans present (`tool.execute`, `mcp.call`, `llm.call`, `graph.invoke`, `investigation.run`, `durable.dispatch`); nesting proven `mcp.call → tool.execute → graph.invoke → investigation.run → durable.dispatch`; low-cardinality instruments `tool.calls`, `mcp.calls`, `llm.calls`, `investigation.total` | **zero secret leakage**: the agent bearer, the internal-service token, and both session tokens are all absent value-level; all 11 forbidden high-cardinality dimensions absent; all 15 sensitive trace attributes on the collector delete-list absent; no span name carries a secret or exceeds 40 characters | — | **Telemetry outage is not business outage**: with the Collector stopped, a full investigation still completed (`MALICIOUS`, 2 tools, 37 Evidence, 1 Finding) and a response proposal still succeeded; telemetry resumed after the Collector restarted |
| SIEM-SOAR | HISIEM native SOAR lifecycle | `/api/soar/*` driven directly, **independent of Copilot** | **PASS** | playbook `pb-7c895046...` authored → published → enabled (revision 8); execution `exec-899d6e3e-...` node runs `start` → `gate`(human) → `mkcase`(business) → `ack`(business) → `end`, all `success` | **the human gate ran first and the alert was proven unmutated while waiting** (`status=open`, no `case_id`, no `operator`); only after approval did the real effects appear: `alert.status=acknowledged`, `alert.operator=soar:exec-899d6e3e-...`, `alert.case_id=case-20260917-27f7` (`aggregation=soar`), case timeline 10 events | — | Also observed: an invalid transition was truthfully rejected (`不允许从 open 流转到 investigating`) with the alert left untouched |

## Evidence Classification

### TRUE RUNTIME E2E

- **INGEST**: real syslog over TCP → Logstash → `siem-events-*` (6 ECS documents carrying the unique
  `e2e-<epoch>` marker) and Kafka `siem-events` (partition offsets advanced; DLQ stayed empty).
- **DETECT**: the running `SIEM Detection Engine` Flink job produced a window alert
  (`rule-ssh-brute-force-001`, `critical`, `event_count=5`, `alert.entity=198.51.100.142`, with the
  five real `related_events`) and a single-event alert (`rule-ssh-auth-failure-001`).
- **ALERT**: the same alert identities are visible in Elasticsearch and through the HISIEM control-api
  with matching `alert.id`, `rule_id`, `severity`, `status`, `event_count`, `source.ip`.
- **COPILOT-LAUNCH**: `POST /api/alerts/{es_id}/agent-investigation` created a Copilot investigation
  (`ce8a94e3-929c-4f18-8f6a-6aa062a7e9e2`) and returned its workspace route.
- **COPILOT-RUN**: the Copilot process called the real HISIEM control-api through `HisiemHttpAdapter`
  and ran the real graph end to end: `hisiem.get_detection_rule` and `hisiem.search_events` both
  SUCCEEDED, producing 20 persisted Evidence rows (19 `HISIEM_LOG_SEARCH` from real events + 1
  `SYSTEM` rule metadata), 1 Finding citing platform evidence, 1 SUPPORTED hypothesis, and the
  immutable verdict **MALICIOUS 0.9**. No FakeHisiem, FakeUnitOfWork, or MockTransport was involved.
- **RESPONSE**: a proposal created through the HISIEM BFF came back `WAITING_APPROVAL` with
  `policy_decision=REQUIRE_APPROVAL`, and its `target_refs` were derived server-side from the
  investigation's persisted source alert — the browser supplied no target, tenant, actor, or credential.
- **APPROVE / REJECT / SOAR / IDEMPOTENCY**: approval bound to the exact revision + content hash
  produced a durable command that reached HISIEM's internal SOAR API and executed as
  `exec-eec71561-4b62-4d44-b9d0-f764250eaf58` (`success`, nodeRuns `start`+`end`), which Copilot then
  **observed** and surfaced in the workspace. Rejection produced zero execution commands. A repeated
  identical approval produced no duplicate execution.
- **UI**: a real (unmocked) Chromium session logged in through the HISIEM login page and drove
  `/overview`, `/logs`, `/alerts`, `/alerts/{id}`, `/cases`, `/soar/executions`, and
  `/copilot/investigations/{id}`, asserting the authority labels, the finding→evidence drawer, the
  response invariant banner, hard-refresh reconstruction, narrow-viewport usability, and the absence of
  CoT/prompt/checkpoint/bearer leakage. The browser never issued a request to `:8000`.

### Integration Evidence

- HISIEM root Maven reactor (17 modules, including Testcontainers-backed tests where Docker is
  available) — BUILD SUCCESS.
- Copilot full pytest DB-backed integration suites (Postgres 5434 scratch DB) — 1763 passed, 9 skipped.

### Logical E2E

- Copilot `tests/e2e/*` (FakeHisiem / in-memory graph) — kept green as regression evidence only, not as
  substitute for any PASS above.

### Browser Contract E2E

- `web/e2e/copilot-authority.spec.js`, `investigation-workspace.spec.js`, `response-workflow.spec.js`,
  `log-search-overview.spec.js`, `playbook-editor.spec.js` (mock `page.route`) — 34 passed. Regression
  evidence only.

## Production Defects Found

### DEFECT-001 — The standalone SOAR worker cannot start: tenant mapper is not scanned

- Severity: **High** (documented runnable service, dead on arrival)
- Repository: HISIEM
- Component: `applications/soar-worker` → `SoarWorkerApplication` `@MapperScan`
- Reproduction: `java -jar applications/soar-worker/target/hsiem-soar-worker.jar`
  → `No qualifying bean of type 'TenantMapper' … ` → `AuthService` → `TenantService` → `MyBatisTenantRepository`
- Root Cause: `@SpringBootApplication(scanBasePackages = "com.xscsiem.hsiem_platform")` component-scans
  `AuthService`, which requires `TenantService` → `TenantMapper`, but the worker's `@MapperScan` only
  covered the `control` and `soar` mapper classes — not the `tenant` package that `control-api` does scan.
- Fix: added `TenantMapper.class` to the worker's control-plane `@MapperScan` (mirrors `HsiemPlatformApplication`).
- Regression Test: `SoarWorkerContextTest` — boots the real Spring context on H2 (PostgreSQL mode) and
  resolves `TenantMapper`. The previous test only asserted annotations and never created a context.
- Runtime Revalidation: worker progressed past this error after the fix.
- Status: **FIXED** (uncommitted)

### DEFECT-002 — The standalone SOAR worker cannot start: no Jackson 2 `ObjectMapper` bean in a non-HTTP app

- Severity: **High**
- Repository: HISIEM
- Component: `applications/soar-worker` composition root
- Reproduction: after DEFECT-001 was fixed, the worker failed with
  `No qualifying bean of type 'com.fasterxml.jackson.databind.ObjectMapper'` while creating `alertService`
  → `elasticsearchGateway`.
- Root Cause: Spring Boot 4 auto-configures **Jackson 3** (`tools.jackson`, via `spring-boot-jackson`); the
  Jackson 2 `com.fasterxml.jackson.databind.ObjectMapper` bean is only auto-configured as part of the HTTP
  message converters (`spring-boot-http-converter`, pulled in by webmvc). `control-api` is a web app and
  therefore has it; the `WebApplicationType.NONE` worker does not — yet it still component-scans the same
  beans that require one.
- Fix: the worker's composition root now declares an explicit Jackson 2 `ObjectMapper` bean.
- Regression Test: same `SoarWorkerContextTest` (context creation fails without it).
- Runtime Revalidation: worker progressed past this error after the fix.
- Status: **FIXED** (uncommitted)

### DEFECT-003 — The standalone SOAR worker cannot start: agent/internal-service properties are not bound

- Severity: **High**
- Repository: HISIEM
- Component: `applications/soar-worker/src/main/resources/application.properties`
- Reproduction: after DEFECT-002 was fixed, the worker failed with
  `Copilot 服务凭据未配置：app.agent.bearer-token / HISIEM_AGENT_BEARER_TOKEN 必须为非空`
- Root Cause: the worker's `application.properties` never bound `app.agent.*` or
  `app.internal-service.*`, so `@Value("${app.agent.bearer-token:}")` resolved to the empty default even
  when `HISIEM_AGENT_BEARER_TOKEN` was exported in the environment.
- Fix: added the same property bindings `control-api` uses (values still come only from environment
  variables; an unset credential still fails closed).
- Regression Test: covered by the same context test (it supplies test-only values).
- Runtime Revalidation: the worker started successfully (`Started SoarWorkerApplication in 2.281 s`) and
  later processed the Copilot-triggered execution end to end.
- Status: **FIXED** (uncommitted)

### DEFECT-004 — `GET /api/alerts/{id}` returns HTTP 200 with an empty body for an unknown id

- Severity: **Medium** (API contract correctness; produced a misleading downstream error)
- Repository: HISIEM
- Component: `AlertController.detail` → `AlertService.detail` → `esGet("/siem-alerts/_doc/{id}")`
- Reproduction:
  - `GET /api/alerts/2d317caf86ab…` (ES `_id`) → `200` + full document (works)
  - `GET /api/alerts/ba9c511e-dfa2…` (the `alert.id` the list exposes) → **`200` with a 0-byte body**
  - Observed downstream: Copilot's alert hydration reported `EXTERNAL_SERVICE_ERROR:
    "HISIEM returned a non-JSON body"` (a 502 to HISIEM), instead of a clean 404.
- Root Cause: `service.detail(id)` returns `esGet(...)`, which yields `null` when Elasticsearch responds
  404; the controller then returns a null body with a 200 status. `AlertService.update` already guards
  this case with `if (cur == null) throw new NotFoundException(...)`; `detail` does not.
- Fix: **APPLIED (closure run).** `AlertService.detail` now mirrors `update()`: when `esGet` returns
  `null` (Elasticsearch answers `200` with `found:false`) it throws `NotFoundException`. `AlertService`
  was not redesigned; the change is the guard itself.

  ```java
  Map<String, Object> doc = esGet("/siem-alerts/_doc/" + id);
  if (doc == null) {
      throw new NotFoundException("Alert not found: " + id);
  }
  return doc;
  ```

- Regression Test: **ADDED** — `AlertServiceTest.detail_unknownAlert_isNotFound` mocks the **real**
  observed Elasticsearch shape (HTTP `200` + `{"found": false}`) rather than a synthetic `404`, plus
  `detail_foundAlert_returnsDocument` for the happy path. Result: 14 tests, 0 failures. (An earlier
  draft mocked `Response(404, ...)`, which was wrong: the gateway throws for non-2xx, and runtime
  measurement showed Elasticsearch answers `200` / `found:false` for a missing document.)
- Runtime Revalidation: `GET /api/alerts/0000000000000000000000000000000000000000` → **`404 NOT_FOUND`**;
  a real alert id → `200`. Copilot alert hydration now receives a clean `404` instead of a non-JSON body.
- Status: **FIXED and verified.**

### DEFECT-005 — An unreachable MCP server is classified `PROVIDER_ERROR`, not `UNAVAILABLE`

- Severity: **Low** (classification precision only; the failure is still typed, fails closed, and
  produces zero Evidence — no false success and no unsafe state)
- Repository: Copilot
- Component: `MCPToolProvider` failure mapping over the official MCP Python SDK
- Reproduction: stop the local MCP server and drive an admitted `mcp.*` capability through the Agent.
- Root Cause: the official high-level client collapses a transport failure into `MCPError(-32603)`,
  which is structurally indistinguishable from a genuine remote internal error. The Copilot maps that
  to `PROVIDER_ERROR`, which is truthful about the provider but does not separate "the server could not
  be reached" from "the server answered with an internal error".
- Fix: **NOT APPLIED, deliberately.** A message-string-based remap would be a fragile heuristic that
  could mislabel a real remote internal error — a worse failure than an imprecise but honest category.
  Separating the two correctly needs the transport failure to surface as a distinct exception type from
  the SDK, or a Copilot-side connectivity probe before the call; both are design changes beyond this
  validation's scope.
- Status: **OPEN** — a known classification limitation, not a correctness or security defect.

### DEFECT-006 — Client errors were reported as `500 INTERNAL_ERROR`

- Severity: **Medium** (API contract correctness; misleads client retry logic and pollutes the server error rate)
- Repository: HISIEM
- Component: `GlobalExceptionHandler` (control-api)
- Reproduction, all three observed against the running control-api before the fix:
  - `GET /api/ops/entity-risk` (unknown route) → `500 INTERNAL_ERROR`
  - `GET /api/soar/action-dictionary` (missing required `objectType`) → `500 INTERNAL_ERROR`
  - `DELETE /api/alerts` (unsupported method) → `500 INTERNAL_ERROR`
- Root Cause: the advice covers `MethodArgumentNotValidException`, `ConstraintViolationException`,
  `HttpMessageNotReadableException` and `MethodArgumentTypeMismatchException`, but **not**
  `MissingServletRequestParameterException`, `NoResourceFoundException` / `NoHandlerFoundException`, or
  `HttpRequestMethodNotSupportedException`. All three therefore fell through to the catch-all
  `@ExceptionHandler(Exception.class)` and were logged as "Unhandled API error".
- Fix: **APPLIED.** Additive only — no existing handler was changed:
  - `MissingServletRequestParameterException` joined the existing `MALFORMED_REQUEST` group → `400`
  - a new handler for `NoResourceFoundException` / `NoHandlerFoundException` → `404`
  - a new handler for `HttpRequestMethodNotSupportedException` → `405`
- Regression Test: **ADDED** — `ApiErrorMappingTest` (3 tests). Red/green verified: with the fix
  reverted all three fail with exactly the production symptom
  (`Status expected:<400|404|405> but was:<500>`); with the fix, 3/3 pass.
- Runtime Revalidation: `404 接口不存在: /api/ops/entity-risk`, `400 MALFORMED_REQUEST`,
  `405 METHOD_NOT_ALLOWED`; real endpoints unaffected (`GET /api/alerts/{unknown}` → `404`,
  `action-dictionary?objectType=alert` → `200`, `GET /api/cases/{unknown}` → `404`).
- Status: **FIXED and verified.**

**Corrective uncommitted changes exist** (DEFECT-001..004, DEFECT-006). No defect was hidden or omitted.

## Blocked / Skipped Scenarios

**Closure run: nothing is blocked.** Every row that was `NOT RUN` or `PARTIAL` in the first run was
executed against the real runtime on 2026-09-17 and is now `PASS`. The table records how each was
unblocked, because in three cases the first attempt was genuinely blocked and the resolution is part
of the evidence.

| Scenario | First run | Closure run | How it was unblocked / what the blocker actually was |
|---|---|---|---|
| RULE | NOT RUN | **PASS** | No blocker; simply not driven. Driven through the public API, including four distinct invalid-DSL rejections |
| CASE | NOT RUN | **PASS** | Manual case creation requires **≥2 open alerts** — a documented business rule, not a defect. The first attempt used one alert and was correctly refused with `400` |
| RISK | NOT RUN | **PASS** | No blocker. `entity-risk.py` writes the `siem-entity-risk` ES projection, so recalculation is observable as a real A/B delta |
| KNOWLEDGE | NOT RUN - "no deterministic embedding profile is selectable at runtime" | **PASS** | The blocker was real but narrower than stated: ingestion *is* supported via `knowledge.cli --embedding-provider deterministic-test-only`. One further wrinkle: `EMBEDDING_PROVIDER` accepts only `unconfigured`/`openai_compatible`, so the fixture is selected by the CLI flag alone and the flag is a **global** argument that must precede the subcommand. The runtime Copilot then truthfully degrades HYBRID → `LEXICAL_ONLY` and says so in the retrieval provenance |
| MCP | NOT RUN | **PASS** | No blocker. A deterministic local Streamable HTTP server using the official SDK was staged; the schema fingerprint is computed by the Copilot's own `external_schema_fingerprint` so admissions match the observed schema exactly |
| OTEL | NOT RUN - "tracing disabled, no Collector" | **PASS** | No blocker: the collector image pulls and the repo's `infra/otel-collector/collector.yaml` runs as-is apart from the local exporter endpoint and `tls.insecure` |
| DURABILITY | NOT RUN | **PASS** | No blocker |
| ATTENTION | NOT RUN - "no safe way to exhaust the retry budget without altering production retry semantics, which is forbidden" | **PASS** | The premise was too pessimistic. The retry budget is a module constant (`_MAX_ATTEMPTS = 10`, exponential backoff capped at 120 s), so the budget is exhaustible by **waiting** — the correct seam is *time*, not configuration. It was reached in ~8 minutes of real time with **no** production change |
| TENANT | PARTIAL | **PASS** | The "default admin can claim tenant B" observation was **not** a defect: the creating admin is auto-added as **owner** of the new tenant, so `/api/tenants/mine` confirms a legitimate membership. Cross-tenant investigation read and approve are both refused |
| FAILURE | PARTIAL | **PASS** | The Copilot-down and MCP-unreachable variants were injected with reversible runtime failures |
| SECURITY | PARTIAL | **PASS** | The internal-SOAR bearer negatives and the MCP endpoint/SSRF boundary were exercised over real HTTP |
| SIEM-SOAR | PARTIAL | **PASS** | The native lifecycle was driven end to end, independent of Copilot. Two harness mistakes were corrected along the way: `CreatePlaybookRequest` accepts **no** `graph` (the graph must be stored via `PUT`), and the approval-status filter value is lowercase `pending` |

`PRE-SEAL SYSTEM GATE` is claimed PASS **because of the completed table above**, not because the earlier
partial matrix was reinterpreted.

## Security Boundary Validation

### Browser → HISIEM
- Real login for ADMIN/ANALYST/AUDIT succeeded; a fresh admin-provisioned user is forced through
  password rotation (`428 PASSWORD_CHANGE_REQUIRED`) before any other call.
- Missing session → 401; invalid token → 401; ANALYST/AUDIT cannot create users → 403.
- A non-member tenant → 403; omitting the tenant header does **not** widen scope (identical result to the
  caller's own tenant).

### HISIEM → Copilot
- The service credential is required: with `HISIEM_AGENT_BEARER_TOKEN` unset the control-api **fails to
  start** (fail-closed), and Copilot rejects requests without the matching bearer.
- The paired credentials in `.env.local` were verified to match by fingerprint only (never printed).
- Browser never receives the service bearer, and never calls Copilot directly (asserted by observing all
  browser requests).

### Copilot → HISIEM Internal SOAR
- `POST /api/internal/soar/executions` succeeded only with the configured bearer + `X-Tenant-ID`; the
  ephemeral E2E credential was supplied to both sides at runtime only and never written to git.
- The resulting execution was independently confirmed in HISIEM's own API and node runs.

### Tenant / Actor Boundary
- Tenant and actor come from the HISIEM-derived trusted context; body-level `tenant_id`/`actor`
  injection was accepted by HTTP but **not persisted** (stored proposal carried only the server-derived
  target and `playbook_id`).

### Approval TOCTOU
- `expected_revision` + `expected_content_hash` are enforced: a stale revision **and** a wrong hash each
  returned `409 AGENT_STATE_CONFLICT`, the proposal stayed `WAITING_APPROVAL`, and the HISIEM execution
  count did not change.

### MCP Boundary
- Not exercised at runtime this run (see blocked table). Stage C's deterministic suites remain green.

### Secret Leakage / Error Sanitization
- Error bodies contained no bearer, `Authorization`, password, DSN, or stack trace
  (`{"code":"UNAUTHORIZED", …}`).
- Upstream bodies are normalized at the BFF (bounded `503 AGENT_UNAVAILABLE`, `502 AGENT_REJECTED`,
  `409 AGENT_STATE_CONFLICT`).
- The browser-rendered workspace contained no `chain_of_thought`, `raw_prompt`,
  `langgraph_checkpoint`, `checkpoint_id`, or `Bearer ` text.
- No credential, token, DSN, or `.env` content was printed during this run.

## Data Consistency Validation

### Alert — ES ↔ HISIEM API
- For the brute-force alert the ES document and the API representation agree on `alert.id`,
  `alert.rule_id`, `alert.severity`, `alert.status`, `event_count`, and `source.ip`
  (`c66f003e-54c8-40bb-81aa-91635b947368`, `rule-ssh-brute-force-001`, `critical`, `open`, `5`,
  `198.51.100.142`).
- The single-event alert used for the approved chain (`86701bf9bf0df5ec2afe63ead4c0214d936fc0c5`) is
  readable both in ES and via `GET /api/alerts/{_id}` (200, 4095-byte body).

### Investigation — Copilot DB ↔ API/Workspace
- The workspace read model returned exactly the persisted facts: 20 evidence rows
  (19 `HISIEM_LOG_SEARCH` + 1 `SYSTEM`), 1 finding citing platform evidence, 1 SUPPORTED hypothesis and
  the immutable verdict `MALICIOUS 0.9`; the tool audit showed `get_detection_rule` and `search_events`
  both SUCCEEDED.

### SOAR — HISIEM PG ↔ API ↔ Copilot Observation
- HISIEM API: `exec-eec71561-4b62-4d44-b9d0-f764250eaf58`, `status=success`, playbook
  `pb-a3b539a1-fa97-478e-8344-7a36d8e87816`, `triggerType=MANUAL`, nodeRuns `start`+`end` = success.
- Copilot workspace: `execution.provider=hisiem`, `status=SUCCEEDED`, same `external_execution_id`,
  observed via the internal SOAR read endpoint.
- Submission status (`SUBMITTED`) and execution status (`SUCCEEDED`) are displayed as distinct facts;
  submission success was never presented as execution success.

### Browser — UI ↔ Durable Backend Truth
- The unmocked browser session rendered the same investigation that the API reports (verdict, evidence
  authority classes, finding→evidence linkage, response lifecycle), and a hard refresh reconstructed the
  workspace from backend state alone.

## Failure and Recovery Validation

| Failure Injection | Expected | Observed | Data Integrity | Recovery | Duplicate Side Effect |
|---|---|---|---|---|---|
| Copilot service credential absent (`HISIEM_AGENT_BEARER_TOKEN` empty) | Copilot refuses; HISIEM returns a bounded error | Copilot returned `502 EXTERNAL_SERVICE_ERROR`; HISIEM mapped it to `503 AGENT_UNAVAILABLE` with no upstream body or credential in the response | No partial investigation row; HISIEM otherwise fully usable | Minting a valid bearer and restarting Copilot restored the chain immediately | None |
| Copilot→HISIEM adapter credential missing | Bounded failure, no fabricated Evidence | `EXTERNAL_SERVICE_ERROR: "HISIEM returned a non-JSON body"` (see DEFECT-004) | No Evidence written | Resolved by using the correct alert identity + valid bearer | None |
| Search window exceeding the 24h policy cap (harness error) | Tool rejected by policy, no Evidence | `hisiem.search_events` recorded as `FAILED` and the run continued | No Evidence written for the rejected call | Harness narrowed the window to ±30 min; the same tool then succeeded | None |
| Ambiguous verdict grounding (finding citing `SYSTEM` evidence only) | No definitive verdict | The graph downgraded to `INCONCLUSIVE` ("No platform-grounded finding supported the model's proposed verdict") and the response policy returned `DENY / denying_verdict` with zero execution | Result persisted as INCONCLUSIVE | Correct by design; the platform-grounded run reached MALICIOUS | None |
| Rejected response proposal | Zero durable execution | HISIEM executions 15 → 15 | Proposal persisted as REJECTED | N/A | None |
| Repeated identical approval | No second execution intent | HISIEM executions 12 → 12, HTTP 200 idempotent convergence | Proposal stayed SUBMITTED | N/A | None |
| Copilot down while HISIEM is up (closure run) | Bounded failure, HISIEM unaffected | HISIEM returned a bounded `503`; no partial rows, no leaked upstream body; HISIEM remained fully usable | None | Restarting Copilot restored the chain immediately | None |
| Admitted MCP capability with the MCP server stopped (closure run) | Typed failure, fail-closed, no Evidence | Typed `PROVIDER_ERROR` (see DEFECT-005), **zero Evidence** written, run unaffected | No Evidence row created | Server restart restored the capability | None |
| **Telemetry backend down** (closure run) | Telemetry outage must not become business outage | Collector stopped entirely: the investigation still completed (`MALICIOUS`, 2 tools, 37 Evidence, 1 Finding) and a response proposal still returned `200`; Copilot `/healthz` stayed `ok` | Full durable result persisted | Collector restart resumed export (sink records 54 → 62) | None |
| Approved command against a persistently failing SOAR endpoint (closure run) | No false rejection claim, no fabricated execution id, no infinite retry | `ATTENTION_REQUIRED` after exactly 10 attempts; `submitted_at=null`, `execution=null` | Proposal stayed `APPROVED` with no execution | Requires a human; no automatic retry followed in the next 180 s | None |
| Known-invalid alert transition driven by a SOAR playbook (closure run) | Rejected truthfully, no partial mutation | `不允许从 open 流转到 investigating`, node run recorded `failed`, alert left untouched | Alert unchanged | Playbook corrected to a valid transition; the next execution succeeded | None |
| Playbook ordering that creates a case after acknowledging the alert (closure run) | Rejected truthfully | `告警非 open 状态(已处置或已结案)` — the engine reported the real reason and did not half-apply the playbook | Alert stayed `acknowledged`; no case created | Reordered the playbook so the approval gate precedes any mutation | None |

## Regression Gates

### HISIEM

| Gate | Result | Evidence |
|---|---|---|
| Maven Spotless (root) | **PASS** (closure run) | `./mvnw -o spotless:check` → `BUILD SUCCESS` for the whole reactor. The first run's violation in `modules/agent-adapter` was purely formatting and was corrected repo-wide; the only files this closure run formatted itself are `GlobalExceptionHandler.java` and the new test |
| Maven Spotless (soar-worker) | **PASS** | `BUILD SUCCESS` |
| Root Maven tests | **PASS** (closure run) | Full reactor `./mvnw -o test` → `BUILD SUCCESS`, exit 0, **0 failures / 0 errors**, 17/17 modules `SUCCESS`. The Flink `detection-job` module ran 60 tests, 0 failures. An intermediate reactor run failed `RuleLintTest` **because of this run's own temporary RULE artifact** (`infra/rules/rule-e2e-runtime-001.yaml` made the published-rule count 7 where the test asserts exactly 6); deleting the temporary rule restored green — the test was right and the artifact was ours |
| Flink Spotless | **PASS** | Covered by `./mvnw -f flink/pom.xml clean package` |
| Flink clean package | **PASS** | 60 tests, 0 failures, `BUILD SUCCESS` (6.1 s) — run in isolation; an earlier failure was a self-inflicted race between two concurrent Maven builds on `flink/target` |
| Frontend lint | **PASS** | `eslint .` clean |
| Frontend unit | **PASS** | 40 tests, 0 failures |
| Frontend build | **PASS** | `vite build` ✓ |
| Existing Playwright (mock) | **PASS** | 34 tests passed |

### Copilot

| Gate | Result | Evidence |
|---|---|---|
| mypy | **PASS** | `Success: no issues found in 215 source files` |
| Ruff | **PASS** | `All checks passed!` |
| git diff --check | **PASS** | exit 0 (clean tree) |
| Architecture tests | **PASS** | 263 passed |
| Full pytest | **PASS** | 1763 passed, 9 skipped, 0 failed, 0 errors (158.11 s) |

The 9 skips are the repository-defined opt-in live gates (1 × GP-01 real-HISIEM materialization,
8 × Command Code live-LLM smoke) requiring external infrastructure/credentials.

## Closure Run (2026-09-17)

This run did **not** restart validation from zero. It used the sections above as the authoritative
evidence ledger, re-validated the existing uncommitted SOAR-worker fixes, resolved `DEFECT-004`,
completed the missing and partial matrix rows, and resolved the Maven Spotless gate. Everything below
is fresh evidence from this run; nothing above was deleted.

### 1. The existing SOAR-worker fixes were validated, not rewritten

| Check | Result |
|---|---|
| Fixes are minimal (no architecture expansion) | `TenantMapper.class` added to the control-plane `@MapperScan`; one `ObjectMapper` bean; five `app.agent.*` / `app.internal-service.*` properties |
| No credential committed | PASS - the properties are placeholders bound from environment variables |
| `SoarWorkerContextTest` genuinely boots the context | PASS - `@SpringBootTest(webEnvironment = NONE)` on H2 with `@Autowired TenantMapper`; 1 test, 0 failures, and it fails without the fixes |
| Fixes survive the remaining scenarios | PASS - the SOAR worker was alive for DURABILITY, SECURITY, SIEM-SOAR and the Copilot SOAR bridge scenarios |

They were **not** rewritten for style.

### 2. `DEFECT-004` resolved

See the defect entry above for the fix, the regression test, and the real-Elasticsearch-shape discovery.
Rerun after the fix: alert detail by a known `_id` → `200`; by an unknown id → `404 NOT_FOUND`;
`AlertServiceTest` 14/14. The invalidated paths (alert detail, Copilot alert hydration, unknown-alert
investigation behaviour, error sanitization) were re-exercised.

### 3. Newly executed scenario rows

**RULE.** `POST /api/detection-rules` with a unique id → `201`; `GET` reads back the exact condition
`{'type': 'field_equals', 'field': 'event.action', 'value': 'e2e_runtime_probe'}`. Four invalid-DSL
writes each returned `400 INVALID_ARGUMENT` with a distinct message — `unsupported rule condition:
field_between`, `condition.field is not a valid ECS field`, `condition.conditions has an invalid size`,
and `unsupported rule condition: nope` for a brand-new rule. The rejected rule is `404`, and the
previously valid rule serialises byte-identically before and after all four rejections. The temporary
rule was then deleted.

**CASE.** Two open alerts → case `case-20260917-ff78` (`aggregation=manual`), entities extracted
`203.0.113.9` and `198.51.100.142`, status `open` → `investigating` → `resolved` with
`verdict=true_positive`, a 10-event timeline drawn from the real Elasticsearch event stream, and the
alert cascading to `closed`. Negatives: unknown case `404`, invalid status `400`, unknown alert `404`,
single-alert manual aggregation `400`, resolve without a verdict `400`, append to a resolved case `400`.
Adding an alert already present is an idempotent `200 {"added":[]}`.

**RISK.** A controlled A/B on the real entity `198.51.100.142`: setting criticality to `extreme` and
recalculating moved `asset.criticality` `1.0 → 2.0` and `risk_score` `186 → 372` in the
`siem-entity-risk` projection, with a fresh `@timestamp`. `audit_logs` recorded `criticality.set`,
`criticality.recalc` and `criticality.delete` with actor and timestamp. Everything was restored and
verified back to `1.0` / `186`. No direct database insert was used anywhere.

**KNOWLEDGE.** A guidance document was ingested through `knowledge.cli ingest-file` with the
repository's deterministic embedding fixture into the real Copilot pgvector PostgreSQL: document
`f5ffb06f-9172-4aec-86c3-09521c538962`, 4 chunks, ACTIVE profile
`deterministic/deterministic-test-v1 dim=64 COSINE`. Hybrid search returned 4 hits with `kcit:`
citations, and `resolve-citation` resolved one. Driving the Agent with knowledge as its **only**
evidence produced 3 `KNOWLEDGE` Evidence rows carrying real `citation_identity` records, a Finding
citing them, and **a verdict downgraded to `INCONCLUSIVE` at confidence 0.0** with the reason
"No platform-grounded finding supported the model's proposed verdict". The browser showed
`平台事实 0 · 支持性上下文 3` and never labelled the knowledge evidence a platform fact. No
FakeRepository. The retrieval provenance truthfully reports `LEXICAL_ONLY` because the Copilot process
has no embedding provider configured — it degrades rather than claiming a mode it did not use.

**MCP.** A deterministic local Streamable HTTP server built on the official SDK exposed one admitted
read-only capability plus four hostile/edge-case tools. The admitted `mcp.e2e.lookup` was invoked by
the Agent and persisted as real Evidence; the unadmitted `e2e_dynamic_only` stayed non-selectable; a
remote error was typed; an oversized result was rejected as `RESULT_TOO_LARGE` **without truncation**;
prompt-injection content stayed DATA; and a deliberately drifted schema produced `SCHEMA_MISMATCH`
fail-closed. Fingerprint `182c8c08...` matched exactly.

**OTEL.** The repository's own `infra/otel-collector/collector.yaml` was run (only the local exporter
endpoint and `tls.insecure` changed) in front of a second collector acting as the sink, with the
Copilot exporting OTLP/gRPC to it. 2,536 spans were collected. Required spans were present and
correctly nested: `mcp.call → tool.execute → graph.invoke → investigation.run → durable.dispatch`.
**Zero** secret leakage at value level (agent bearer, internal-service token, admin session token and
tenant-B session token all absent), all 11 forbidden high-cardinality dimensions absent, all 15
sensitive trace attributes on the collector delete-list absent, and no span name longer than 40
characters or carrying a secret. Then the Collector was stopped: a full investigation still completed
(`MALICIOUS`, 2 tools, 37 Evidence, 1 Finding) and a response proposal still returned `200`, with
Copilot `/healthz` `ok` — **telemetry outage is not business outage**. Restarting the Collector
resumed export (sink records 54 → 62).

**ATTENTION_REQUIRED.** An approved command was submitted to an endpoint that answers `503` on every
attempt. The retry budget is a module constant (`_MAX_ATTEMPTS = 10`, exponential backoff capped at
120 s), so it was exhausted by real time rather than by reconfiguration — taking about eight
minutes. The terminal state is `ATTENTION_REQUIRED` with `attempt_count=10`,
`last_error_code=HTTP_503`, `attention_required_at` set, and **`submitted_at=null` and
`execution=null`**. The failing endpoint counted 10 submit attempts and **zero further attempts over
the next 180 seconds**, and the stored payload contains no provider-rejected wording and no external
execution id. Fourteen browser assertions passed, covering both the summary banner and the
従应 tab detail block, with no non-2xx API call and no console error. **No production retry
semantics were changed.**

**SIEM-SOAR (HISIEM's own lifecycle, independent of Copilot).** A playbook was authored, the graph
stored via `PUT`, published and enabled (revision 8), then triggered manually against a real open
alert. The execution paused at `waiting_human` with node runs `start`→`gate`, and while it waited the
alert was verified **unmutated** (`status=open`, no `case_id`, no `operator`) — the approval gate
genuinely precedes any action. After approval the node runs completed `mkcase`→`ack`→`end`, all
`success`, and the real effects appeared: `alert.status=acknowledged`,
`alert.operator=soar:exec-899d6e3e-...`, `alert.case_id=case-20260917-27f7` with `aggregation=soar` and
a 10-event case timeline. Copilot's `START_SOAR_PLAYBOOK` coverage was **not** substituted for this.

### 4. Completed partial rows

**TENANT.** A real second tenant was created. The default tenant cannot read or approve the other
tenant's investigation; a missing tenant header does not widen scope. The one earlier surprise — the
default admin reaching tenant B — is explained and is not a defect: the creating admin is auto-added
as **owner** of the new tenant, which `/api/tenants/mine` confirms. Full ES data-plane multi-tenancy
is explicitly **not** claimed; it is outside the current contract.

**FAILURE.** Three reversible runtime failures were injected: Copilot down while HISIEM is up
(bounded `503`, HISIEM fully usable, immediate recovery), an admitted MCP capability with the MCP
server stopped (typed failure, fail-closed, zero Evidence), and the telemetry backend down (above).
Plus the two genuine playbook-driven business failures the SOAR engine rejected truthfully with the
alert left untouched.

**SECURITY.** The internal-SOAR bearer negatives are now complete over real HTTP — missing bearer,
invalid bearer, wrong scheme, a cross-service token, and a tenant mismatch, five for five refused
with sanitized errors — and the MCP endpoint/host policy refuses non-configured and SSRF-shaped
endpoints. No authentication, tenant, approval, idempotency or security boundary was weakened at any
point in either run.

### 5. Regression gates after the closure-run changes

| Gate | Result | Evidence |
|---|---|---|
| `./mvnw -o spotless:check` (root) | **PASS** | `BUILD SUCCESS` for the whole reactor |
| `./mvnw -o test` (root reactor) | **PASS** | `BUILD SUCCESS`, exit 0, 0 failures / 0 errors, 17/17 modules `SUCCESS` |
| Flink `detection-job` | **PASS** | 60 tests, 0 failures |
| `ApiErrorMappingTest` red/green | **PASS** | 3/3 fail without the fix (`expected:<400|404|405> but was:<500>`), 3/3 pass with it |
| `AlertServiceTest` | **PASS** | 14 tests, 0 failures |
| `SoarWorkerContextTest` | **PASS** | 1 test, 0 failures |
| Runtime re-verification of both fixes | **PASS** | `404` / `400` / `405` and the alert `404` all confirmed against the restarted control-api |
| Copilot static and functional gates | **PASS** | No Copilot production file was changed in the closure run, so the first run's results stand: mypy 215 files clean, ruff clean, `git diff --check` clean, 263 architecture tests, 1763 pytest passed / 9 skipped / 0 failed |
| Frontend | **PASS** | No frontend source file was changed; the browser assertions above ran against the live Vite dev server |

The 9 Copilot skips remain the repository-defined opt-in live gates (1 × GP-01 real-HISIEM
materialization, 8 × Command Code live-LLM smoke) that require external infrastructure or credentials.

## Final Git State

Captured at the end of the closure run.

### HISIEM

```text
branch: add_frame
HEAD: bce6586977055876675d052b03af8d58066aa2e6
git status --short (26 entries):
M applications/control-api/src/main/java/com/xscsiem/hsiem_platform/agent/AgentInvestigationController.java
 M applications/control-api/src/main/java/com/xscsiem/hsiem_platform/alert/AlertController.java
 M applications/control-api/src/main/java/com/xscsiem/hsiem_platform/auth/InternalServiceAuthFilter.java
 M applications/control-api/src/main/java/com/xscsiem/hsiem_platform/auth/SecurityConfig.java
 M applications/control-api/src/main/java/com/xscsiem/hsiem_platform/onboarding/GlobalExceptionHandler.java
 M applications/control-api/src/main/java/com/xscsiem/hsiem_platform/soar/InternalSoarController.java
 M applications/control-api/src/main/java/com/xscsiem/hsiem_platform/tenant/TenantContextFilter.java
 M applications/control-api/src/test/java/com/xscsiem/hsiem_platform/agent/AgentInvestigationControllerTest.java
 M applications/control-api/src/test/java/com/xscsiem/hsiem_platform/agent/AgentInvestigationServiceTest.java
 M applications/control-api/src/test/java/com/xscsiem/hsiem_platform/agent/AgentLaunchControllerTest.java
 M applications/control-api/src/test/java/com/xscsiem/hsiem_platform/agent/AgentLaunchServiceTest.java
 M applications/control-api/src/test/java/com/xscsiem/hsiem_platform/agent/AgentResponseBffSecurityTest.java
 M applications/control-api/src/test/java/com/xscsiem/hsiem_platform/alert/AlertServiceTest.java
 M applications/control-api/src/test/java/com/xscsiem/hsiem_platform/auth/InternalServiceAuthFilterTest.java
 M applications/control-api/src/test/java/com/xscsiem/hsiem_platform/soar/InternalSoarControllerIntegrationTest.java
 M applications/soar-worker/pom.xml
 M applications/soar-worker/src/main/java/com/xscsiem/hsiem_platform/entrypoints/SoarWorkerApplication.java
 M applications/soar-worker/src/main/resources/application.properties
 M modules/agent-adapter/src/main/java/com/xscsiem/hsiem_platform/agent/AgentInvestigationService.java
 M modules/agent-adapter/src/main/java/com/xscsiem/hsiem_platform/agent/AgentLaunchResponse.java
 M modules/agent-adapter/src/main/java/com/xscsiem/hsiem_platform/agent/AgentLaunchService.java
 M modules/security-ops/src/main/java/com/xscsiem/hsiem_platform/alert/AlertService.java
 M modules/soar-core/src/main/java/com/xscsiem/hsiem_platform/soar/SoarService.java
?? applications/control-api/src/test/java/com/xscsiem/hsiem_platform/onboarding/ApiErrorMappingTest.java
?? applications/soar-worker/src/test/java/com/xscsiem/hsiem_platform/entrypoints/SoarWorkerContextTest.java
?? applications/soar-worker/src/test/resources/

git diff --stat:
.../agent/AgentInvestigationController.java        |  22 +-
 .../hsiem_platform/alert/AlertController.java      |  15 +-
 .../auth/InternalServiceAuthFilter.java            |  48 ++--
 .../hsiem_platform/auth/SecurityConfig.java        | 119 ++++++----
 .../onboarding/GlobalExceptionHandler.java         |  88 ++++---
 .../soar/InternalSoarController.java               |  60 ++---
 .../hsiem_platform/tenant/TenantContextFilter.java |  22 +-
 .../agent/AgentInvestigationControllerTest.java    |  67 ++++--
 .../agent/AgentInvestigationServiceTest.java       | 256 +++++++++++++--------
 .../agent/AgentLaunchControllerTest.java           |   5 +-
 .../agent/AgentLaunchServiceTest.java              | 204 +++++++++-------
 .../agent/AgentResponseBffSecurityTest.java        | 131 ++++++-----
 .../hsiem_platform/alert/AlertServiceTest.java     |  44 ++++
 .../auth/InternalServiceAuthFilterTest.java        |  62 +++--
 .../InternalSoarControllerIntegrationTest.java     | 198 ++++++++++------
 applications/soar-worker/pom.xml                   |   8 +
 .../entrypoints/SoarWorkerApplication.java         |  24 +-
 .../src/main/resources/application.properties      |   9 +
 .../agent/AgentInvestigationService.java           | 121 ++++++----
 .../hsiem_platform/agent/AgentLaunchResponse.java  |   3 +-
 .../hsiem_platform/agent/AgentLaunchService.java   |  87 ++++---
 .../xscsiem/hsiem_platform/alert/AlertService.java |   8 +-
 .../xscsiem/hsiem_platform/soar/SoarService.java   |  24 +-
 23 files changed, 1023 insertions(+), 602 deletions(-)
```

### Copilot

```text
branch: capability-mcp
HEAD: 09487a72cf46a368fe82a797b7e8dabf7638e07d
git status --short:
?? FULL_RUNTIME_E2E_TEST_REPORT.md

git diff --stat:
(empty)
```

- Production code modified: **YES — HISIEM only** (the SOAR-worker fixes for DEFECT-001..003, the
  `AlertService.detail` guard for DEFECT-004, and the `GlobalExceptionHandler` handlers for DEFECT-006,
  plus their regression tests and the repo-wide formatting correction Spotless required).
- Uncommitted fixes: **YES.** They were deliberately **not** committed, pushed, rebased, reset,
  restored, cleaned or discarded at any point in either run — that instruction was in force for the
  entire validation and is still in force.
- The pre-existing uncommitted work in the HISIEM working tree was preserved exactly; the closure run
  only added to it.
- Temporary artifacts: **removed.** The `D:\Project\.e2e-runtime` harness directory (deterministic
  model server, local MCP servers, failing SOAR stub, two-hop OTel collector configs, runtime-only
  credentials, all scenario scripts and logs), the temporary rule
  `infra/rules/rule-e2e-runtime-001.yaml`, the temporary Playwright runtime specs and their
  `test-results/`, and the temporary E2E playbooks' inputs were deleted. A `git status` check on the
  HISIEM `web/` tree confirms no temporary frontend artifact remains.
- `FULL_RUNTIME_E2E_TEST_REPORT.md` itself is a test artifact (this document), not production code; it
  is the intentional working-tree addition to the Copilot repository and was **updated, not replaced**,
  so the DEFECT-001..004 history above is preserved verbatim.
- Commit: **NO**
- Push: **NO**

## Final Conclusion

### Proven by real runtime

- Real log ingestion (Logstash → Elasticsearch + Kafka) with a unique per-run marker.
- Real window and single-event detection by the running Flink job, with deterministic alert identity.
- Alert and entity identity consistency between Elasticsearch and the HISIEM API.
- Real HISIEM authentication and RBAC enforcement over HTTP, including the first-login password gate.
- The full native rule lifecycle: create, read back, and four distinct invalid-DSL rejections that
  leave the previously valid rule byte-identical.
- The full native case lifecycle: two open alerts → case → status → timeline → resolve, with the
  alert cascading to `closed`, plus six negative paths.
- Entity criticality → recalculated risk projection → audit trail, proven as an A/B delta and restored.
- Real alert → investigation launch through the HISIEM BFF into the Copilot's PostgreSQL.
- A real investigation executed by the Copilot process against the real HISIEM API, producing real
  tool invocations, persisted Evidence, a platform-grounded Finding, and an immutable `MALICIOUS` verdict.
- The verdict-grounding guard, twice: a finding grounded only on `SYSTEM` evidence, and a finding
  grounded only on `KNOWLEDGE` evidence, each failed to produce a definitive verdict (the second
  downgraded to `INCONCLUSIVE` at confidence 0.0). Knowledge is supporting context and cannot
  authorize an observed-fact verdict.
- Real knowledge: a document ingested into the Copilot's pgvector PostgreSQL through the supported CLI,
  served by hybrid retrieval, cited with a `kcit:` identity, and resolved by the real citation resolver.
- A real admitted MCP capability invoked over Streamable HTTP by the Agent, persisted as Evidence,
  with `mcp.call` nested under `tool.execute`, and a fail-closed `SCHEMA_MISMATCH` on drift.
- A server-derived response target, policy `REQUIRE_APPROVAL`, human approval bound to the exact
  revision + content hash, a durable command, a real HISIEM execution, and Copilot **observing** the
  final execution truth.
- Rejection, repeated approval, and restart-with-pending-command: no duplicate and no stray execution.
- The `ATTENTION_REQUIRED` state reached by exhausting the real retry budget, with no false
  provider-rejection claim, no fabricated execution id, and no further automatic retry.
- HISIEM's **own** SOAR lifecycle, independent of Copilot: a human approval gate that provably
  precedes any mutation, then real business effects (`alert.status`, `alert.operator=soar:exec-...`,
  a `soar`-aggregated case).
- Full OpenTelemetry through a real Collector using the repository's own configuration, with zero
  secret leakage, no forbidden high-cardinality dimensions, and a telemetry outage that provably did
  not become a business outage.
- The unmocked browser console rendering the real investigation with correct authority semantics,
  refresh reconstruction, a usable narrow viewport, no internal-reasoning leakage, and the
  human-attention state derived from durable backend truth.

### Proven only by integration/logical/mock tests

Nothing required by the matrix is left in this category. The residual items are explicitly out of
scope rather than unproven: ATT&CK canonical technique resolution was not re-imported at runtime
(no operator STIX bundle is available locally and runtime fetching is forbidden by design; it was
covered by the Stage A validation), and rule *deployment/reconciliation* was not driven because it
would restart the live Flink detection job the rest of the runtime depends on.

### Remaining blockers

1. `DEFECT-005` (open, **Low**): an unreachable MCP server is classified `PROVIDER_ERROR` rather than
   `UNAVAILABLE`. The failure is typed, fails closed and yields zero Evidence; the imprecision comes
   from the official SDK collapsing transport failure into `MCPError(-32603)`. Deliberately not fixed
   with a message-string heuristic.
2. The corrective changes are **uncommitted** in both repositories, by instruction.

No unresolved correctness or security defect remains in any required runtime scenario.

### Pre-seal decision

```text
PASS
```
