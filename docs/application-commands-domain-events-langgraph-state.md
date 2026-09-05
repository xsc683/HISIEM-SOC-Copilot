# HISIEM SOC Copilot V1 — Application Commands, Domain Events & LangGraph State Mapping

## 1. 目的

本文定义 HISIEM SOC Copilot V1 的 Application Command、Domain Event、LangGraph State 与编排边界。

本文建立在以下规范之上：

- `product-positioning.md`
- `v1-user-flow-and-scope.md`
- `domain-model.md`
- `persistence-schema.md`
- `python-package-boundary.md`

核心规则：

```text
Command = 改变业务事实的意图
Query = 读取事实，不改变业务状态
Domain Event = 已经提交的业务事实
LangGraph = 编排运行时，不拥有业务权威
LLM = Candidate producer，不拥有权威业务状态
```

---

## 2. Application / Domain / Runtime 调用关系

所有业务状态修改必须经过：

```text
API / Graph Node / System Trigger
            ↓
     Application Command
            ↓
       Command Handler
            ↓
 Domain Aggregate / Domain Service
            ↓
     Invariant Validation
            ↓
        Persistence
            ↓
       Domain Events
```

读取路径：

```text
API / Graph Node
      ↓
Query / Read Port
      ↓
Repository / HISIEM / External Provider
```

Graph Node 不得直接修改 Domain State、ORM 或数据库。

---

## 3. Trusted Command Context

业务权威上下文必须由已认证的 `TrustedContextProvider` 提供。

可信上下文至少包含：

```text
tenant_id
actor identity
roles / permissions
authorization scope
```

以下字段不得由普通 Request Body、LLM 或 Tool Result 声明为权威事实：

```text
tenant_id
actor identity
role
authorization
approval actor
```

开发/测试 Header Provider 只属于 Adapter，不改变上述信任模型。

---

## 4. Command Metadata

所有修改业务事实的 Command 使用统一元数据：

```text
command_id
idempotency_key
correlation_id
causation_id?
expected_revision?
source
```

`source`：

```text
USER
ORCHESTRATOR
SYSTEM
```

认证 Actor 由 Trusted Context 提供，不作为不可信业务 Payload。

`command_id` 表示一次请求；`idempotency_key` 表示同一个逻辑业务操作，两者不得混用。

---

## 5. Command 幂等规则

所有具有持久化效果或外部 Side Effect 的 Command 必须支持稳定 `idempotency_key`。

重复执行同一个逻辑操作时必须返回已有结果，不得重复创建领域对象或重复执行外部动作。

典型 Key：

```text
investigation:{id}:start
investigation:{id}:plan:{revision}
tool:{tool_invocation_id}:evidence
investigation:{id}:result:{candidate_hash}
proposal:{id}:approval:{content_revision}
proposal:{id}:execute:{content_revision}
```

---

## 6. V1 Command Catalog

### 6.1 API 入口 Command（持久化创建 + 调度）

```text
StartAlertInvestigation
CancelInvestigation
ApproveResponse
RejectResponse
```

`StartAlertInvestigation` 只负责 durable creation + scheduling：校验并写入 Investigation（CREATED）+ Domain Event + Outbox，随后由 Outbox Dispatcher 调度执行。它不直接驱动 Graph / Checkpoint。

### 6.2 Orchestrator / System Command

```text
StartInvestigation
ChangeInvestigationPhase
ReviseInvestigationPlan
RegisterHypotheses
RecordEvidenceBatch
AssessHypotheses
RecordFindings
FinalizeInvestigationResult
CreateResponseProposal
EvaluateResponsePolicy
RequestResponseApproval
SubmitApprovedResponse
RecordResponseExecutionObservation
CompleteInvestigation
CompleteInvestigationAfterResponse
FailInvestigation
```

`StartInvestigation` 是受信 Orchestrator / System Command：由 Durable Runtime（Outbox Dispatcher → Investigation Runner）在 Graph 执行前调用，将 Investigation 从 `CREATED` 桥接为 `RUNNING`。

读取 Alert、Events、Threat Intelligence、Knowledge、SOAR Execution 等操作是 Query / Tool / Port，不是 Domain Command。

---

## 7. Command 规范

### StartAlertInvestigation

输入：

```text
source_alert_ref {provider, resource_type, address_id, business_id?}
```

