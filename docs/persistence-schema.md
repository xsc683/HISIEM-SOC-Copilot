# HISIEM SOC Copilot V1 — Persistence Schema

## 1. 目的

本文将既有 Product、Domain、Application Command、Domain Event 与 LangGraph State 设计映射为 V1 持久化模型，并定义数据库所有权、事务、迁移、并发与恢复约束。

核心原则：

```text
Domain facts are durable.
Runtime state is replaceable.
External platform facts remain external.
Persistence does not leak into domain.
LangGraph does not become the domain model.
```

---

## 2. PostgreSQL 边界

V1 使用 PostgreSQL，并划分两个独立 Schema：

```text
PostgreSQL
├── copilot
│   ├── Domain Tables
│   ├── Application Tables
│   └── Operational Tables
└── langgraph_checkpoint
    └── LangGraph-owned checkpoint tables
```

- `copilot`：由 HISIEM SOC Copilot 所有，使用 Alembic 管理。
- `langgraph_checkpoint`：由 LangGraph PostgreSQL checkpointer 所有，由 LangGraph 自身 setup/migration 机制管理。

两者可以位于同一 PostgreSQL Database，但必须使用独立 schema、独立连接池和独立迁移所有权。

禁止 Alembic 管理 LangGraph checkpoint tables，也禁止 LangGraph 管理 Copilot domain tables。

---

## 3. PostgreSQL Driver 与 Session

V1 统一采用：

```text
PostgreSQL
+ psycopg 3
+ SQLAlchemy Async
```

Application：

```text
SQLAlchemy AsyncEngine
SQLAlchemy AsyncSession
postgresql+psycopg
```

LangGraph：

```text
AsyncPostgresSaver
psycopg async connection/pool
```

SQLAlchemy Session/Transaction 与 LangGraph Checkpointer Connection 不得共享。

Application Command 的事务单位：

```text
1 Application Command
→ 1 AsyncSession
→ 1 Database Transaction
```

并发 asyncio Task 必须各自使用独立 AsyncSession。

---

## 4. 数据类型规范

### Primary Key

Domain Entity 使用 UUID，由 Application 生成。

`domain_event.sequence` 使用 PostgreSQL `BIGINT GENERATED ALWAYS AS IDENTITY`，作为事件顺序游标。

### Timestamp

统一使用 `TIMESTAMPTZ`，语义为 UTC。

### Enum

V1 不使用 PostgreSQL Native ENUM，采用 `VARCHAR + named CHECK constraint`。

### JSONB

JSONB 仅用于有界、结构化、整体读取且无独立生命周期的 Value Object，例如：

```text
budget_limits
entity_refs
observation
raw_reference
uncertainties
attack_mappings
response_recommendations
response parameters
event payload
tool arguments
```

禁止使用 JSONB 替代 Investigation、Evidence、Hypothesis、Finding、Approval、ResponseProposal 等核心 Entity。

### Hash

内容指纹使用 SHA-256 Hex：`CHAR(64)`。

主要用于 Evidence deduplication、InvestigationResult identity、ResponseProposal approval binding。

### Optimistic Lock

需要并发保护的 Aggregate 使用：

```text
lock_version BIGINT NOT NULL DEFAULT 0
```

更新必须携带 expected version；0 行更新表示并发冲突，应用层映射为 409 Conflict。

---

## 5. `copilot` Schema 表集合

```text
Domain
────────────────────────────
investigation
plan_revision
plan_step
plan_step_state
hypothesis
hypothesis_assessment
hypothesis_assessment_evidence
evidence
finding
finding_evidence
investigation_result
investigation_result_finding
response_proposal
response_proposal_target
response_proposal_evidence
approval_request
approval_decision
response_execution_ref
response_submission

Application / Runtime
────────────────────────────
orchestration_binding
command_receipt

Events / Delivery
────────────────────────────
domain_event
outbox_message

Operational
────────────────────────────
tool_invocation

Knowledge (P3-A)
────────────────────────────
knowledge_document
knowledge_document_version
embedding_profile
knowledge_content_chunk
knowledge_chunk_embedding
attack_release
attack_technique
attack_release_projection
knowledge_chunk
```

---

## 6. `investigation`

```text
id UUID PK

tenant_id TEXT NOT NULL

source_provider VARCHAR(32) NOT NULL
source_resource_type VARCHAR(32) NOT NULL
source_address_id TEXT NOT NULL
source_business_id TEXT NULL

initiated_by_subject TEXT NOT NULL
initiated_by_display_name TEXT NULL

status VARCHAR(32) NOT NULL
phase VARCHAR(32) NULL

current_plan_revision INTEGER NOT NULL DEFAULT 0

budget_limits JSONB NOT NULL
termination_reason VARCHAR(64) NULL

lock_version BIGINT NOT NULL DEFAULT 0
result_id UUID NULL
response_proposal_id UUID NULL

created_at TIMESTAMPTZ NOT NULL
started_at TIMESTAMPTZ NULL
finished_at TIMESTAMPTZ NULL
cancelled_at TIMESTAMPTZ NULL
```

Status（Investigation Analysis Lifecycle）：

```text
CREATED
RUNNING
COMPLETED
FAILED
CANCELLED
```

约束：

```text
CHECK (status IN ('CREATED','RUNNING','COMPLETED','FAILED','CANCELLED'))
```

`WAITING_APPROVAL` / `EXECUTING_RESPONSE` 不属于 Investigation Status。响应工作流
（Proposal → Approval → SOAR Execution）是由 `response_proposal` /
`response_execution_ref` 拥有的**独立后置生命周期**：审批、拒绝与 Provider 执行状态
永不改变 `investigation.status`。

`response_proposal_id` 是 COMPLETED Investigation 到其 ResponseProposal 的单向关联
（one-way link），同样不属于生命周期状态：

```text
至多设置一次
相同 proposal id 重复设置 ⇒ 幂等（不写入）
不同 proposal id ⇒ 确定性冲突（绝不静默覆盖）
设置时不得修改 status / finished_at / termination_reason，不得重开 terminal Investigation
```

V1 固定：

```text
source_provider = hisiem
source_resource_type = alert
```

但 Schema 保留 ExternalResourceRef 的通用表达。

### Active Investigation Constraint

Active Status（仅 Investigation Analysis Lifecycle）：

```text
CREATED
RUNNING
```

数据库必须建立 Partial Unique Index（名称保持 `uq_investigation_active_alert`，Unit of
Work 依赖该名称把 IntegrityError 翻译为确定性 409）：

```sql
CREATE UNIQUE INDEX uq_investigation_active_alert
ON copilot.investigation (
    tenant_id,
    source_provider,
    source_resource_type,
    source_address_id
)
WHERE status IN (
    'CREATED',
    'RUNNING'
);
```

该约束最终保证：

> One Active Investigation per Tenant + Alert.

Application 层仍需提前查询并返回已有 Active Investigation；数据库 Unique Index 处理竞争条件。

主要索引：

```text
(tenant_id, status, created_at DESC)
(tenant_id, source_address_id)
(created_at DESC)
```

---

## 7. `plan_revision`

