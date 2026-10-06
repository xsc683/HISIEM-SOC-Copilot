# Stage E / E3 — Authority + Reliability Implementation Report

## 1. Header / authorities / baseline

| Item | Value |
|---|---|
| Stage / step | Stage E / E3 (AUTHORITY + RELIABILITY) |
| Date | 2026-09-17 |
| HISIEM baseline | branch `add_frame` @ `d0baed9d111ecefb33d5a9222d916042863b11c9` — **identical to `origin/add_frame`**, 0 working-tree entries |
| Copilot baseline | branch `capability-mcp` @ `d4cfc8ed69043d0c14991833223ff81eb8d1e3a0` — **identical to its remote**, E0/E1/E2 work preserved |
| Authorities read in full | `Stage-E_E3_Authority-Reliability_VibeCoding_Prompt.md`; `00_Four-Plane_Architecture_Contract_Freeze.md`; `06_Stage-E_XP-01_Cross-Plane_Evaluation_Pack.md` |
| Frozen upstream artifacts | E1 XP-01 contract package (`evaluation/cross_plane/**`) — **unchanged**; E2 adapters/drivers — **unchanged except the two E3-owned stubs noted in §16**; GP-01 — unchanged; KB-GOLDEN-V1 — unchanged |
| Execution architecture | `persisted/runtime production facts → E3 measurement adapter → E1 typed measurement → E1 deterministic hard gate → cross-plane-gate-results/v1` |
| Gate/decision ownership | `evaluation.cross_plane.evaluate_scenario` remains the ONLY decision point. No `AuthorityScore`, no `ReliabilityScore`, no weighted percentage, no second state machine |
| Git state | No commit, no push, no stage, no reset/restore/clean/rebase/amend. Both repositories are exactly as found plus the E3 artifacts listed in §4 |
| Secrets | No `.env` content, bearer token, API key, DB password, session token or `Authorization` header was printed at any point. The runtime slice read its credential from the gitignored dev env and passed it to a child process without echoing it |

Verified XP-01 catalog identity (unchanged): **29 scenarios / 13 hard gates / 9 gate families / 37 expected fact tokens / 21 forbidden fact tokens**, pack `XP-01` v1, `catalog_identity = 4c930693391c845b…`.

## 2. Final E3 status

```text
E3 IMPLEMENTATION: PASS
E4 READINESS: READY
```

Every AUTHORITY and RELIABILITY scenario has an executable XP-01 path and PASSes; no correctness, security, authority or reliability defect remains unresolved; no scenario is BLOCKED; no temporary artifact remains in either repository; both project states are documented.

## 3. Exact E3 scenario inventory

Derived programmatically from the implemented E1 catalog (`scenarios_for_family(GateFamily.AUTHORITY | GateFamily.RELIABILITY)`), never hard-coded:

```text
E3 total: 10
AUTHORITY:   5  XP-AUTH-001 … XP-AUTH-005
RELIABILITY: 5  XP-REL-001  … XP-REL-005
```

Not E3-owned and deliberately untouched: `XP-OBS-001/002` (OBSERVABILITY, E4), `XP-UX-001/002` (WORKSPACE, E5).

