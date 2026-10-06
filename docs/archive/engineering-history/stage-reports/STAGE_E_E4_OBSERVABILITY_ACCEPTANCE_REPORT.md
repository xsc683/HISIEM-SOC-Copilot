# Stage E / E4 — Observability Acceptance Report

## 1. Header / authorities / baseline

| Item | Value |
|---|---|
| Stage / step | Stage E / E4 (OBSERVABILITY acceptance) |
| Date | 2026-09-18 (runtime slice executed 2026-09-17/18) |
| HISIEM baseline | branch `add_frame` @ `d0baed9d111ecefb33d5a9222d916042863b11c9` — **identical to `origin/add_frame`**, 0 working-tree entries |
| Copilot baseline | branch `capability-mcp` @ `d4cfc8ed69043d0c14991833223ff81eb8d1e3a0` — **identical to its remote**, E0/E1/E2/E3 work preserved |
| Authorities read | `00_Four-Plane_Architecture_Contract_Freeze.md` (§5 observability contract), `05_Stage-E_Cross-Plane_Integration_Evaluation_Spec.md`, `06_Stage-E_Detailed_Design.md` (§7.7), `Stage-E_E4_Observability-Acceptance_VibeCoding_Prompt.md`, E0/E1/E2/E3 reports, `FULL_RUNTIME_E2E_TEST_REPORT.md` |
| Frozen upstream artifacts | XP-01 contract package, catalog, gate/reason/fact vocabularies, `cross-plane-gate-results/v1`, E2 semantics, E3 semantics — **all unchanged** |
| Prior baselines reproduced | E1 `172` · E2 `15/15` · E3 `10/10` · GP-01 `346` · KB-GOLDEN-V1 `679/0` — all exact |
| Git state | No commit, no push, no stage, no reset/restore/clean/rebase/amend |
| Secrets | No `.env` content, bearer, API key, DB password, session/service token or `Authorization` header was printed. The runtime slice reads its credential from the gitignored dev env and passes it to a child process without echoing it |

Verified XP-01 identity (unchanged): **29 scenarios / 13 hard gates / 9 gate families / 3 execution profiles / 37 expected + 21 forbidden fact tokens**.

## 2. Final E4 status

```text
E4 IMPLEMENTATION: PASS
E5 READINESS: READY
```

Both OBSERVABILITY scenarios have executable XP-01 paths and PASS; the representative lifecycle is correlatable across the durable boundary; telemetry safety and metric cardinality hold on real emissions; a real collector outage does not change the business outcome; no production file was changed; nothing is BLOCKED.

## 3. Exact E4 scenario inventory

Derived programmatically from the implemented E1 catalog (`scenarios_for_family(GateFamily.OBSERVABILITY)`), never hard-coded:

```text
E4 total: 2
XP-OBS-001  Representative trace correlation              runtime-integrated
XP-OBS-002  Metrics cardinality and telemetry safety      deterministic
```

Not E4-owned and untouched: `XP-UX-001/002` (WORKSPACE — E5).

| Scenario ID | Profile | Measurement Adapter | Required Gates | Runtime Needed | Artifact | Result |
|---|---|---|---|---|---|---|
| XP-OBS-001 | runtime-integrated | `required_spans_present` (+ `trace_continuation_facts`, `oracle_firewall_of`) | EXPECTED_FACTS_PRESENT, ORACLE_FIREWALL | real Copilot runtime process + real OTel Collector + real HISIEM SOAR | `test_xp_obs_001_representative0/xp-01/XP-OBS-001/gate-results.json` | **PASS** |
| XP-OBS-002 | deterministic | `metric_label_safety`, `telemetry_safety_facts`, `secret_scan_of` | EXPECTED_FACTS_PRESENT, SECRET_LEAK, FORBIDDEN_FACTS_ABSENT | none (pure rules over real captured label sets/surfaces) | `test_the_deterministic_scenari0/xp-01/XP-OBS-002/gate-results.json` | **PASS** |

**2 / 2 PASS.** No scenario is BLOCKED, skipped, or left unimplemented. Completed XP-01 across Stage E: **27 / 29** (25 from E2 + 10 from E3 + 2 from E4; the 2 WORKSPACE scenarios belong to E5).

## 4. Files changed