```text
id UUID PK
investigation_id UUID NOT NULL FK
revision INTEGER NOT NULL
goal TEXT NOT NULL
generator_kind VARCHAR(32) NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

约束：

```text
UNIQUE(investigation_id, revision)
```

PlanRevision immutable，不得 UPDATE 历史 Revision。

---

## 8. `plan_step`

```text
id UUID PK
plan_revision_id UUID NOT NULL FK
step_key TEXT NOT NULL
ordinal INTEGER NOT NULL
objective TEXT NOT NULL
```

约束：

```text
UNIQUE(plan_revision_id, step_key)
UNIQUE(plan_revision_id, ordinal)
```

---

## 9. `plan_step_state`

Plan Definition 与运行状态分离。

```text
plan_step_id UUID PK FK
status VARCHAR(16) NOT NULL
started_at TIMESTAMPTZ NULL
completed_at TIMESTAMPTZ NULL
updated_at TIMESTAMPTZ NOT NULL
lock_version BIGINT NOT NULL DEFAULT 0
```

Status：

```text
PENDING
ACTIVE
COMPLETED
SKIPPED
```

`PlanRevision / PlanStep` 是 immutable definition，`PlanStepState` 是 mutable progress projection。

---

## 10. `hypothesis`

```text
id UUID PK
investigation_id UUID NOT NULL FK
statement TEXT NOT NULL
current_status VARCHAR(32) NOT NULL
assessment_revision INTEGER NOT NULL DEFAULT 0
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

Status：

```text
OPEN
SUPPORTED
CONTRADICTED
UNRESOLVED
```

索引：

```text
(investigation_id, current_status)
```

---

## 11. `hypothesis_assessment`

Immutable Assessment History。

```text
id UUID PK
investigation_id UUID NOT NULL FK
hypothesis_id UUID NOT NULL FK
revision INTEGER NOT NULL
status VARCHAR(32) NOT NULL
reason_summary TEXT NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

约束：

```text
UNIQUE(hypothesis_id, revision)
```

不得 UPDATE。

---

## 12. `hypothesis_assessment_evidence`

```text
assessment_id UUID NOT NULL FK
evidence_id UUID NOT NULL FK
relation VARCHAR(16) NOT NULL
```

Relation：

```text
SUPPORTS
CONTRADICTS
CONTEXT
```

Primary Key：

```text
(assessment_id, evidence_id, relation)
```

Application / Domain 必须额外验证 Hypothesis、Evidence、Assessment 属于同一 Investigation。

---

## 13. `evidence`

核心 Immutable Evidence Ledger。

```text
id UUID PK
investigation_id UUID NOT NULL FK

source_type VARCHAR(32) NOT NULL
source_provider VARCHAR(64) NOT NULL
source_operation VARCHAR(128) NOT NULL

source_resource_provider VARCHAR(32) NULL
source_resource_type VARCHAR(32) NULL
source_resource_address_id TEXT NULL
source_resource_business_id TEXT NULL

source_tool_invocation_id UUID NULL

observed_at TIMESTAMPTZ NULL
collected_at TIMESTAMPTZ NOT NULL

observation JSONB NOT NULL
summary TEXT NULL
raw_reference JSONB NULL
entity_refs JSONB NOT NULL DEFAULT '[]'

content_hash CHAR(64) NOT NULL
dedup_key CHAR(64) NOT NULL
```

约束：

```text
UNIQUE(investigation_id, dedup_key)
```

`source_tool_invocation_id` 是 Operational Reference，不建立对 `tool_invocation` 的强 FK。

`observation` 只保存用于调查的有界事实快照，不复制完整无界日志集合、完整 Tool Response 或 Elasticsearch Response。

`raw_reference` 保存资源/查询/外部检索引用，不承担 SIEM 数据副本职责。

---

## 14. `finding`

```text
id UUID PK
investigation_id UUID NOT NULL FK
statement TEXT NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

Finding immutable。

---

## 15. `finding_evidence`

```text
finding_id UUID NOT NULL FK
evidence_id UUID NOT NULL FK
```

Primary Key：

```text
(finding_id, evidence_id)
```

Domain 规则：Finding 必须至少引用一个 Evidence。

---

## 16. `investigation_result`

每个 Investigation V1 最多一个 Final Result。

```text
id UUID PK
investigation_id UUID NOT NULL UNIQUE FK

verdict_disposition VARCHAR(32) NOT NULL
verdict_summary TEXT NOT NULL
confidence DOUBLE PRECISION NOT NULL

uncertainties JSONB NOT NULL DEFAULT '[]'
attack_mappings JSONB NOT NULL DEFAULT '[]'
response_recommendations JSONB NOT NULL DEFAULT '[]'

content_hash CHAR(64) NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

约束：

```text
0.0 <= confidence <= 1.0
```

Disposition：

```text
MALICIOUS
BENIGN
INCONCLUSIVE
```

该表 immutable。

---

## 17. `investigation_result_finding`

```text
result_id UUID NOT NULL FK
finding_id UUID NOT NULL FK
```

Primary Key：

```text
(result_id, finding_id)
```

该表冻结 Final Result 实际引用的 Findings；后续新增 Finding 不得改变已经 Finalize 的 Result。

---

## 18. `response_proposal`

第二 Aggregate Root。

```text
id UUID PK
investigation_id UUID NOT NULL UNIQUE FK
result_id UUID NOT NULL UNIQUE FK

status VARCHAR(32) NOT NULL
action_key VARCHAR(128) NOT NULL
parameters JSONB NOT NULL
reason TEXT NOT NULL

policy_decision VARCHAR(32) NULL
policy_reason TEXT NULL

content_revision INTEGER NOT NULL DEFAULT 1
content_hash CHAR(64) NOT NULL
lock_version BIGINT NOT NULL DEFAULT 0

created_by_subject TEXT NOT NULL
created_by_display_name TEXT NULL

