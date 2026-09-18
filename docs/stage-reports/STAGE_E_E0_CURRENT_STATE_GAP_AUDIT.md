# Stage E — E0 Current-State / Gap Audit

- Date: 2026-09-17 (Asia/Shanghai)
- Auditor: Claude Code (Opus 5), executing `Stage-E_E0_Current-State_Gap-Audit_VibeCoding_Prompt.md`
- HISIEM branch: `add_frame`
- HISIEM HEAD: `d0baed9d111ecefb33d5a9222d916042863b11c9`
- HISIEM remote: `origin/add_frame` = `d0baed9d111ecefb33d5a9222d916042863b11c9` (in sync; working tree clean)
- Copilot branch: `capability-mcp`
- Copilot HEAD: `d4cfc8ed69043d0c14991833223ff81eb8d1e3a0`
- Copilot remote: `origin/capability-mcp` = `d4cfc8ed69043d0c14991833223ff81eb8d1e3a0` (in sync; working tree clean before this report)
- Authorities read in full:
  - `00_Four-Plane_Architecture_Contract_Freeze.md` (FROZEN BASELINE v1.0, 2026-09-14)
  - `05_Stage-E_Cross-Plane_Integration_Evaluation_Spec.md`
  - `06_Stage-E_Detailed_Design.md` (DESIGN BASELINE v1.0, 2026-09-17)
- Runtime baseline: `FULL_RUNTIME_E2E_TEST_REPORT.md` (both runs; 25/25 matrix PASS, PRE-SEAL SYSTEM GATE PASS)

No file was created, modified, or deleted in E0 other than this report. No commit, no push, no reset, no restore, no clean, no rebase, no force-push.

---

# 26.2 Executive Status

```text
E0 AUDIT: PASS

Stage E implementation readiness:
READY FOR E1
```

Rationale, against the §27 pass criteria:

| §27 criterion | Status | Evidence |
|---|---|---|
| 00 / 05 / 06 found and fully read | PASS | All three read in full from `D:\Project\four-plane\` |
| Local + remote Git state verified | PASS | Both repos clean, branch/HEAD/remote identical |
| Evaluation Plane inspected from code, not docs | PASS | 13,186 lines across `evaluation/` + `evaluation_harness/` inspected; gate functions read, not inferred |
| GP-01 frozen-specific code identified | PASS | §26.5 |
| Reusable generic primitives identified | PASS | §26.4, §26.9 |
| XP-01 gaps evidence-backed | PASS | §26.6, §26.7 |
| Hard-gate measurement sources identified | PASS | §26.8 |
| Runtime evidence vs automated-evaluation gaps distinguished | PASS | §26.10; runtime evidence is NOT counted as an evaluation gate |
| Production-change budget explicit | PASS | §26.11 — expected production changes: none |
| No unresolved authority/truth-source contradiction blocks E1 | PASS | §26.12 — one non-blocking observability *coverage* observation, no architecture contradiction |
| E1 boundary statable precisely | PASS | §26.13 |

E0 PASS does **not** mean Stage E is implemented. No XP-01 code, contract, artifact, or CLI command exists yet.

---

# 26.3 Current Architecture Map

## 26.3.1 Copilot — plane ownership by package

| Plane / concern | Owning package(s) | Key production modules |
|---|---|---|
| Knowledge (retrieval/citation/ATT&CK) | `domain/knowledge`, `application/commands+handlers+services`, `infrastructure/knowledge`, `infrastructure/embedding` | `application/services/knowledge_retrieval.py`, `agent/knowledge/catalog.py`, `domain/knowledge/` |
| Capability governance | `agent/tools` | `registry.py` (`AGENT_SELECTABLE_TOOLS`, `ToolSpec`, `model_selectable`), `policy.py` (`validate_candidate`), `executor.py`, `provider_router.py`, `native_provider.py`, `providers.py` |
| MCP provider | `infrastructure/mcp`, `agent/tools/providers.py` | `infrastructure/mcp/provider.py`, `AdmissionEntry`, `ProviderInvocationContext`, `ResultBounds`, `ProviderFailureCode` |
| Evidence / Finding / Verdict | `domain/investigation`, `agent/evidence`, `agent/graph` | `agent/evidence/normalizer.py`, `agent/graph/nodes.py` (`_findings_with_platform_evidence` L1095, applied in `finalize_result` L1193-1207) |
| Response / Approval / Durable | `domain/response`, `application/commands+handlers`, `infrastructure/durable` | `domain/response/enums.py`, `domain/response/policy.py`, `infrastructure/durable/dispatcher.py`, `investigation_runner.py`, `response_runner.py` |
| Observability | `infrastructure/observability` | `bootstrap.py`, `context.py`, `metrics.py`, `tools.py` |
| Workspace / API boundary | `api/routers`, `api/schemas` | `api/routers/investigations.py`, `api/schemas/workspace.py` |
| Evaluation Plane | `evaluation`, `evaluation_harness`, `evaluation/knowledge`, and the non-production driver `knowledge/evaluation.py` | see §26.4 |

## 26.3.2 HISIEM — boundaries Stage E consumes (unchanged, sealed)

| Boundary | Location | Stage E relevance |
|---|---|---|
| Vue Investigation Workspace | `web/src/views/copilot/InvestigationWorkspaceView.vue`, `web/src/components/copilot/*`, `web/src/utils/copilot.js` | XP-UX-001/002 |
| Playwright browser contract suite | `web/e2e/*.spec.js` (5 specs) | XP-UX-001/002 extension surface |
| BFF agent-investigation | `applications/control-api/.../agent/AgentInvestigationController.java` (`/api/agent-investigations`) | launch / workspace / cancel / proposal / approve / reject forwarding |
| Internal SOAR API | `applications/control-api/.../soar/InternalSoarController.java` | execution truth |
| SOAR worker | `applications/soar-worker` | observed execution |
| Tenant / actor propagation | `TenantContextFilter.java`, `modules/platform-contracts/.../TenantContext.java` | XP-TEN-001/002 |
| Cases / audit | `CaseController.java`, `audit_logs` via `/api/auth/audit-logs` | XP-AUTH-001 |

## 26.3.3 Dependency direction (verified by AST scan, E0 §22)

```text
production layers (domain, application, agent, api, infrastructure, bootstrap)
    -> import evaluation / evaluation_harness?:  ZERO occurrences   ✔

packages that import evaluation_harness:
    evaluation/cli.py only                                          ✔

packages that import evaluation.*:
    knowledge/evaluation.py (non-production driver), evaluation_harness
    -> pinned exactly by tests/architecture/test_knowledge_boundary.py
       test_only_the_non_production_knowledge_and_harness_see_evaluation
```

All three required rules hold. Rule 4 ("evaluation harness may bridge sealed evaluation input to real runtime") is satisfied by `evaluation_harness/harness.py`, which is the only sanctioned `SealedManifest -> Container` bridge and imports production `agent.graph.builder`, `application.commands.investigation`, `bootstrap.container`, `infrastructure.checkpoint.postgres`, `infrastructure.llm.scripted`.

**E1 binding constraint (evidence-backed):** `tests/architecture/test_knowledge_boundary.py:488` asserts
`set(_evaluation_importers()) == {"evaluation_harness", "knowledge"}` — **exact equality**, with the docstring "Nothing else may join this list without editing this test, which is the point."
Consequence: an XP-01 adapter placed in a *new* top-level package that imports the `evaluation` package will fail this architecture test. E1 must either (a) place the adapter under the already-allowlisted `evaluation_harness` or `knowledge` packages, or (b) extend `ALLOWED_EVALUATION_IMPORTERS` deliberately as a reviewed change.

---

# 26.4 Existing Evaluation Inventory

Scope: 13,186 lines across `src/hisiem_soc_copilot/evaluation/` (6,824 incl. `knowledge/`), `src/hisiem_soc_copilot/evaluation_harness/` (5,014), plus the driver `src/hisiem_soc_copilot/knowledge/evaluation.py` (~570).

## 26.4.1 GP-01 family — `evaluation/` (dataset materialize -> seal -> verify)

| Module / File | Responsibility | Scenario Ownership | Reusable for XP-01 | Frozen? | Evidence |
|---|---|---|---|---|---|
| `evaluation/contracts.py` (24 KB) | `ScenarioSpec`, `ScenarioOracle`, `SealedManifest`, `RunIdentity`, `EventTimePlan`, `MaterializationState`, canonical JSON/hash, error taxonomy | GP-01 only (defaults are GP-01 literals: `rule-ssh-brute-force-001`, F1..F5/S1/W1) | Partially — the *idioms* (frozen dataclass spec, canonical hash, typed error taxonomy) yes; the type itself no | **FROZEN** | `GP01_*` constants L43-67; `ScenarioSpec` L70 |
| `evaluation/oracle.py` (63 L) | Private oracle projection; asserts evidence roles ⊆ semantic roles and control role excluded | GP-01 | Idiom yes (`oracle_from_scenario`), type no | **FROZEN** | `oracle.py` L38-63 |
| `evaluation/launch_projection.py` (24 L) | The ONLY production-safe projection (`EvaluationLaunchRef`) | GP-01 | **Yes — directly** | **FROZEN** | `launch_ref()` |
| `evaluation/manifest.py`, `sealer.py` (569 + 600 L) | Sealed manifest build + integrity chain + atomic publication lock | GP-01 | Idiom yes (atomic publish, integrity chain) | **FROZEN** | `test_pure_sealer_modules_never_import_reader_or_injector` |
| `evaluation/materializer.py` (666 L) | Real HISIEM dataset materialization (syslog injection, event/alert resolution) | GP-01 | No — dataset-specific | **FROZEN** | `tests/unit/evaluation/test_materializer.py` |
| `evaluation/hisiem_reader.py` (36 KB) | HISIEM read/poll client for materialization | GP-01 | No | FROZEN | `test_hisiem_reader.py` |
| `evaluation/injector.py`, `syslog.py`, `time_plan.py`, `identity.py`, `verifier.py`, `ledger.py` | Injection, rendering, time planning, run identity, verification, ledger | GP-01 | Individual idioms yes | FROZEN | unit suites |
| `evaluation/cli.py` (20 KB) | Hand-rolled `if command ==` dispatch: `materialize`, `resume`, `seal`, `verify-manifest`, `prepare`, `execute`, `execute-real-model`, `execute-tool-evidence`, `score-execution`, `evaluate-gp01` | GP-01 | **Yes — the extension point** | FROZEN | `cli.py` L411-470 |

## 26.4.2 GP-01 family — `evaluation_harness/` (execute -> score -> suite)

| Module / File | Responsibility | Scenario Ownership | Reusable for XP-01 | Frozen? | Evidence |
|---|---|---|---|---|---|
| `record.py` (353 L) | `EvaluationExecutionRecord` + **`atomic_write_json`** (same-dir temp + fsync + `os.replace`) | GP-01 | **Yes — core artifact primitive** | FROZEN | `record.py` L304-335 |
| `classification.py` (193 L) | `AttemptClassification`, `CLASS_VALID_PASS` / `CLASS_VALID_FAIL` / `CLASS_INVALID_PROVIDER_TRANSIENT` / `CLASS_ABORT`, `classify_attempt` | GP-01 | **Yes — directly** ("valid failures count" is a Stage E principle) | FROZEN | `test_classification.py` |
| `quality.py` (504 L) | `ToolEvidenceQuality` + `build_tool_evidence_quality` — pure gate over bounded facts; already carries `dangling_citation_count`, `cross_investigation_citation_count`, `oracle_firewall_pass`, `secret_scan_pass`, `control_event_evidence_ids` | GP-01 (E1-C3) | **Yes — gate semantics + reason codes** | FROZEN | `quality.py` L330-420 |
| `quality_harness.py` (467 L) | **Read-only fact projection through repository ports** (`_read_quality_facts`), oracle-firewall scan, secret scan | GP-01 | **Yes — the read model idiom** | FROZEN | `quality_harness.py` L205-290 |
| `score.py` (540 L) | `EvaluationScore` + deterministic `score_gp01`; 14 stable failure codes; schema `evaluation-score/v1`; allowlisted read-back | GP-01 | **Yes — reason codes + artifact discipline** | FROZEN | `score.py` L91-104, L331-478 |
| `score_harness.py` (362 L) | Wires durable facts + oracle into the scorer | GP-01 | **Yes** | FROZEN | `test_score_execution_chain.py` |
| `suite.py` (790 L) | `SuiteSummary` + `execute_gp01_suite` + `build_suite_summary` + `validate_bounds` (`DEFAULT_VALID_RUNS`, `DEFAULT_MAX_ATTEMPTS`, `HARD_CAP`); **`secret_scan_pass` + `_SECRET_MARKERS` + `SUITE_FAIL_SECRET_SCAN_VIOLATION`**; unique per-suite artifact path | GP-01 | **Yes — the suite/aggregation primitive** | FROZEN | `suite.py` L60-140, L260-340 |
| `telemetry.py` (312 L) | `ModelTelemetry` E1-C2 provider-baseline gate + `REQUIRED_OPERATIONS`; strict usage-record allowlist | GP-01 / E1-C2 | Allowlist idiom yes; expected constants are Command-Code-specific | FROZEN | `telemetry.py` L37-58 |
| `harness.py` (1283 L) | `EvaluationProfile` (`E1_C1_SCRIPTED`, `E1_C2_REAL_MODEL`), `build_start_command`, `record_from_manifest`, `resolve_execution_provenance`, `execute_execution`, `execute_real_model_run` | GP-01 | **Yes** | FROZEN | `harness.py` L94-106 |
| `runtime.py`, `__init__.py` | selector loop policy; public API surface (68 exported names) | GP-01 | Yes | FROZEN | `__init__.py` |

## 26.4.3 KB-GOLDEN-V1 family — `evaluation/knowledge/` + `knowledge/evaluation.py`

This is the **second, already-existing evaluation family**, and the single most important E0 discovery for XP-01 scope.

| Module / File | Responsibility | Scenario Ownership | Reusable for XP-01 | Frozen? | Evidence |
|---|---|---|---|---|---|
| `evaluation/knowledge/corpus.py` (1311 L) | Sealed corpus + case set: **18 documents, 22 cases, 3 tenants** across 9 categories: `SEMANTIC_GUIDANCE`, `EXACT_IDENTIFIER`, `NEAR_MISS_WRONG_DOC`, `SCOPE_COMPETITION`, `TENANT_RUNBOOK`, `CROSS_TENANT_DENIED`, `PROMPT_INJECTION_POISON`, `RETIRED_EXCLUSION`, `ATTACK_QUERY`; `POISONED_DOCUMENT_KEYS`, `PROMPT_INJECTION_MARKERS`, `RETIRED_DOCUMENT_KEYS` | KB-GOLDEN-V1 | **Yes — covers XP-TEN-001 and XP-SEC-001 corpus needs directly** | Sealed family; `CORPUS_VERSION=1` | executed at runtime: `18 documents, 22 cases, 3 tenants` |
| `evaluation/knowledge/metrics.py` (196 L) | `cross_tenant_leakage_count`, `forbidden_retrieval_count`, `citation_resolution_rate`, `recall_at_k`, `ndcg_at_k`, `reciprocal_rank` | KB-GOLDEN-V1 | **Yes** | FROZEN | `metrics.py` |
| `evaluation/knowledge/runner.py` (526 L) | `EvalMode` (HYBRID/LEXICAL_ONLY/VECTOR_ONLY), `ModeUnavailableError`, `HybridGate`, `run_mode`, `run_suite`, `hybrid_verdict`; retrieval **injected as async callables** | KB-GOLDEN-V1 | **Yes** | FROZEN | `runner.py` L50-232 |
| `evaluation/knowledge/artifact.py` (204 L) | `knowledge-retrieval-eval/v1`, bounded excerpts (240 chars), `build_artifact`, `write_artifact` | KB-GOLDEN-V1 | **Yes** | FROZEN | `artifact.py` L33-51 |
| `evaluation/knowledge/corpus_identity.py` (310 L) | `corpus_fingerprint`, `corpus_identity`, `preflight_corpus`, `CORPUS_PRECONDITION_FAILED` | KB-GOLDEN-V1 | **Yes** | FROZEN | `corpus_identity.py` |
| `knowledge/evaluation.py` (~570 L) | **The adapter that drives the sealed package over the REAL production stack**: ingests via `KnowledgeIngestionHandler`, retrieves via `KnowledgeRetrievalService.retrieve`, re-validates every citation via `KnowledgeCitationResolver.resolve`; distinguishes `DEPLOYMENT_CONFIGURED` from `PLUMBING_ONLY` **in the artifact file name** | KB-GOLDEN-V1 | **Yes — the precedent E1 should follow** | Non-production driver | module docstring L1-29 |
| `knowledge/cli.py` | `evaluate` subcommand: `--k`, `--mode`, `--out`, `--overwrite`, `--embedding-provider` | KB-GOLDEN-V1 | **Yes** | Non-production CLI | `cli.py` L163-182 |

Live artifact on disk: `.eval-runs/knowledge/kb-golden-v1-k5-plumbing-only.json`.

## 26.4.4 Evaluation artifact layout (existing convention)

```text
.eval-runs/<family>/<run-id>/manifest.json          # gp-01 dataset (sealed)
.eval-runs/knowledge/<suite>-k<N>[-plumbing-only].json
.eval-executions/gp-01/<run-id>/<execution-id>/     # execution.json, score.json,
                                                    # tool-evidence-quality.json,
                                                    # model-telemetry.json
```

The 06 §11.1 proposed tree (`<evaluation-root>/xp-01/<run-id>/scenarios/<sid>/...`) maps onto this convention with `scenarios/` under an `xp-01` family directory. No new root is required.

---

# 26.5 GP-01 Freeze Map

Classification key: `GP01_FROZEN_SPECIFIC` / `GENERIC_REUSABLE_PRIMITIVE` / `GENERIC_BUT_CURRENTLY_EMBEDDED`.

| Component | Classification | Reason | Stage E action |
|---|---|---|---|
| `ScenarioSpec` (defaults `gp-01`, `rule-ssh-brute-force-001`, F1..F5/S1/W1, `expected_verdict="MALICIOUS"`) | **GP01_FROZEN_SPECIFIC** | Every default is a GP-01 literal; the type *is* the committed scenario. 06 §3.2 explicitly forbids adding optional cross-plane fields | **Do not touch.** XP-01 defines `CrossPlaneScenarioSpec` separately (06 §5.1) |
| `ScenarioOracle` / `oracle_from_scenario` | **GP01_FROZEN_SPECIFIC** (shape) + **GENERIC_REUSABLE_PRIMITIVE** (the "oracle = bounded semantic predicates, never prose" idiom) | Predicates are generic; the container is GP-01's | Reuse the *idiom*: XP-01 expected/forbidden facts are bounded machine predicates |
| `GP01_LOGICAL_DATASET`, `GP01_*` constants | **GP01_FROZEN_SPECIFIC** | Dataset identity | Do not touch |
| `SealedManifest` + `sealer.py` integrity chain | **GP01_FROZEN_SPECIFIC** (schema `gp-eval-manifest/v1`) + **GENERIC_REUSABLE_PRIMITIVE** (atomic publish, schema-version rejection) | Version string and integrity semantics are GP-01's | Reuse `atomic_write_json`; XP-01 gets its own schema string |
| `launch_projection.launch_ref` | **GENERIC_REUSABLE_PRIMITIVE** | Maps 1:1 onto the production `ExternalResourceRef`; carries no GP-01 semantics | **Reuse unchanged** |
| `atomic_write_json` (`record.py`) | **GENERIC_REUSABLE_PRIMITIVE** | Same-dir temp + fsync + `os.replace`; zero GP-01 coupling | **Reuse unchanged** for every XP-01 artifact |
| `AttemptClassification` / `classify_attempt` | **GENERIC_REUSABLE_PRIMITIVE** | "valid failure counts; only transport/limit errors are excludable" is a Stage E principle (06 §4.6) | **Reuse unchanged** |
| `ToolEvidenceQuality` gate (`dangling_citation_count`, `cross_investigation_citation_count`, `oracle_firewall_pass`, `secret_scan_pass`, `control_event_evidence_ids`) | **GENERIC_BUT_CURRENTLY_EMBEDDED** | The invariants are generic, but the gate is bound to `expected_s1_index` / `expected_s1_document_id` / `SEARCH_EVENTS_TOOL_NAME` and to `findings_citing_s1_evidence` | Reuse the *reason codes and fact types*; XP-KNOW-001/004 supply their own identity expectations |
| `score.py` failure codes (`DANGLING_EVIDENCE_CITATION`, `CROSS_INVESTIGATION_EVIDENCE_CITATION`, `ORACLE_FIREWALL_VIOLATION`, `CONTROL_EVENT_USED_AS_EVIDENCE`, …) | **GENERIC_REUSABLE_PRIMITIVE** | Stable, machine-authoritative, already aligned with Stage E gate names | **Reuse the codes** (see §26.8) |
| `SuiteSummary` + `_SECRET_MARKERS` + `SUITE_FAIL_SECRET_SCAN_VIOLATION` + `suite_summary_path` | **GENERIC_REUSABLE_PRIMITIVE** | Bounded, allowlisted, schema-versioned, secret-scanned, unique per suite | **Reuse the discipline**; XP-01 has its own suite schema string and family fields |
| `ModelTelemetry` (`REQUIRED_OPERATIONS`, `EXPECTED_MODEL="deepseek/deepseek-v4-flash"`, `EXPECTED_PROVIDER="command_code"`) | **GENERIC_BUT_CURRENTLY_EMBEDDED** | The allowlist + gate idiom is generic; the expected constants are the E1-C2 live baseline | Do **not** extend for the deterministic profile. XP-01 records its own model-profile facts |
| `EvaluationProfile` (`E1_C1_SCRIPTED`, `E1_C2_REAL_MODEL`) | **GP01_FROZEN_SPECIFIC** | GP-01's transport-profile enum, with GP-01-specific preconditions (dispatcher disabled) | XP-01 defines its own `execution_profile` field per 06 §5.2. **Do not add members** |
| `knowledge/evaluation.py` driver | **GENERIC_REUSABLE_PRIMITIVE** | Adapter idiom: sealed package + injected real production callables + bounded artifact fields | **Reuse the pattern** for the XP-01 adapter |

**Conclusion:** GP-01 stays immutable. There is **no evidence** that `ScenarioSpec` was designed for cross-plane extensibility (every field defaults to a GP-01 literal; the type is frozen and hash-bearing). 06 §3.2's prohibition is correct and must be honoured.

---

# 26.6 XP-01 Gap Matrix

| Area | Current Capability | Reuse | Missing Gap | Production Change? | Proposed Stage E Extension |
|---|---|---|---|---|---|
| Knowledge | Tool -> citation resolve -> citation revalidate -> EvidenceNormalizer -> immutable Evidence; ATT&CK resolver; tenant scope; LEXICAL/VECTOR/HYBRID with truthful provenance. Unit + integration + runtime evidence. | `knowledge.retrieve_security_guidance` path, `KnowledgeCitationResolver`, `EvidenceNormalizer`, KB-GOLDEN-V1 corpus/metrics | No **hard-gate** wrapper that asserts the whole evidence chain for one investigation with a stable reason code and a versioned artifact | **No** | `evaluation/cross_plane/` gate functions + adapter reading persisted Evidence/Finding via ports |
| Capability | `ToolRegistry` + `validate_candidate` (registry/model-selectable/budget) + `ProviderRouter` | `registry.py`, `policy.py` | No gate that records "no Knowledge-specific bypass" as a named scenario result | **No** | XP-CAP-001 gate over the same registry/policy objects |
| MCP | Full Stage C provider: discovery, normalization, admission, SHA-256 schema fingerprint, drift -> `SCHEMA_MISMATCH`, read-only, bounds, typed failures, protected args; real Streamable HTTP integration test | `AdmissionEntry.is_model_selectable` (= `model_selectable and risk=="READ_ONLY"`), `RiskClass`, `ProviderFailureCode`, `ResultBounds`, `tests/integration/test_mcp_streamable_http.py` | No XP scenario pack, **and the local MCP server exists only as an inline fixture inside one test function** | **No** | Factor the in-process server into a reusable fixture; XP-MCP-001..005 gates |
| Tenant | Trusted-context tenant everywhere; `find_investigation_ids_by_evidence_ids` / `find_investigation_ids_by_finding_ids` owner lookups; KB `CROSS_TENANT_DENIED`; integration tests already use `tenant-b` | Repository owner-lookup ports; `tests/fixtures/response_flow.py` (`OTHER_TENANT`) | No cross-plane gate asserting tenant isolation for *both* Knowledge and MCP in one pack | **No** | XP-TEN-001/002 gates reusing existing corpora + fixtures |
| Security | Prompt injection is DATA (KB `PROMPT_INJECTION_POISON` corpus); secrets env-sourced; metrics allowlist; `_SECRET_MARKERS` scans | KB injection corpus, `_SECRET_MARKERS`, `sanitize_metric_attributes` | No unified secret/sensitive scan across **Evidence + artifact + telemetry** for one run | **No** | XP-SEC-003 gate composing existing scanners |
| Authority | Full state machines: `ResponseProposalStatus`, `PolicyDecision`, `ApprovalDecisionKind`, `ResponseSubmissionStatus` (incl. `ATTENTION_REQUIRED`), `ResponseExecutionStatus`; `PolicyDenyReason` incl. `CROSS_TENANT_TARGET` / `DENYING_VERDICT`; knowledge-only verdict guard in `nodes.py` | Domain enums + read-only proposal/approval/submission/execution repositories | No gate that reads those persisted facts and emits `APPROVAL_EXECUTION_CONFLATED` / `SUBMISSION_SUCCESS_CONFLATED` | **No** | XP-AUTH-001..005 gates over read-only ports |
| Reliability | Typed failure taxonomy; bounded retry/backoff; `ATTENTION_REQUIRED`; telemetry outage proven not to break business flow (runtime) | `AttemptClassification`, `ProviderFailureCode`, durable dispatcher | No deterministic scenario asserting empty-vs-unavailable and tool-timeout as named gates | **No** | XP-REL-001..005 gates |
| Observability | OTel base, W3C context, durable link, span/metric taxonomy; label allowlist **rejects** unknown labels | `start_span`, `sanitize_metric_attributes`, `_ALLOWED_LABEL_VALUES` | No automated gate over a captured telemetry snapshot; no test-scoped Collector config (only `infra/otel-collector/collector.yaml`) | **No** | XP-OBS-001/002 gates over an existing Collector export |
| Workspace | Sealed Stage D workspace + authority semantics; 5 Playwright specs (34 tests, mock-based); runtime browser acceptance proven ad hoc | `web/e2e/*.spec.js`, `web/src/utils/copilot.js` authority derivations | No versioned XP-UX gate; no durable-state-vs-UI assertion pack | **No** (unless a real defect is found, per 06 §13.1) | XP-UX-001/002 targeted Playwright + durable cross-check |
| Evaluation artifacts | `atomic_write_json`, schema-version rejection, allowlists, bounded fields, secret scan, unique suite identity | `record.py`, `score.py`, `quality.py`, `suite.py` | XP-01-specific schemas (`CrossPlaneScenarioResult`, `CrossPlaneSuiteSummary`) | **No** | New schema strings + reuse of the primitives |
| Suite aggregation | `SuiteSummary` with `family_results`-equivalent per-scenario fields, `secret_scan_pass`, `gate_failures`, `suite_gate` | `suite.py` | Cross-family aggregation (8 families, no weighted average) | **No** | `CrossPlaneSuiteSummary` |
| CLI | Hand-rolled dispatch, `command + target` arity, 10 commands | `evaluation/cli.py` | `evaluate-xp01` / `score-xp01` / `verify-xp01` (names not frozen) | **No** | Extend the same dispatcher; do not add a second CLI |

---

# 26.7 Scenario Readiness Matrix

Profiles: `D` = deterministic, `R` = runtime-integrated, `L` = live-model (informational).

| Scenario ID | Existing Evidence | Missing Evaluation Capability | Profile | Fixture | Artifact | Prod Change | Complexity |
|---|---|---|---|---|---|---|---|
| XP-KNOW-001 Knowledge grounding/citation | Unit + integration + **runtime** (E2E: 3 KNOWLEDGE Evidence rows, real `kcit:` citation, resolver returned `resolved`); `citation_integrity_pass` / `DANGLING_EVIDENCE_CITATION` exist in `score.py` | A named cross-plane gate binding Tool -> Citation -> Evidence -> Finding for one investigation, plus its own artifact | **D** | Reuse KD deterministic profile + a corpus document | New `gate-results.json` under `xp-01` | No | **S** |
| XP-KNOW-002 Knowledge-only verdict guard | Implemented guard in `nodes.py` L1095/1193; **runtime-proven** in E2E (verdict downgraded to `INCONCLUSIVE`, confidence 0.0); unit tests exist | A gate that asserts the guard fired and records `KNOWLEDGE_AUTHORITY_VIOLATION == 0` | **D** | Scripted model forced to cite knowledge-only | Reuse gate artifact | No | **S** |
| XP-KNOW-003 Empty vs unavailable | `ModeUnavailableError`, typed `KnowledgeRetrievalUnavailableError`; KB-GOLDEN-V1 mode-unavailable handling; unit tests | A gate distinguishing successful-empty from unavailable, with `FAILURE_NORMALIZED_AS_EMPTY` | **D** | Empty-result + unavailable stubs | Reuse | No | **S** |
| XP-KNOW-004 Citation invalidation/drift | `RETIRED_EXCLUSION` corpus category + `resolve_citation` revalidation + hash chain; Stage A validation covered it | A gate that a stale/drifted citation never becomes Evidence | **D** | Retire-then-cite fixture (corpus already ships `RETIRED_DOCUMENT_KEYS`) | Reuse | No | **S** |
| XP-CAP-001 Governed Knowledge tool | `validate_candidate` + `ToolBudgetExhausted` + `AGENT_SELECTABLE_TOOLS`; unit tests | Named gate "no Knowledge bypass" over the registry/policy objects | **D** | None beyond existing | Reuse | No | **S** |
| XP-MCP-001 Admitted read-only MCP | Stage C unit + integration (`test_mcp_streamable_http.py`) + **runtime** (admitted `mcp.e2e.lookup`, persisted Evidence, `mcp.call` nested) | Reusable local MCP server fixture; a gate binding admission -> invocation -> Evidence | **D** | **Factor** the inline server out of the integration test | Reuse | No | **M** |
| XP-MCP-002 Unadmitted dynamic tool | `is_registered` -> `UnknownToolError`; Stage C unit tests; runtime boundary case observed | Named gate `UNADMITTED_CAPABILITY_SELECTED == 0` | **D** | Server exposing an unadmitted tool | Reuse | No | **S** |
| XP-MCP-003 Write capability not selectable | `RiskClass` includes `WRITE`; `is_model_selectable` requires `READ_ONLY` — an enforced pure invariant | Gate `WRITE_CAPABILITY_SELECTED == 0`; explicitly assert no `Agent -> MCP write -> execute` path | **D** | Admission with `risk="WRITE"` | Reuse | No | **S** |
| XP-MCP-004 Schema drift | `external_schema_fingerprint` + `SCHEMA_MISMATCH` fail-closed; unit + runtime observed | Gate `SCHEMA_DRIFT_NOT_REJECTED == 0` | **D** | Drifted server | Reuse | No | **S** |
| XP-MCP-005 Result bounds | `ResultBounds` + `RESULT_TOO_LARGE` without truncation; runtime observed | Gate "raw oversized payload never entered model context" | **D** | Oversized tool | Reuse | No | **S** |
| XP-TEN-001 Knowledge tenant isolation | KB-GOLDEN-V1 `CROSS_TENANT_DENIED` + `cross_tenant_leakage_count`; integration `tenant-b` tests; **runtime** cross-tenant refusals | Gate `CROSS_TENANT_LEAK == 0` wired into the cross-plane pack | **D** | Reuse KB corpus as-is | Reuse knowledge artifact | No | **S** |
| XP-TEN-002 MCP tenant isolation | Tenant injected only via `ProviderInvocationContext`; spoofed model tenant rejected (Stage C) | Gate "model/provider/result cannot override tenant"; credential never model-visible | **D** | MCP server + tenant-B | Reuse | No | **S** |
| XP-SEC-001 Knowledge prompt injection | KB `PROMPT_INJECTION_POISON` + `PROMPT_INJECTION_MARKERS`; Stage A validation; unit tests | Gate "retrieved instructions stayed DATA" with a reason code | **D** | Reuse KB corpus as-is | Reuse | No | **S** |
| XP-SEC-002 MCP prompt injection | Runtime observed (injection tool content remained DATA); Stage C unit tests | Same gate for the MCP channel | **D** | Injection tool (server fixture) | Reuse | No | **S** |
| XP-SEC-003 Secret/sensitive scan | `_SECRET_MARKERS` + `secret_scan_pass` in `suite.py` and `quality.py`; `SECRET_SCAN_VIOLATION` reason code; **runtime** value-level secret-absence proof at the Collector sink | A unified scan across Evidence + XP artifacts + telemetry snapshot | **D** (+R for telemetry) | None | `secret-scan.json` | No | **S** |
| XP-AUTH-001 Verdict vs analyst disposition | Distinct domain types; workspace labels; Stage D browser contract tests; runtime observed | Gate reading both from durable state | **D** | Existing response fixtures | Reuse | No | **S** |
| XP-AUTH-002 Policy vs human approval | `PolicyDecision` vs `ApprovalDecisionKind`; `POLICY_DECISION` recorded separately; TOCTOU 409 proven at runtime | Gate `policy result never synthesizes approval` | **D** | `tests/fixtures/response_flow.py` | Reuse | No | **S** |
| XP-AUTH-003 Approval vs execution | `ResponseProposalStatus.APPROVED` != execution; rejected proposal produced zero execution commands at runtime | Gate `APPROVAL_EXECUTION_CONFLATED == 0` | **D** | Reuse | Reuse | No | **S** |
| XP-AUTH-004 Submission vs execution success | `ResponseSubmissionStatus` separate from `ResponseExecutionStatus`; runtime observed | Gate `SUBMISSION_SUCCESS_CONFLATED == 0` | **D** | Reuse | Reuse | No | **S** |
| XP-AUTH-005 HISIEM observed result is truth | `ResponseExecutionRef` from observation; runtime observed `SUCCEEDED` from HISIEM | Gate `EXECUTION_TRUTH_MISMATCH == 0` | **R** (needs real SOAR) | Real HISIEM SOAR + playbook | Reuse + integration report | No | **M** |
| XP-REL-001 Tool timeout | Typed `TIMEOUT`; `validate_search_span` policy proven at runtime | Gate "typed timeout, zero false-success Evidence" | **D** | Slow/stub tool | Reuse | No | **S** |
| XP-REL-002 MCP unavailable | Fail-closed, typed; **runtime** observed with zero Evidence. **DEFECT-005**: classified `PROVIDER_ERROR`, not `UNAVAILABLE` | Gate must assert fail-closed + zero Evidence, **not** a specific category (see 06 §14.2) | **D** | Stopped server | Reuse | No | **S** |
| XP-REL-003 Knowledge unavailable | Typed unavailable; must not masquerade as empty | Same as XP-KNOW-003 from the failure side | **D** | Unavailable retrieval stub | Reuse | No | **S** |
| XP-REL-004 Response submit uncertainty | `ATTENTION_REQUIRED` semantics; **runtime-proven** (10 attempts, `submitted_at=null`, `execution=null`, zero further retries over 180 s, browser rendered it) | Gate `submission_treated_as_success == 0` reusing the observed durable state | **D** (state) / **R** (real retry budget) | Failing SOAR endpoint | Reuse | No | **M** |
| XP-REL-005 Observability outage | **Runtime-proven** (Collector stopped; investigation still `MALICIOUS`, proposal still 200); unit coverage of best-effort metric recording | Gate `TELEMETRY_DEPENDENCY_VIOLATION == 0` over the same flow | **R** | Collector + a way to stop it | Reuse | No | **M** |
| XP-OBS-001 Representative trace | W3C context, durable link, span family implemented (see §26.12 OBS-001); **runtime** captured 2,536 spans with proven nesting | An automated span-presence/correlation assertion over a captured export | **R** | Test-scoped Collector config | `telemetry-evidence.json` | No | **M** |
| XP-OBS-002 Metrics cardinality/safety | `sanitize_metric_attributes` **rejects** any label outside `_ALLOWED_LABEL_VALUES`; the 9 forbidden Stage E labels are absent from the allowlist by construction; **runtime** value-level absence proven | A gate over a captured snapshot asserting both forbidden-label absence and raw prompt/result absence | **D** (unit) / **R** (snapshot) | Reuse | Reuse | No | **S** |
| XP-UX-001 Workspace authority semantics | Sealed Stage D; `copilot-authority.spec.js` (16 specs); runtime browser assertions (14/14) proven | Versioned XP-UX gate asserting labels are not colour-only and that authority classes are preserved | **D** (contract) / **R** (browser) | Playwright + live stack | `workspace-evidence.json` | No | **M** |
| XP-UX-002 Refresh/stale reconstruction | Refresh reconstruction + narrow viewport proven at runtime; `investigation-workspace.spec.js` | Gate "refreshed durable state wins over a stale snapshot" | **D** (contract) / **R** (browser) | Playwright | Reuse | No | **M** |

**Count: 27 scenarios** (06 §7 baseline is "approximately 20"; XP-MCP-005, XP-AUTH-005 and XP-REL-005 are additions the design already implies).

---

# 26.8 Hard Gate Measurability

| Gate | Measurement Source | Current Support | Gap | Deterministic? | Runtime Required? |
|---|---|---|---|---|---|
| `cross_tenant_leak == 0` | `EvidenceRepository.find_investigation_ids_by_evidence_ids`, `FindingRepository.find_investigation_ids_by_finding_ids`; KB `cross_tenant_leakage_count` | **DIRECTLY_MEASURABLE_NOW** | Wire result into an XP gate | Yes | No |
| `knowledge_only_definitive_verdict == 0` | Persisted `InvestigationResult.verdict.disposition` + Finding citations + Evidence source types (`nodes.py` guard) | **DIRECTLY_MEASURABLE_NOW** | Named gate + reason code | Yes | No |
| `unadmitted_mcp_selected == 0` | `ToolRegistry.is_registered` / `validate_candidate` -> `UnknownToolError` | **DIRECTLY_MEASURABLE_NOW** (pure) | Named gate | Yes | No |
| `write_mcp_selected == 0` | `AdmissionEntry.is_model_selectable` (`model_selectable and risk == "READ_ONLY"`), `RiskClass` incl. `WRITE` | **DIRECTLY_MEASURABLE_NOW** (pure) | Named gate; add explicit "no Agent->MCP-write->execute path" assertion | Yes | No |
| `secret_leak == 0` | `_SECRET_MARKERS` + `secret_scan_pass` (`suite.py`, `quality.py`); reason code `SECRET_SCAN_VIOLATION` already exists | **DIRECTLY_MEASURABLE_NOW** | Extend markers/scan scope to XP artifacts + telemetry snapshot | Yes | No |
| `dangling_citation == 0` | `quality.py` `dangling_citation_count`; `score.py` `FAIL_DANGLING_EVIDENCE_CITATION` | **DIRECTLY_MEASURABLE_NOW** | Reuse code; supply XP identity expectations | Yes | No |
| `cross_investigation_citation == 0` | `quality.py` `cross_investigation_citation_count`; `FAIL_CROSS_INVESTIGATION_EVIDENCE_CITATION` | **DIRECTLY_MEASURABLE_NOW** | Same | Yes | No |
| `execution_without_approval == 0` | `ResponseApprovalRepository` + `ResponseSubmissionRepository` + `ResponseExecutionRepository` read-only ports; `ResponseProposalStatus` transitions | **DIRECTLY_MEASURABLE_NOW** | Named gate | Yes | No |
| `submission_treated_as_success == 0` | `ResponseSubmissionStatus` vs `ResponseExecutionStatus` on the same proposal | **DIRECTLY_MEASURABLE_NOW** | Named gate | Yes | No |
| `telemetry_changed_business_state == 0` | Persisted business result compared across a telemetry-available/telemetry-down pair | **MEASURABLE_WITH_EVALUATION_EXTENSION** | Needs a paired-run scenario + Collector control | Partly | **Yes** (real Collector) |
| `metric label cardinality == 0` (XP-OBS-002) | `sanitize_metric_attributes` + `_ALLOWED_LABEL_VALUES` (forbidden labels absent by construction) | **DIRECTLY_MEASURABLE_NOW** (unit) + snapshot gate | Snapshot assertion | Yes | No |
| `no raw prompt / raw completion / full ToolResult in telemetry` | Stage B blanking + `capture_parameters=False`; `_USAGE_ALLOWLIST`; runtime value-level proof | **MEASURABLE_WITH_EVALUATION_EXTENSION** | Gate over a captured export | Yes | Yes (snapshot) |
| `oracle never entered production input` | `ORACLE_FIREWALL_VIOLATION` + `_oracle_firewall_pass` scanning production artifacts for role labels | **DIRECTLY_MEASURABLE_NOW** | Extend marker set for XP | Yes | No |
| `EXPECTED_FACT_MISSING` / `FORBIDDEN_FACT_PRESENT` | New XP-01 generic gates | **MISSING** | Implement in E1 | Yes | No |

**No hard gate requires a production change.** The two that require runtime (`telemetry_changed_business_state == 0`, raw-prompt absence in a live export) need a real Collector, which the repository already ships config for and the E2E run already exercised.

---

# 26.9 Artifact Reuse Plan

| Artifact | Decision | Existing Primitive | New Schema Needed? | Secret Risk | Notes |
|---|---|---|---|---|---|
| `execution.json` | **REUSE_EXISTING** | `execution_harness.record.EvaluationExecutionRecord` + `atomic_write_json` | No | Low (allowlisted) | GP-01 record is scenario-neutral enough; XP may add a thin XP-side record if fields diverge |
| `gate-results.json` | **NEW_ARTIFACT_REQUIRED** | `atomic_write_json`, `_bounded_str/_bounded_int`, allowlist-on-read idiom from `score.py` | **Yes** — `xp-gate-results/v1` | Low | `CrossPlaneScenarioResult` per 06 §10.1 |
| `telemetry-evidence.json` | **NEW_ARTIFACT_REQUIRED** | `_USAGE_ALLOWLIST` pattern in `telemetry.py`; `ModelTelemetry` allowlist-on-read | **Yes** | **Medium** — must allowlist span/metric keys and never store payloads | Bounded span-name/metric-name sets only |
| `workspace-evidence.json` | **NEW_ARTIFACT_REQUIRED** | Bounded-string idiom; `MAX_FIELD_LEN=200` convention | **Yes** | Low | Assertions against durable state, never UI-local state |
| `suite-summary.json` | **EXTEND_EXISTING** | `evaluation_harness.suite.SuiteSummary` (**schema-versioned, allowlisted, `secret_scan_pass`, `gate_failures`, unique path, never overwrites a prior suite**) | **Yes** — `xp-suite-summary/v1` (do **not** widen `SUITE_SCHEMA_VERSION`) | Low | Family results per 06 §21; PASS only if all family hard gates pass; no weighted average |
| `integration-report.md` | **REUSE_EXISTING** (pattern) | `report`/`report_suite` renderers in `harness.py`/`suite.py`; `FULL_RUNTIME_E2E_TEST_REPORT.md` as the human-report precedent | No | Low | Markdown, bounded, no secrets |
| `secret-scan.json` | **EXTEND_EXISTING** | `_SECRET_MARKERS` + `secret_scan_pass` already exist in two modules | No (reuse `SECRET_SCAN_VIOLATION`) | Low | Unify the two marker tuples; do not fork a third |
| `manifest.json` (XP pack) | **EXTEND_EXISTING** | `evaluation/manifest.py` + `sealer.py` atomic publication | **Yes** — `xp-pack-manifest/v1` | Low | Do not reuse `gp-eval-manifest/v1` |

Artifact invariants already satisfied by existing code and directly reusable: schema version + reject-on-mismatch, atomic write, field allowlist, bounded field size, hash/identity, secret scan, unique per-suite identity, immutable prior summaries. **No new artifact-safety mechanism is required.**

---

# 26.10 Test Reuse / New Coverage Map

Exact collection counts (2026-09-17): **1,772 tests** — `tests/unit` 1,262 · `tests/architecture` 263 · `tests/integration` 166 · `tests/e2e` 41 · `tests/live` 9.

### Reuse unchanged
- `tests/architecture/*` (263) — boundaries incl. the evaluation boundary and the pinned evaluation-importer set.
- `tests/unit/evaluation/**`, `tests/unit/evaluation_harness/**` — oracle firewall, score, suite, classification, tool-evidence quality.
- `tests/unit/knowledge/**`, `tests/unit/agent/**` (MCP provider, durable dispatcher, knowledge tool), `tests/unit/observability/**` (metrics).
- `tests/integration/persistence/test_knowledge_persistence.py` — real-PG KB-GOLDEN-V1 `run_suite` at L2788.
- `web/e2e/copilot-authority.spec.js`, `response-workflow.spec.js`, `investigation-workspace.spec.js`.

### Extend
- `tests/integration/test_mcp_streamable_http.py` — **factor the inline `MCPServer` + `_serve` into a reusable fixture** (currently one test function owns it).
- `tests/fixtures/response_flow.py` — reuse `OTHER_TENANT` for XP-TEN; add XP-only helpers rather than editing GP-01 fixtures.
- `tests/architecture/test_evaluation_boundary.py` — if XP-01 adds a subpackage under `evaluation/`, extend `_ALLOWED_FRAGMENTS`; if it adds a new top-level adapter package, extend `ALLOWED_EVALUATION_IMPORTERS` in `test_knowledge_boundary.py` **deliberately**.

### New deterministic tests (E1-E3)
- `CrossPlaneScenarioSpec` schema/version/identity; gate result schema; reason-code stability; forbidden-label rejection; fail-closed on unknown schema version.
- Pure gate functions: `cross_tenant_leak`, `knowledge_only_definitive_verdict`, `unadmitted_mcp_selected`, `write_mcp_selected`, `secret_leak`, `dangling_citation`, `cross_investigation_citation`, `execution_without_approval`, `submission_treated_as_success`.

### New integration tests
- XP-01 runner/scorer/suite over a disposable PostgreSQL, reading facts through the existing read-only ports.
- Local deterministic MCP server fixture driven through the real `MCPToolProvider`.

### New runtime acceptance (E4-E6)
- Representative trace export assertion; metrics-cardinality snapshot; sensitive-telemetry scan; Collector-outage paired run; durable-boundary correlation.

### Targeted browser acceptance (E5)
- XP-UX-001/002 only. Per 06 §13.4, do **not** turn every scenario into Playwright.

**Do not duplicate:** the sealed A/B/C/D unit and integration suites already restate their own layer's behaviour. Stage E adds cross-plane coverage, not a second copy of Knowledge/MCP/observability unit tests.

**Runtime evidence exists but is NOT an evaluation gate** (E0 §7 requirement). The `FULL_RUNTIME_E2E_TEST_REPORT.md` proves the 25 runtime scenarios by hand-driven execution with an ad-hoc harness that was then deleted. It supplies *facts* (e.g. the exact `ATTENTION_REQUIRED` payload shape, the Collector-outage outcome, the injection-as-DATA behaviour) but no **versioned, re-runnable, machine-scored** gate. That distinction is the entire reason XP-01 exists.

---

# 26.11 Production Change Budget

```text
Expected production changes:
    NONE.

Evaluation-only changes:
    src/hisiem_soc_copilot/evaluation/cross_plane/          (new package: contracts, catalog, gates, artifacts)
    src/hisiem_soc_copilot/evaluation_harness/cross_plane_*.py  (new: runner, score, suite)
    src/hisiem_soc_copilot/evaluation/cli.py                (extend the existing dispatcher)
    tests/unit/evaluation/cross_plane/**                    (new)
    tests/integration/evaluation_harness/**                 (extend)
    tests/architecture/test_evaluation_boundary.py          (extend allowlist only if a new subpackage appears)
    tests/architecture/test_knowledge_boundary.py           (only if a NEW top-level adapter package is added)

HISIEM expected changes:
    NONE.
    Reason: every Stage E gate is measurable from existing formal boundaries —
    BFF agent-investigation endpoints, internal SOAR API, tenant filter, and the
    Copilot's own read-only repository ports and OTel export. No evaluation-only
    endpoint, debug bypass, or test-only authorization path is required, and
    06 §17 forbids them.
```

This is the default preference and the evidence supports it. A production change would be justified only if a real correctness/security/integration defect is found during E1-E7 (06 §17), with the same five-part evidence discipline used for DEFECT-004/006.

---

# 26.12 Contradictions / Blockers

**No architecture contradiction found.** No authority/truth-source conflict blocks E1.

Two non-blocking observations are recorded so E1 does not trip over them. Neither is an architecture contradiction and neither requires E1 to stop.

### OBS-001 — `00` §5.3 span taxonomy is partially unimplemented (NON-BLOCKING, scope clarification)

- Frozen contract: `00` §5.3 declares `queue.wait`, `alert.hydrate`, `native.call`, and a knowledge sub-tree (`embedding`, `postgres.fts`, `pgvector.search`, `retrieval.merge`).
- Actual behaviour (AST + call-site evidence):

| Span | Implemented | Emission site |
|---|---|---|
| `investigation.run` | Yes | `infrastructure/durable/investigation_runner.py:92` |
| `graph.invoke` | Yes | `investigation_runner.py:142` |
| `llm.call` | Yes | `infrastructure/llm/openai_compatible.py:319` |
| `tool.execute` | Yes | `infrastructure/observability/tools.py:69` |
| `knowledge.retrieve` | Yes (only for `knowledge.retrieve_security_guidance`) | `tools.py:83` |
| `mcp.call` | Yes | `tools.py:157` |
| `investigation.persist` | Yes | `investigation_runner.py:113,121` |
| `response.submit` | Yes | `infrastructure/durable/response_runner.py:92` |
| `response.observe` | Yes | `response_runner.py:408` |
| `durable.dispatch` | Yes | `infrastructure/durable/dispatcher.py:175` |
| `native.call` | **No** | — (06 §12.1 qualifies it "when implemented/available") |
| `queue.wait`, `alert.hydrate` | **No** | — |
| `embedding`, `postgres.fts`, `pgvector.search`, `retrieval.merge` | **No** | — |

- Impact: **none on `05`/`06` acceptance.** `06` §12.1 — the operative Stage E observability authority — lists only the implemented family (plus `native.call` with an explicit availability qualifier). A Stage E observability gate written against `06` §12.1 is satisfiable today; one written against `00` §5.3 would fail on four span families that were never instrumented.
- Smallest correction: none required now. E1 must scope the XP-OBS-001 assertion to the implemented family and record the `00` §5.3 remainder as unimplemented taxonomy. If Stage E later needs those spans, they become an E4 instrumentation item, not an XP-01 gate item.
- Affected planes: Observability (only).
- E1 must stop until resolved: **No.**

### EVAL-001 — the evaluation-importer allowlist is pinned by exact equality (NON-BLOCKING, E1 constraint)

- `tests/architecture/test_knowledge_boundary.py:488` asserts `set(_evaluation_importers()) == {"evaluation_harness", "knowledge"}`.
- Impact: a new top-level adapter package that imports `evaluation` fails the 263-test architecture suite. This is intentional and correct ("Nothing else may join this list without editing this test").
- Smallest correction: E1 places the XP-01 adapter inside `evaluation_harness` (preferred — it is the sanctioned bridge) or inside `knowledge` (the KB-GOLDEN-V1 precedent), and only extends the allowlist if a genuinely separate package is justified.
- Affected planes: Evaluation (only).
- E1 must stop until resolved: **No** — it is a placement decision, documented here so E1 gets it right the first time.

### Non-blocking known deviation carried forward

`DEFECT-005` — an unreachable MCP server is classified `PROVIDER_ERROR` rather than `UNAVAILABLE`. Verified in E0: **OPEN / LOW / NON-BLOCKING**. The Copilot tree is byte-identical to HEAD (`git diff HEAD` empty; per-file `git hash-object` matches), so **no message-string heuristic exists anywhere**; the failure is typed, fails closed, and yields zero Evidence. `06` §14.2 explicitly endorses this stance. XP-REL-002 must therefore gate on fail-closed + zero fabricated Evidence, not on a specific category string.

---

# 26.13 Recommended E1 Implementation Boundary

### E1 scope
1. Freeze the XP-01 contract layer: `CrossPlaneScenarioSpec`, `CrossPlaneScenarioResult`, `CrossPlaneSuiteSummary`, gate-family enum, execution-profile enum, stable gate IDs and reason codes, schema-version strings.
2. Freeze the gate model: hard gate = `PASS|FAIL` only; informational metrics never repair a hard-gate failure; family PASS only if all its required hard gates pass; no weighted average.
3. Implement the deterministic gate primitives as **pure functions** over bounded fact types (no IO), reusing the existing reason codes where already defined.
4. Wire the CLI entry points into the **existing** `evaluation/cli.py` dispatcher.
5. Unit-test the contract, the schema rejection path, and every pure gate.

### E1 non-scope
- No XP scenario execution against runtime (that is E2-E5).
- No new evaluation framework, database, artifact store, or CLI.
- No production source, test, GP-01 manifest, sealer, scorer, or harness modification.
- No edit to `ScenarioSpec`, `EvaluationProfile`, `SUITE_SCHEMA_VERSION`, `SCORE_SCHEMA_VERSION`, `QUALITY_SCHEMA_VERSION`, or the sealed oracle.
- No HISIEM change.

### Exact reusable modules (import, do not fork)
```text
evaluation/launch_projection.py        launch_ref
evaluation/contracts.py                canonical_json, sha256_hex  (idioms only)
evaluation_harness/record.py           atomic_write_json, EvaluationExecutionRecord
evaluation_harness/classification.py   AttemptClassification, classify_attempt, CLASS_*
evaluation_harness/quality.py          EvidenceFact, FindingFact, ToolInvocationFact,
                                       gate reason codes
evaluation_harness/score.py            stable FAIL_* reason codes, _bounded_* helpers,
                                       allowlist-on-read idiom
evaluation_harness/suite.py            _SECRET_MARKERS, suite_summary_path, validate_bounds,
                                       GATE_PASS / GATE_FAIL
infrastructure/observability/metrics.py  sanitize_metric_attributes, _ALLOWED_LABEL_VALUES
agent/tools/registry.py, policy.py, providers.py   governance invariants under test
application/ports/repositories.py      read-only fact projection
```

### Exact frozen files / contracts (do not modify)
```text
evaluation/contracts.py, oracle.py, manifest.py, sealer.py, materializer.py,
  hisiem_reader.py, injector.py, syslog.py, time_plan.py, identity.py,
  verifier.py, ledger.py, launch_projection.py
evaluation_harness/harness.py, score.py, score_harness.py, quality.py,
  quality_harness.py, suite.py, telemetry.py, record.py, classification.py, runtime.py
evaluation/knowledge/**            (KB-GOLDEN-V1 sealed family)
knowledge/evaluation.py, knowledge/cli.py
.eval-runs/**, .eval-executions/** (existing sealed/live artifacts)
```

### Expected new modules/files
```text
src/hisiem_soc_copilot/evaluation/cross_plane/__init__.py
src/hisiem_soc_copilot/evaluation/cross_plane/contracts.py
src/hisiem_soc_copilot/evaluation/cross_plane/catalog.py
src/hisiem_soc_copilot/evaluation/cross_plane/gates.py
src/hisiem_soc_copilot/evaluation/cross_plane/artifacts.py
tests/unit/evaluation/cross_plane/test_contracts.py
tests/unit/evaluation/cross_plane/test_gates.py
tests/unit/evaluation/cross_plane/test_artifacts.py
```
(If, and only if, a runtime adapter is needed in E1 — it is not — it belongs in `evaluation_harness/`, never in a new top-level package.)

### Expected modified modules/files
```text
src/hisiem_soc_copilot/evaluation/cli.py                 add the XP dispatch branch only
tests/architecture/test_evaluation_boundary.py           extend _ALLOWED_FRAGMENTS for cross_plane
                                                         (needed because cross_plane is under evaluation/)
```

### Required focused tests
```text
pytest tests/unit/evaluation/cross_plane -q
pytest tests/architecture/test_evaluation_boundary.py tests/architecture/test_knowledge_boundary.py -q
pytest tests/unit/evaluation_harness -q          (GP-01 regression: unchanged)
ruff check . && mypy src
```

### E1 acceptance criteria
1. `CrossPlaneScenarioSpec`, `CrossPlaneScenarioResult`, `CrossPlaneSuiteSummary` exist, are frozen dataclasses, are schema-versioned, and reject an unknown schema version on read.
2. Every hard gate is a pure function returning `PASS|FAIL` with stable reason codes; no weighted score exists anywhere.
3. GP-01 artifacts and code are provably unchanged (`git diff` on the frozen file list is empty).
4. `mypy src` and `ruff check .` clean; the 263 architecture tests pass unmodified except for the one documented `_ALLOWED_FRAGMENTS` extension.
5. No production layer imports `evaluation` or `evaluation_harness` (re-asserted by the architecture suite).
6. No oracle/expected-fact data can reach model input, prompts, tool arguments, ToolResult, Evidence, production state, HISIEM requests, or workspace payloads — enforced by an oracle-firewall test, not by convention.
7. The XP CLI entry point is reachable through the existing `evaluation/cli.py` dispatcher and creates no artifact during schema-failure paths.

---

# Appendix A — Non-secret environment inventory (E0 §24 / 06 §20)

| Component | Repo / branch / SHA | Local path | Host / port | Health endpoint | Validation status |
|---|---|---|---|---|---|
| HISIEM PostgreSQL | SIEM @ `add_frame` `d0baed9` | docker `siem-postgres` | 5432 | `pg_isready` | PASS (runtime E2E) |
| Elasticsearch | SIEM @ `add_frame` `d0baed9` | docker `siem-elasticsearch` | 9200 | `_cluster/health` | PASS |
| Kafka | SIEM | docker `siem-kafka` | 9092 | topic list | PASS |
| Logstash | SIEM | docker `siem-logstash` | 5000-5007 / 9600 | TCP ingest | PASS |
| Flink | SIEM | docker `siem-flink-jobmanager` / `taskmanager` | 8081 | `flink list` | PASS |
| HISIEM control-api | SIEM @ `add_frame` `d0baed9` | host JVM | 8080 | `/actuator/health` | PASS |
| SOAR worker | SIEM @ `add_frame` `d0baed9` | host JVM | — | Kafka consumer group | PASS |
| HISIEM Vue | SIEM | host Vite | 5173 | HTTP 200 | PASS |
| Copilot PostgreSQL | Copilot @ `capability-mcp` `d4cfc8e` | docker `copilot-postgres` (pgvector) | 5433 | `pg_isready` | PASS (Alembic migrated) |
| Copilot FastAPI | Copilot @ `capability-mcp` `d4cfc8e` | host uvicorn | 8000 | `/healthz` | PASS |
| Deterministic model | — | `ScriptedModelProvider` (`infrastructure/llm/scripted.py`) | in-process | — | PASS |
| Local MCP server | — | in-process fixture (`tests/integration/test_mcp_streamable_http.py`) | ephemeral | — | PASS |
| OTel Collector | Copilot @ `capability-mcp` `d4cfc8e` | `infra/otel-collector/collector.yaml` | 4317 / 4318 | Collector startup log | PASS (runtime E2E) |
| Browser | HISIEM Vue | Playwright + Chrome | — | — | PASS |

Service auth mode: Browser -> HISIEM session auth -> BFF -> Copilot service bearer; Copilot -> HISIEM internal SOAR service token. Actual credentials: **REDACTED / never printed**. Secrets are environment-sourced with fail-closed empty defaults; `.env.local` is gitignored and was not read.

---

# Appendix B — E0 method and limits

- Git state verified in both repositories before inspection (`branch --show-current`, `rev-parse HEAD`, `status --short`, `rev-parse origin/<branch>`). Both clean; no divergence from the E0-prompt expectation.
- All three authorities read in full (00: 845 lines; 05: 182; 06: 1776) plus the E0 prompt (1536).
- Findings are derived from AST scans, call-site grep, live module introspection (corpus/case counts, schema strings), and the two-run runtime report — not from filenames or documentation alone.
- Import-direction claims come from a full AST scan of every `.py` file under `src/hisiem_soc_copilot/`, resolving relative imports against their package.
- The runtime topology was **not** started in E0; nothing in this audit required it.
- Test counts are exact `pytest --collect-only` results, not estimates.
- One inaccuracy in the E0 prompt's own framing was corrected while auditing: the knowledge-only verdict guard lives entirely in `agent/graph/nodes.py` (`_findings_with_platform_evidence` L1095, applied in `finalize_result` L1193) — there is no `agent/graph/workflow.py` in this repository.
