# HISIEM SOC Copilot — V1 核心用户流程与功能边界

## 1. 文档目的

本文定义 HISIEM SOC Copilot V1 的核心用户旅程、Investigation 生命周期、产品能力边界、HISIEM / SOC Copilot / SOAR 职责划分，以及 V1 的验收标准。

本文作为后续领域模型、API 契约、交互设计和系统架构的产品基线。

## 2. V1 核心业务目标

V1 仅解决一个完整业务场景：**Security Alert Investigation**。

SOC Analyst 从 HISIEM Alert 发起调查，SOC Copilot 自动获取权威上下文、规划调查、调用读取型安全能力收集 Evidence、验证安全假设并形成 Investigation Result；如需要响应，则生成 Response Proposal，经人工审批后交由 HISIEM SOAR 执行。

核心闭环：

```text
HISIEM Alert
    ↓
Start Investigation
    ↓
Load Authoritative Context
    ↓
Plan Investigation
    ↓
Collect Evidence
    ↓
Evaluate Hypotheses
    ↓
Produce Investigation Result
    ↓
Response Needed?
   /            \
 No             Yes
 │               ↓
 │        Response Proposal
 │               ↓
 │        Human Approval
 │          /          \
 │       Reject       Approve
 │          │             ↓
 │          │         HISIEM SOAR
 │          │             ↓
 │          │      Execution Result
 └──────────┴─────────────┘
              ↓
          Completed
```

## 3. 参与者

### 3.1 SOC Analyst

负责：

- 在 HISIEM 中选择 Alert。
- 启动 Investigation。
- 查看 Investigation 状态和进度。
- 查看 Plan、Evidence、Findings、Verdict 和 Uncertainties。
- 查看 Response Proposal。
- 在具有权限时批准或拒绝响应。
- 取消尚未进入响应执行阶段的 Investigation。

### 3.2 Senior SOC Analyst / Incident Responder

负责：

- 审核 Investigation Result。
- 检查关键 Evidence。
- 审核 Response Proposal。
- 批准或拒绝高风险响应。
- 查看 SOAR Execution Result。

V1 不强制要求调查人与审批人为不同用户，但权限模型不得阻碍未来实施职责分离。

### 3.3 HISIEM

HISIEM 是以下对象的业务事实来源：

```text
Event
Alert
Case
Detection Rule
Tenant
User / RBAC
SOAR Playbook
SOAR Execution
Security Platform State
```

### 3.4 HISIEM SOC Copilot

SOC Copilot 负责：

```text
Investigation
Investigation Plan
Evidence
Hypothesis
Finding
Verdict
Uncertainty
Response Proposal
Approval Request
Investigation History
```

### 3.5 HISIEM SOAR

SOAR 负责执行确定性的安全响应流程。

SOC Copilot 不替代 SOAR 执行引擎。

## 4. V1 主入口

V1 唯一主入口：

```text
HISIEM Alert Detail
    ↓
Start Investigation
```

如果当前 Alert 已存在 Active Investigation，则用户应进入已有 Investigation，而不是重复创建新的活动调查。

V1 不提供以下主入口：

```text
Chat
Case
Threat Hunt
Generic Task
Manual Prompt
```

## 5. Investigation 启动规则

启动请求只传递必要的身份上下文和 Alert Reference。

SOC Copilot 必须重新从 HISIEM 获取权威 Alert 数据。

```text
Browser
   ↓
HISIEM
   ↓
Authenticated User
Tenant Context
Alert Reference
   ↓
SOC Copilot
   ↓
Retrieve Authoritative Alert
```

以下内容不得直接由客户端声明为权威事实：

```text
Alert content
Tenant
Actor identity
RBAC permissions
Security state
```

## 6. 核心用户流程

### 6.1 Start

用户：

```text
Open Alert
    ↓
Start Investigation
```

系统创建 Investigation，并进入：

```text
CREATED
    ↓
RUNNING
```

### 6.2 Context Loading