可选幂等输入：

```text
idempotency_key    → 同一 key 返回同一逻辑结果（Command Receipt）
```

可信信息（Tenant / Actor）来自 Trusted Context。

行为：

```text
validate authorization
→ (短读事务) resolve/check active Investigation
→ 关闭短读事务（不留业务事务跨越 HISIEM HTTP）
→ hydrate authoritative Alert（HISIEM get_alert，无 DB 事务打开）
→ (新事务) re-check active Investigation
→ absent 才创建 Investigation（status=CREATED）
→ 同一事务持久化 InvestigationCreated 事件 + Outbox 消息
→ COMMIT
```

事务边界：

- 任何 HISIEM HTTP（`get_alert`）不得在打开的 DB 业务事务内执行。
- `source_alert_ref` 必须 `provider=hisiem && resource_type=alert`；HISIEM 调用一律使用 `address_id`，绝不从 `business_id` 推断寻址 ID。
- 并发启动收敛：partial unique index（`tenant_id + provider + resource_type + address_id`，active）作为最终并发护栏；hydrate 后 re-check 使并发启动返回既有 Active Investigation，而不是创建第二个。

同一 `tenant + source_alert_ref` 已存在 Active Investigation 时必须返回现有 Investigation，不创建第二个 Active Investigation。

`StartAlertInvestigation` 自身 **不** 直接驱动 Graph。它创建后由 Outbox Dispatcher 调度：Runner 随后执行 `StartInvestigation`（CREATED → RUNNING）并在 Graph 执行前建立 `OrchestrationBinding`。

主要事件：

```text
InvestigationCreated
```

`InvestigationStarted` 由 Runner 的 `StartInvestigation` 在运行时触发，不属于 API 启动命令。

---

### StartInvestigation

受信 Orchestrator / System Command（由 Durable Runner 调用，不属于 API 请求面）。

输入：

```text
investigation_id
tenant_id           → 受信 Orchestrator scope
```

允许状态：

```text
CREATED
```

目标状态：

```text
RUNNING
```

行为：

```text
validate authorization (orchestrator)
→ (短读) 拒绝 terminal Investigation（Domain 优先于 Checkpoint）
→ ensure OrchestrationBinding（investigation_id ↔ 确定性 thread_id）
→ CREATED → RUNNING
→ 编译并执行 / resume Graph（AsyncPostgresSaver checkpointer）
```

事件：

```text
InvestigationStarted
```

`OrchestrationBinding` 在每次 Graph 执行 / resume 前确认存在（idempotent，确定性 `thread_id`），使恢复始终命中同一线程的 Checkpoint。

---

### CancelInvestigation

允许状态：

```text
CREATED
RUNNING
WAITING_APPROVAL
```

目标状态：

```text
CANCELLED
```

`EXECUTING_RESPONSE` 后不得通过 Cancel Investigation 暗示取消 SOAR Execution。

事件：

```text
InvestigationCancelled
```

---

### ChangeInvestigationPhase

仅允许 `RUNNING`。

Phase：

```text
HYDRATING
PLANNING
INVESTIGATING
VERIFYING
FINALIZING
```

Phase 不改变 Investigation Business Status。

事件：

```text
InvestigationPhaseChanged
```

---

### ReviseInvestigationPlan

输入必须为已完成结构校验的 Plan Candidate。

每次修改创建新的 Immutable `PlanRevision`，不得覆盖旧 Revision。

事件：

```text
InvestigationPlanRevised
```

---

### RegisterHypotheses

注册 Hypothesis Candidate，初始状态为 `OPEN`。

不得未经 Assessment 直接创建 `SUPPORTED` 或 `CONTRADICTED` Hypothesis。

事件：

```text
HypothesisRegistered
```

---

### RecordEvidenceBatch

输入必须来自 Evidence Normalizer，而不是模型直接生成的 Evidence。

必须验证：

```text
Investigation = RUNNING
provenance exists
source references valid
tool invocation belongs to the Investigation when present
deduplication passes
```

一个 Tool Result 可以产生 `0..N Evidence`。

事件：

```text
EvidenceRecorded
```

---

### AssessHypotheses

必须验证 Hypothesis 和引用 Evidence 均存在且属于同一 Investigation。

`SUPPORTED` / `CONTRADICTED` Assessment 必须包含 EvidenceRelation。

