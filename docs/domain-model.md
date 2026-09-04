# HISIEM SOC Copilot V1 — Domain Model & Investigation State Machine

## 1. 文档目的

本文定义 HISIEM SOC Copilot V1 的领域对象、对象所有权、关系、不变量、Investigation 状态机以及数据可信边界。

本文作为后续 Application Layer、Persistence、Agent Runtime、LangGraph State、Tool Contract 和 API 设计的领域基线。

## 2. 总体领域模型

V1 核心业务链：

```text
HISIEM Alert
    ↓
Investigation
    ├── Plan Revision
    ├── Hypothesis
    ├── Evidence
    ├── Finding
    └── Investigation Result
             ↓
      Response Proposal
             ↓
          Approval
             ↓
        HISIEM SOAR
             ↓
    Response Execution Ref
```

核心 Aggregate Root：

```text
Investigation
ResponseProposal
```

## 3. Domain / Runtime / Platform 分层

系统存在三类状态，必须严格分离。

### 3.1 HISIEM Source of Truth

HISIEM 拥有：Alert、Event、Case、Detection Rule、Tenant、User / RBAC、SOAR Playbook、SOAR Execution。

### 3.2 SOC Copilot Domain State

Copilot 拥有：Investigation、PlanRevision、Hypothesis、HypothesisAssessment、Evidence、Finding、InvestigationResult、ResponseProposal、ApprovalRequest、ApprovalDecision。

### 3.3 Agent Runtime State

Agent Runtime / LangGraph 拥有：working context、current graph position、current candidate plan、working hypothesis set、evidence references、iteration counters、budget counters、pending tool request、pending model candidate、checkpoint state。

Runtime State 不得替代 Domain State。

## 4. Investigation — Aggregate Root

定义：针对一个 HISIEM Alert 开展的一次独立、安全、可追踪的调查实例。

```text
Investigation

id
tenant_ref
source_alert_ref
initiated_by

status
phase

current_plan_revision
result_id?
response_proposal_id?

budget
termination_reason?

revision

created_at
started_at?
finished_at?
cancelled_at?
```

核心不变量：

```text
tenant_ref immutable
source_alert_ref immutable
initiated_by immutable

Investigation belongs to exactly one Tenant
Investigation originates from exactly one Alert

At most one active Investigation
per tenant + source_alert_ref

Terminal status cannot transition

Lifecycle changes must pass
Investigation state-machine validation
```

同一个 Alert 可以存在多个历史 Investigation，但同一时刻最多一个 Active Investigation。

## 5. ExternalResourceRef — Value Object

Copilot 不复制 HISIEM Resource Model，只保存稳定引用。

```text
ExternalResourceRef

provider
resource_type
address_id
business_id?
```

`address_id` 是 HISIEM API 实际寻址资源使用的稳定标识；`business_id` 为可选业务展示标识。下游不得通过业务展示字段自行推断寻址 ID。

## 6. ActorRef — Value Object

```text
ActorRef

subject_id
tenant_id
display_name?
role_snapshot?
```

ActorRef 必须来自已认证身份上下文。

以下字段不得由请求 Body、LLM 或 Tool Result 声明：subject_id、tenant_id、authorization、role。

## 7. PlanRevision — Entity

Investigation Plan 使用 Revision，而不是原地覆盖。

```text
PlanRevision

id
investigation_id
revision

goal
steps[]

generated_by
created_at
```

### PlanStep

```text
PlanStep

step_id
objective
status
```

状态：`PENDING / ACTIVE / COMPLETED / SKIPPED`。

Plan 表达调查目标；ToolInvocation 表达一次具体执行。二者必须分离。

## 8. Evidence — Immutable Entity

定义：从可追踪来源获得并固定保存，用于支持 Investigation 判断的 Observation。

```text
Evidence

id
investigation_id

source
source_resource_ref?
source_tool_call_id?

observed_at?
collected_at

observation
summary?
raw_reference?
content_hash?

entity_refs[]
```

核心不变量：

