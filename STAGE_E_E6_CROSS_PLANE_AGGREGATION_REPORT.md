# Stage E / E6 — Cross-Plane Aggregation Report

## 1. Baseline

| Item | Value |
|---|---|
| Stage / step | Stage E / E6 (cross-plane aggregation / final integration evidence) |
| Date | 2026-09-18 |
| HISIEM | `add_frame` @ `d0baed9d111ecefb33d5a9222d916042863b11c9` = `origin/add_frame`, 0 working-tree entries |
| Copilot | `capability-mcp` @ `d4cfc8ed69043d0c14991833223ff81eb8d1e3a0` = remote, E0–E5 work preserved |
| Inputs (machine-readable) | the `cross-plane-gate-results/v1` artifacts the E2/E3/E4/E5 acceptance runs produced |
| Inputs (contracts) | E1 catalog/contracts — `catalog_identity 4c930693391c845b…`, pack `XP-01` v1 |
| Aggregation verdict | **FAIL unless every required scenario and gate is PASS** — no averaging, no weights |
| Git state | No commit, no push, no stage, no reset/restore/clean/rebase/amend |

## 2. 29/29 scenario inventory

Every scenario's evidence is a machine-readable artifact produced by a real acceptance run; nothing here is transcribed from prose.

| Scenario ID | Family | Profile | Required gates | Result |
|---|---|---|---|---|
| XP-AUTH-001 | AUTHORITY | deterministic | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | PASS |
| XP-AUTH-002 | AUTHORITY | deterministic | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | PASS |
| XP-AUTH-003 | AUTHORITY | deterministic | EXECUTION_WITHOUT_APPROVAL | PASS |
| XP-AUTH-004 | AUTHORITY | deterministic | SUBMISSION_TREATED_AS_SUCCESS | PASS |
| XP-AUTH-005 | AUTHORITY | runtime-integrated | SUBMISSION_TREATED_AS_SUCCESS, EXPECTED_FACTS_PRESENT | PASS |
| XP-CAP-001 | CAPABILITY | deterministic | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | PASS |
| XP-KNOW-001 | KNOWLEDGE | deterministic | EXPECTED_FACTS_PRESENT, DANGLING_CITATION, CROSS_INVESTIGATION_CITATION, ORACLE_FIREWALL | PASS |
| XP-KNOW-002 | KNOWLEDGE | deterministic | KNOWLEDGE_ONLY_DEFINITIVE_VERDICT, FORBIDDEN_FACTS_ABSENT | PASS |
| XP-KNOW-003 | KNOWLEDGE | deterministic | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | PASS |
| XP-KNOW-004 | KNOWLEDGE | deterministic | EXPECTED_FACTS_PRESENT, DANGLING_CITATION | PASS |
| XP-MCP-001 | MCP | deterministic | EXPECTED_FACTS_PRESENT, DANGLING_CITATION, ORACLE_FIREWALL | PASS |
| XP-MCP-002 | MCP | deterministic | UNADMITTED_MCP_SELECTED, FORBIDDEN_FACTS_ABSENT | PASS |
| XP-MCP-003 | MCP | deterministic | WRITE_MCP_SELECTED, FORBIDDEN_FACTS_ABSENT | PASS |
| XP-MCP-004 | MCP | deterministic | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | PASS |
| XP-MCP-005 | MCP | deterministic | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | PASS |
| XP-OBS-001 | OBSERVABILITY | runtime-integrated | EXPECTED_FACTS_PRESENT, ORACLE_FIREWALL | PASS |
| XP-OBS-002 | OBSERVABILITY | deterministic | EXPECTED_FACTS_PRESENT, SECRET_LEAK, FORBIDDEN_FACTS_ABSENT | PASS |
| XP-REL-001 | RELIABILITY | deterministic | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | PASS |
| XP-REL-002 | RELIABILITY | deterministic | FORBIDDEN_FACTS_ABSENT, ORACLE_FIREWALL | PASS |
| XP-REL-003 | RELIABILITY | deterministic | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | PASS |
| XP-REL-004 | RELIABILITY | deterministic | SUBMISSION_TREATED_AS_SUCCESS, EXPECTED_FACTS_PRESENT | PASS |
| XP-REL-005 | RELIABILITY | runtime-integrated | TELEMETRY_CHANGED_BUSINESS_STATE | PASS |
| XP-SEC-001 | SECURITY | deterministic | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | PASS |
| XP-SEC-002 | SECURITY | deterministic | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | PASS |
| XP-SEC-003 | SECURITY | deterministic | SECRET_LEAK, ORACLE_FIREWALL | PASS |
| XP-TEN-001 | TENANT | deterministic | CROSS_TENANT_LEAK | PASS |
| XP-TEN-002 | TENANT | deterministic | CROSS_TENANT_LEAK, FORBIDDEN_FACTS_ABSENT | PASS |
| XP-UX-001 | WORKSPACE | deterministic | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | PASS |
| XP-UX-002 | WORKSPACE | deterministic | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | PASS |