Assessment 采用 Immutable Revision History。

事件：

```text
HypothesisAssessed
```

---

### RecordFindings

Finding Candidate 必须引用至少一个有效 Evidence，且所有 Evidence 属于同一 Investigation。

创建后 Finding Immutable。

事件：

```text
FindingRecorded
```

---

### FinalizeInvestigationResult

仅允许 `RUNNING`。

规则：

```text
MALICIOUS / BENIGN
→ 至少一个 grounded Finding

INCONCLUSIVE
→ 至少一个 Uncertainty / Missing Evidence explanation

ATT&CK mapping
→ 必须完成知识引用解析与验证
```

生成 Immutable `InvestigationResult`。

Finalize Result 本身不一定完成 Investigation；后续可能进入 Response Proposal / Approval。

事件：

```text
InvestigationResultFinalized
```

---

### CreateResponseProposal

前置条件：Finalized InvestigationResult 已存在。

必须完成：

```text
action registry resolution
target resolution
same-tenant validation
parameter validation
supporting Evidence validation
```

模型提供的 Action / Target / Parameters 仍然只是 Candidate。

事件：

```text
ResponseProposalCreated
```

---

### EvaluateResponsePolicy

必须由确定性 Policy Engine 执行。

V1 仅允许：

```text
DENY
REQUIRE_APPROVAL
```

V1 不允许 `ALLOW_AUTOMATIC`。

事件：

```text
ResponsePolicyEvaluated
```

---

### RequestResponseApproval

前置条件：

```text
ResponseProposal exists
PolicyDecision = REQUIRE_APPROVAL
Investigation = RUNNING
```

ApprovalRequest 必须绑定：

```text
proposal_id
proposal_content_revision
proposal_content_hash
```

状态：

```text
RUNNING → WAITING_APPROVAL
```

事件：

```text
ResponseApprovalRequested
```

---

### ApproveResponse

必须验证：

```text
Investigation = WAITING_APPROVAL
actor authenticated and authorized
same tenant
ApprovalRequest pending
Proposal revision/hash unchanged
no previous ApprovalDecision
```

创建 Immutable `ApprovalDecision(APPROVE)`。

状态：

```text
WAITING_APPROVAL → EXECUTING_RESPONSE
```

事件：

```text
ResponseApproved
```

---

### RejectResponse

与 Approve 使用相同的身份、租户和 Proposal 版本校验。

创建 Immutable `ApprovalDecision(REJECT)`。

状态：

```text
WAITING_APPROVAL → COMPLETED
```

InvestigationResult 不得因 Reject 修改。

事件：

```text
ResponseRejected
InvestigationCompleted
```

---

### SubmitApprovedResponse

这是 V1 具有外部 Side Effect 的核心 Application Command。

前置条件：

```text
ApprovalDecision = APPROVE
Investigation = EXECUTING_RESPONSE
Proposal revision/hash remains valid
```

执行：

```text
resolve HISIEM SOAR Playbook
→ build validated execution request
→ submit with stable idempotency key
→ persist ResponseExecutionRef
```

稳定 Submission Key：

```text
proposal:{proposal_id}:execute:{content_revision}
```

重复执行必须恢复/返回同一个 SOAR Execution，不得产生第二次实际响应。

事件：

```text
ResponseExecutionSubmitted
```

---

### RecordResponseExecutionObservation

SOAR Execution 的 Source of Truth 始终是 HISIEM。

Copilot 只更新 `ResponseExecutionRef` Projection；仅在观察状态变化时产生：

```text
ResponseExecutionStatusChanged
```

SOAR 到达 Terminal State 后可以触发 `CompleteInvestigationAfterResponse`。

---

### CompleteInvestigation

允许用于：

```text
Final Result 无可执行 Response
或
ResponseProposal 被 Policy DENY
```

状态：

```text
RUNNING → COMPLETED
```

事件：

```text
InvestigationCompleted
```

---

### CompleteInvestigationAfterResponse

前置条件：

```text
Investigation = EXECUTING_RESPONSE
ResponseExecutionRef exists
SOAR Execution observed terminal
```

状态：

```text
EXECUTING_RESPONSE → COMPLETED
```

SOAR `FAILED` 仍允许 Investigation 正常完成。

事件：

```text
InvestigationCompleted
```

---

### FailInvestigation