```text
Evidence must belong to one Investigation
Evidence must have provenance
Evidence must have collected_at
Evidence is immutable after creation
Evidence cannot be created from unsupported model imagination
```

需要修正旧 Evidence 时必须新增新 Evidence，不修改历史记录。

## 9. EvidenceSource — Value Object

```text
EvidenceSource

type
provider
operation
```

建议 V1 类型：`HISIEM_ALERT / HISIEM_EVENT / HISIEM_LOG_SEARCH / HISIEM_ENTITY / THREAT_INTEL / KNOWLEDGE / SYSTEM`。

每条 Evidence 必须具有明确 Source。

## 10. EvidenceRelation — Value Object

Evidence 本身不声明是否支持某个结论。

```text
EvidenceRelation

evidence_id
relation
```

关系类型：`SUPPORTS / CONTRADICTS / CONTEXT`。

## 11. Hypothesis — Entity

定义：Investigation 中一个待 Evidence 支持、反驳或保留未决的安全解释。

```text
Hypothesis

id
investigation_id

statement
status
assessment_revision

created_at
updated_at
```

状态：`OPEN / SUPPORTED / CONTRADICTED / UNRESOLVED`。

## 12. HypothesisAssessment — Immutable Entity

```text
HypothesisAssessment

id
hypothesis_id
revision

status
evidence_relations[]
reason_summary

created_at
```

每次重新评估形成新 Revision。

当状态为 `SUPPORTED` 或 `CONTRADICTED` 时必须至少存在一条 EvidenceRelation。

## 13. Finding — Immutable Entity

定义：已经由 Evidence 支撑的事实性调查判断。

```text
Finding

id
investigation_id

statement
evidence_citations[]

created_at
```

核心不变量：

```text
Finding must cite >= 1 Evidence
Every cited Evidence must exist
Every cited Evidence must belong to the same Investigation
```

Finding 创建后不可原地修改。

## 14. Hypothesis 与 Finding

```text
Hypothesis = 尚待验证的解释
Finding = 已有 Evidence 支撑的调查事实
```

Hypothesis 不得直接出现在最终 Result 中作为已确认事实。

## 15. InvestigationResult — Immutable Entity

```text
InvestigationResult

id
investigation_id

verdict
finding_ids[]

uncertainties[]
attack_mappings[]
response_recommendations[]

created_at
```

创建并 Finalize 后不可修改。

未来重新评估必须生成新 Investigation 或显式 Result Revision，而不是覆盖旧结果。

## 16. Verdict — Value Object

```text
Verdict

disposition
summary
confidence
```

V1 disposition：`MALICIOUS / BENIGN / INCONCLUSIVE`。

具体攻击语义放入 `summary`，不扩张大量业务枚举。

## 17. ConfidenceScore — Value Object

```text
0.0 <= confidence <= 1.0
```

Confidence 不等于 Evidence Strength、Authorization、Risk Score 或 Approval，不得直接触发 Side Effect。

## 18. Uncertainty — Value Object

```text
Uncertainty

description
missing_information?
related_hypothesis_ids[]
```

当 Verdict 为 INCONCLUSIVE 时，必须至少提供 Uncertainty 或 Missing Evidence Explanation。

## 19. Copilot Verdict 与 HISIEM Analyst Verdict

两者必须严格分离。

```text
Copilot Investigation Verdict
→ Agent 调查结论

HISIEM Analyst Verdict
→ 人工业务处置事实
```

Copilot Result 不得自动更新 HISIEM `true_positive / false_positive / duplicate`。

## 20. ResponseRecommendation

ResponseRecommendation 是无执行权限的建议。

```text
ResponseRecommendation

description
reason
```

它不能直接进入 Side Effect 路径。

## 21. ResponseProposal — Aggregate Root

定义：经过领域验证、准备进入人工审批并最终映射到确定性 SOAR 能力的执行意图。

```text
ResponseProposal

id
investigation_id
result_id

status

action
target_refs[]
parameters

reason
evidence_ids[]

policy_decision

revision
content_hash?

approval_request_id?
execution_ref?

created_at
```