| Scenario ID | Family | Profile | Measurement Adapter | Gate IDs | Artifact | Result |
|---|---|---|---|---|---|---|
| XP-AUTH-001 | AUTHORITY | deterministic | `authority_role_separation` (real workspace projection: `_result`, `_workspace_approval`) | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | `test_every_deterministic_e3_sc0/xp-01/XP-AUTH-001/gate-results.json` | **PASS** |
| XP-AUTH-002 | AUTHORITY | deterministic | `policy_and_approval_facts` (real `ResponseCommandHandler` + real policy) | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | `…/XP-AUTH-002/gate-results.json` | **PASS** |
| XP-AUTH-003 | AUTHORITY | deterministic | `authorized_execution_authority`, `authority_command_facts`, `logical_command_identities` | EXECUTION_WITHOUT_APPROVAL | `…/XP-AUTH-003/gate-results.json` | **PASS** |
| XP-AUTH-004 | AUTHORITY | deterministic | `submission_truth` (real `_workspace_submission` / `_workspace_execution`) | SUBMISSION_TREATED_AS_SUCCESS | `…/XP-AUTH-004/gate-results.json` | **PASS** |
| XP-AUTH-005 | AUTHORITY | runtime-integrated | `submission_truth` + `observed_execution_truth` (real HISIEM observed state) | SUBMISSION_TREATED_AS_SUCCESS, EXPECTED_FACTS_PRESENT | `test_xp_auth_005_real_hisiem_e0/xp-01/XP-AUTH-005/gate-results.json` | **PASS** |
| XP-REL-001 | RELIABILITY | deterministic | `tool_result_from_failure` + `failure_facts` (real `MCPToolProvider` timeout) | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | `…/XP-REL-001/gate-results.json` | **PASS** |
| XP-REL-002 | RELIABILITY | deterministic | `failure_facts` + `oracle_firewall_of` (real provider against a stopped server) | FORBIDDEN_FACTS_ABSENT, ORACLE_FIREWALL | `…/XP-REL-002/gate-results.json` | **PASS** |
| XP-REL-003 | RELIABILITY | deterministic | `retrieval_failure_facts` (real `ToolExecutor` knowledge path, real `KnowledgeRetrievalService`) | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | `…/XP-REL-003/gate-results.json` | **PASS** |
| XP-REL-004 | RELIABILITY | deterministic | `submission_presentation_facts` + `submission_truth` (real exhaustion handler + real timeline projection) | SUBMISSION_TREATED_AS_SUCCESS, EXPECTED_FACTS_PRESENT | `…/XP-REL-004/gate-results.json` | **PASS** |
| XP-REL-005 | RELIABILITY | runtime-integrated | `telemetry_isolation` / `telemetry_isolation_facts` (real OTel Collector, two real worker processes) | TELEMETRY_CHANGED_BUSINESS_STATE | `test_xp_rel_005_a_telemetry_ou0/xp-01/XP-REL-005/gate-results.json` | **PASS** |

**10 / 10 PASS.** No scenario is BLOCKED, skipped, or left unimplemented.

## 4. Files changed

New Copilot files (all uncommitted, none staged):

| Path | Kind | Lines |
|---|---|---|
| `src/hisiem_soc_copilot/evaluation_harness/cross_plane_authority.py` | E3 measurement adapters (authority + reliability) | 884 |
| `src/hisiem_soc_copilot/evaluation_harness/cross_plane_e3_scenarios.py` | E3 drivers, runner, inventory | 608 |
| `tests/support/e3_authority_fixture.py` | real-authority fixture builders | 369 |
| `tests/support/e3_runtime_telemetry_driver.py` | XP-REL-005 runtime driver (one process, one business outcome) | 289 |
| `tests/unit/evaluation_harness/test_cross_plane_authority.py` | Layer 1 adapter tests (49) | 804 |
| `tests/integration/evaluation_harness/test_e3_scenarios.py` | Layer 2 + falsifiability (35) | 842 |
| `tests/integration/evaluation_harness/test_e3_focused_durable.py` | Layer 3 focused durable integration (12) | 833 |
| `tests/integration/evaluation_harness/test_e3_runtime_slice.py` | Layer 4 runtime-integrated (4) | 595 |

Modified (the only tracked file touched in the whole stage): `src/hisiem_soc_copilot/evaluation_harness/__init__.py` — **purely additive** (AST-verified: 63 names added, 0 removed; 69 → 132 exports). The 7 deleted lines are re-sorted `__all__` entries, not removed names.

