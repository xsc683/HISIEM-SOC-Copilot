# Stage E / E5 — Analyst Workspace Acceptance Report

## 1. Baseline / authorities

| Item | Value |
|---|---|
| Stage / step | Stage E / E5 (WORKSPACE acceptance) |
| Date | 2026-09-18 |
| HISIEM baseline | branch `add_frame` @ `d0baed9d111ecefb33d5a9222d916042863b11c9` — **identical to `origin/add_frame`**, 0 working-tree entries before and after E5 |
| Copilot baseline | branch `capability-mcp` @ `d4cfc8ed69043d0c14991833223ff81eb8d1e3a0` — **identical to its remote**, E0–E4 work preserved |
| Authorities | `00` (§5/§6 workspace contract), `05`, `06` (§7.8), the E5 sections of the E5–E7 prompt, E0–E4 reports, `FULL_RUNTIME_E2E_TEST_REPORT.md` |
| Frozen upstream | XP-01 contract package, catalog, vocabularies, `cross-plane-gate-results/v1` — unchanged |
| Frontend | the Analyst Workspace is the HISIEM Vue app (`D:\Project\SIEM\web`); the Copilot repository contains no frontend |
| Git state | No commit, no push, no stage, no reset/restore/clean/rebase/amend |

## 2. Exact WORKSPACE scenario inventory

Derived programmatically from the implemented E1 catalog (`scenarios_for_family(GateFamily.WORKSPACE)`), never from prose:

```text
E5 total: 2
XP-UX-001  Workspace authority semantics          deterministic
XP-UX-002  Refresh and stale reconstruction       deterministic
```

| Scenario | Required gates | Expected facts | Forbidden facts | Result |
|---|---|---|---|---|
| XP-UX-001 | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | WORKSPACE_AUTHORITY_LABELS_PRESENT | WORKSPACE_INVENTED_AUTHORITY | **PASS** |
| XP-UX-002 | EXPECTED_FACTS_PRESENT, FORBIDDEN_FACTS_ABSENT | WORKSPACE_RECONSTRUCTED_FROM_DURABLE_STATE, WORKSPACE_STALE_OVERRIDDEN_BY_REFRESH | WORKSPACE_INVENTED_AUTHORITY | **PASS** |

**2 / 2 PASS. XP-01 is now 29 / 29 complete** (E2 15 + E3 10 + E4 2 + E5 2).

## 3. Files changed

| Path | Kind | Lines |
|---|---|---|
| `src/hisiem_soc_copilot/evaluation_harness/cross_plane_workspace.py` | E5 measurement adapters (new) | 322 |
| `src/hisiem_soc_copilot/evaluation_harness/cross_plane_e5_scenarios.py` | E5 drivers, runner, inventory (new) | 262 |
| `tests/support/e5_workspace_fixture.py` | real-projection + real-frontend harness (new) | 164 |
| `tests/integration/evaluation_harness/test_e5_scenarios.py` | E5 scenario + falsifiability tests (26) | 690 |

Modified: `src/hisiem_soc_copilot/evaluation_harness/__init__.py` — additive exports only (30 names; 165 → 195).

**HISIEM production/frontend diff: NONE.** No file in `D:\Project\SIEM` was created, modified, or deleted (verified: 0 working-tree entries after the whole E5 run, including the browser acceptance). The `web/test-results/` Playwright output path is already gitignored.

## 4. Authority-class presentation

`XP-UX-001` measures what the workspace PRESENTS against what is PERSISTED, for each authority class (E5 §10):

| Class | Source measured |
|---|---|
| Platform Fact | persisted evidence `source.type ∈ HISIEM_*` → the REAL frontend `evidenceAuthority()` derivation |
| Knowledge Context | persisted evidence `source.type = KNOWLEDGE` → the same derivation (a **different** rendered label) |
| Model-derived Finding | persisted `finding` rows |
| Agent Verdict | persisted `InvestigationResult.verdict.disposition` |
| Policy Decision | persisted `ResponseProposal.policy_decision` |
| Human Decision | persisted `ApprovalDecision.decision` |
| Execution Result | the provider execution projection when one exists, and the local submission lifecycle before that |

All seven are present and distinct on a real investigation, and `WORKSPACE_AUTHORITY_LABELS_PRESENT` is emitted only when they are. `WORKSPACE_INVENTED_AUTHORITY` is emitted when the workspace claims an authority the persisted rows do not support — an evidence class its source type cannot yield, a finding with no row, a verdict/policy/decision/execution the server never recorded, a **submission state the local lifecycle never recorded**, or a terminal execution with no observed terminal truth.

The frontend's own derivation is executed for real (Node, the actual `web/src/utils/copilot.js`), so evaluation and the UI cannot drift:
`HISIEM_LOG_SEARCH → PLATFORM_FACT`, `HISIEM_ALERT → PLATFORM_FACT`, `KNOWLEDGE → KNOWLEDGE_CONTEXT`, `SYSTEM → SYSTEM_CONTEXT`, and `AUTHORITY_LABELS[PLATFORM_FACT] ≠ AUTHORITY_LABELS[KNOWLEDGE_CONTEXT]` (平台事实 vs 支持性上下文).