created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
```

Status：

```text
CREATED
DENIED
WAITING_APPROVAL
APPROVED
REJECTED
SUBMITTED
```

约束：

```text
CHECK (status IN ('CREATED','DENIED','WAITING_APPROVAL','APPROVED','REJECTED','SUBMITTED'))
```

`SUBMITTED` 是真实的生命周期状态（不是死状态）：只有 durable submit worker 拿到
HISIEM 返回的**真实非空 `execution_id`** 并写入 `response_execution_ref` 之后，
Proposal 才会进入 `SUBMITTED`（见 §23）。

Policy：

```text
DENY
REQUIRE_APPROVAL
```

V1 Proposal Content 创建后不允许用户修改。

`content_revision` 用于 Approval Contract；`lock_version` 用于并发控制，两者不得混淆。

`created_by_subject` / `created_by_display_name` 是**不可变的 Proposer Provenance**：
创建时由服务端从已认证的 trusted context 推导，写入后永不修改；它绝不是请求体字段，
也**绝不**取 `investigation.initiated_by_subject` —— 后者回答的是"谁跑了这次
Investigation"，是另一个问题（只要提出响应的人不是调查者本人，答案就不同）。
`created_by_subject` 非空：空 proposer 是构造错误，`ResponseProposal.__post_init__`
直接抛 `DomainError`。

Proposer Provenance **刻意不参与 Approval Content Hash**：`_compute_content_hash()`
只覆盖 `action_key` / `target_refs` / `parameters`，因此 provenance 不可能让审批契约
匹配或失配。`created_by_display_name` 可为空。

存量行（早于 provenance 采集）在迁移中回填为字面量 `'unknown'`（add nullable →
backfill → set NOT NULL），不为它们虚构任何身份。

---

## 19. `response_proposal_target`

```text
proposal_id UUID NOT NULL FK
ordinal INTEGER NOT NULL
provider VARCHAR(32) NOT NULL
resource_type VARCHAR(32) NOT NULL
address_id TEXT NOT NULL
business_id TEXT NULL
```

Primary Key：

```text
(proposal_id, ordinal)
```

约束：

```text
UNIQUE(proposal_id, provider, resource_type, address_id)
```

所有 Target 必须在 Proposal 创建前完成 resolution 与 same-tenant validation。

---

## 20. `response_proposal_evidence`

```text
proposal_id UUID NOT NULL FK
evidence_id UUID NOT NULL FK
```

Primary Key：

```text
(proposal_id, evidence_id)
```

ResponseProposal 至少需要一个 Supporting Evidence。

---

## 21. `approval_request`

```text
id UUID PK
proposal_id UUID NOT NULL UNIQUE FK
proposal_content_revision INTEGER NOT NULL
proposal_content_hash CHAR(64) NOT NULL
requested_reason TEXT NOT NULL
requested_at TIMESTAMPTZ NOT NULL
```

ApprovalRequest 必须绑定精确：

```text
proposal_id
proposal_content_revision
proposal_content_hash
```

---

## 22. `approval_decision`

Immutable Human Authority Fact。

```text
id UUID PK
approval_request_id UUID NOT NULL UNIQUE FK
decision VARCHAR(16) NOT NULL
actor_subject_id TEXT NOT NULL
actor_tenant_id TEXT NOT NULL
actor_display_name TEXT NULL
reason TEXT NULL
decided_at TIMESTAMPTZ NOT NULL
```

Decision：

```text
APPROVE
REJECT
```

`UNIQUE(approval_request_id)` 保证一个 ApprovalRequest 最多一个 Decision。

---

## 23. `response_execution_ref`

HISIEM SOAR Execution Projection。

```text
proposal_id UUID PK FK
provider VARCHAR(32) NOT NULL
execution_id TEXT NOT NULL
submission_key TEXT NOT NULL
status VARCHAR(32) NOT NULL
submitted_at TIMESTAMPTZ NOT NULL
last_observed_at TIMESTAMPTZ NOT NULL
started_at TIMESTAMPTZ NULL
finished_at TIMESTAMPTZ NULL
safe_result JSONB NULL
safe_error_code TEXT NULL
safe_error_message TEXT NULL
```

Status：

```text
QUEUED
RUNNING
SUCCEEDED
FAILED
```

约束：

```text
UNIQUE(provider, execution_id)
UNIQUE(submission_key)
```

**行存在性规则：该表不存在 placeholder / `pending-{proposal_id}` 行。**
`response_execution_ref` 行只在 HISIEM 返回**真实非空 `execution_id`** 之后，由 durable
submit worker 在同一本地事务中创建，并同时把 Proposal 从 `APPROVED` 推进到 `SUBMITTED`。
因此 `UNIQUE(provider, execution_id)` 与 `UNIQUE(submission_key)` 是真正的 Provider
引用唯一性约束，而不是占位符的唯一性。

Proposal 处于 `APPROVED` 且**没有** `response_execution_ref` 行，是正常的
"approved, awaiting submission" 状态；若 HISIEM 明确拒绝该次提交（未创建 execution），
Proposal 合法地保持 `APPROVED`，绝不伪造 execution id。

`submission_key`（稳定提交 / 幂等身份）：

```text
response:{tenant_id}:{proposal_id}
```

该值作为 HISIEM 的 `Idempotency-Key` 请求头提交，必须保持在 HISIEM 的 128 字符上限内。

真正 SOAR Execution 的 Source of Truth 始终是 HISIEM。

---

## 23.1 `response_submission`

本地提交生命周期（LOCAL Submission Lifecycle）：durable 地回答"这一份被批准的提交
是否被 Provider 接受"。它**不是** Provider Execution Status，也不是第二个 execution
投影 —— 与 §23 相同，该表永不保存 placeholder / 伪造的 execution identity。

```text
proposal_id UUID PK FK
submission_key TEXT NOT NULL
status VARCHAR(32) NOT NULL
attempt_count INTEGER NOT NULL DEFAULT 0
last_error_code TEXT NULL
safe_error_message TEXT NULL
created_at TIMESTAMPTZ NOT NULL
updated_at TIMESTAMPTZ NOT NULL
submitted_at TIMESTAMPTZ NULL
failed_at TIMESTAMPTZ NULL
attention_required_at TIMESTAMPTZ NULL
```

Status：

```text
PENDING
RETRYING
SUBMITTED
FAILED_DEFINITIVE
ATTENTION_REQUIRED
```

约束：

```text
UNIQUE(submission_key)
CHECK (status IN ('PENDING','RETRYING','SUBMITTED','FAILED_DEFINITIVE','ATTENTION_REQUIRED'))
CHECK (attempt_count >= 0)
```

`submission_key` 与 §23 使用同一个稳定幂等身份：

```text
response:{tenant_id}:{proposal_id}
```

该表不携带 tenant 列，只能通过所属 Investigation 做 tenant-scoped 访问
（`response_proposal.investigation_id → investigation.tenant_id`）。

生命周期（与 §32 的事务边界一一对应）：

```text
审批事务（APPROVE）
    → 创建 PENDING 行：与不可变 ApprovalDecision、WAITING_APPROVAL → APPROVED、
      response_execution_queued 事件及其 outbox 投递在**同一事务**内
    → 仍然不创建任何 response_execution_ref

submit 投递遇到 TRANSIENT / UNCERTAIN 失败
    （HTTP 408 / 425 / 429、5xx、transport error，或 Provider 返回空 execution_id）
    → RETRYING，attempt_count 递增，记录有界的 last_error_code 与 safe_error_message
    → 追加 response_submission_retrying 事件
    → outbox 投递保持可重试，并使用**同一个**稳定 key

submit 投递被 Provider 确定性拒绝（HTTP 400 / 404 / 409 / 422）
    → FAILED_DEFINITIVE，写入 failed_at
    → 追加 response_submission_failed 事件，outbox 投递进入 DEAD_LETTER
    → 仍然不创建任何 response_execution_ref

submit 投递自动重试预算耗尽，且每次失败都仍是 TRANSIENT / UNCERTAIN
    （timeout、transport error、HTTP 408 / 425 / 429、5xx）
    → ATTENTION_REQUIRED，写入 attention_required_at，保留 attempt_count、
      last_error_code 与 safe_error_message
    → 追加 response_submission_attention_required 事件（无 outbox 目的地）
    → 该事实落库**之后**，outbox 投递才进入 DEAD_LETTER
    → 仍然不创建任何 response_execution_ref

submit 成功
    → 在创建真实 response_execution_ref 并把 Proposal 从 APPROVED 推进到
      SUBMITTED 的**同一事务**内写入 SUBMITTED + submitted_at
```

`FAILED_DEFINITIVE` 是关于**提交**的事实，不是关于执行的事实：Provider 从未创建
execution，因此既没有 execution 可以失败，也**不得**写
`response_execution_ref.status = FAILED`；Proposal 合法地保持 `APPROVED`。Workspace
必须显示"提交失败"且不展示任何外部执行编号，并停止轮询。

`ATTENTION_REQUIRED` 是自动重试预算耗尽后的终态**本地**状态，与
`FAILED_DEFINITIVE` 刻意不同：`FAILED_DEFINITIVE` 断言 Provider **拒绝**了该次提交，
而这里我们**不能**断言任何一方的结论 —— 既不能断言 Provider 拒绝，也不能断言没有
execution 存在（每次尝试都在 Provider 给出答案之前失败）。该事实由 submit 投递的
exhaustion hook 在 dead-letter **之前**写入（见 §27）：`response_submission` 先变为
`ATTENTION_REQUIRED` 并记录 `attention_required_at`，随后 outbox 投递才被置为
`DEAD_LETTER`，因此 Workspace 永远不会读到"投递已终止"而本地提交仍显示"正在重试"。
该状态之后不再有任何自动重试，Workspace 必须显示"提交状态不确定 / 需要人工处理"，
不展示任何外部执行编号，并停止轮询。

---

## 24. `orchestration_binding`

连接 Domain Identity 与 LangGraph Runtime Identity。

```text
investigation_id UUID PK FK
thread_id TEXT NOT NULL UNIQUE
graph_name VARCHAR(64) NOT NULL
graph_version VARCHAR(64) NOT NULL
state_schema_version INTEGER NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

