# Evaluation Closure Contract (E1-C4 / E1-C5 / E1-C6)

## 1. Scope

This document freezes the GP-01 evaluation closure: the deterministic correctness
scorer (E1-C4), the bounded repeatability collector and its attempt classifier
(E1-C5), and the evaluation-suite aggregation (E1-C6).

```text
SealedManifest (GP-01)
      ↓
execute_real_model_run            (E1-C2, unchanged)
      ↓
tool-evidence-quality.json        (E1-C3, unchanged)
      ↓
score.json                        (E1-C4, deterministic correctness)
      ↓
bounded multi-run collector       (E1-C5, attempt classification)
      ↓
suite-summary.json                (E1-C6, aggregation)
```

The closure reuses the existing execution / telemetry / quality paths unchanged. It
does not reimplement `StartAlertInvestigation`, the outbox dispatcher, the runner,
LangGraph, the real `ModelProvider`, the E1-C2 telemetry gate, or the E1-C3 quality
evaluator. Production code never imports `evaluation_harness`; the harness is the only
sanctioned `SealedManifest → Container` bridge.

---

## 2. Frozen principles

```text
No LLM-as-a-Judge. Scoring is deterministic from persisted, machine-authoritative facts.
Only transport/limit model errors are excludable as provider-transient.
A valid failure sample always counts toward the denominator and is never replaced.
GP-01 closure requires 3/3 valid correctness passes.
Efficiency metrics (tokens / latency / call counts) are informational and never gate.
The sealed oracle is evaluation-only: it may appear in evaluation artifacts, never in
production artifacts, model input, prompts, tools, or production state.
```

---

## 3. E1-C4 — Deterministic correctness scorer

### 3.1 Inputs (all read-only)

The scorer consumes only persisted, machine-authoritative facts:

```text
sealed oracle    manifest.oracle.expected_verdict + required_evidence_roles   (manifest)
result fact      InvestigationResult.verdict.disposition + finding_ids        (ports)
result findings  Finding rows for the Investigation + their owner lookup       (ports)
quality fact     tool-evidence-quality.json (E1-C3, S1 provenance / gate facts)
telemetry        model-telemetry.json (E1-C2 gate + bounded usage)
execution fact   execution.json (status, investigation_id, counts, duration)
```

`score_gp01(oracle, result, quality, findings, telemetry, execution) -> EvaluationScore`
is pure. IO (reads, artifact writes, orchestration) is separate from the calculation.

The scorer MUST NOT: call a model, call a tool, run the Agent/Graph, parse verdict
prose, score NL similarity, use embeddings/fuzzy matching, or send anything to another
model.

### 3.2 Correctness gate (PASS iff ALL hold)

```text
execution_status              == COMPLETED
InvestigationResult           present for the investigation
E1-C2 model telemetry gate    == PASS
E1-C3 tool/evidence gate      == PASS
actual disposition            == sealed expected_verdict
required_evidence_roles       all matched (GP-01: ["S1"])
required role grounded        exact S1 Evidence → Finding → successful search_events ToolInvocation
control role (W1)             excluded from Evidence
dangling citations            == 0
cross-investigation citations == 0
oracle firewall               PASS
>= 1 grounded Finding         PARTICIPATES in InvestigationResult.finding_ids
every result finding_id       resolves to a persisted Finding of the SAME Investigation
```

A correct Finding that merely exists in the database is NOT sufficient: it must
participate in the final `InvestigationResult.finding_ids`. Confidence is informational
only — there is no confidence threshold.

### 3.3 Evidence-coverage model

`required_evidence_roles` is resolved evaluation-side from
`manifest.oracle.required_evidence_roles`. A role is `matched` only when the E1-C3 exact
provenance match for that role succeeded. GP-01 has one required role (`S1`), so
`evidence_coverage = 1.0` on match and `< 1` (FAIL) when a required role is unmatched.
The role→evidence mapping is data-driven so later scenarios can supply multiple role
matches without rewriting the engine.

### 3.4 Score artifact

Path: `<execution-dir>/score.json`; schema `evaluation-score/v1`. Written atomically.
Unknown or missing schema versions are rejected explicitly. Fields are bounded and on an
explicit allowlist; no secret-bearing field may be written. Running the scorer twice on
unchanged inputs yields semantically identical output.

```text
schema_version; execution_id; dataset_run_id; investigation_id
expected_verdict; actual_verdict; verdict_match
required_evidence_roles; matched_evidence_roles; evidence_coverage
tool_evidence_gate; grounded_required_evidence
grounded_finding_ids; result_finding_ids; grounded_findings_in_result
result_finding_integrity_pass; control_exclusion_pass; citation_integrity_pass
model_telemetry_gate; oracle_firewall_pass; correctness_gate; gate_failures
informational: confidence; model_calls; tool_calls; search_events_calls;
               evidence_count; finding_count; duration_ms;
               input_tokens; output_tokens; total_tokens
```

### 3.5 CLI

```text
python -m hisiem_soc_copilot.evaluation.cli score-execution <dataset_run_id> <execution_id>
```

Read-only and offline: it MUST NOT run the Agent, call a model, call a tool, or mutate a
production row. It may be re-run; the deterministic artifact replaces any prior score for
that execution.