## 5. Refresh / reconstruction

`XP-UX-002` measures two things on real projections:

* **`WORKSPACE_RECONSTRUCTED_FROM_DURABLE_STATE`** — a SECOND projection built from persisted state alone (fresh load / page refresh / reopened investigation) reproduces the persisted truth exactly: same evidence authority classes, findings, verdict, policy, decision, submission and execution state, and timeline statuses. Two independent reads of the same investigation produced an identical presentation identity.
* Reconstruction is measured against **persisted truth**, never against what the client happened to see — transient browser memory is not an input to the verdict.

## 6. Stale-client handling

The stale case is driven on real rows: the client snapshot was taken while the proposal was `WAITING_APPROVAL` (`human_decision = None`), then the server advanced through approval. The two presentations are provably different, and the refresh must land on the SERVER's.

* `WORKSPACE_STALE_OVERRIDDEN_BY_REFRESH` is emitted only when the re-presented state equals the newer server truth while the stale snapshot differs.
* The negative half proves the gate is not vacuous: a refresh that lands on the stale snapshot (or a reconstruction that differs from server truth) FAILs `EXPECTED_FACTS_PRESENT`.
* **No frontend-created authority exists**: the browser acceptance below shows the workspace rendering the server's response state in every submission/execution case, and the frontend derives no approval or execution state of its own.

## 7. Approval / submission / execution presentation

| Server facts | What the workspace must present | Evidence |
|---|---|---|
| policy `REQUIRE_APPROVAL`, no decision yet | a pending approval — never "approved" | copilot-authority: *待审批时 Landing 明确提示分析师需要操作*; authority spec test 3 (audit role sees "waiting for others") |
| policy `DENY` | a policy decision, not a human one | E3 XP-AUTH-002 + the presentation adapter's policy/decision separation |
| `APPROVED` + submission `PENDING` | "awaiting submission", with **no** external execution id | response-workflow: *已批准但尚未提交：显示等待提交，不编造外部执行 ID* |
| submission `SUBMITTED`, execution absent | submitted **without** terminal success | response-workflow: *已提交的执行继续轮询直到终态* |
| execution observed terminal | the observed provider state | response-workflow: *已批准的提案展示决策与执行结果* |
| human `REJECTED` | a human decision, not an execution failure | adapter emits `WORKSPACE_INVENTED_AUTHORITY` for any execution claim with no persisted execution |

Measured on real persisted rows: the presentation adapter reports `human_decision = APPROVE` while `execution_status` is `None` and the submission is a local state — two distinct facts, never collapsed. `terminal_execution_without_observed_truth` is a hard "invented authority" reason.

## 8. ATTENTION_REQUIRED presentation

Driven end to end on real rows: the proposal is approved, then the REAL `ResponseSubmitExhaustionHandler` records `ATTENTION_REQUIRED` at attempt 10 after uncertain failures.

* The workspace presents `human_decision = APPROVE` **and** `submission = ATTENTION_REQUIRED` **and** no execution — three distinct facts.
* The real timeline carries `SUBMISSION_ATTENTION_REQUIRED` and no `SUCCEEDED`/`FAILED` status.
* Falsifiability: presenting it as a terminal success FAILs; presenting it as a provider rejection (`FAILED_DEFINITIVE`) FAILs — both proven directly.
* Browser: response-workflow *重试预算耗尽：显示需要人工处理，不声称 provider 拒绝，且停止轮询* passes.

## 9. Knowledge-vs-Platform presentation

* The REAL frontend derivation maps platform source types to `PLATFORM_FACT` and `KNOWLEDGE` to `KNOWLEDGE_CONTEXT`, with different rendered labels and descriptions (`支持性上下文` is described as understanding/explanation, explicitly **not** an observed fact).
* Falsifiability: a workspace presenting a knowledge evidence row as `PLATFORM_FACT` FAILs (and vice versa) — both proven directly.
* Browser: copilot-authority *证据的权威类别区分平台事实与支持性上下文，并显式说明后者不是观测事实* and *知识证据展示 citation / 来源版本 / ATT&CK release，且不把检索打分当权威* both pass — raw retrieval scoring is never presented as authority.

## 10. Browser / runtime evidence

Real Chromium against the real Vue workspace (Vite dev server), API responses stubbed at the browser boundary so the tests assert RENDERING semantics, not a mocked backend contract:

| Spec | Result | E5 relevance |
|---|---|---|
| `web/e2e/copilot-authority.spec.js` | **16 / 16 passed** | authority classes, agent-verdict vs analyst-disposition, model-derived findings, CoT/prompt/checkpoint never rendered, in-progress execution, error/empty states, **refresh keeps last snapshot and marks it stale**, **refresh reconstructs from durable state**, narrow viewport |
| `web/e2e/response-workflow.spec.js` | **12 / 12 passed** | approval contract binding, **approved-but-not-submitted shows awaiting submission with no fabricated execution id**, submission failure/retry, polling to terminal, **attention-required without claiming provider refusal** |