核心约束：

```text
Investigation ID != LangGraph thread_id
```

---

## 25. `command_receipt`

支持 Application Command Idempotency。

```text
id UUID PK                          # surrogate technical primary key
idempotency_key TEXT NOT NULL
command_id UUID NOT NULL UNIQUE     # 每个 command 至多一条 receipt
command_type VARCHAR(128) NOT NULL
tenant_id TEXT NOT NULL
aggregate_type VARCHAR(64) NULL
aggregate_id UUID NULL
result_ref_type VARCHAR(64) NULL    # 保留列（当前恒为 NULL，未使用）
result_ref_id TEXT NULL             # 保留列（当前恒为 NULL，未使用）
request_fingerprint TEXT NULL
safe_result JSONB NULL
completed_at TIMESTAMPTZ NOT NULL
```

约束：

```text
UNIQUE(tenant_id, command_type, idempotency_key)
```

Idempotency-Key 的作用域是 **Tenant + Command Type**，不是全系统 global key：
两个 tenant 可以各自使用相同的 `idempotency_key`，同一 tenant 下不同
`command_type` 也可使用相同的 key —— 它们都是互不相关的幂等空间。数据库唯一性
由 `(tenant_id, command_type, idempotency_key)` 复合约束保证。

`request_fingerprint` 保存业务请求的有界指纹（例如 `source_alert_ref`），用于
检测 `same key + different business request`：同一个 Idempotency-Key 被绑定到
一个不同的业务请求上是确定性的幂等冲突（409 `IDEMPOTENCY_CONFLICT`），而不是
静默返回原始（错误）逻辑结果。

`safe_result` 只保存重复调用时需要返回的最小安全结果，不保存 raw Tool response、凭据、Prompt 或敏感日志内容。

并发：两个携带相同 Idempotency-Key 的请求同时通过 replay 查找（都未命中）后会
在 `UNIQUE(tenant_id, command_type, idempotency_key)` 上竞争。loser 的 commit
冲突被翻译为 `CommandReceiptConflictError`（infrastructure 层），handler 在全新
事务中重读 winner 的 receipt 并比较 `request_fingerprint`：
- same request → 返回 winner 的原始 aggregate；
- different request → `IdempotencyConflictError`（409）。

绝不把该唯一性冲突作为 raw `IntegrityError` → HTTP 500 泄漏。

---

## 26. `domain_event`

Domain Event append-only。

```text
sequence BIGINT GENERATED ALWAYS AS IDENTITY PK
event_id UUID NOT NULL UNIQUE
event_type VARCHAR(128) NOT NULL
event_version INTEGER NOT NULL
aggregate_type VARCHAR(64) NOT NULL
aggregate_id UUID NOT NULL
aggregate_revision BIGINT NOT NULL
tenant_id TEXT NOT NULL
correlation_id UUID NOT NULL
causation_id UUID NULL
actor_subject_id TEXT NULL
payload JSONB NOT NULL
occurred_at TIMESTAMPTZ NOT NULL
```

索引：

```text
(tenant_id, sequence)
(aggregate_type, aggregate_id, sequence)
(event_type, occurred_at)
(correlation_id)
```

不得 UPDATE Domain Event Payload。

---

## 27. `outbox_message`

```text
id UUID PK
event_id UUID NOT NULL FK
destination VARCHAR(128) NOT NULL
status VARCHAR(16) NOT NULL
attempt_count INTEGER NOT NULL DEFAULT 0
available_at TIMESTAMPTZ NOT NULL
locked_at TIMESTAMPTZ NULL
locked_by TEXT NULL
lease_token TEXT NULL
published_at TIMESTAMPTZ NULL
last_error_code VARCHAR(128) NULL
traceparent TEXT NULL                 -- optional diagnostic W3C context; not business correlation
created_at TIMESTAMPTZ NOT NULL
```

Destination（Event Type → Outbox Destination）：

```text
investigation_created            → investigation.graph.run
response_execution_queued        → response.execution.submit
response_execution_submitted     → response.execution.observe
response_execution_observed      → response.execution.observe
response_execution_observation_failed → response.execution.observe
```

`response_submission_retrying`、`response_submission_failed` 与
`response_submission_attention_required` **不在此表中**：它们是关于本地提交的事实
（见 §23.1），不是需要投递的交付，因此不产生 outbox 行；它们只作为 Domain Event
追加进事件账本。`response_submission_attention_required` 只是事实：它**没有** outbox
目的地，因此它不会投递任何东西；"自动重试已停止"这一语义由 submit 投递自身的
exhaustion hook 承担 —— 该 hook 先持久化上述事实，随后该 submit 投递（
`response_execution_queued` 的投递）才进入 `DEAD_LETTER`。

响应执行的 durable 语义：`response.execution.submit` 与 `response.execution.observe`
是**两个独立职责** —— submit 仅在一次性提交时获取真实 Provider execution id；
observe 只推进一个已经提交的 execution。一个合法但非终态的 Provider execution
（QUEUED/RUNNING/…）通过追加 `response_execution_observed` 并把新 outbox 行的
`available_at` 设为**未来时刻**来重新调度（durable delayed observation），
**绝不允许**靠耗尽 attempts 把该投递重试到 `DEAD_LETTER` —— 一个正在运行中的
execution 不是失败。

同理，一次**读取**真实 Provider execution 的失败（transient / uncertain 的
`get_execution_status()` 失败：timeout、transport error、HTTP 408 / 425 / 429、5xx）
不消耗该 observe 投递的重试预算：它追加 `response_execution_observation_failed`
（映射到 `response.execution.observe`，`available_at` 设为未来时刻）并正常结束本次
投递，因此 Provider 长时间不可用也**永不**会把对账职责耗尽 attempts 而进入
`DEAD_LETTER`。此时 `response_execution_ref` 投影**刻意保持不变** —— 我们对该
execution 一无所知，其 status / 时间戳 / 错误保持为最后一次成功观测所留下的值，
绝不因读取失败而把 execution 标记为 `FAILED`。

Status：

```text
PENDING        # 待投递
PROCESSING     # 已租约认领（worker 正在投递）
PUBLISHED      # 投递成功（terminal）
FAILED         # 可重试失败（attempt_count < max，按 backoff 重试）
DEAD_LETTER    # 永久失败（attempt_count >= max；terminal，永不回收）
```

租约时间语义（统一）：

```text
locked_at      # = 最后一次成功 claim / renewal 的时间（绝不写入未来）
lease expired  # locked_at <= now - lease_timeout
```

```text
claim            → PROCESSING + locked_at = now + locked_by = worker
                   + 写入 fresh lease_token
renew_lease      → locked_at = now（仅当 id + status=PROCESSING + lease_token 匹配）
lease expired    → 其它 worker 可回收（在过期 PROCESSING 租约上重新 claim）
attempt++        → 仅在 worker 持有租约时安全递增
DEAD_LETTER      → attempt_count >= max_attempts 后置入；terminal，不再被 claim
```

Fencing：`lease_token` 是 claim 写入的 fencing token。所有
`renew` / `mark_published` / `mark_failed` / `mark_dead_letter` 都必须携带并匹配
该 token（`WHERE id + status=PROCESSING + lease_token`）。持有 stale token 的旧
worker（其租约已被回收）匹配 0 行 → 被拒绝。正常长时间执行在每次成功 renewal
时把 `locked_at` 重置为 renewal 时刻；worker 崩溃停止 renewal 后，
`last locked_at + lease_timeout` 一到消息即可被其它 worker 回收。