SOC Copilot 获取调查初始上下文，包括：

```text
Alert
Detection Rule
Related Events
Time Range
Source / Destination
User
Host
Existing Security Context
```

此阶段仅建立 Investigation Context，不产生最终 Verdict。

### 6.3 Investigation Planning

SOC Copilot 创建结构化 Investigation Plan。

典型步骤包括：

```text
Validate detection pattern
Search related activity
Check successful authentication
Inspect post-event behavior
Lookup threat intelligence
Retrieve security guidance
Evaluate compromise hypothesis
```

Plan 可以随新的 Evidence 创建新 Revision。

V1 不允许用户直接编辑 Plan。

### 6.4 Evidence Collection

Agent 可以自主调用读取型能力：

```text
Read Alert
Search Events / Logs
Read Related Events
Read Detection Context
Read Entity Context
Lookup Threat Intelligence
Retrieve Security Knowledge
Retrieve MITRE ATT&CK Context
Retrieve Runbook Guidance
```

读取型操作无需逐次人工审批。

每个进入 Investigation 的关键观察必须转换为标准 Evidence，并保留 Provenance。

### 6.5 Hypothesis Evaluation

Agent 围绕明确 Hypothesis 调查。

Hypothesis 根据 Evidence 被更新为：

```text
OPEN
SUPPORTED
CONTRADICTED
UNRESOLVED
```

Evidence 不足时继续调查。

Evidence 已足够或达到终止条件时生成 Investigation Result。

### 6.6 Investigation Result

最终结果必须为结构化对象，至少包含：

```text
Verdict
Confidence
Findings
Evidence
Uncertainties
MITRE ATT&CK Mapping
Response Recommendations
```

关键 Finding 必须引用 Evidence。

系统必须允许 `INCONCLUSIVE`，不得强制产生确定判断。

## 7. Evidence 追溯要求

每条关键 Evidence 必须能够回答：

```text
来源是什么？
对应哪个资源？
何时产生？
实际 Observation 是什么？
哪些 Finding 使用了它？
```

核心关系：

```text
External Observation
        ↓
     Evidence
        ↓
      Finding
        ↓
    Hypothesis
        ↓
      Verdict
```

Finding 可以引用多个 Evidence；Evidence 可以支持多个 Finding 或 Hypothesis。

## 8. Response Recommendation 与 Response Proposal

### 8.1 Response Recommendation

Response Recommendation 是调查结果中的建议，不具备执行能力。

### 8.2 Response Proposal

准备进入执行路径的响应必须形成结构化 Response Proposal：

```text
Action
Target
Parameters
Reason
Supporting Evidence
Risk
```

Response Proposal 必须引用支持其必要性的 Evidence。

## 9. V1 响应安全边界

V1 固定规则：

```text
READ
→ Agent may execute autonomously

WRITE / SIDE EFFECT
→ Human approval required
```

以下操作全部属于 Side Effect：

```text
Block IP
Disable User
Isolate Host
Start Response Playbook
Modify Case State
Modify Alert State
Any External Security Change
```

V1 不提供低风险写操作自动放行。

## 10. Human Approval

进入执行路径的 Response Proposal 必须进入 `WAITING_APPROVAL`。

审批人可以：

```text
APPROVE
REJECT
```

批准后交由 HISIEM SOAR 执行；拒绝后不产生 Side Effect，且不改变已经形成的 Investigation Result。

## 11. SOAR Execution

SOC Copilot 负责：

```text
recommend
propose
explain
request approval
submit approved response intent
observe execution result
```

HISIEM SOAR 负责：

```text
execute
manage workflow
retry
track state
persist execution
record execution result
```

SOC Copilot 不实现第二套响应工作流执行引擎。

## 12. Investigation Result 与 Response Execution Result

二者必须独立。

```text
Investigation Result
= 调查是否完成及其安全判断

Response Execution Result
= 批准后的响应是否成功执行
```

因此允许：

```text
Investigation: COMPLETED
Verdict: MALICIOUS

Response Execution: FAILED
```