仅用于无法完成有效 Investigation 的不可恢复系统/领域错误。

允许来源状态：

```text
CREATED
RUNNING
```

不得用于表示：

```text
Evidence insufficient
Budget exhausted
single Tool unavailable
SOAR execution failed
```

事件：

```text
InvestigationFailed
```

---

## 8. Query / Read Port Catalog

典型 Query：

```text
GetInvestigation
GetInvestigationWorkspace
GetEvidence
GetFindings
GetAlert
SearchEvents
GetRelatedEvents
GetEntityContext
LookupThreatIntel
RetrieveKnowledge
ResolveAttackTechnique
GetAvailableSoarPlaybooks
GetSoarExecution
```

所有 Tenant-owned Query 必须由可信上下文绑定 Tenant Scope。

Tool Call 本身不是 Domain Command。

---

## 9. Domain Event 规范

Domain Event 表示已经成功提交并成为系统事实的业务变化。

Event 命名必须使用过去时。

统一 Envelope：

```text
event_id
event_type
event_version
aggregate_type
aggregate_id
aggregate_revision
tenant_id
occurred_at
correlation_id
causation_id?
actor_ref?
payload
```

Payload 只保存最小业务事实、IDs、状态变化和安全摘要。

不得保存：

```text
full raw logs
full Tool Result
full prompts
Chain-of-Thought
credentials / secrets
```

---

## 10. V1 Domain Event Catalog

Investigation：

```text
InvestigationCreated
InvestigationStarted
InvestigationPhaseChanged
InvestigationPlanRevised
HypothesisRegistered
HypothesisAssessed
EvidenceRecorded
FindingRecorded
InvestigationResultFinalized
InvestigationCompleted
InvestigationCancelled
InvestigationFailed
```

Response：

```text
ResponseProposalCreated
ResponsePolicyEvaluated
ResponseApprovalRequested
ResponseApproved
ResponseRejected
ResponseExecutionSubmitted
ResponseExecutionStatusChanged
```

---

## 11. Domain Event Persistence

V1 不使用 Event Sourcing。

Aggregate / Entity 表是业务事实 Source of Truth；Domain Events 用于：

```text
Audit Timeline
SSE / projection input
orchestration trigger
observability correlation
transactional outbox
```

业务状态、Domain Event 与 Outbox Message 必须在同一事务内持久化。

```text
BEGIN
→ persist domain changes
→ insert domain_event
→ insert outbox_message
COMMIT
```

事件消费者使用 `event_id` 幂等去重。

---

## 12. Command → Event 映射

| Command | 主要 Event |
|---|---|
| StartAlertInvestigation | InvestigationCreated |
| StartInvestigation | InvestigationStarted |
| CancelInvestigation | InvestigationCancelled |
| ChangeInvestigationPhase | InvestigationPhaseChanged |
| ReviseInvestigationPlan | InvestigationPlanRevised |
| RegisterHypotheses | HypothesisRegistered |
| RecordEvidenceBatch | EvidenceRecorded |
| AssessHypotheses | HypothesisAssessed |
| RecordFindings | FindingRecorded |
| FinalizeInvestigationResult | InvestigationResultFinalized |
| CreateResponseProposal | ResponseProposalCreated |
| EvaluateResponsePolicy | ResponsePolicyEvaluated |
| RequestResponseApproval | ResponseApprovalRequested |
| ApproveResponse | ResponseApproved |
| RejectResponse | ResponseRejected, InvestigationCompleted |
| SubmitApprovedResponse | ResponseExecutionSubmitted |
| RecordResponseExecutionObservation | ResponseExecutionStatusChanged |
| CompleteInvestigation | InvestigationCompleted |
| CompleteInvestigationAfterResponse | InvestigationCompleted |
| FailInvestigation | InvestigationFailed |

---

## 13. LangGraph 职责

LangGraph 只负责：

```text
orchestration
branching
loop
checkpoint
retry coordination
human pause / resume
```

LangGraph 不负责：

```text
Domain ownership
Tenant / RBAC authority
Domain invariants
Evidence validity
Policy authority
Approval authority
SOAR execution ownership
```

Graph Edge 决定下一步编排；Domain 决定业务变化是否合法。

---

## 14. Graph Input / Runtime Identity

Graph 外部输入保持最小：

```text
InvestigationGraphInput
- investigation_id
```