约束：

```text
UNIQUE(event_id, destination)
CHECK (status IN ('PENDING','PROCESSING','PUBLISHED','FAILED','DEAD_LETTER'))
```

主要索引：

```text
(status, available_at)
WHERE status IN ('PENDING', 'FAILED')

(status, locked_at)
WHERE status = 'PROCESSING'     # 过期租约回收
```

---

## 28. `tool_invocation`

Operational Audit。

```text
id UUID PK
investigation_id UUID NOT NULL FK
tool_name VARCHAR(128) NOT NULL
tool_version VARCHAR(64) NULL
idempotency_key TEXT NOT NULL
arguments JSONB NOT NULL
status VARCHAR(32) NOT NULL
provider_request_id TEXT NULL
started_at TIMESTAMPTZ NOT NULL
finished_at TIMESTAMPTZ NULL
error_code VARCHAR(128) NULL
safe_error_message TEXT NULL
result_metadata JSONB NULL
```

约束：

```text
UNIQUE(investigation_id, idempotency_key)
```

不持久化完整 Tool Result。Tool Result 经 Normalizer 生成 Evidence 后，核心 Domain 只依赖 Evidence。

---

## 28.1 P3-A 知识子系统表集合

P3-A（Knowledge + Hybrid Retrieval）在 `copilot` schema 中落地 9 张表，由三个迁移建立：

```text
ed6af82d9b13   knowledge_document / knowledge_document_version / embedding_profile
               knowledge_chunk / attack_technique
c41f7b2e9d08   knowledge_content_chunk / knowledge_chunk_embedding / attack_release
a5e93c07fd21   attack_release_projection
```

9 张表分两类，其区别是整套设计的支点：

```text
Immutable Knowledge Truth（只追加，永不 UPDATE）
────────────────────────────
knowledge_document
knowledge_document_version
knowledge_content_chunk
attack_technique
attack_release_projection

Rebuildable Projection / Supporting Record（可重建，或被取代）
────────────────────────────
knowledge_chunk_embedding
embedding_profile
attack_release
knowledge_chunk              （pre-closure 遗留表，P3-A 不再读写）
```

前提条件：本子系统使用 pgvector 的 `vector` 与 PostgreSQL 的 `tsvector`。`vector` 扩展必须装在
本连接 `search_path` 可达的 schema（即 `copilot`）中，否则 `ed6af82d9b13` 的 `CREATE TABLE` 会以
一条难以定位的 `type "vector" does not exist` 失败。两个相关迁移都在任何 DDL **之前**显式校验，
并以可执行的信息失败——失败时"本次运行什么都没改"对运行本身成立，不依赖调用方的事务配置。

---

## 28.2 `knowledge_document`

一个外部标识的知识文档。可变部分只有生命周期。

```text
id UUID PK
source_kind VARCHAR(32) NOT NULL
external_key VARCHAR(512) NOT NULL
visibility VARCHAR(16) NOT NULL
tenant_id VARCHAR(128) NULL
title VARCHAR(512) NOT NULL
status VARCHAR(16) NOT NULL
active_version_id UUID NULL
revision INTEGER NOT NULL DEFAULT 0
lock_version INTEGER NOT NULL DEFAULT 0
created_at TIMESTAMPTZ NOT NULL
retired_at TIMESTAMPTZ NULL
```

Source Kind：

```text
MITRE_ATTACK
CURATED_GUIDANCE
TENANT_RUNBOOK
```

Visibility：

```text
GLOBAL
TENANT
```

Status：

```text
ACTIVE
RETIRED
```

约束：

```text
CHECK (source_kind IN ('MITRE_ATTACK','CURATED_GUIDANCE','TENANT_RUNBOOK'))
CHECK (visibility IN ('GLOBAL','TENANT'))
CHECK (status IN ('ACTIVE','RETIRED'))
CHECK ((visibility = 'GLOBAL' AND tenant_id IS NULL)
    OR (visibility = 'TENANT' AND tenant_id IS NOT NULL))
CHECK ((status = 'ACTIVE' AND retired_at IS NULL)
    OR (status = 'RETIRED' AND retired_at IS NOT NULL))
CHECK (revision >= 0)
CHECK (lock_version >= 0)
```

`visibility` 是**作用域**，不是权限：GLOBAL 无 tenant，TENANT 恰好属于一个 tenant。
scope 不变量是数据库 CHECK 而不是应用约定，所以"GLOBAL 却带 tenant"不可表达。

身份是 scope 内的 `(source_kind, external_key)`。不能用**一条** `UNIQUE(source_kind, external_key)`：
PostgreSQL 的 `NULL` 在普通 unique index 中永不冲突，那条索引会静默放过任意多行 GLOBAL 文档。
身份因此由**两条 PARTIAL unique index** 表达：

```sql
CREATE UNIQUE INDEX uq_knowledge_document_global_key
ON copilot.knowledge_document (source_kind, external_key)
WHERE visibility = 'GLOBAL';

CREATE UNIQUE INDEX uq_knowledge_document_tenant_key
ON copilot.knowledge_document (tenant_id, source_kind, external_key)
WHERE visibility = 'TENANT';
```

主要索引：

```text
(tenant_id, status)
```

`source_kind`、`external_key`、`visibility` immutable；唯一的生命周期动作是 `ACTIVE -> RETIRED`，
`RETIRED` 是终态，不支持 `RETIRED -> ACTIVE`。

`active_version_id` 是**指针**，在同一事务内与该 version 及其 chunk 一起写入，因此不会指向一个检索投影
缺失的 version。它刻意**不是** Foreign Key：两张表互相引用，deferred 循环约束并不会比事务本身多给任何
保证。

ORM ↔ Domain：

```text
KnowledgeDocumentRow   ↔   domain.knowledge.entities.KnowledgeDocument（Aggregate Root）

mapper: document_to_row / row_to_document
```

`source_kind` / `visibility` / `status` 以原始字符串存储，schema 的 CHECK 是仲裁者，mapper 用枚举值
转换；读到枚举外的值直接抛错而不是强制归一，因为损坏的行是需要浮出的 bug，不是需要抹平的东西。

`row_to_document` 直接构造 aggregate，绝不调用 `KnowledgeDocument.create()`：那个工厂会追加
`knowledge_document_created` Domain Event，而重新载入一行不是新的业务事实，重放会让审计账本重复。

---

## 28.3 `knowledge_document_version`

一个不可变的文档版本。内容变化是**追加**，没有任何 UPDATE 路径。

```text
id UUID PK
document_id UUID NOT NULL FK
version INTEGER NOT NULL
content_hash CHAR(64) NOT NULL
title VARCHAR(512) NOT NULL
normalized_content TEXT NOT NULL
language VARCHAR(16) NOT NULL
source_version VARCHAR(128) NULL
metadata JSONB NOT NULL DEFAULT '{}'
ingested_at TIMESTAMPTZ NOT NULL
effective_at TIMESTAMPTZ NULL
```

约束：

```text
CHECK (version >= 1)
CHECK (content_hash ~ '^[0-9a-f]{64}$')
CHECK (length(normalized_content) > 0)

UNIQUE(document_id, version)
UNIQUE(document_id, content_hash)
```

`UNIQUE(document_id, content_hash)` 把"重复摄取相同字节不可能产生第二个 version"变成数据库事实，
这正是并发摄取会收敛而不是竞争的原因。