## 22. Response Action

LLM 不得定义任意 Action。Action 必须来自系统 Allowlist / Action Registry。

候选示例：`BLOCK_SOURCE_IP / DISABLE_ACCOUNT / ISOLATE_HOST / START_SOAR_PLAYBOOK`。

禁止 Arbitrary Shell、Arbitrary URL、Arbitrary Script、Unknown Tool、Unknown Action。

## 23. V1 Response 执行粒度

V1 优先将 ResponseProposal 解析到稳定的 HISIEM SOAR Playbook，而不是由 Copilot 逐条执行基础设施动作。

```text
ResponseProposal
       ↓
Resolved SOAR Playbook
       ↓
Policy Validation
       ↓
Human Approval
       ↓
HISIEM SOAR Execution
```

Copilot 不实现第二套响应 Workflow Engine。

## 24. ResponseProposal 不变量

进入审批前必须满足：

```text
InvestigationResult finalized
action registered
target successfully resolved
target belongs to same tenant
supporting Evidence exists
all Evidence belongs to same Investigation
parameters pass schema validation
policy validation completed
```

禁止 model-generated unresolved resource ID、cross-tenant target、unknown action、arbitrary executable content、missing supporting Evidence。

## 25. PolicyDecision

V1 Policy 结果仅允许：`DENY / REQUIRE_APPROVAL`。

V1 不提供 `ALLOW_AUTOMATIC`。

Human Approval 不能替代 Policy Validation。无效 Proposal 必须直接 DENY。

## 26. ApprovalRequest — Entity

```text
ApprovalRequest

id
proposal_id
proposal_revision
proposal_content_hash

requested_at
requested_reason
```

Approval 必须绑定精确 Proposal 版本。

## 27. ApprovalDecision — Immutable Entity

```text
ApprovalDecision

id
approval_request_id

decision
actor
reason?

decided_at
```

Decision：`APPROVE / REJECT`。

核心不变量：Decision exactly once；Actor 必须已认证且具有审批权限；Proposal 必须仍匹配已审批 revision/hash；LLM 不得产生 ApprovalDecision。

## 28. ResponseExecutionRef — Projection

SOAR Execution 由 HISIEM 拥有。Copilot 只保存引用和观察状态：

```text
ResponseExecutionRef

provider
execution_id
last_observed_status
submitted_at
last_observed_at
```

该对象不是 SOAR Execution 的 Source of Truth。

## 29. ToolInvocation — Runtime / Audit Entity

ToolInvocation 不属于核心调查领域。

```text
ToolInvocation

id
investigation_id

tool
arguments
status
duration
error
result_metadata
```

处理路径：

```text
ToolInvocation
      ↓
ToolResult
      ↓
Evidence Normalizer
      ↓
Evidence
```

只有 Evidence 进入核心调查语义。

## 30. 对象所有权

| 对象 | Source of Truth |
|---|---|
| Alert | HISIEM |
| Event / Log | HISIEM |
| Detection Rule | HISIEM |
| Case | HISIEM |
| Tenant | HISIEM |
| User / RBAC | HISIEM |
| SOAR Playbook | HISIEM |
| SOAR Execution | HISIEM |
| Investigation | SOC Copilot |
| PlanRevision | SOC Copilot |
| Hypothesis | SOC Copilot |
| HypothesisAssessment | SOC Copilot |
| Evidence | SOC Copilot |
| Finding | SOC Copilot |
| InvestigationResult | SOC Copilot |
| ResponseProposal | SOC Copilot |
| ApprovalRequest | SOC Copilot |
| ApprovalDecision | Authenticated Human + Copilot persistence |
| LangGraph Checkpoint | Agent Runtime |
| LLM Response | Model Provider / Runtime |
| ToolInvocation | Runtime / Audit |
| Trace / Span | Observability |

最高原则：`LLM owns no authoritative business state.`

## 31. 核心对象关系

