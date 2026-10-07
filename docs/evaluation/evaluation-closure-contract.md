# 评估闭合契约

> **来源。** 这次闭合是作为三个工作步骤交付的，在阶段报告里记为 **E1-C4**（确定性评分器）、
> **E1-C5**（可重复性收集器）与 **E1-C6**（套件聚合）。那些代号是工程史标签；本文档按「它做什么」
> 来命名每一件，只在某段代码是某个具体产物的来源时才写出它。编号体系本身见
> [`../evidence/architecture-analysis/08-评估体系.md`](../evidence/architecture-analysis/08-评估体系.md)。

## 1. 范围

本文档冻结 GP-01 评估闭合：确定性正确性评分器、有界可重复性收集器及其尝试分类器，以及评估套件聚合。

```text
SealedManifest (GP-01)
      ↓
execute_real_model_run                              [E1-C2]
      ↓
tool-evidence-quality.json                          [E1-C3]
      ↓
score.json                     deterministic scorer  [E1-C4]
      ↓
bounded multi-run collector    attempt classifier    [E1-C5]
      ↓
suite-summary.json             suite aggregation     [E1-C6]
```

闭合原样复用既有的执行 / 遥测 / 质量路径。它不重新实现 `StartAlertInvestigation`、outbox 派发器、
runner、LangGraph、真的 `ModelProvider`、真实模型遥测闸门（`E1-C2`）或工具-证据质量评估器
（`E1-C3`）。生产代码永不导入 `evaluation_harness`；harness 是唯一被认可的
`SealedManifest → Container` 桥。

---

## 2. 冻结的原则

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

## 3. 确定性正确性评分器

### 3.1 输入（全部只读）

评分器只消费持久化的、机器权威的事实：

```text
sealed oracle    manifest.oracle.expected_verdict + required_evidence_roles   (manifest)
result fact      InvestigationResult.verdict.disposition + finding_ids        (ports)
result findings  Finding rows for the Investigation + their owner lookup       (ports)
quality fact     tool-evidence-quality.json (E1-C3, S1 provenance / gate facts)
telemetry        model-telemetry.json (E1-C2 gate + bounded usage)
execution fact   execution.json (status, investigation_id, counts, duration)
```

`score_gp01(oracle, result, quality, findings, telemetry, execution) -> EvaluationScore`
是纯的。IO（读取、产物写出、编排）与计算本身是分开的。

评分器不得：调用模型、调用工具、跑 Agent/Graph、解析判定散文、给自然语言相似度打分、使用
embedding/模糊匹配，或把任何东西发给另一个模型。

### 3.2 正确性闸门（当且仅当下列全部成立时 PASS）

```text
execution_status              == COMPLETED
InvestigationResult           present for the investigation
model telemetry gate          == PASS      (the real-model gate, E1-C2)
tool-evidence quality gate    == PASS      (E1-C3)
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

一个仅仅是「存在于数据库里」的正确 Finding **不**够：它必须参与最终的
`InvestigationResult.finding_ids`。置信度只作信息用途——不存在置信度阈值。

### 3.3 证据覆盖模型

`required_evidence_roles` 在评估侧从 `manifest.oracle.required_evidence_roles` 解析。一个 role 只有在
该 role 的工具-证据质量精确来源匹配成功时才算 `matched`（`E1-C3`）。GP-01 有一个必需 role
（`S1`），所以匹配时 `evidence_coverage = 1.0`，有不匹配的必需 role 时 `< 1`（FAIL）。role→证据的
映射是数据驱动的，好让后续场景能提供多个 role 匹配而不必重写引擎。

### 3.4 评分产物

路径：`<execution-dir>/score.json`；schema `evaluation-score/v1`。原子写入。未知或缺失的 schema
版本会被显式拒绝。字段有界且在白名单上；不得写入任何携带密钥的字段。对未变输入跑两次评分器，产出
语义上完全相同的输出。

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

只读且离线：它不得跑 Agent、调用模型、调用工具，或改动任何生产行。它可以重复运行；确定性产物会替换
该执行此前的任何评分。

---

## 4. 可重复性 / 稳健性

### 4.1 分类（只用稳定错误码）

一次尝试由稳定的模型错误码分类——永不依据 HTTP 状态码或异常散文：

```text
VALID_PASS                  full contract held, correctness_gate PASS  → counts
VALID_FAIL                  full contract held, correctness_gate FAIL  → counts
INVALID_PROVIDER_TRANSIENT  transport/limit outage only                → preserved, never counts
ABORT                       suite-aborting condition                   → stops the suite
```

`INVALID_PROVIDER_TRANSIENT` 当且仅当下列**全部**成立时才算：

```text
the provider baseline / config is valid;
there IS actual failed model usage;
every failed usage error_category ∈ {MODEL_UNAVAILABLE, MODEL_RATE_LIMITED, MODEL_TIMEOUT};
no MODEL_CONFIGURATION / MODEL_REFUSAL / MODEL_OUTPUT_VALIDATION / provider-contract mismatch;
the model gate failure (`E1-C2`) is explainable by those transient failures.
```

有歧义的情形（瞬时错误与确定性错误混杂，或闸门失败无法由那次中断解释）算 `VALID_FAIL`（保守失败）。

```text
MODEL_UNAVAILABLE / RATE_LIMITED / TIMEOUT   → INVALID_PROVIDER_TRANSIENT (may be replaced)
MODEL_REFUSAL / MODEL_OUTPUT_VALIDATION      → VALID_FAIL (counts)
MODEL_CONFIGURATION / contract mismatch /
  preflight failure / unexpected infra error  → ABORT (no retry-until-success)