禁止从 Graph Input 接受以下权威事实：

```text
tenant_id
actor / role
authorization
full alert payload
approval result
```

Investigation 与 LangGraph Thread 必须独立：

```text
Investigation.id != LangGraph thread_id
```

通过 `OrchestrationBinding` 映射：

```text
investigation_id
thread_id
graph_name
graph_version
state_schema_version
```

---

## 15. InvestigationGraphState

Graph State 使用 bounded structured state，推荐 `TypedDict`。

规范字段：

```text
schema_version
investigation_id
investigation_revision
alert_context
plan_revision_id
iteration
budget
pending_tool_request
last_tool_invocation_id
last_tool_error
new_evidence_ids
assessment
result_id
response_proposal_id
proposal_revision
approval_request_id
response_execution_id
stop_reason
```

### 关键字段语义

`investigation_id`
: 唯一必需 Domain Reference。

`investigation_revision`
: Optimistic Concurrency 使用的运行时快照，不替代 Domain 状态。

`alert_context`
: 从 HISIEM 获取的有界、标准化运行时快照，不是 Source of Truth。

`plan_revision_id`
: 当前持久化 Plan Revision 引用，不复制完整 Plan History。

`budget` → 拆分为确定性剩余计数（Runtime Authority，逐节点消耗，随 Checkpoint 持久化）

```text
budget_remaining_steps
budget_remaining_tool_calls
budget_remaining_llm_calls
budget_deadline_at        # max_duration_seconds 派生的 UTC epoch 秒
```

- FRESH 运行由 Aggregate 的 `BudgetLimits` 播种为满额；resume 保留 Checkpoint 中的剩余计数 —— 进程崩溃 / 重启不会把预算错误重置为满额（从而获得无限调用）。
- `max_llm_tokens` 保留给真实 provider 的 token 计量；本轮确定性上限为 `max_llm_calls`。
- 预算由 Runtime 权威执行；模型 / Graph Node 不能上调。
- 耗尽 / deadline → finalize 已有事实 → COMPLETED + INCONCLUSIVE（非 FAILED）。

`new_evidence_ids`
: 仅表示本轮 Evidence Delta，不保存完整 Evidence Collection。

`assessment`
: 仅保存下一步路由所需的结构化结果，例如 `CONTINUE` / `FINALIZE`。

`result_id` / `response_proposal_id` / `approval_request_id` / `response_execution_id`
: 只保存持久化 Domain / External Projection Reference。

---

## 16. 禁止进入 Graph State 的内容

```text
System Prompt
Formatted Prompt
Full LLM conversation history
Chain-of-Thought
Full Tool Result
Full Evidence collection
Full Domain Aggregate
Full Domain Event history
HTML / UI representation
Authentication token
API key
credentials
model-supplied authorization claims
```

V1 不使用 `MessagesState` 作为核心 Investigation State。

Prompt 必须根据 Domain Data、bounded working context 与 Prompt Template 按需构造。

---

## 17. State Reducer 规则

大多数字段采用 overwrite 语义。

以下集合不得简单使用 append-only reducer：

```text
evidence IDs
tool calls
events
findings
```

因为 checkpoint retry / resume 可能重复执行节点。

需要合并引用时必须按稳定 ID 去重。

完整业务集合的 Source of Truth 始终是 Domain Repository。

---

## 18. Graph Output

Graph Output 只用于 Runtime 调用方：

```text
investigation_id
domain_status
result_id?
response_proposal_id?
response_execution_id?
```

产品 UI 不依赖 Graph Output，必须通过 `GetInvestigationWorkspace` 获取 Domain Read Model。

---

## 19. V1 Graph 逻辑结构

Read-only 前缀（本阶段实现）：

```text
START
  ↓
load_investigation        ← 绑定 RUNNING；FRESH 运行播种运行期预算，resume 保留已消耗预算
  ↓
hydrate_alert             ← HISIEM get_alert（无 DB 事务跨越 HTTP）
  ↓
plan                      ← 消耗 1 次 LLM-Call 预算
  ↓
decide_next               ← 每次咨询消耗 1 次 LLM-Call；CONTINUE 消耗 1 step + 1 tool-call
  │
  ├── execute_and_ingest  ← 运行 1 个白名单只读工具 + 审计 + 归一化 Evidence
  │         │
  │         └────────────→ decide_next        （loop）
  │
  └── assess              ← convergence：AssessHypotheses + RecordFindings（消耗 1 次 LLM-Call）
          ↓
     finalize_result      ← verdict（消耗最后 1 次 LLM-Call / deadline 到达则确定性 INCONCLUSIVE）
          ↓
     complete             ← RUNNING → COMPLETED（read-only 无 executable response）
          ↓
        END
```