`source_version` 只记录哪个 release **创建**了这一行。两个 release 可以携带逐字节相同的 technique 内容
而共用同一个 version，因此它**不是**"哪个 release 投影了这个 version"的答案——那是 §28.9 的职责。

该表 immutable。

ORM ↔ Domain：

```text
KnowledgeDocumentVersionRow   ↔   domain.knowledge.entities.KnowledgeDocumentVersion

mapper: version_to_row / row_to_version
```

该表的列名是 `metadata`，而 ORM 属性名是 `doc_metadata`（`metadata` 被 `DeclarativeBase` 占用）；
这一处重命名只存在于 mapper 中，任何其它地方都不得自行映射。

---

## 28.4 `embedding_profile`

语料被索引到的向量空间。

```text
id UUID PK
provider VARCHAR(64) NOT NULL
model_id VARCHAR(128) NOT NULL
dimension INTEGER NOT NULL
distance_metric VARCHAR(16) NOT NULL
normalization VARCHAR(16) NOT NULL
profile_version INTEGER NOT NULL
status VARCHAR(16) NOT NULL
created_at TIMESTAMPTZ NOT NULL
retired_at TIMESTAMPTZ NULL
```

约束：

```text
CHECK (dimension >= 1 AND dimension <= 8192)
CHECK (distance_metric IN ('COSINE'))
CHECK (normalization IN ('NONE','L2'))
CHECK (status IN ('ACTIVE','RETIRED'))
CHECK (profile_version >= 1)
CHECK ((status = 'ACTIVE' AND retired_at IS NULL)
    OR (status = 'RETIRED' AND retired_at IS NOT NULL))
```

`distance_metric` 只冻结 `COSINE` 一个值：第二个 metric 是排序语义变更，需要自己的一轮设计，
不在此处预先加入，检索路径显式拒绝其它值。

索引：

```text
UNIQUE(provider, model_id, dimension, distance_metric, normalization, profile_version)
```

"至多一个 ACTIVE"由 partial unique index 表达，而不是应用代码——应用代码扛不住两个并发激活：

```sql
CREATE UNIQUE INDEX uq_embedding_profile_single_active
ON copilot.embedding_profile (status)
WHERE status = 'ACTIVE';
```

ORM ↔ Application Port：

```text
EmbeddingProfileRow   ↔   application.ports.knowledge.EmbeddingProfileRecord

mapper: profile_to_row / row_to_profile
```

该表**没有** Domain Aggregate：它是检索配置，不是业务事实。

---

## 28.5 `knowledge_content_chunk`

不可变的引用目标，即 citation 指向的东西。

`knowledge_chunk` 把内容与 embedding 放在同一行，重新索引会删掉 citation 指向的正是那些行，
历史 `kcit:` handle 随之全部失效。本表是那次 closure 的内容半边：内容写一次，永不重写——
它必须活过一次重新 embedding、一次检索投影重建、一次重新切分（同一个 version 的**新 generation**，
追加而非替换）、一次重启、一次 retirement，以及一个后续版本。

```text
id UUID PK
document_id UUID NOT NULL FK
document_version_id UUID NOT NULL FK
generation INTEGER NOT NULL
ordinal INTEGER NOT NULL
heading_path TEXT NOT NULL
content TEXT NOT NULL
content_hash CHAR(64) NOT NULL
token_count INTEGER NOT NULL
language VARCHAR(16) NOT NULL
chunker_version VARCHAR(64) NOT NULL
lexical_document TSVECTOR GENERATED ALWAYS AS
    (to_tsvector('simple', coalesce(heading_path,'') || ' ' || content)) STORED NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

约束：

```text
CHECK (ordinal >= 0)
CHECK (generation >= 1)
CHECK (length(content) > 0)
CHECK (content_hash ~ '^[0-9a-f]{64}$')
CHECK (token_count >= 0)
CHECK (length(chunker_version) > 0)
```

主要索引：

```text
UNIQUE(document_version_id, generation, ordinal)
(document_id)
(document_version_id)
GIN (lexical_document)
```

`ordinal` 在一个 generation 内是**全序**，正是这一点让检索的稳定语义排序键成为全序而不是偏序。

`generation` 让 chunker 变更非破坏：对同一个不可变 version 重新切分是**追加** generation N+1 并原地保留
N，于是指向旧切分的 citation 仍然可解析。本子系统**没有** `delete_for_version`：不可变的历史知识内容
不通过 P3-A 删除。

`chunker_version` 被持久化，摄取据此判断**当前** generation 是否匹配冻结的 chunker 配置，而不是静默地
继续服务由另一套代码构建的投影。

`lexical_document` 是 GENERATED 列，全文本索引建在**这张表**上——词法检索读的是内容而不是向量，
所以词法通道的寿命长于任何一次向量空间变更；而它是 GENERATED 的，所以这个投影不可能与它所描述的内容漂移。

ORM ↔ Domain：

```text
KnowledgeContentChunkRow   ↔   domain.knowledge.entities.KnowledgeContentChunk

mapper: content_chunk_to_row / row_to_content_chunk
```

Citation handle 形如 `kcit:<chunk_uuid>:<content_hash_prefix>`，命名的是**本表**的主键；
重新 embedding、重建检索投影、重启、retirement 与后续 version 都不会让它失效。

---

## 28.6 `knowledge_chunk_embedding`

content chunk 的**可重建** embedding 投影。

本表不含内容，因此可以随时删除重建：换 embedding profile、重新索引、整库重建向量，都只是替换这里的行，
不破坏任何 citation——citation 命名的是 content chunk，而不是这一行。

```text
id UUID PK
content_chunk_id UUID NOT NULL FK
embedding_profile_id UUID NOT NULL FK
embedding VECTOR NOT NULL
indexed_at TIMESTAMPTZ NOT NULL
```

约束：

```text
UNIQUE(content_chunk_id, embedding_profile_id)
```

主要索引：

```text
(embedding_profile_id)
```

`UNIQUE(content_chunk_id, embedding_profile_id)` 让重新 embedding 幂等：同一个 chunk 在同一个空间里
恰好有一个向量。

`embedding` 是**无维度**的 `vector` 类型：schema 里不烘焙任何维度，所以第二个不同尺寸的 embedding 模型
不需要迁移；维度契约由 ACTIVE 的 embedding profile 强制。刻意**不建** HNSW、也**不建** IVFFlat——P3-A
做精确排序，ANN 索引会静默改变"找到的是哪些邻居"。

ORM ↔ Application Port：

```text
KnowledgeChunkEmbeddingRow   ↔   application.ports.knowledge.ChunkEmbeddingRecord

mapper: embedding_to_row（写入方向；读取走 retrieval 查询）
```

---

## 28.7 `attack_release`

ATT&CK 权威，粒度是 **release**。

```text
id UUID PK
framework VARCHAR(32) NOT NULL
source_release VARCHAR(32) NOT NULL
content_fingerprint CHAR(64) NULL
status VARCHAR(16) NOT NULL
technique_count INTEGER NOT NULL
created_at TIMESTAMPTZ NOT NULL
activated_at TIMESTAMPTZ NULL
```

Status：

```text
ACTIVE
INACTIVE
```

约束：

```text
CHECK (status IN ('ACTIVE','INACTIVE'))
CHECK (content_fingerprint IS NULL OR content_fingerprint ~ '^[0-9a-f]{64}$')
CHECK (technique_count >= 0)