model gate PASS + quality gate FAIL                → VALID_FAIL (counts)
model gate PASS + quality gate PASS + score FAIL   → VALID_FAIL (counts)
```

一个有效的失败样本**永不**被丢弃、替换，或通过重跑到换来替换掉。

### 4.2 有界收集器

```text
python -m hisiem_soc_copilot.evaluation.cli evaluate-gp01 <dataset_run_id> \
    --valid-runs 3 --max-attempts 6
```

默认 `valid_runs=3`、`max_attempts=6`；两者都有硬上限，并被校验
（`1 <= valid_runs <= max_attempts <= cap`）。不存在无界循环。每次执行都用**新的**
`execution_id`。失败与无效的尝试被保留，永不删除。收集器在以下情形停止：收满 `valid_runs` 个有效
样本、`max_attempts` 用尽、或套件中止：

```text
max_attempts exhausted before valid_runs  → INSUFFICIENT_VALID_RUNS
config / baseline / preflight error       → immediate ABORT
```

瞬时尝试单独汇报，而且一次语义 PASS 并不要求它为零。

### 4.3 GP-01 通过策略

```text
required valid samples        = 3
required correctness passes   = 3/3   (correctness_pass_rate == 1.0; 2/3 is FAIL)
```

一个有效的失败样本不会被重跑。

---

## 5. 评估套件汇总

### 5.1 产物

路径：`<executions_dir>/gp-01/<dataset_run_id>/suites/<suite_id>/suite-summary.json`；schema
`evaluation-suite-summary/v1`。每次运行一个唯一的 `suite_id`——更早的汇总永不被覆盖。未知或缺失的
schema 版本会被拒绝。

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

`p95` 不由三个样本计算。效率聚合只作信息用途，永不设闸。

### 5.2 套件 PASS（当且仅当下列全部成立）

```text
preflight PASS; sealed manifest unchanged; 3 valid samples collected
every valid sample: model gate PASS, quality gate PASS, correctness gate PASS
verdict correct 3/3; required evidence 3/3; grounding 3/3;
control exclusion 3/3; citation integrity 3/3; result-finding integrity 3/3
oracle firewall PASS for every valid execution; secret scan PASS
no suite-aborting error; final worktree CLEAN
```

---

## 6. 产物信任边界

```text
production artifacts (oracle-free):  execution.json, model-telemetry.json
evaluation artifacts (bounded oracle ok):  tool-evidence-quality.json, score.json,
                                           suite-summary.json
```

它们都不得含有 `CMD_API_KEY` / `Authorization` / `Bearer` / 密码 / 带凭据的 DSN / 原始 prompt /
原始 completion / 原始 HTTP 响应 / 环境转储 / 思维链。每个字段都在白名单上、且是有界的。

---

## 7. 不在范围内

不改 Agent prompt、工具或图；不改真实模型 provider 配置；不新增生产领域/schema 迁移；生产评估代码
里不写原始 SQL。GP-01 不重新物化、不重新封存。