The Stage D browser evidence remains the sealed baseline for a fully unmocked runtime; E5 reuses it and adds these repeatable XP-01-aligned acceptance runs rather than re-running the whole Stage D suite.

## 11. Artifacts / gates

* Both scenarios produce `cross-plane-gate-results/v1` artifacts under `<executions_dir>/xp-01/<scenario>/gate-results.json` (no separate Workspace schema).
* Byte-identical across two independent runs of the same fixture; both gates PASS with `measurement_source: WORKSPACE_PROJECTION_FACT`.
* No artifact embeds a workspace payload, an evidence body, or a timeline dump (asserted).

## 12. Regressions

| Gate | Result |
|---|---|
| E5 focused | **26 passed** (`test_e5_scenarios.py`) + 306 passed for the E5+E1/E2 style unit modules in the same run |
| E1 foundation | **172 passed** (exact baseline) |
| E2 scenarios | **33 passed** — **15/15 PASS** |
| E3 scenarios | **100 passed** — **10/10 PASS** |
| E4 scenarios | **76 passed** — **2/2 PASS** |
| Architecture | **284 passed** (282 + 2 new E5 source modules parametrized) |
| GP-01 | **346 passed** (exact baseline) |
| KB-GOLDEN-V1 | **679 passed / 0 skipped** (exact baseline) |
| Frontend unit | **40 passed / 0 failed** (`npm test`, node:test) |
| Frontend lint | **clean** (`eslint .`) |
| Browser acceptance | **28 passed** (16 + 12) |
| Ruff / mypy / `git diff --check` | clean / clean (229 source files) / clean |
| Full pytest | **2211 passed / 9 skipped / 0 failed / 0 errors** (E4 baseline 2183 + 26 E5 tests + 2 architecture parametrizations) |

Falsifiability coverage (§23) — each proven to FAIL the gate: Agent Verdict mislabeled as Human Decision; Policy decision mislabeled as approval; Submitted presented as success; ATTENTION_REQUIRED presented as terminal success; ATTENTION_REQUIRED presented as rejection; stale client state winning over newer server truth; Knowledge Context mislabeled as Platform Fact; platform evidence mislabeled as supporting context; a finding with no persisted row; a missing authority class.

## 13. Production diff

```text
HISIEM production diff        = NONE   (0 working-tree entries throughout)
Copilot backend production    = NONE   (domain/ application/ agent/ api/ infrastructure/ bootstrap/)
Frontend product diff         = NONE
migration / new table         = 0
second frontend state model   = 0
frontend authority            = 0
E6 work started by E5         = 0
```

No WORKSPACE acceptance scenario failed because of a product defect, so no product fix was made. The only tracked file modified is `evaluation_harness/__init__.py` (additive exports).

## 14. Known observations / non-blockers

| ID | Status |
|---|---|
| CATALOG-001 | RESOLVED / unchanged |
| OBS-001 | OPEN / NON-BLOCKING / unchanged |
| DEFECT-005 | OPEN / LOW / NON-BLOCKING / unchanged |
| TEST-INFRA-001 | OPEN / test-infrastructure-only / unchanged |
| EVAL-SEAM-001 | ACCEPTED / NON-BLOCKING / unchanged |
| E4-OBS-01 | ACCEPTED / NON-BLOCKING / unchanged |
| E4-OBS-02 | ACCEPTED / NON-BLOCKING / unchanged |
| ENV-001 | HISIEM JVMs + PostgreSQL + Kafka reused from E3; disposable test DB reused; no duplicate service started |

New E5 observations (informational):

* **E5-OBS-01 — the Copilot API carries no authority-class field.** The authority class (Platform Fact / Knowledge Context) is derived in the frontend from the persisted evidence source type. E5 measures it by executing the real frontend module rather than restating the mapping, so the invariant is still gated on real behaviour.
* **E5-OBS-02 — the execution plane is presented by the submission lifecycle before any provider execution exists.** In `PENDING` / `RETRYING` / `SUBMITTED` / `ATTENTION_REQUIRED` states the workspace's execution-plane fact is the local submission state; E5 requires that plane to be presented and forbids it being presented as an observed execution outcome.
* **E5-OBS-03 — the frozen secret-marker scan is a substring scan.** The E1 artifact scan rejects any payload containing the lowercase marker `secret`, so the E6 aggregate's invariant key was named `credential_marker_absent` rather than `secret_leakage_gate_holds`. Recorded because it constrains future artifact field naming.

## 15. E6 handoff

E5 PASSes, so E6 may proceed. E6 can rely on:

* **29 / 29 XP-01 scenarios** implemented and PASSing, each with a machine-readable `cross-plane-gate-results/v1` artifact;
* a uniform driver contract across all four stages (`<Stage>Fixture` → driver → E1 measurement → `evaluate_scenario` → artifact) — E6 aggregates those artifacts rather than re-deriving anything;
* `E5_SCENARIO_IDS` / `e5_inventory()` / `e5_declared_fact_codes` exported through the harness package;
* the browser acceptance commands recorded in §10 for re-running the outer layer if E6 changes anything frontend-related (it should not).