预算规则：

```text
max_steps / max_tool_calls / max_llm_calls / max_duration_seconds
= Runtime Authority，确定性执行，Checkpoint 持久化，resume 不重置
max_llm_tokens          = 保留给真实 provider token 计量（本轮不确定执行）

exhaustion / deadline   → finalize available facts → COMPLETED + INCONCLUSIVE
绝不默认 FAILED
```

`execute_and_ingest` 在单个 Checkpoint 步内执行工具并写入其归一化 Evidence：crash mid-node 重跑整个节点，Tool 审计（by-key）与 Evidence（by dedup key）均幂等 —— checkpointed resume 既不丢也不重复。

后续阶段追加 Response 分支（V1 之外的 Response 提议 / Approval 流程）：

---

## 20. Graph Node → Application Mapping

| Graph Node | Application / Query |
|---|---|
| load_investigation | GetInvestigation + 运行期预算播种 / resume 保留 |
| hydrate_alert | GetAlert / related context queries |
| plan | ChangeInvestigationPhase, ReviseInvestigationPlan, RegisterHypotheses（消耗 1 LLM-Call） |
| decide_next | LLM structured candidate only（每次咨询消耗 1 LLM-Call） |
| execute_and_ingest | ToolExecutor / Query Port + RecordEvidenceBatch（单节点执行工具 + 审计 + Evidence） |
| assess | ChangeInvestigationPhase, AssessHypotheses, RecordFindings（消耗 1 LLM-Call） |
| finalize_result | ChangeInvestigationPhase, FinalizeInvestigationResult（verdict 消耗最后 1 LLM-Call；deadline 到达则确定性 INCONCLUSIVE） |
| complete | CompleteInvestigation |
| request_approval | RequestResponseApproval |
| wait_approval | LangGraph interrupt only |
| load_approval | Query persisted ApprovalDecision |
| submit_response | SubmitApprovedResponse |

Graph Node 必须保持 Thin，不直接执行 SQL、ORM Update、Domain 字段修改或绕过 Application Layer。

---

## 21. Agentic / Deterministic Boundary

LLM 可以产生：

```text
Plan Candidate
next-step Candidate
Hypothesis Candidate
Finding Candidate
Verdict Candidate
Response Recommendation / Action Candidate
```

确定性代码负责：

```text
schema validation
trusted scope binding
tool execution
evidence normalization
reference resolution
domain validation
policy
authorization
approval persistence
SOAR submission
state transition
persistence
```

标准模型输出处理链：

```text
LLM Candidate
→ Schema Validation
→ Domain Validation
→ Reference Resolution
→ Policy Validation
→ Human Approval when required
```

Structured Output 只保证结构，不代表语义权威。

---

## 22. Tool 调用边界

读取型 Tool 的标准执行链：

```text
Model Tool Candidate
→ Schema Validation
→ Trusted Tenant / Resource Scope Binding
→ Permission / Budget / Tool Policy
→ Execute
→ Raw Tool Result
→ Evidence Normalizer
→ Evidence
```

模型不得自由决定 Tenant、Actor 或 Authorization Scope。

Tool Result 始终是 Data，不是 Instruction。

---

## 23. Human-in-the-loop

Approval 必须拆为：

```text
request_approval
→ wait_approval
→ load_approval
```

### request_approval

通过 `RequestResponseApproval` 持久化正式 `ApprovalRequest`。

### wait_approval

只执行 LangGraph interrupt，不创建 Domain Fact，不产生 Side Effect。

### Human API

```text
Authenticated HTTP Request
→ TrustedContext
→ ApproveResponse / RejectResponse
→ persist immutable ApprovalDecision
→ emit Domain Event
```

### Resume

Graph Resume Payload 只能携带引用，例如：

```text
approval_request_id
decision_id
```

Resume 后必须重新读取持久化 `ApprovalDecision`。

不得直接信任 Resume Payload 中的 `approved=true` 作为授权事实。

---