UNIQUE(framework, source_release)
```

"每个 framework 至多一个权威 release"是一条 partial unique index。`attack_technique.active` 表达不了这条
规则：它是 technique-per-row，建在它上面的任何 unique index 都写不出这个约束，两个 release 可以同时自称
current。用 SQL 而不是应用代码，因为应用代码扛不住两个并发激活：

```sql
CREATE UNIQUE INDEX uq_attack_release_single_active
ON copilot.attack_release (framework)
WHERE status = 'ACTIVE';
```

`content_fingerprint` 让被 pin 的 release immutable：以不同内容重新导入同一个 release 会被**拒绝**而不是
被吸收。它只对本迁移从既有 technique 行 **ADOPT** 出来的 release 为 `NULL`——那些行是在 release 被指纹化
之前写入的，从未计算过指纹；在迁移里重算会在这门语言里放进**第二份** release 指纹定义，无法与权威函数保持
同步。这种 release 在下次导入相同字节时按**已存储的内容**重新推导指纹并 pin 住，而 `NULL` 指纹永不视为匹配。

ORM ↔ Application Port：

```text
AttackReleaseRow   ↔   application.ports.knowledge.AttackReleaseRecord

mapper: release_to_row / row_to_release
```

---

## 28.8 `attack_technique`

某个已 pin release 的规范 technique 行。

```text
id UUID PK
framework VARCHAR(32) NOT NULL
technique_id VARCHAR(32) NOT NULL
source_release VARCHAR(32) NOT NULL
name VARCHAR(512) NOT NULL
description TEXT NOT NULL
tactics JSONB NOT NULL
platforms JSONB NOT NULL
source_stix_id VARCHAR(128) NOT NULL
content_hash CHAR(64) NOT NULL
active BOOLEAN NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

约束：

```text
CHECK (content_hash ~ '^[0-9a-f]{64}$')

UNIQUE(framework, technique_id, source_release)
FK (framework, source_release) REFERENCES attack_release
    ON DELETE RESTRICT
```

重新导入同一个 release 不会复制 technique；新 release 是新的行集，旧 release 的行永不删除。

该 Foreign Key 由 `c41f7b2e9d08` 在 ADOPT 之后建立，使"technique 行没有已注册 release"从此不可表达——
这正是 technique 永远不可能成为回答"哪个 release 是 current"的地方的原因。

`active` **镜像**所属 release 的权威，从不自己决定权威：在 legacy flag 相互矛盾的地方，镜像跟随 release，
因为一个非权威 release 上残留 `active = true` 恰恰是 release 表存在的目的所要变成不可表达的那种状态。

ORM ↔ Application Port：

```text
AttackTechniqueRow   ↔   application.ports.knowledge.AttackTechniqueRecord

mapper: technique_to_row / row_to_technique
```

为同一个 technique 生成的 MITRE KnowledgeDocument 与之**相关但不是同一个权威**：document 是检索内容，
本行是 technique 记录。

---

## 28.9 `attack_release_projection`

一个 release 的 technique 与它 staged 的那个不可变 knowledge document version 之间的绑定。

```text
id UUID PK
framework VARCHAR(32) NOT NULL
source_release VARCHAR(32) NOT NULL
technique_id VARCHAR(32) NOT NULL
document_id UUID NOT NULL FK
document_version_id UUID NOT NULL FK
content_hash CHAR(64) NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

约束：

```text
CHECK (content_hash ~ '^[0-9a-f]{64}$')

UNIQUE(framework, source_release, technique_id)
UNIQUE(framework, source_release, document_id)

FK (framework, source_release)                REFERENCES attack_release           ON DELETE RESTRICT
FK (framework, technique_id, source_release)  REFERENCES attack_technique          ON DELETE RESTRICT
FK (document_id)                              REFERENCES knowledge_document        ON DELETE RESTRICT
FK (document_version_id)                      REFERENCES knowledge_document_version ON DELETE RESTRICT
```

主要索引：

```text
(document_id)
```

release identity 与 knowledge content identity 是两个不同的事实：两个 release 可以携带逐字节相同的
technique 内容，因而共用同一个 `knowledge_document_version`，而 `source_version` 只记录哪个 release
**创建**了那一行。没有这条显式绑定时，没有任何事实能说"v15.1 对 T1110 的投影**就是**这个 version"，
于是重新激活 v14.1 无法恢复它自己的投影，一个 staged 的 release 可以改变检索所服务的内容。

绑定在 **stage** 时写入并永不重新推导，所以重新激活一个更旧的 release 恢复的是那个 release staged 的
version，而不是"按当前匹配内容重建"出来的 version。

本表不授予任何权威：哪个 release 权威由 §28.7 的 `status` 决定，这里只说某个 release 的投影**是**哪个
version——原子切换把文档指针移到那个 version。

ORM ↔ Application Port：

```text
AttackReleaseProjectionRow   ↔   application.ports.knowledge.AttackReleaseProjectionRecord

mapper: row_to_projection / projection_values
```

绑定以数据库为权威，永不从内容反推。

---

## 28.10 `knowledge_chunk`

**pre-closure 遗留表**。`knowledge_chunk` 曾是"一行既是内容又是 embedding"，这正是重新索引会杀死历史
citation 的原因。`c41f7b2e9d08` 把每一行按**原主键**复制进 §28.5 / §28.6 的拆分对，并**刻意保留**这张表，
以便 `downgrade` 逐字节放回 closure 之前的行。P3-A 从此不再读写它。

```text
id UUID PK
document_id UUID NOT NULL FK
document_version_id UUID NOT NULL FK
ordinal INTEGER NOT NULL
heading_path TEXT NOT NULL
content TEXT NOT NULL
content_hash CHAR(64) NOT NULL
token_count INTEGER NOT NULL
language VARCHAR(16) NOT NULL
chunker_version VARCHAR(64) NOT NULL
embedding_profile_id UUID NOT NULL FK
embedding VECTOR NOT NULL
lexical_document TSVECTOR GENERATED ALWAYS AS
    (to_tsvector('simple', coalesce(heading_path,'') || ' ' || content)) STORED NOT NULL
created_at TIMESTAMPTZ NOT NULL
```

约束：

```text
CHECK (ordinal >= 0)
CHECK (length(content) > 0)
CHECK (content_hash ~ '^[0-9a-f]{64}$')
CHECK (token_count >= 0)
```

主要索引：

```text
UNIQUE(document_version_id, ordinal)
(document_id)
(document_version_id)
(embedding_profile_id)
GIN (lexical_document)
```

两条保留理由都是承重的。`downgrade -1` 要能逐字节恢复 pre-closure schema，而不是重建可能表达不出来的
行——一个还没有 embedding 行的 content chunk 没有忠实的 `knowledge_chunk` 形式。以及：一张存在于数据库
里但不在 ORM metadata 里的表是**永久**的 `alembic check` drift，会掩盖下一次真正的 drift。

ORM：

```text
KnowledgeChunkRow（声明但无 mapper，也不被任何 P3-A 路径读写）
```

`knowledge doctor` 报告这张表及其行数，操作者在 catalog 里遇到它时能知道它是被取代的残留，而不是 live schema。

---

## 29. 明确不创建的表

V1 不建立：

```text
agent_run
chat_session
conversation
message
memory
long_term_memory
agent_reasoning
chain_of_thought
generic_task
case_copy
alert_copy
event_copy
soar_execution
```

尤其 `alert / event / case / soar_execution` 不得在 Copilot 中重新建立业务副本。

---

## 30. Foreign Key 与 Delete Rule

Copilot 自身 Entity 使用 PostgreSQL FK。

HISIEM External Resource 不使用 Database FK，例如 source_alert_ref、target_ref、SOAR execution ID。

V1 不提供 Domain Delete。Investigation、Evidence、Finding、Result、Approval 等均不设计用户级删除。

Foreign Key 默认使用：

```text
ON DELETE RESTRICT
```

生产环境不得依赖 `ON DELETE CASCADE` 删除完整调查历史。

---

## 31. Domain Transaction Boundary

普通业务 Command：

```text
BEGIN