```text
HISIEM Alert 1 ── N Investigation
Investigation 1 ── N PlanRevision
Investigation 1 ── N Hypothesis
Hypothesis 1 ── N HypothesisAssessment
Investigation 1 ── N Evidence
Investigation 1 ── N Finding
Hypothesis N ── N Evidence via EvidenceRelation
Finding N ── N Evidence via Citation
Investigation 1 ── 0..1 InvestigationResult
Investigation 1 ── 0..1 ResponseProposal
ResponseProposal 1 ── 0..1 ApprovalRequest
ApprovalRequest 1 ── 0..1 ApprovalDecision
ResponseProposal 1 ── 0..1 ResponseExecutionRef
```

## 32. InvestigationResult 不变量

当 Verdict 为 `MALICIOUS` 或 `BENIGN` 时至少必须存在一个 grounded Finding。

当 Verdict 为 `INCONCLUSIVE` 时必须存在至少一项 Uncertainty、Missing Evidence 或 Unresolved Question。

## 33. Investigation Status

V1 业务状态：

```text
CREATED
RUNNING
WAITING_APPROVAL
EXECUTING_RESPONSE
COMPLETED
FAILED
CANCELLED
```

这些状态表示业务生命周期。

PLANNING、TOOL_CALLING、LLM_REASONING、RAG_RETRIEVAL、VERIFYING_NODE 等运行时概念不得作为 Investigation Status。

## 34. Investigation Phase

RUNNING 内部阶段：

```text
HYDRATING
PLANNING
INVESTIGATING
VERIFYING
FINALIZING
```

Phase 可以循环；Status 保持稳定。

## 35. Investigation State Machine

```text
                 CREATED
                    │
                  start
                    ▼
                 RUNNING
               ↙   ↑   ↘
     investigate   │   finalize
          / verify │
               loop
                    │
             ┌──────┴──────┐
             │             │
             ▼             ▼
        COMPLETED    WAITING_APPROVAL
                           │
                   ┌───────┴───────┐
                   │               │
                reject          approve
                   │               │
                   ▼               ▼
              COMPLETED    EXECUTING_RESPONSE
                                   │
                         execution terminal
                                   │
                                   ▼
                              COMPLETED
```

额外终止路径：CREATED / RUNNING / WAITING_APPROVAL → CANCELLED。

系统不可恢复错误：CREATED / RUNNING → FAILED。

## 36. 合法状态转换

| From | Command | To |
|---|---|---|
| CREATED | StartInvestigation | RUNNING |
| CREATED | CancelInvestigation | CANCELLED |
| CREATED | FailStart | FAILED |
| RUNNING | ContinueInvestigation | RUNNING |
| RUNNING | FinalizeWithoutResponse | COMPLETED |
| RUNNING | RequestResponseApproval | WAITING_APPROVAL |
| RUNNING | CancelInvestigation | CANCELLED |
| RUNNING | FailInvestigation | FAILED |
| WAITING_APPROVAL | RejectResponse | COMPLETED |
| WAITING_APPROVAL | ApproveResponse | EXECUTING_RESPONSE |
| WAITING_APPROVAL | CancelInvestigation | CANCELLED |
| EXECUTING_RESPONSE | ObserveTerminalExecution | COMPLETED |

未列出的状态转换全部非法。终态 `COMPLETED / FAILED / CANCELLED` 不得恢复。

## 37. Status 操作约束

| Status | 允许操作 |
|---|---|
| CREATED | Start、Cancel |
| RUNNING | Read Tool、Add Evidence、Revise Plan、Assess Hypothesis、Create Finding、Finalize、Cancel |
| WAITING_APPROVAL | Approve、Reject、Cancel、Read |
| EXECUTING_RESPONSE | Observe Execution、Read |
| COMPLETED | Read only |
| FAILED | Read only |
| CANCELLED | Read only |

进入 EXECUTING_RESPONSE 后，`Cancel Investigation` 不再允许。未来如需取消 SOAR Execution，必须提供独立业务命令。

## 38. FAILED 与 INCONCLUSIVE

FAILED 表示系统未能完成有效 Investigation。