---

## 4. E1-C5 — Repeatability / robustness

### 4.1 Classification (stable codes only)

An attempt is classified from stable model error codes — never from HTTP status or
exception prose:

```text
VALID_PASS                  full contract held, correctness_gate PASS  → counts
VALID_FAIL                  full contract held, correctness_gate FAIL  → counts
INVALID_PROVIDER_TRANSIENT  transport/limit outage only                → preserved, never counts
ABORT                       suite-aborting condition                   → stops the suite
```

`INVALID_PROVIDER_TRANSIENT` holds ONLY when ALL of:

```text
the provider baseline / config is valid;
there IS actual failed model usage;
every failed usage error_category ∈ {MODEL_UNAVAILABLE, MODEL_RATE_LIMITED, MODEL_TIMEOUT};
no MODEL_CONFIGURATION / MODEL_REFUSAL / MODEL_OUTPUT_VALIDATION / provider-contract mismatch;
the E1-C2 gate failure is explainable by those transient failures.
```

Ambiguous cases (mixed transient + deterministic errors, or a gate failure not
explainable by the outage) count as `VALID_FAIL` (fail conservative).

```text
MODEL_UNAVAILABLE / RATE_LIMITED / TIMEOUT   → INVALID_PROVIDER_TRANSIENT (may be replaced)
MODEL_REFUSAL / MODEL_OUTPUT_VALIDATION      → VALID_FAIL (counts)
MODEL_CONFIGURATION / contract mismatch /
  preflight failure / unexpected infra error  → ABORT (no retry-until-success)
E1-C2 PASS + E1-C3 FAIL                       → VALID_FAIL (counts)
E1-C2 PASS + E1-C3 PASS + E1-C4 FAIL          → VALID_FAIL (counts)
```

A valid failure sample is NEVER discarded, replaced, or rerun-to-replace.

### 4.2 Bounded collector

```text
python -m hisiem_soc_copilot.evaluation.cli evaluate-gp01 <dataset_run_id> \
    --valid-runs 3 --max-attempts 6
```

Defaults `valid_runs=3`, `max_attempts=6`; both bounded by a hard cap and validated
(`1 <= valid_runs <= max_attempts <= cap`). There is no unlimited loop. Every execution
uses a NEW `execution_id`. Failed and invalid attempts are preserved, never deleted. The
collector stops when `valid_runs` valid samples are collected, `max_attempts` is
exhausted, or the suite aborts:

```text
max_attempts exhausted before valid_runs  → INSUFFICIENT_VALID_RUNS
config / baseline / preflight error       → immediate ABORT
```

Transient attempts are reported separately and are not required to be zero for a
semantic PASS.

### 4.3 GP-01 pass policy

```text
required valid samples        = 3
required correctness passes   = 3/3   (correctness_pass_rate == 1.0; 2/3 is FAIL)
```

A valid failed sample is not rerun.

---

## 5. E1-C6 — Evaluation suite summary

### 5.1 Artifact

Path: `<executions_dir>/gp-01/<dataset_run_id>/suites/<suite_id>/suite-summary.json`;
schema `evaluation-suite-summary/v1`. A unique `suite_id` per run — an earlier summary
is never overwritten. Unknown or missing schema versions are rejected.

```text
schema_version; suite_id; scenario_id; dataset_run_id
requested_valid_runs; max_attempts; total_attempts; valid_attempts;
invalid_provider_transient_attempts; valid_passes; valid_failures;
correctness_pass_rate; verdict_match_count; required_evidence_pass_count;
grounding_pass_count; control_exclusion_pass_count; citation_integrity_pass_count;
result_finding_integrity_pass_count;
attempts[] { attempt_number; execution_id; classification; execution_status;
             model_gate; quality_gate; correctness_gate; score_artifact }
abort_category?
informational efficiency per duration_ms / model_calls / tool_calls /
  search_events_calls / total_tokens : {min, median, max}   (null where unreported)
suite_gate; gate_failures
```

`p95` is not computed from three samples. Efficiency aggregates are informational and
never gate.

### 5.2 Suite PASS (iff ALL hold)

```text
preflight PASS; sealed manifest unchanged; 3 valid samples collected
every valid sample: E1-C2 PASS, E1-C3 PASS, E1-C4 PASS
verdict correct 3/3; required evidence 3/3; grounding 3/3;
control exclusion 3/3; citation integrity 3/3; result-finding integrity 3/3
oracle firewall PASS for every valid execution; secret scan PASS
no suite-aborting error; final worktree CLEAN
```

---

## 6. Artifact trust boundaries

```text
production artifacts (oracle-free):  execution.json, model-telemetry.json
evaluation artifacts (bounded oracle ok):  tool-evidence-quality.json, score.json,
                                           suite-summary.json
```

None may contain `CMD_API_KEY` / `Authorization` / `Bearer` / password / credential-
bearing DSN / raw prompts / raw completions / raw HTTP responses / environment dumps /
chain-of-thought. Every field is allowlisted and bounded.

---

## 7. Out of scope

No change to Agent prompts, tools, or graph; no change to the real model provider
configuration; no new production domain/schema migration; no raw SQL in production
evaluation code. GP-01 is not rematerialized or resealed.