## 24. Approval Recovery

如果 ApprovalDecision 已提交而 Graph Resume 失败：

```text
Domain Fact remains valid
```

恢复逻辑可以依据：

```text
Investigation = WAITING_APPROVAL
+
ApprovalRequest already decided
```

重新 Resume。

重复 Resume 必须读取同一 Immutable Decision，不产生重复审批。

---

## 25. Side Effect Recovery

`SubmitApprovedResponse` 必须可安全重试。

逻辑：

```text
check existing ResponseExecutionRef
  ├── exists → return existing
  └── absent
       ↓
submit using stable submission_key
       ↓
persist execution reference
```

如果 SOAR 已接受请求但 Copilot 在保存引用前崩溃，重试必须使用相同 Submission Key 恢复同一 Execution。

---

## 26. Error 与 Budget 语义

### Transient Infrastructure Error

有限重试，不直接改变 Domain Verdict。

### Recoverable LLM / Tool Error

结构化反馈给编排层，允许 bounded retry / re-plan。

### Policy / Authorization Error

确定性拒绝，不允许模型尝试绕过。

### Unexpected Unrecoverable Error

无法完成有效 Investigation 时才调用 `FailInvestigation`。

### Budget Exhaustion

Runtime Authority 确定性执行：

```text
max_steps
max_tool_calls
max_llm_calls        # 本轮确定性 LLM 调用上限（convergence 需要 2 个固定调用，会预留给 assess + verdict）
max_duration_seconds # 派生出 wall-clock deadline；deadline 到达后不再执行工具、不再发起模型咨询
```

耗尽 / deadline 时：

```text
stop investigation loop
→ finalize available facts
→ COMPLETED + INCONCLUSIVE
```

而不是默认 `FAILED`。

Checkpoint restart / resume 后预算不被重置为满额（剩余计数随 Checkpoint 持久化），因此无法通过反复重启获得无限调用。`max_llm_tokens` 保留给真实 provider token 计量，不参与确定性执行。

---

## 27. Checkpoint 与 Domain Reconciliation

Checkpoint 只用于：

```text
crash recovery
Agent loop resume
human approval pause/resume
runtime inspection
```

Checkpoint 不是 Domain Source of Truth。

任何 Resume 首先重新读取 Investigation Domain State。

建议 Reconciliation：

```text
COMPLETED / FAILED / CANCELLED
→ END

WAITING_APPROVAL
→ wait/load persisted approval

EXECUTING_RESPONSE + no execution ref
→ submit_response

EXECUTING_RESPONSE + execution ref exists
→ END

RUNNING
→ continue orchestration
```

Domain 与 Checkpoint 冲突时，Domain State 优先。

---

## 28. Graph Versioning

Graph State 必须包含：

```text
schema_version
```

`OrchestrationBinding` 必须保存：

```text
graph_version
state_schema_version
```

部署新 Graph 时必须显式判断既有 Checkpoint 是否兼容，不能假设所有运行中 Checkpoint 可被新版本直接加载。

---

## 29. Trace / Domain Event / Timeline 分离

```text
Trace
= 技术执行细节

Domain Event
= 已提交的业务事实

Timeline
= 面向用户的业务投影视图
```

LLM Call 通常只进入 Trace。

Evidence 创建可以同时产生 Trace、`EvidenceRecorded` Event 和 Timeline Item。

Prompt 和私有模型推理不得进入 Domain Event 或用户 Timeline。

---

## 30. 最终约束

```text
Commands express intent.
Queries read facts.
Aggregates own invariants.
Domain Events describe committed facts.
LangGraph owns orchestration only.
Graph State owns bounded working state only.
Checkpoint owns runtime recovery only.
LLM output is candidate only.
Authenticated human owns ApprovalDecision.
HISIEM owns platform facts and SOAR execution.
```

最终控制链：

```text
LLM Candidate
→ Graph Node
→ Application Command
→ Domain Validation
→ Persisted Domain Fact
→ Domain Event
```

涉及 Side Effect：

```text
Model Recommendation
→ ResponseProposal
→ Policy
→ ApprovalRequest
→ Authenticated Human Decision
→ Idempotent SubmitApprovedResponse
→ HISIEM SOAR
```

> Graph 可以决定下一步尝试做什么，但只有 Application + Domain 才能决定什么能够成为系统事实。