INCONCLUSIVE 表示 Investigation 正常结束，但 Evidence 不足。

因此 `COMPLETED + INCONCLUSIVE` 是合法且常见的结果。Budget Exhaustion、部分数据源不可用、Evidence 冲突等优先转化为 INCONCLUSIVE，而不是 FAILED。

## 39. Response Execution Failure

SOAR Execution Failure 不改变 Investigation Result。

合法结果：

```text
Investigation.status = COMPLETED
InvestigationResult.verdict = MALICIOUS
ResponseExecution.status = FAILED
```

## 40. 并发控制

Investigation 与 ResponseProposal 必须具有 `revision`。

涉及状态变化的命令采用 Optimistic Concurrency。并发冲突不得使用 Last-write-wins。

审批必须绑定 `proposal_id + proposal_revision + proposal_content_hash`。

## 41. 数据可信模型

系统采用两个独立维度：`Provenance Authority` 与 `Instruction Trust`。

## 42. Provenance Authority

来源分类：

```text
PLATFORM_AUTHORITY
SYSTEM_AUTHORITY
HUMAN_AUTHORITY
EXTERNAL_EVIDENCE
MODEL_DERIVED
```

- PLATFORM_AUTHORITY：HISIEM Tenant、User identity、Alert、Event、Case、SOAR state。
- SYSTEM_AUTHORITY：Copilot IDs、timestamps、status、revision、budget、policy decision、hashes。
- HUMAN_AUTHORITY：认证用户 Approve、Reject、Cancel。
- EXTERNAL_EVIDENCE：Threat Intelligence、Knowledge documents、MITRE data、Runbook content。
- MODEL_DERIVED：Plan、Hypothesis、Finding Candidate、Verdict Candidate、Confidence、Response Recommendation、Response Action Candidate。

## 43. Instruction Trust

只有 System Policy、Authenticated Human Command、Validated Application Command 可以形成 Control Command。

以下内容始终属于 `DATA_ONLY`：Alert description、event.original、message、Threat Intel result、RAG document、MITRE text、Runbook text、Tool result、Model output。

> **Data can inform decisions; Data cannot authorize actions.**

## 44. 字段权威来源

| 字段 | 权威来源 | Model 可产生 |
|---|---|---:|
| investigation_id | Copilot System | 否 |
| tenant_id | Authenticated HISIEM Context | 否 |
| initiated_by | Authenticated Principal | 否 |
| source_alert_ref | HISIEM Integration | 否 |
| Alert fields | HISIEM | 否 |
| Event fields | HISIEM | 否 |
| created_at | System Clock | 否 |
| status | Domain State Machine | 否 |
| phase | Runtime Coordinator | 否 |
| budget counters | Runtime | 否 |
| Plan | Model Candidate | 是 |
| Hypothesis | Model Candidate | 是 |
| Evidence provenance | System / Tool Executor | 否 |
| Evidence resource ref | System / Tool Executor | 否 |
| Evidence observation | Source-derived | 不得凭空产生 |
| Evidence summary | Model Transformation | 是 |
| Finding | Model Candidate | 是 |
| Finding evidence_ids | Candidate + deterministic validation | 是 |
| Verdict | Model Candidate | 是 |
| Confidence | Model / Evaluator | 是 |
| ATT&CK candidate | Model / Retrieval | 是 |
| Valid ATT&CK reference | Knowledge Resolver | 否 |
| Response Recommendation | Model | 是 |
| Action Candidate | Model | 是 |
| Allowed Action | Action Registry | 否 |
| Target Candidate | Model | 是 |
| Resolved Target | HISIEM / Resolver | 否 |
| SOAR Playbook | HISIEM Registry | 否 |
| Policy Decision | Policy Engine | 否 |
| Approval Decision | Authenticated Human | 否 |
| Approval Actor | Authentication Context | 否 |
| SOAR Execution ID | HISIEM SOAR | 否 |
| SOAR Execution Status | HISIEM SOAR | 否 |

## 45. Model Output 处理规则

所有 LLM 输出均为 Candidate。