| Path | Kind | Lines |
|---|---|---|
| `src/hisiem_soc_copilot/evaluation_harness/cross_plane_observability.py` | E4 measurement adapters (new) | 457 |
| `src/hisiem_soc_copilot/evaluation_harness/cross_plane_e4_scenarios.py` | E4 drivers, runner, inventory (new) | 363 |
| `tests/support/e4_observability_fixture.py` | real-pipeline capture harness (new) | 214 |
| `tests/support/e4_observability_driver.py` | XP-OBS-001 runtime driver (new) | 441 |
| `tests/unit/evaluation_harness/test_cross_plane_observability.py` | Layer 1 adapter tests (48) | 322 |
| `tests/integration/evaluation_harness/test_e4_scenarios.py` | Layer 2/3 focused integration + falsifiability (25) | 475 |
| `tests/integration/evaluation_harness/test_e4_runtime_slice.py` | Layer 4 runtime-integrated (3) | 299 |

Modified: `src/hisiem_soc_copilot/evaluation_harness/__init__.py` — **purely additive** exports (33 names added, 0 removed; 132 → 165). Three helpers that already existed under E3 (`gate_coverage`, `measured_facts`, `missing_scenarios`, `declared_fact_codes`) are exported once from E3's module and re-exported from E4's alias names (`e4_*`) to avoid a duplicate export name.

## 5. Representative trace coverage

The runtime slice drives one real cross-plane lifecycle and captures what the REAL pipeline emits (the production `setup_telemetry` bootstrap, `start_span`, `linked_worker_span`, real OTLP export to a real Collector; the only addition is an extra in-process span processor used for measurement).

Observed semantic operations in the representative run — exactly the required set:

```text
durable.dispatch · graph.invoke · investigation.persist · investigation.run
response.observe · response.submit · tool.execute
```

| Measurement | Value |
|---|---|
| Spans captured in the representative lifecycle | 531 (a second run: 690) |
| Required semantic operations present | 7 / 7 |
| Distinct traces in the lifecycle | 106 |
| Persisted W3C traceparents in the durable store | 9 (9 valid) |
| `durable.dispatch` spans / with a link | 10 / 8 |
| Logical durable commands for the authorization | 1 |

`llm.call` is **deliberately not required** by this slice: the runtime uses the production deterministic `ScriptedModelProvider` (config-selected), which is not instrumented. Requiring the span would have forced either a fabricated trace or new production telemetry added purely for acceptance (E4 §7). The real provider's `llm.call` emission remains covered by the Stage B observability regression, and its telemetry SAFETY is measured by E4's own span/attribute checks — see §16 (E4-OBS-01).

## 6. Trace propagation / durable-link coverage

| Invariant | Evidence |
|---|---|
| W3C propagation across synchronous boundaries | The real HTTP/SQL client instrumentation is installed by the production bootstrap and the inbound `traceparent` is honoured (Stage B bootstrap regression); the E4 slice's spans carry valid 128-bit trace ids and 64-bit span ids |
| Durable continuation instead of one long synchronous trace | Each worker delivery runs as a NEW trace (`linked_worker_span` → `SpanKind.CONSUMER` with a fresh root context) that carries a LINK to the producer |
| Link validity | 8 of 10 `durable.dispatch` spans carry a link; the link is built from the traceparent persisted with the outbox row, validated by production's own `validate_traceparent` |
| Persisted context is real W3C | 9 / 9 non-null outbox traceparents validate as version-00 traceparents |
| Business identity independent of trace identity | 106 distinct traces, **exactly 1** logical durable command (`response:default:<proposal>`) — `TraceContinuationFacts.business_identity_independent_of_trace` is True |
| New trace after durable recovery does not imply a new command | Asserted on the same facts: the command identity count is 1 while the trace count is > 1 |

No new propagation contract was introduced: the adapter calls `infrastructure.observability.context.validate_traceparent` itself rather than carrying a second idea of traceparent grammar.

## 7. Telemetry safety coverage

Measured over REAL emissions (focused integration) and the runtime slice:

* **Span attribute keys** are projected to KEYS ONLY (E4 §21) — no attribute value, no span event, and no payload ever leaves `project_spans`.
* The real tool-path spans carry only `tool_name` / `tool_provider` / `server_category` / `result`: capability identity, provider class, and an outcome category — no argument, no ToolResult body, no provider payload.
* `knowledge.retrieve` and `mcp.call` are emitted by the production instrumented executor with the same bounded key set; MCP credentials, trusted endpoint secrets, and full provider payloads are absent by construction (no code path attaches them).
* The frozen XP-01 marker scan runs over the bounded telemetry surfaces (span names + attribute keys, metric label sets) and reports only matching marker **spellings**; a matched value is never copied into a measurement or an artifact.
* **Raw-content classes** (`prompt`, `raw_prompt`, `completion`, `tool_result`, `authorization`, `bearer`, `api_key`, `password`, `credential`, `dsn`, `embedding*`, `chain_of_thought`, `reasoning`, `env`) are checked as attribute KEYS; finding one is reported through the frozen `SECRET_MARKER_PRESENT` token so a single frozen gate decides telemetry safety (documented in the adapter; no new vocabulary invented).
* The API-boundary sanitizers (`_sanitize_client_request/response`, `_sanitize_server_request`, the collector's drop lists) remain the production enforcement and are covered by the Stage B regression.

No secret, token, DSN, prompt, completion, ToolResult body, embedding vector, or chain-of-thought was printed, persisted into an artifact, or embedded in this report.

## 8. Metric-cardinality coverage

| Invariant | Evidence |
|---|---|
| Production label sets stay bounded | The focused integration captures the label sets production ACTUALLY hands to the sanitizer and re-runs the REAL `sanitize_metric_attributes` over them: all accepted → `TELEMETRY_METRIC_BOUNDED` |
| Forbidden identities are absent from metric labels | The allowlist has exactly 9 label keys (`error_category`, `model_provider`, `operation`, `response_state`, `result`, `retrieval_mode`, `server_category`, `tool_name`, `tool_provider`). Its intersection with the 15 forbidden identities is **empty**; each of the 15 is independently refused by the real sanitizer in a parametrized test |
| Unknown keys and unknown values are refused | The sanitizer rejects the whole observation (fail-closed), so a dropped instrument can never record a high-cardinality dimension |
| The decision is production's, not evaluation's | A test asserts the adapter's verdict agrees with `sanitize_metric_attributes` for accepted, refused, and forbidden inputs by construction |
| Business IDs remain safe TRACE/log correlation | `bind_log_context` allowlists 5 business ids for LOGS ONLY and never writes them to spans or metric labels — the production contract that keeps correlation available without cardinality risk |

## 9. Collector outage / business-independence coverage

E4 runs its OWN representative lifecycle twice — once with the real Collector reachable, once with it stopped — and compares the PERSISTED business outcome:

| Run | Telemetry backend | Persisted business outcome |
|---|---|---|
| 1 | real Collector up | investigation `COMPLETED`, verdict `MALICIOUS`, evidence + finding persisted, policy `REQUIRE_APPROVAL`, approval `APPROVE`, proposal `SUBMITTED`, submission `SUBMITTED`, execution provider `hisiem` with a real provider execution |
| 2 | Collector stopped | **identical** on every compared field |

* `business_outcome_equivalent(...)` is True, and the frozen gate measurement reports `TELEMETRY_OUTAGE_ISOLATED`.
* The invariant is decided by the **frozen** `TELEMETRY_CHANGED_BUSINESS_STATE` gate (E4 does not introduce a second outage gate for the same invariant — one implementation, two callers).
* With the Collector down the runtime still produced every required semantic span: only the EXPORT target was gone. No investigation, tool, knowledge, MCP, approval, or durable-execution failure was caused by the outage.
* **No business dependency on OTel (§19):** no production module outside `infrastructure/observability/` reads a span context, recording state, or export result; the outage run's identical business outcome is the behavioural half of the same proof.
* The negative half is proven: an outcome that differed under outage yields `TELEMETRY_ALTERED_BUSINESS_STATE`.

## 10. Measurement adapters

Two new evaluation-only modules under the sanctioned `evaluation_harness` bridge (no adapter lives in a production layer).

`cross_plane_observability.py` — measures, never decides:

| Adapter | Reads / calls |
|---|---|
| `required_spans_present` / `missing_required_operations` | bounded `SpanFact` projections |
| `SpanFact` / `sensitive_attribute_keys` | attribute KEYS only (values never read) |
| `trace_continuation_facts` | real persisted traceparents + captured links + logical command ids; W3C validity via production's `validate_traceparent` |
| `metric_label_safety` / `evaluate_metric_labels` | the REAL `sanitize_metric_attributes` |
| `telemetry_secret_scan` | the frozen E1 marker scan (reusing E2's `secret_scan_of`) |
| `telemetry_safety_facts` / `span_projection_safe` / `telemetry_surface_violations` | raw-content/credential attribute keys + the real secret scan |
| `collector_outage_facts` / `business_outcome_equivalent` | E3's `telemetry_isolation_facts` (one implementation, two callers) |

Discipline kept: no raw span or telemetry dump in a measurement or artifact; no second sanitizer, validator, or telemetry policy; no weighted score; measurements carry only the frozen XP-01 fact vocabulary and are sourced `TELEMETRY_FACT`.

## 11. Artifact evidence

* `XP-OBS-001` → `cross-plane-gate-results/v1`, `pack_id: XP-01`, `execution_profile: runtime-integrated`, both gates PASS, `measurement_source: TELEMETRY_FACT` / `RUNTIME_PROCESS_FACT`, ~1.4 KB, carrying a bounded representative `trace_id` reference.
* `XP-OBS-002` → deterministic artifact, three gates PASS, ~1.9 KB, byte-identical across two independent runs of the same fixture.
* No artifact exceeds the 256 KB bound, none embeds raw telemetry, and the secret scan runs before any bytes reach disk. No `observability-results/v1` or similar variant was introduced.

## 12. Runtime environment / components used

Inspected before launching anything (`jps -l`, `docker ps`, `docker ps -a`, health endpoints, ports):

| Component | State | Action |
|---|---|---|
| HISIEM control-api (host JVM, 8080) | already running (E3 residue) | **reused** |
| HISIEM SOAR worker (host JVM) | already running | **reused** |
| `siem-postgres` (5432) | already running, healthy | **reused** — protected operator data untouched |
| `siem-kafka` (9092) | already running, healthy | **reused** |
| `copilot-pgvector` (5434) | already running | **reused** as the disposable test database (the protected Copilot operator DB on 5433 was never touched) |
| `siem-elasticsearch` (9200) | stopped | **started** during an early probe of a real-alert investigation path, then **stopped again** at the end of E4 (the final slice does not need it — see §16, E4-OBS-02) |
| OTel Collector `e4-otel-collector` (repo config, 4317/4318) | created per test | **started and removed by the fixture**; no residue |
| Copilot runtime | — | **started** as a real worker process per run (the E4 driver); no long-running residue |

No duplicate service was launched and no duplicate port bound. The representative lifecycle crosses: API/ASGI boundary → durable outbox → durable dispatch (new trace + link) → agent graph → real tool execution → persistence → response policy/approval → durable SOAR submission through the **real** HISIEM internal boundary → durable observation. The investigation's upstream HISIEM READ port is scripted and the model is scripted — the same substitution the repository's own durable-chain integration test makes (Elasticsearch is not required by this slice).

## 13. Architecture-boundary evidence

`tests/architecture` — **282 passed** (E3 baseline 280 + the 2 new source modules picked up by the layer-import parametrization, which is that test's intended behaviour).

Re-confirmed: production layers never import `evaluation` / `evaluation_harness`; the XP-01 pure package imports only its allowed surface and has no IO/clock imports; no XP-01 scenario/gate identity appears in production source; the E4 adapters live under the sanctioned bridge, so `ALLOWED_EVALUATION_IMPORTERS` needed **no** change; the two new modules pass the forbidden-import check as ordinary layer modules.

## 14. Test/regression evidence

All commands run fresh in this session:

| Gate | Command | Result |
|---|---|---|
| E4 focused | `pytest tests/unit/evaluation_harness/test_cross_plane_observability.py tests/integration/evaluation_harness/test_e4_scenarios.py tests/integration/evaluation_harness/test_e4_runtime_slice.py` | **76 passed** (48 + 25 + 3) |
| Stage B observability | `pytest tests/unit/observability -q` | **17 passed** |
| E1 foundation | `pytest tests/unit/evaluation/cross_plane tests/unit/evaluation_harness/test_cross_plane_adapter.py tests/architecture/test_cross_plane_boundary.py -q` | **172 passed** (exact baseline) |
| E2 scenarios | `pytest tests/integration/evaluation_harness/test_e2_scenarios.py -q` | **33 passed**, asserting **15/15 E2 scenarios PASS** |
| E3 scenarios | `pytest tests/unit/evaluation_harness/test_cross_plane_authority.py tests/integration/evaluation_harness/test_e3_scenarios.py tests/integration/evaluation_harness/test_e3_focused_durable.py tests/integration/evaluation_harness/test_e3_runtime_slice.py -q` | **100 passed**, asserting **10/10 E3 scenarios PASS** |
| GP-01 | same, with all XP-01 additions excluded | **346 passed** (exact baseline) |
| KB-GOLDEN-V1 | `pytest tests/unit/knowledge tests/unit/evaluation/knowledge tests/integration/persistence/test_knowledge_persistence.py -q` | **679 passed / 0 skipped** (exact baseline) |
| Architecture | `pytest tests/architecture -q` | **282 passed** |
| Static | `ruff check src tests` / `mypy src` / `git diff --check` | **clean / no issues in 227 source files / clean** |
| Full pytest | `pytest -q` | **2183 passed / 9 skipped / 0 failed / 0 errors** |

Positive + negative + falsifiability coverage (E4 §22/§23) — each of these proves a failure is reachable:

| Invariant | Falsifiability proof |
|---|---|
| required semantic span absent | direct invalid measurement → EXPECTED_FACTS_PRESENT FAIL |
| secret in span attributes | synthetic sentinel on a span key → SECRET_LEAK / FORBIDDEN_FACTS_ABSENT FAIL |
| raw ToolResult projected into telemetry | `tool_result` attribute key → FORBIDDEN_FACTS_ABSENT FAIL |
| high-cardinality metric label | `investigation_id` / `trace_id` label → FORBIDDEN_FACTS_ABSENT FAIL |
| collector outage changes the business outcome | differing persisted outcomes → `TELEMETRY_ALTERED_BUSINESS_STATE` |
| oracle token on a production surface | XP-OBS token in a scanned surface → ORACLE_FIREWALL FAIL |
| missing expected / present forbidden fact | every declared token of both scenarios, proven to FAIL its gate |
| missing measurement | a scenario with no collected facts raises `GateMeasurementMissingError` — never a silent PASS |

Full-suite arithmetic, so the delta is auditable: E3 baseline **2105** + **76** new E4 tests + **2** architecture parametrizations over the 2 new source modules = **2183**. The 9 skips are the same 9 as the E2/E3 baselines; **no new skip was introduced** for E4 — both OBSERVABILITY scenarios execute for real, and the runtime slice skips only with an explicit reason when its runtime is absent.

Two cross-test isolation defects were found and fixed during E4 rather than left in: the telemetry capture now hands the SDK back a **pristine, uninstalled** state (global providers + the once-guards + un-instrumented libraries), so the Stage B bootstrap test still owns its own install; and the outage test compares the **required semantic operations** rather than every span, since SQL/HTTP instrumentation span sets legitimately differ run to run and are not a business invariant.

## 15. Production diff

```text
HISIEM production diff            = 0   (repo clean, HEAD == origin, 0 working-tree entries)
Copilot production-layer diff     = 0   (domain/ application/ agent/ api/ infrastructure/ bootstrap/)
frontend diff                     = 0
migration / new table             = 0
second observability platform / tracing vendor / collector stack / telemetry DB = 0
LLM-as-a-Judge                    = 0
DEFECT-005 work                   = 0   (still OPEN / LOW / NON-BLOCKING)
TEST-INFRA-001 repair             = 0   (not absorbed)
E5 work                           = 0
new spans added for acceptance    = 0   (queue.wait / alert.hydrate / native.call / embedding /
                                         postgres.fts / pgvector.search / retrieval.merge NOT added)
```

The only tracked file modified in the whole stage is `evaluation_harness/__init__.py` (additive exports). No E1 contract, catalog, gate, reason code, or fact token was added, removed, or re-semanticised. No Stage B observability defect was found, so no production telemetry change was made.

## 16. Known observations / non-blockers

Carried forward unchanged, none absorbed into E4:

| ID | Status |
|---|---|
| CATALOG-001 | RESOLVED (E1: 29 scenarios implemented) |
| OBS-001 | **OPEN / NON-BLOCKING / unchanged** — the 00 §5.3 taxonomy examples (`queue.wait`, `alert.hydrate`, `native.call`, `embedding`, `postgres.fts`, `pgvector.search`, `retrieval.merge`) are still not implemented. E4 acceptance does **not** require them: the required semantic spans are present and correlatable, so no production telemetry was added merely for completeness |
| DEFECT-005 | OPEN / LOW / NON-BLOCKING / unchanged |
| TEST-INFRA-001 | OPEN / test-infrastructure-only (not absorbed) |
| EVAL-SEAM-001 | ACCEPTED / NON-BLOCKING / unchanged |
| ENV-001 | resolved for this stage: the E3-residue JVMs and containers were reused rather than duplicated; see §12 for the exact inventory |

New observations from E4 (informational, non-blocking):

* **E4-OBS-01 — `llm.call` is not required by the runtime slice.** The slice runs the production deterministic `ScriptedModelProvider`, which emits no `llm.call`. E4 accepted this rather than adding instrumentation to a scripted provider for acceptance purposes. The emission exists in the real `OpenAICompatibleModelProvider` and is covered by the Stage B regression.
* **E4-OBS-02 — the investigation's upstream HISIEM read port is scripted in this slice.** Elasticsearch is not part of the smallest representative slice, and the repository's own durable-chain integration test makes the same substitution. The Copilot→HISIEM SOAR boundary remains real (control-api + SOAR worker + Kafka), so the trace still crosses real cross-plane HTTP and the durable boundary.
* **E4-OBS-03 — raw-content absence shares the frozen `SECRET_MARKER_PRESENT` token.** The catalog has no dedicated token for "raw prompt/completion/ToolResult reached telemetry", so E4 reports those classes through the existing telemetry-safety token rather than inventing vocabulary. Documented in the adapter; no catalog change.
* **E4-OBS-04 — `siem-elasticsearch` was started and stopped again.** It was brought up during an early probe of a real-alert investigation path, found unnecessary for the final slice, and returned to its original stopped state. It holds no E4 state.
* **E4-OBS-05 — two cross-test isolation defects were found and fixed** during E4 (global telemetry provider/instrumentation leakage between test modules; a span-set equality assertion that was not a business invariant). Both are described in §14.

## 17. E5 handoff

E5 (WORKSPACE) is **not started** — `XP-UX-001/002` remain unimplemented, and no frontend, `AuthorityTag`, Playwright, refresh/stale-reconstruction, or workspace-projection work was touched.

What E5 inherits:

* XP-01 is 27/29 complete; the contract package, the 13-gate model, the non-compensating verdict, and `cross-plane-gate-results/v1` are frozen and unchanged.
* The E2/E3/E4 driver pattern (`<Stage>Fixture` → driver → E1 measurement → `evaluate_scenario` → artifact) is proven for deterministic, focused-integration, and runtime-integrated profiles and can be mirrored without touching E1.
* `MeasurementSource.WORKSPACE_PROJECTION_FACT` already exists; `forbidden_id` vocabulary already contains `WORKSPACE_INVENTED_AUTHORITY` and the expected vocabulary contains `WORKSPACE_AUTHORITY_LABELS_PRESENT`, `WORKSPACE_RECONSTRUCTED_FROM_DURABLE_STATE`, `WORKSPACE_STALE_OVERRIDDEN_BY_REFRESH` — no contract change is expected.
* The E4 telemetry capture harness (`tests/support/e4_observability_fixture.py`) attaches to, and cleanly relinquishes, the SDK-global telemetry pipeline; any E5 test needing spans should reuse it rather than installing its own provider.
* The runtime slice shape (`tests/support/e4_observability_driver.py` + `test_e4_runtime_slice.py`) is the reusable pattern for real-process scenarios, including explicit skip reasons and no production change.

What E5 must not modify: the E1 contract package and vocabularies, the E2/E3/E4 adapters' semantics, GP-01, KB-GOLDEN-V1, `ALLOWED_EVALUATION_IMPORTERS`, and the `evaluation_harness/__init__.py` export convention (add names, re-sort the whole `__all__`, never re-sort before adding).