Response 执行失败不得改变 Investigation Result。

## 13. Investigation 生命周期

V1 对外状态：

```text
CREATED
RUNNING
WAITING_APPROVAL
EXECUTING_RESPONSE
COMPLETED
FAILED
CANCELLED
```

内部运行阶段使用独立 Phase 表达：

```text
HYDRATING
PLANNING
INVESTIGATING
VERIFYING
FINALIZING
```

Graph Node、LLM Call、Tool Call、RAG Retrieval 等运行时步骤不得进入业务 Status。

## 14. 合法状态转换

```text
CREATED
  ├─ Start → RUNNING
  ├─ Cancel → CANCELLED
  └─ Start Failure → FAILED

RUNNING
  ├─ Continue → RUNNING
  ├─ Finalize without Response → COMPLETED
  ├─ Request Approval → WAITING_APPROVAL
  ├─ Cancel → CANCELLED
  └─ Fatal Failure → FAILED

WAITING_APPROVAL
  ├─ Reject → COMPLETED
  ├─ Approve → EXECUTING_RESPONSE
  └─ Cancel → CANCELLED

EXECUTING_RESPONSE
  └─ Response reaches terminal state → COMPLETED
```

以下状态为终态：

```text
COMPLETED
FAILED
CANCELLED
```

V1 不支持终态恢复。重新调查必须创建新的 Investigation。

## 15. FAILED 与 INCONCLUSIVE

`FAILED` 表示系统没有完成一次有效 Investigation。

`INCONCLUSIVE` 表示 Investigation 已正常完成，但 Evidence 不足以产生确定判断。

因此：

```text
Investigation.status = COMPLETED
Verdict = INCONCLUSIVE
```

Budget Exhaustion、部分数据源不可用、Evidence 冲突等情况优先产生有效的 INCONCLUSIVE Result，而不是 FAILED。

## 16. 用户可见 Investigation Workspace

V1 采用结构化 Investigation Workspace，而不是 Chat-first UI。

核心区域：

```text
Alert Summary
Investigation Status
Investigation Plan
Evidence
Hypotheses
Findings
Verdict
Confidence
Uncertainties
ATT&CK Mapping
Response Proposal
Approval
Response Execution
Investigation Timeline
```

用户可观察：

```text
Plan
Action
Tool Activity
Evidence
Finding
Decision
Result
```

不得展示模型内部私有 Chain-of-Thought。

## 17. V1 用户操作

```text
Start Investigation
Open Investigation
Cancel Investigation
Review Plan
Review Evidence
Review Findings
Review Verdict
Review Uncertainties
Review Response Proposal
Approve Response
Reject Response
View Response Execution
```

## 18. V1 In Scope

### Investigation

```text
Alert-driven Investigation
Authoritative Alert hydration
Single active Investigation per Alert/Tenant
Persistent lifecycle
Cancellation
Failure handling
Budget limits
Progress visibility
```

### Security Investigation

```text
Alert retrieval
Event/log search
Time-window expansion
Entity context retrieval
Threat intelligence lookup
Security knowledge retrieval
MITRE ATT&CK retrieval
Runbook retrieval
Dynamic planning
Hypothesis evaluation
Evidence sufficiency decision
```

### Evidence and Result

```text
Evidence normalization
Evidence provenance
Evidence-resource linkage
Finding-Evidence citation
Hypothesis tracking
Verdict
Confidence
Inconclusive
Uncertainty
ATT&CK Mapping
```

### Response

```text
Response Recommendation
Response Proposal
Policy validation
Approval Request
Approve / Reject
Approved response submission
SOAR Execution observation
```

### Product UX

```text
Investigation Workspace
Status / Phase
Plan
Evidence View
Finding View
Verdict View
Approval View
Response Execution View
Timeline
```

### Auditability

关键业务事件必须可追踪：

```text
investigation created
investigation started
tool operation performed
evidence created
plan revised
hypothesis assessed
finding created
result finalized
response proposed
approval requested
approval decided
response submitted
response completed
investigation completed
investigation cancelled
investigation failed
```