load aggregate
validate expected lock_version
apply domain change
insert/update domain rows
insert command_receipt
insert domain_event
insert outbox_message

COMMIT
```

以上必须原子提交。

以下行为不得发生在数据库事务内部：

```text
LLM Call
HISIEM HTTP Call
Threat Intel Call
RAG Retrieval
MCP network operation
SOAR remote execution
long-running Tool Call
```

正确结构：

```text
External Read / Computation
→ validated candidate
→ short DB transaction
```

---

## 32. SOAR Side Effect Transaction

`SubmitApprovedResponse` 使用外部幂等边界。实际实现分为两段：审批事务只记录
**durable submission intent**（不调用 HISIEM、不写入 execution ref），真正的提交由
durable submit worker 在数据库事务之外完成：

```text
Approval 事务（短事务，禁止 SOAR 调用）
1. Persist ApprovalDecision
2. proposal APPROVED
3. Append ResponseApprovalDecided
4. Append ResponseExecutionQueued（携带稳定 submission_key）
5. Insert outbox_message → response.execution.submit
6. Commit

Durable submit worker（事务外调用 HISIEM）
7. Reload approved Proposal（权威契约来自数据库，不来自队列 payload）
8. Derive stable submission_key
9. Check existing ResponseExecutionRef —— 已存在则收敛（no-op），绝不第二次执行
10. Call HISIEM SOAR with Idempotency-Key = submission_key
11. Open new transaction
12. Insert ResponseExecutionRef（仅当 HISIEM 返回真实非空 execution_id）
13. proposal APPROVED → SUBMITTED + Append ResponseExecutionStarted
    （Provider 非终态时同时 Append ResponseExecutionSubmitted）
14. Commit
```

如果 SOAR 已接受请求但 Copilot 在持久化 execution ref 前崩溃，重试必须使用相同 `submission_key`，由 HISIEM 去重并恢复同一 execution，禁止生成第二次实际响应。

上面的审批事务同时创建 `response_submission` 的 `PENDING` 行；submit worker 的成功与
失败路径如何持久化该行（SUBMITTED / RETRYING / FAILED_DEFINITIVE /
ATTENTION_REQUIRED），见 §23.1。

---

## 33. Aggregate Lock Order

涉及多个 Aggregate 的事务固定锁顺序：

```text
Investigation
→ ResponseProposal
→ ApprovalRequest / Decision
```

主要并发控制仍采用 Optimistic Lock，不使用长期 Row Lock。

---

## 34. Migration Ownership

### Copilot

Alembic 管理：

```text
copilot.*
```

### LangGraph

LangGraph 自身 setup/migration 管理：

```text
langgraph_checkpoint.*
```

Alembic Autogenerate 必须显式排除 `langgraph_checkpoint`。

---

## 35. Alembic Policy

所有 Constraint 必须命名：

```text
pk_*
fk_*
uq_*
ck_*
ix_*
```

推荐 Naming Convention：

```python
{
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

Autogenerate 只产生 Candidate Migration。每个 Migration 必须检查 constraint、index、nullable、default、data migration、rename、partial index 与 downgrade behavior。

CI 至少执行：

```text
alembic upgrade head
alembic check
```

---

## 36. Initial Migration Layout

```text
0001_investigation_domain
    investigation
    plan
    hypothesis
    evidence
    finding
    result

0002_response_domain
    response_proposal
    approval
    response_execution_ref

0003_runtime_and_events
    orchestration_binding
    command_receipt
    domain_event
    outbox_message
    tool_invocation
    indexes
```

---

## 37. LangGraph Checkpoint

Production 使用 `AsyncPostgresSaver`。

Checkpoint Schema：

```text
langgraph_checkpoint
```

应用不得 ORM map、Repository wrap、Join 或直接修改 LangGraph 内部 checkpoint tables。

Checkpoint 用于：

```text
fault recovery
interrupt / resume
runtime inspection
```

不用于：

```text
Domain audit
business history
long-term memory
```

Graph State 必须保持 bounded、structured、serializable。

Production 禁止 pickle fallback；Checkpoint 不得包含 credentials、API keys、raw auth tokens、private chain-of-thought 或 full raw logs。

Investigation 进入 Terminal State 后，checkpoint 可以按运维 retention policy 清理，但 Domain data、Domain Event、Evidence 与 Approval 不得随之删除。

---

## 38. Persistence Integration Tests

必须使用真实 PostgreSQL，至少覆盖：

```text
Alembic migration from empty DB
Repository round-trip
Optimistic locking
Partial unique active-investigation constraint
Evidence dedup constraint
Single ApprovalDecision constraint
ResponseExecutionRef uniqueness
Domain Event + Outbox atomic persistence
```

并发测试必须模拟两个并发 StartInvestigation 请求针对同一 Tenant + Alert，最终只能存在一个 Active Investigation。

LangGraph Persistence Test 必须验证真实 PostgreSQL checkpoint 的 restart/resume。

HITL Recovery Test 必须验证 ApprovalDecision 已落库后进程重启仍能安全恢复。

Side-effect Recovery Test 必须验证 SOAR 已接受请求但 Copilot 崩溃时，相同 submission key 不产生重复响应。

---

## 39. Persistence Invariants Summary

数据库层直接保证：

```text
one active investigation per tenant + alert
one result per investigation
one response proposal per investigation/result
one approval request per proposal
one approval decision per request
one execution ref per proposal（仅在真实 provider execution id 存在后创建，无 placeholder 行）
unique plan revision
unique hypothesis assessment revision
evidence deduplication
confidence range
status enum validity
referential integrity
one active embedding profile
unique embedding profile identity
one active attack release per framework
unique attack release per framework + source release
unique knowledge document identity per scope
knowledge document scope coherence
unique knowledge document version per document
knowledge document version deduplication
unique chunk ordinal per document version + generation
one embedding per content chunk per profile
every attack technique belongs to a registered attack release
every attack release projection binds a registered release, technique and document version
```

Domain/Application 层保证：

```text
legal state transition
same-Investigation evidence references
grounded Finding
valid Result
valid ATT&CK resolution
target tenant ownership
action registry
policy
authorization
approval permission
```

---

## 40. 冻结决策

| 项目 | 决策 |
|---|---|
| Database | PostgreSQL |
| ORM | SQLAlchemy Async |
| PostgreSQL Driver | psycopg 3 |
| Domain Migration | Alembic |
| Domain Schema | `copilot` |
| Checkpoint | AsyncPostgresSaver |
| Checkpoint Schema | `langgraph_checkpoint` |
| Checkpoint Migration | LangGraph-owned |
| Domain IDs | UUID |
| Timestamp | TIMESTAMPTZ / UTC |
| Status storage | VARCHAR + named CHECK |
| Aggregate concurrency | Optimistic Lock |
| Active Alert Investigation | PostgreSQL Partial Unique Index |
| Domain History | Immutable/append-only where specified |
| Domain Events | Append-only, not Event Sourcing |
| Event delivery | Transactional Outbox |
| Graph persistence | Separate from Domain persistence |
| SQL transaction | Short-lived; no LLM/network calls inside |
| Long-term Memory | Not part of V1 schema |
| Chat persistence | Not part of V1 |
| Raw SIEM replication | Forbidden |
| SOAR Execution | HISIEM-owned |
| Side-effect idempotency | Mandatory |