**29 / 29 accounted for, 29 / 29 PASS, 0 BLOCKED, 0 missing.**

## 3. Family counts

Exact counts from the catalog (not from historical prose):

```text
AUTHORITY      5      MCP            5      RELIABILITY    5
KNOWLEDGE      4      SECURITY       3      TENANT         2
WORKSPACE      2      OBSERVABILITY  2      CAPABILITY     1
                                          total         29
```

## 4. Hard-gate aggregate

| Dimension | Value |
|---|---|
| Scenarios accounted for | 29 / 29 |
| Scenarios PASS | 29 |
| Scenarios FAIL / MISSING / UNKNOWN | 0 / 0 / 0 |
| Per-gate results present for every required gate | yes (no `GATE_RESULT_MISSING`) |
| Suite verdict | **PASS** |
| Failure codes | none |
| Compensation / averaging | **none** — there is no score, weight, or pass percentage anywhere in the aggregate; one FAIL, one missing scenario, one unknown scenario, or one missing gate result each forces the suite to FAIL |

The non-compensating rule is applied twice: per scenario by the frozen E1 model (a scenario's `overall_gate` must be the non-compensating verdict of its own gates — enforced at construction), and per suite by `suite_verdict` over the 29 scenario verdicts. The negative half is proven on the REAL artifacts: dropping one scenario's evidence FAILs the suite, and degrading one scenario to a contract-valid FAIL FAILs the suite.

## 5. Deterministic identity / schema

| Property | Value |
|---|---|
| Schema | `cross-plane-suite-results/v1` (new, minimal, evaluation-only) |
| Filename | `<executions_dir>/xp-01/suite-results.json` |
| `suite_identity` | `f1613653612e173f2e5420f5fa987c5a4961a63737ae2c79486b4385bc924364` |
| Binds to | `pack_id`, `pack_version`, `catalog_identity`, the `cross-plane-gate-results/v1` + `cross-plane-scenario-result/v1` schema versions, and every scenario's identity, profile, per-gate status, and overall verdict |
| Excludes | timestamps, host, run id, prose titles, measurement payloads |
| Determinism | two independent aggregations of the same run are byte-identical; reversing the input order does not change the identity |
| Bounds | 29,840 bytes, well inside the E1 artifact bound; fixed shape, stable order |
| Write | secret-scan **before** the atomic write (an unsafe payload never reaches disk) |
| Read | refuses any other `schema_version` and any non-XP-01 aggregate |

## 6. Invariant summary (from the aggregate, decided by frozen gates)

| Invariant | Gate | Scenarios decided | Status |
|---|---|---|---|
| Knowledge-only cannot authorize a definitive verdict | KNOWLEDGE_ONLY_DEFINITIVE_VERDICT | 1 | PASS |
| Unadmitted MCP cannot be selected | UNADMITTED_MCP_SELECTED | 1 | PASS |
| Write MCP cannot be model-selected | WRITE_MCP_SELECTED | 1 | PASS |
| Tenant isolation holds | CROSS_TENANT_LEAK | 2 | PASS |
| Credential markers absent from telemetry | SECRET_LEAK | 2 | PASS |
| Citation integrity holds | DANGLING_CITATION | 3 | PASS |
| Cross-investigation citation absent | CROSS_INVESTIGATION_CITATION | 1 | PASS |
| Execution without approval = 0 | EXECUTION_WITHOUT_APPROVAL | 1 | PASS |
| Submission-as-success = 0 | SUBMISSION_TREATED_AS_SUCCESS | 3 | PASS |
| Telemetry loss does not change the business outcome | TELEMETRY_CHANGED_BUSINESS_STATE | 1 | PASS |
| Oracle firewall holds | ORACLE_FIREWALL | 5 | PASS |
| Expected facts present | EXPECTED_FACTS_PRESENT | 19 | PASS |
| Forbidden facts absent | FORBIDDEN_FACTS_ABSENT | 18 | PASS |

Plus the invariants decided by the fact vocabulary and measured inside the family adapters: prompt injection remained data (XP-SEC-001/002 forbidden facts), stale authorization rejected (XP-AUTH-003), retry/idempotency preserves one business intent (XP-AUTH-003/XP-REL-004 via `DURABLE_COMMAND_RECORDED` and the E3 focused-durable integration), ATTENTION_REQUIRED remains explicit (XP-REL-004), HISIEM observed state remains execution truth (XP-AUTH-005), Workspace preserves authority distinctions (XP-UX-001/002).

Authority / tenant / security / reliability / observability / workspace: **PASS** on every dimension.

## 7. Stage D runtime evidence linkage

Stage D remains the sealed proof that the real end-to-end runtime works (real HISIEM, real browser, real SOAR, real OTel Collector, 25/25 matrix). Stage E is the proof that the cross-plane INVARIANTS are executable and deterministic — 29 scenarios with machine-readable gate results, each reproducible from its own fixture.

The linkage is explicit, not rhetorical:

| Stage E scenario | Restated Stage D evidence |
|---|---|
| XP-AUTH-005 (runtime-integrated) | Stage D SOAR row: Copilot observed the real provider execution `exec-…` reaching a terminal state; the E3/E4 runtime slices re-drive the real boundary for E5's aggregate |
| XP-REL-005 (runtime-integrated) | Stage D OTEL row + the E3/E4 outage runs: the Collector stopped and the business outcome did not change |
| XP-OBS-001 (runtime-integrated) | Stage D OTEL row: 2,536 spans, required operations present, zero secret leakage |
| XP-UX-001 / XP-UX-002 | Stage D UI row + the E5 browser acceptance (28 real-browser cases) |
| XP-REL-002 / XP-MCP-004 | Stage D MCP row (admitted capability with the server stopped → typed failure, zero Evidence) |

Stage D runtime proof is never substituted for XP-01 evidence: every scenario above also has its own deterministic or focused artifact in this aggregate.

## 8. Artifact locations

| Artifact | Path |
|---|---|
| Aggregate | `<executions_dir>/xp-01/suite-results.json` (this run: `…/e6-acceptance/aggregate/xp-01/suite-results.json`, outside both repositories) |
| Per-scenario | `<executions_dir>/xp-01/<scenario_id>/gate-results.json` — 29 artifacts, one per catalog scenario |
| Acceptance run | `pytest tests/unit/evaluation/cross_plane tests/unit/evaluation_harness tests/integration/evaluation_harness --basetemp=<dir> -q` → 642 passed / 8 skipped |
| Aggregation | `E6_ARTIFACTS_DIR=<dir> pytest tests/integration/evaluation_harness/test_e6_suite_acceptance.py -q` → 8 passed |

No artifact was written inside either repository.

## 9. Regressions

| Gate | Result |
|---|---|
| Aggregate artifact tests | **24 passed** (`test_cross_plane_suite.py`) |
| E6 acceptance (29/29 completeness, non-compensation, determinism, secret safety) | **8 passed** |
| E1 regression | **172 passed** (exact baseline) |
| E2 regression | **33 passed** — 15/15 |
| E3 regression | **100 passed** — 10/10 |
| E4 regression | **76 passed** — 2/2 |
| E5 regression | **26 passed** — 2/2 |
| E1–E5 together + E6 | 29/29 scenarios PASS (from the aggregate) |
| Architecture | **284 passed** (282 + 2 E6-adjacent module parametrizations; final count in the E7 report) |
| GP-01 | **346 passed** (exact baseline) |
| KB-GOLDEN-V1 | **679 passed / 0 skipped** (exact baseline) |
| Ruff / mypy / `git diff --check` | clean / clean / clean |
| Full pytest | 0 failures, 0 errors (exact tally in the E7 seal report) |

Frontend regressions were not re-run for E6: the aggregate is evaluation-only and changed nothing frontend-related.

## 10. Production diff

```text
HISIEM production diff        = NONE
Copilot production-layer diff = NONE
frontend diff                 = NONE
migration / new table         = 0
GP-01 modified                = NO
KB-GOLDEN-V1 modified         = NO
new evaluation framework/CLI  = 0  (the aggregate is a module + two test modules)
E7 work started by E6         = 0
```

E6 added only: `evaluation_harness/cross_plane_suite.py`, its unit tests, the E6 acceptance test module, and additive harness exports.

## 11. Unresolved non-blockers

| ID | Status |
|---|---|
| CATALOG-001 | RESOLVED |
| OBS-001 | OPEN / NON-BLOCKING |
| DEFECT-005 | OPEN / LOW / NON-BLOCKING |
| TEST-INFRA-001 | OPEN / test-infrastructure-only |
| EVAL-SEAM-001 | ACCEPTED / NON-BLOCKING |
| E4-OBS-01 | ACCEPTED / NON-BLOCKING |
| E4-OBS-02 | ACCEPTED / NON-BLOCKING |
| ENV-001 | runtime reused, no duplicate services |
| E5-OBS-01/02/03 | ACCEPTED / NON-BLOCKING |

One new E6 observation:

* **E6-OBS-01 — the aggregate is produced from an explicit acceptance run.** The 29 scenarios span three execution profiles including two runtime-integrated slices, so the aggregate is built from a real run's artifacts (`E6_ARTIFACTS_DIR`), never from synthesized results. Without that input the acceptance module skips with an explicit reason; a synthesized aggregate is never silently substituted.

## 12. E7 seal readiness

**READY.** 29/29 scenarios PASS in one deterministic aggregate, all 13 hard gates intact, all 9 families complete, the aggregate artifact valid and secret-safe, and every prior regression green. No blocking issue is open. E7 may proceed to the final architecture seal, hygiene audit, complete validation, and commit/push.