## 19. V1 Out of Scope

```text
General Chat
General Security Q&A
Case-driven Investigation
Threat Hunting
Multi-alert Investigation
Cross-incident Correlation
Malware Analysis
Vulnerability Investigation
Autonomous Response
Automatic Alert Closure
Direct Shell / Script Execution
Direct Endpoint / Firewall / IAM Control
Full Case Management
Multi-analyst Collaborative Editing
Complex Multi-level Approval
Cross-investigation Learning
Long-term User Profiling
Automatic Policy Learning
Multi-SIEM Support
Multi-SOAR Support
Cross-tenant Investigation
Complex Multi-Agent SOC
```

## 20. 异常流程

- Alert 不存在或不可访问：`FAILED`，不得启动无权威源的调查。
- 权限不足：拒绝操作，不得通过 Copilot 绕过 HISIEM Tenant / RBAC。
- 单一 Tool / Data Source 失败：记录失败；如果其他 Evidence 足以继续则继续，否则允许最终 `INCONCLUSIVE`。
- Evidence 冲突：保留冲突 Evidence，并在 Result 中产生 Uncertainty。
- Budget Exhausted：停止继续调查，保留已有 Evidence，优先产生 `COMPLETED + INCONCLUSIVE`。
- 用户取消：保留已有调查记录和 Evidence，状态进入 `CANCELLED`。
- Approval Reject：不执行 Side Effect，Investigation Result 保留。
- SOAR Failure：Investigation 仍可 `COMPLETED`，Response Execution 单独记录 `FAILED`。

## 21. V1 代表性验收场景

首个端到端代表性场景：

```text
SSH Brute Force
    ↓
Possible Account Compromise
```

典型流程：

```text
HISIEM Alert
    ↓
Start Investigation
    ↓
Search authentication failures
    ↓
Search successful authentication
    ↓
Lookup source reputation
    ↓
Inspect post-login activity
    ↓
Retrieve ATT&CK / Runbook
    ↓
Assess compromise hypotheses
    ↓
Grounded Findings
    ↓
Verdict
    ↓
Response Proposal
    ↓
Human Approval
    ↓
HISIEM SOAR
    ↓
Execution Result
```

最终 Workspace 必须能够回答：

```text
发生了什么？
为什么形成该判断？
Evidence 在哪里？
哪些问题仍未确认？
建议执行什么响应？
谁批准或拒绝了响应？
SOAR 最终执行结果是什么？
```

## 22. V1 Definition of Done

V1 只有同时满足以下条件才视为完成：

1. 可以从真实 HISIEM Alert 启动 Investigation。
2. Copilot 根据 Alert Reference 获取权威数据。
3. 同一 Tenant + Alert 同时最多存在一个 Active Investigation。
4. Agent 可以自主执行多个读取型调查步骤。
5. Agent 可以根据 Evidence 动态继续或终止调查。
6. 关键 Finding 必须可追溯到 Evidence。
7. Evidence 必须保留 Provenance。
8. 系统支持 INCONCLUSIVE。
9. Investigation Result 为结构化结果。
10. Response Proposal 必须有 Evidence 支持。
11. 所有 V1 Side Effect 必须人工审批。
12. Approved Response 由 HISIEM SOAR 执行。
13. Investigation Result 与 Response Execution Result 独立记录。
14. Investigation 生命周期和关键事件可观察。
15. Investigation 可在合法阶段取消。
16. Tenant / RBAC 边界不得被 Copilot 绕过。
17. SSH Brute Force → Account Compromise 场景能够端到端运行。
18. 整个闭环能够被审计和复现。

## 23. V1 核心约束

```text
Investigate
→ autonomous read-only investigation

Explain
→ evidence-grounded result

Recommend
→ structured response proposal

Control
→ human approval before any side effect
```

> **SOC Copilot 可以自主调查，但不得自主改变安全环境。**