```text
LLM Candidate
      ↓
Schema Validation
      ↓
Domain Validation
      ↓
Reference Resolution
      ↓
Policy Validation
      ↓
Human Approval
```

Structured Output 仅保证结构合法，不代表业务语义可信。

## 46. Tool Call 参数处理规则

模型不得控制 tenant_id、actor identity、authorization scope、resource ownership。

Tool 执行流程：

```text
Model Tool Arguments
        ↓
Schema Validation
        ↓
Authenticated Scope Binding
        ↓
Resource Scope Validation
        ↓
Budget / Policy
        ↓
Execution
```

## 47. Evidence 生成路径

```text
Tool / External Source
        ↓
Raw Result
        ↓
Evidence Normalizer
        ↓
Evidence
        ├── provenance
        ├── source reference
        ├── observation
        └── optional summary
```

LLM 不得凭空创建 Evidence。

## 48. ATT&CK Mapping

LLM 或 Retrieval 可以产生 Technique Candidate。正式保存前必须经过 Knowledge Resolver 校验 framework、technique_id、name、version/source。无效 Technique ID 不得进入最终 Investigation Result。

## 49. Runtime 与 Domain 交互

Graph Node 不得直接修改业务持久状态。

```text
Graph Node
    ↓
Application Command
    ↓
Domain Aggregate / Domain Service
    ↓
Invariant Validation
    ↓
Repository
```

Graph Edge 负责流程选择；Domain 层负责业务合法性。

## 50. Investigation 与 LangGraph 边界

`Investigation != LangGraph Thread`。

必须存在独立绑定：

```text
OrchestrationBinding

investigation_id
runtime
thread_id
```

LangGraph Thread ID 属于基础设施标识，不得成为 Domain ID。

## 51. LangGraph State

Graph State 只保存跨步骤需要持续存在的 Working State。

建议：

```text
InvestigationGraphState

investigation_id
alert_context
current_plan
hypothesis_working_set
evidence_ids
iteration
budget_remaining
pending_tool_request?
result_candidate?
response_candidate?
```

formatted prompt、HTML、UI text、duplicate derived values、full Domain database copy 不得作为 Canonical State。

## 52. Persistence 边界

数据库应区分三类持久数据：

```text
Domain Tables
────────────────
investigation
plan_revision
hypothesis
hypothesis_assessment
evidence
finding
investigation_result
response_proposal
approval_request
approval_decision

Runtime Tables
────────────────
LangGraph checkpoints
orchestration binding

Operational Tables
────────────────
tool_invocation
agent_event
trace metadata
```

不得使用单一 `agent_run_state JSONB` 替代完整领域模型。

## 53. Mutable / Immutable 边界

Mutable：Investigation.status、Investigation.phase、Investigation.revision、Hypothesis.current_status、Hypothesis.assessment_revision、ResponseProposal.status、ResponseProposal.revision。

Append-only / Immutable：PlanRevision、Evidence、HypothesisAssessment、Finding、InvestigationResult、ApprovalDecision、Tool execution record、Investigation event。

## 54. V1 明确不建立的领域对象

V1 不定义：AgentRun、GenericTask、ChatSession、Conversation、AgentMessage、Memory、MultiAgentSession、CaseInvestigation、ThreatHunt。

项目核心业务对象统一为 `Investigation`。

## 55. V1 领域基线总结

```text
Investigation
    │
    ├── PlanRevision
    ├── Evidence
    ├── Hypothesis
    ├── Finding
    └── InvestigationResult
              │
              ▼
       ResponseProposal
              │
              ▼
          Approval
              │
              ▼
        HISIEM SOAR
```

核心责任：

```text
Investigation owns lifecycle
Evidence owns provenance
Domain owns invariants
Policy owns action validity
Human owns approval authority
HISIEM owns platform facts and response execution
LLM owns no authoritative business state
```

> **LLM 可以提出领域变化，但只有经过结构校验、领域校验、引用解析、策略判断和必要的人工授权后，变化才能成为系统事实。**