Removed: the two E2-era speculative stubs `submission_truth` and `telemetry_isolation` in `cross_plane_measure.py` (unused by any E2 scenario or test, and the former's `conflated` predicate was wrong). E3 now owns those gates with real implementations — see §16.

## 5. Authority lifecycle coverage

The frozen chain is exercised end to end, with each step measured from persisted or runtime facts:

```text
Investigation Result / Agent Verdict   (Immutable InvestigationResult.verdict)
→ Response Proposal                    (ResponseProposal aggregate, WAITING_APPROVAL)
→ Policy Evaluation                    (evaluate_response_policy → DENY | REQUIRE_APPROVAL)
→ Human Decision                       (ApprovalDecision, immutable, authenticated actor)
→ Durable Execution Command            (response_execution_queued event + its outbox row)
→ Dispatch Attempt                     (AsyncOutboxDispatcher attempt_count)
→ HISIEM SOAR Submission               (real internal SOAR API)
→ HISIEM Observed Execution State      (read back from HISIEM itself)
→ Copilot Projection                   (ResponseExecutionRef, never the truth)
```

Measured separations, all proven by gates on real facts:

* **Agent Verdict ≠ Analyst Disposition** (XP-AUTH-001) — the verdict is `InvestigationResult.verdict.disposition` in the verdict vocabulary; the analyst's act is `ApprovalDecision.decision` in the APPROVE/REJECT vocabulary, recorded against a different entity with an authenticated actor. Conflation (a decision written in verdict vocabulary, or an unauthenticated decision, or a projection that drops the decision) emits `AGENT_VERDICT_TREATED_AS_ANALYST_DISPOSITION`.
* **Policy Decision ≠ Human Approval** (XP-AUTH-002) — a REQUIRE_APPROVAL outcome must have produced an approval *request*, never a decision; a DENY outcome must have produced neither. `POLICY_SYNTHESIZED_APPROVAL` fires when policy produced consent (a decision without a human, a request under DENY, or REQUIRE_APPROVAL with no request).
* **Human Approval ≠ Execution** (XP-AUTH-003) — `EXECUTION_WITHOUT_APPROVAL` FAILs whenever a durable command or an observed execution exists without a recorded approval *of that exact intent*.
* **Submission ≠ Execution Success** (XP-AUTH-004, XP-AUTH-005) — `SUBMISSION_TREATED_AS_SUCCESS` FAILs if the projection presents an execution success the durable execution record does not support.
* **Copilot state ≠ HISIEM execution truth** (XP-AUTH-005) — the runtime slice reads the provider's own state and requires the Copilot projection to agree.

Valid intermediate states were explicitly preserved rather than collapsed (`APPROVED` + durable command + no execution; `SUBMITTED` + `RUNNING`; `PENDING` submission). No terminal-state simplification was introduced anywhere in the adapters or drivers.

## 6. Approval revision/hash/TOCTOU coverage

`AuthorizationBinding` (bounded: approved revision/hash, authorized revision/hash, `is_bound`) is read from the real `ApprovalRequest`, and `current_authorization_binding` binds it to the proposal's *live* revision/hash — i.e. it reads the same two fields production's `ResponseProposal.content_hash_matches` compares (a test asserts the two agree by construction).

* **Positive:** the approved binding matches the live proposal → `authorized_execution_authority` reports `approval_decision = APPROVE` → XP-AUTH-003 PASS, and `DURABLE_COMMAND_RECORDED` is measured from the persisted event ledger.
* **Negative (measured on real objects):** advancing the proposal to revision 2 with a changed parameter map makes `is_bound` False; with a command present the same scenario now FAILS `EXECUTION_WITHOUT_APPROVAL`, because a stale approval authorizes nothing.
* **Negative (as a refusal):** the same stale binding with **no** command produced emits no forbidden fact — the refusal left no dispatchable command behind.
* **Falsifiability (§28, "stale authorization accepted"):** a direct invalid measurement of an unbound authorization that *did* produce a command yields the forbidden token, and the XP-AUTH-003 gate FAILs.

Stage D's runtime TOCTOU proof (stale revision and wrong hash each returning `409 AGENT_STATE_CONFLICT` with the proposal unchanged and no new HISIEM execution) is treated as sealed baseline evidence (E3 §22) and is now expressed as a repeatable XP-01 measurement rather than re-run.

## 7. Durable command + idempotency coverage

* The durable execution command for one authorization is the `response_execution_queued` event plus its `response.execution.submit` outbox row, keyed by the deterministic `submission_key = response:<tenant>:<proposal>` — derived by calling production's own `submission_key`, never re-spelled. `logical_command_identities` reads the real event ledger and submission record; the E3 fixture cannot assert a command it did not record (the property is derived, not supplied).
* **Positive:** one approval → exactly one `response_execution_queued` event, one outbox row, one logical identity, one provider execution. Repeated identical approval converges (HTTP-level replay is covered by Stage D; here the persisted identity count stays 1).
* **Attempts ≠ intents (E3 §14):** a submission at `attempt_count = 3` still yields exactly one identity. No gate requires `attempt_count == 1`.
* **Duplicate detection (E3 §15):** `duplicate_logical_command` is True for two distinct command identities for one proposal **or** two proposals derived from one finalized result. The gated consequence is exercised: a second dispatchable command with no authorizing decision of its own FAILs `EXECUTION_WITHOUT_APPROVAL`.
* **Focused integration (real Postgres, real dispatcher):** a replayed `response.execution.submit` delivery converges on the same provider execution id and never executes a second time; the identity count remains 1.

## 8. Retry/exhaustion coverage

Measured through the REAL dispatcher and runners (no second retry engine in evaluation code):

* **Transient failure** → submission `RETRYING`, `attempt_count` advanced, no `ResponseExecutionRef`, `submitted_at` null, proposal stays `APPROVED`, outbox row failed with a future `available_at`. Nothing was fabricated in either direction.
* **Definitive refusal** (`HTTP_400`) → submission `FAILED_DEFINITIVE`, **no** execution row (the provider refused the submission and created no execution), proposal still `APPROVED`. A provider refusal is never recorded as an execution failure.
* **Exhaustion** → after exactly `_MAX_ATTEMPTS = 10` uncertain attempts the real `ResponseSubmitExhaustionHandler` persists `ATTENTION_REQUIRED` with `attention_required_at` set, `submitted_at` null, `failed_at` null, and no execution row; the delivery is dead-lettered. A reclaimed delivery afterwards does **not** resume submitting (asserted by counting provider calls).
* **Restart/recovery (E3 §19):** a fresh dispatcher + runner pair built after the approval (i.e. the process that comes up after a restart) picks up the pending durable intent and submits it **exactly once** — one provider call, one identity, XP-AUTH-003 PASS on the resulting persisted facts.
* **Observe reconciliation (E3 §20):** a `RUNNING` submission is reconciled by the real observe dispatcher to `SUCCEEDED`, and a `FAILED` observation settles as `FAILED`; submission status and execution status remain distinct facts throughout.

## 9. ATTENTION_REQUIRED semantics

`ATTENTION_REQUIRED` is preserved as **explicit uncertainty**, never reduced to a boolean failure:

* It is a terminal LOCAL state (`is_terminal = True`) that is not an execution status at all — the submission enum has no success member and `ResponseSubmissionStatus.ATTENTION_REQUIRED` is disjoint from `ResponseExecutionStatus`.
* Measured facts: `SUBMISSION_ATTENTION_REQUIRED` + `SUBMISSION_NOT_TREATED_AS_SUCCESS`; `submitted_at` is null, no provider execution exists, and no claim is made that the provider refused the submission.
* `XP-REL-004` PASSes on the real facts, and the negative half proves the gate is not vacuous: collapsing the same state into `FAILED_DEFINITIVE` (or into `SUCCEEDED`) removes `SUBMISSION_ATTENTION_REQUIRED` and FAILs `EXPECTED_FACTS_PRESENT` (or emits `SUBMISSION_TREATED_AS_SUCCESS_PRESENT`).
* The REAL workspace timeline projection presents it as its own kind (`RESPONSE_SUBMISSION_ATTENTION_REQUIRED`) and never as an execution success — asserted against the production `_response_timeline_entries` output, both deterministically and over real persisted rows. Stage D's 14/14 browser assertions on the 従应 tab remain the sealed UI evidence for the same semantics.

## 10. Submit uncertainty / reconciliation

* An uncertain submit (`SOAR_UNAVAILABLE`, timeout, or an empty provider execution id) yields `RETRYING` with the delivery left retryable. **No** terminal state is fabricated: no `SUCCEEDED`, no `FAILED`, no invented `provider_execution_ref` (the repo's existing `SOAR_NO_EXECUTION_ID` test remains the sole owner of that construction — see §16).
* Reconciliation is durable scheduling, not an in-process poll: a non-terminal observation appends a future-dated `response_execution_observed` instead of raising, so a legitimately long-running execution never exhausts the dead-letter budget (unchanged production semantics; the E3 tests assert them, they do not alter them).
* XP-AUTH-005's runtime slice submits a real approved proposal and reconciles the Copilot projection to HISIEM's own observed terminal state.

## 11. HISIEM observed execution truth

Runtime slice executed against the real stack on 2026-09-17:

| Element | Observed |
|---|---|
| HISIEM PostgreSQL (`siem-postgres`, 5432) | up (unchanged data; the protected operator database on 5433 was never touched) |
| HISIEM control-api (host JVM, 8080) | up (`/actuator/health` responds; the internal SOAR boundary answers) |
| HISIEM SOAR worker (host JVM) | up, poll + Kafka consumption active |
| Playbook used | `pb-a3b539a1-fa97-478e-8344-7a36d8e87816` (`published`, `enabled`, `entry_type=alert`, `event_types=["alert.created"]`, tenant `default`) |
| Submission path | Copilot `HisiemSoarAdapter` → `POST /api/internal/soar/executions` with `Idempotency-Key = response:default:<proposal>` |
| Provider execution | a REAL id (`exec-…`) distinct from the proposal id, `provider=hisiem` |
| Observed truth | the provider's terminal state read back from HISIEM equals the Copilot projection; XP-AUTH-005 PASS with `EXECUTION_OBSERVED_FROM_HISIEM` (and `EXECUTION_SUCCEEDED` for the observed run) |
| Adversarial half | a projection claiming `SUCCEEDED` while HISIEM reports `RUNNING` produces `SUBMISSION_TREATED_AS_SUCCESS_PRESENT` |

Corroborating pre-run probe on the same stack: a real execution created through the internal SOAR API returned `success`, and a replay with the **same** `Idempotency-Key` returned the **same** execution id — one logical command, one provider execution.

`SUBMITTED ≠ SUCCESS` held at every instant: before HISIEM's terminal observation the artifact for XP-AUTH-004 is PASS with the submission at `SUBMITTED` and the execution at a non-terminal status.

## 12. Measurement adapters

Two new focused adapter modules under the sanctioned `evaluation_harness` bridge (no adapter lives in a production layer, asserted by `test_xp01_adapter_lives_under_the_sanctioned_bridge`):

**`cross_plane_authority.py`** — measures, never decides. Where production already owns a rule the adapter *calls that rule* rather than restating it:

| Adapter | Reads |
|---|---|
| `authority_role_separation` | real workspace projections `_result`, `_workspace_approval` |
| `policy_and_approval_facts` | the proposal's persisted `policy_decision` + the real `response_policy_decided` ledger events + the real `ApprovalRequest`/`ApprovalDecision` |
| `execution_authority` / `authorized_execution_authority` | the live proposal, the approval contract, and the DERIVED durable command identities |
| `authorization_binding` / `current_authorization_binding` / `stale_authorization_facts` | `ApprovalRequest.proposal_content_revision/hash` vs the live proposal |
| `logical_command_identities` / `duplicate_logical_command` / `durable_command_facts` / `durable_command_ref` | the real event ledger + submission row; `submission_key` from production |
| `submission_truth` | `_workspace_submission` / `_workspace_execution` |
| `submission_presentation_facts` | `_response_timeline_entries` (real timeline) |
| `observed_execution_truth` | the provider's own reported state |
| `tool_result_from_failure` | `agent.tools.executor._tool_result_from_provider` (real mapping) |
| `evidence_from_failed_call` | real `EvidenceNormalizer.normalize_provider_result` |
| `failure_facts` / `retrieval_failure_facts` | the real `ToolResult` + the backend's real availability |
| `telemetry_isolation` / `telemetry_isolation_facts` / `business_outcome` | the persisted business outcome of a real lifecycle |

Discipline kept: no parallel state machine, no invented status, no weighted score, bounded correlation references only (E3 §12/§30) — never a full entity. Measurements carry **only** the frozen XP-01 fact vocabulary; invariants with no token (duplicate command, revision/hash binding) are reported as bounded typed values, never smuggled into a fact set as invented tokens.

## 13. Artifact evidence

The E3 acceptance pass (`--basetemp` outside both repositories) produced **34** `cross-plane-gate-results/v1` artifacts covering **all 10** E3 scenarios:

* 8 deterministic scenarios × (acceptance + determinism `a` + determinism `b`) = 24, plus a further 8 from the artifact-reference check;
* `XP-AUTH-005/gate-results.json` from the real HISIEM run;
* `XP-REL-005/gate-results.json` from the two real worker runs.

Every artifact is `pack_id: XP-01`, `schema_version: cross-plane-gate-results/v1`, `overall_gate: PASS`, and validates on read. The XP-AUTH-005 artifact carries real correlation references (`execution_command_id: response:default:<proposal>`, `provider_execution_ref: exec-…`). Each artifact is byte-identical across two independent runs of the same fixture, and no artifact exceeds the 256 KB bound; the secret scan runs before any bytes reach disk. No `authority-results/v1` or `reliability-results/v1` variant was introduced.

## 14. Architecture-boundary evidence

`tests/architecture` — **280 passed** (baseline 278 + the 2 new source modules picked up by the layer-import parametrization, which is the intended behaviour of that test).

Specifically re-confirmed for E3:

* production layers never import `evaluation` / `evaluation_harness` (0 offenders, and the boundary test is asserted non-vacuous);
* the XP-01 pure package imports only its allowed surface and has no IO/clock imports;
* no XP-01 scenario/gate identity appears in production source (the oracle firewall at the source level);
* the E3 adapters live under the sanctioned `evaluation_harness` bridge, so `ALLOWED_EVALUATION_IMPORTERS` needed **no** change;
* the new modules are registered as ordinary layer modules and pass the forbidden-import check.

## 15. Test/regression evidence

All commands run fresh in this session:

| Gate | Command | Result |
|---|---|---|
| E3 focused tests | `pytest tests/unit/evaluation_harness/test_cross_plane_authority.py tests/integration/evaluation_harness/test_e3_scenarios.py tests/integration/evaluation_harness/test_e3_focused_durable.py tests/integration/evaluation_harness/test_e3_runtime_slice.py` | **100 collected, all pass** (49 + 35 + 12 + 4) |
| E1 foundation | `pytest tests/unit/evaluation/cross_plane tests/unit/evaluation_harness/test_cross_plane_adapter.py tests/architecture/test_cross_plane_boundary.py -q` | **172 passed** (exact E1 baseline) |
| E2 scenario slice | `pytest tests/integration/evaluation_harness/test_e2_scenarios.py -q` | **33 passed**, asserting **15/15 E2 scenarios PASS** (semantics preserved) |
| GP-01 | `pytest tests/unit/evaluation tests/unit/evaluation_harness tests/integration/evaluation_harness -q` (E2/E3 additions excluded) | **346 passed** (exact baseline) |
| KB-GOLDEN-V1 | `pytest tests/unit/knowledge tests/unit/evaluation/knowledge tests/integration/persistence/test_knowledge_persistence.py -q` | **679 passed / 0 skipped** (exact baseline; disposable DB up) |
| Existing Response/Durable | `pytest` over proposal/handler/response-runner/dispatcher/durable-runner/durable-runtime + `test_response_workflow` + `test_durable_chain` + `test_e3_focused_durable` | **174 passed** (162 existing + 12 E3) |
| Architecture | `pytest tests/architecture -q` | **280 passed** |
| Static | `ruff check src tests` / `mypy src` / `git diff --check` | **clean / no issues in 225 source files / clean** |
| Full pytest | `pytest -q` | **2105 passed, 9 skipped, 0 failed, 0 errors** |

Full-suite arithmetic, so the deltas are auditable rather than asserted: E2 baseline **2003** + **100** new E3 tests + **2** architecture parametrizations over the 2 new source modules = **2105**. The 9 skips are the same 9 as the E2 baseline; **no new skip was introduced** for E3 — the two runtime-integrated scenarios execute for real when the runtime is present, and skip with an explicit reason when it is not.

Positive + negative + falsifiability coverage (E3 §27/§28) — every one of these proves a failure is reachable, not just that a PASS is:

| Invariant | Falsifiability proof |
|---|---|
| execution without approval | direct invalid `ExecutionAuthorityMeasurement` → gate FAIL |
| rejected approval dispatchable | `REJECT` + a durable command → gate FAIL |
| stale authorization accepted | unbound binding + a produced command → gate FAIL |
| duplicate logical command | two identities detected, and the second command without its own approval → gate FAIL |
| submission treated as success | `submission_treated_as_success=True` → gate FAIL |
| uncertain submit fabricated terminal state | `ATTENTION_REQUIRED` + `SUCCEEDED` → gate FAIL; collapsed into `FAILED_DEFINITIVE` → EXPECTED_FACTS_PRESENT FAIL |
| policy synthesized approval | decision without a human / request under DENY → gate FAIL |
| agent verdict treated as analyst disposition | verdict-vocabulary decision → gate FAIL |
| false-success Evidence | failed call yielding Evidence → gate FAIL |
| failure normalized as empty | outage reported as an empty success → gate FAIL |
| telemetry changing the business state | differing outcomes under outage → gate FAIL |
| oracle data leaked | an XP-01 token on a production surface → gate FAIL |
| missing measurement | a scenario with no collected facts raises `GateMeasurementMissingError` — never a silent PASS |

## 16. Production diff

```text
HISIEM production diff            = 0   (repo clean, HEAD == origin, 0 working-tree entries)
Copilot production-layer diff     = 0   (domain/ application/ agent/ api/ infrastructure/ bootstrap/)
frontend diff                     = 0
migration / new table             = 0
GP-01 semantic change             = 0
KB-GOLDEN-V1 semantic change      = 0
E4+ implementation                = 0
OBS-001 span work                 = 0
DEFECT-005 heuristic              = 0   (still OPEN / LOW / NON-BLOCKING)
TEST-INFRA-001 repair             = 0   (not absorbed)
new debug/force-retry/force-attention endpoint = 0
test-only production bypass       = 0
second authority/retry/state machine in XP-01  = 0
```

The only tracked file modified in the entire stage is `evaluation_harness/__init__.py` (additive exports). Two corrections inside E3's own predecessor work were made and are disclosed rather than hidden:

1. `cross_plane_measure.py` carried two unused E2-era stubs for the gates E3 owns — `submission_truth` (whose `conflated` predicate was wrong: it flagged a *non-terminal* observed status as conflation) and `telemetry_isolation`. Neither was referenced by any E2 scenario driver or test. They were removed so the E3-owned gates have exactly one implementation.
2. `is_typed_failure` existed in both `cross_plane_measure` (on `ProviderInvocationResult`) and the new module (on `ToolResult`), an ambiguous export. E3's was renamed `tool_result_is_typed_failure` before either name reached the package surface.

No E1 contract, catalog, gate, reason code or fact token was added, removed or re-semanticised.

## 17. Known observations / non-blockers

Carried forward unchanged, none absorbed into E3:

| ID | Status |
|---|---|
| CATALOG-001 | RESOLVED (E1: 29 scenarios implemented) |
| OBS-001 | OPEN / not E3 |
| DEFECT-005 | OPEN / LOW / NON-BLOCKING / not E3 |
| TEST-INFRA-001 | OPEN / test-infrastructure-only (not repaired) |
| EVAL-SEAM-001 | ACCEPTED / NON-BLOCKING |

New observations from E3 (all informational, none blocking):

* **E3-OBS-01 — unused vocabulary headroom.** Four expected fact tokens are declared by the frozen vocabulary but required by no scenario: `APPROVAL_REJECTED_NO_EXECUTION`, `EXECUTION_SUCCESS`-family `EXECUTION_SUCCEEDED`/`EXECUTION_FAILED`, and `ATTACK_TECHNIQUE_RESOLVED_CANONICAL`. E3 *measures* the first three (they appear in the measured fact sets and are asserted in tests) but gates them through existing scenarios — human rejection through XP-AUTH-003's `EXECUTION_WITHOUT_APPROVAL` gate plus the `APPROVAL_REJECTED_NO_EXECUTION` measurement, and observed success/failure through XP-AUTH-005. The catalog was deliberately **not** amended: the invariant is already measured and gated, so E3 §10's "do not add a scenario ID unless the catalog is proven incomplete" applies.
* **E3-OBS-02 — no gate token exists for a duplicate logical command.** `duplicate_logical_command` is measured and asserted directly, and its gated consequence (a second dispatchable command with no authorizing decision) FAILs `EXECUTION_WITHOUT_APPROVAL`. Adding a duplicate-specific token would be a catalog change and was not made.
* **E3-OBS-03 — runtime slice is operator-launched.** XP-AUTH-005 needs the HISIEM slice (PostgreSQL 5432 + control-api + SOAR worker + Kafka) and XP-REL-005 needs the OTel Collector image; both are launched by the operator/tests, not by the suite, and both skip with an explicit reason when absent. The collector container `e3-otel-collector` is created and removed by the fixture; no temporary container is left behind.
* **E3-OBS-04 — `submission_truth` cross-checks the projection against its own input.** The adapter compares production's projection of a record with that record, so it detects a *projection* regression (a non-terminal durable status mapped onto a success) rather than a caller-supplied lie. The fabricated-success direction is therefore proven at the gate level with a direct invalid measurement, per E3 §28, instead of in the adapter.
* **E3-OBS-05 — one test removed to respect a repo-owned structural guard.** An E3 focused-durable test drove the "provider returned no execution id" path on the real database, duplicating the repository's own `test_an_empty_execution_id_is_uncertain_not_failed` and tripping its sibling guard `test_no_fixture_ever_constructs_an_empty_execution_id`. The duplicate was removed rather than worked around; §16 coverage rests on the transient-failure and exhaustion tests plus the repo's existing test.

## 18. E4 handoff

E4 (OBSERVABILITY) and E5 (WORKSPACE) are **not started** — `XP-OBS-001/002` and `XP-UX-001/002` were deliberately left unimplemented.

What E4 can rely on as-is:

* The XP-01 contract package, the 13-gate model, the non-compensating verdict, and the `cross-plane-gate-results/v1` artifact contract are frozen and unchanged; the E3 driver/runner pattern (`E3Fixture` → driver → E1 measurement → `evaluate_scenario` → artifact) is proven and can be mirrored without touching E1.
* `EvaluationProfile` already permits executing a scenario at or above its declared minimum, so `XP-OBS-*` can reuse the runtime-slice structure in `test_e3_runtime_slice.py` (explicit skip reasons, real processes, no production change).
* `MeasurementSource.TELEMETRY_FACT` and `WORKSPACE_PROJECTION_FACT` already exist, so no contract change is expected.
* The E3 runtime slice leaves the HISIEM stack and the disposable test database in the state E4 will find them; the reusable runtime driver shape is `tests/support/e3_runtime_telemetry_driver.py`.

What E4 must not modify: the E1 contract package and vocabularies, the E3 adapters' semantics, GP-01, KB-GOLDEN-V1, `ALLOWED_EVALUATION_IMPORTERS`, and the `evaluation_harness/__init__.py` export ordering convention (re-sort the whole `__all__` after adding names, never re-sort before).
