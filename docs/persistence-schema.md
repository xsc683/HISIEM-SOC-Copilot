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

created_at TIMESTAMPTZ NOT NULL
started_at TIMESTAMPTZ NULL
finished_at TIMESTAMPTZ NULL
cancelled_at TIMESTAMPTZ NULL
```

V1 固定：

```text
source_provider = hisiem
source_resource_type = alert
```

但 Schema 保留 ExternalResourceRef 的通用表达。

### Active Investigation Constraint

Active Status：

```text
CREATED
RUNNING
WAITING_APPROVAL
EXECUTING_RESPONSE
```

数据库必须建立 Partial Unique Index：

```sql
CREATE UNIQUE INDEX uq_active_investigation_alert
ON copilot.investigation (
    tenant_id,
    source_provider,
    source_resource_type,
    source_address_id
)
WHERE status IN (
    'CREATED',
    'RUNNING',
    'WAITING_APPROVAL',
    'EXECUTING_RESPONSE'
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

Policy：

```text
DENY
REQUIRE_APPROVAL
```

V1 Proposal Content 创建后不允许用户修改。

`content_revision` 用于 Approval Contract；`lock_version` 用于并发控制，两者不得混淆。

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
last_observed_status VARCHAR(32) NOT NULL
submitted_at TIMESTAMPTZ NOT NULL
last_observed_at TIMESTAMPTZ NOT NULL
```

约束：

```text
UNIQUE(provider, execution_id)
UNIQUE(submission_key)
```

`submission_key`：

```text
proposal:{proposal_id}:execute:{content_revision}
```

真正 SOAR Execution 的 Source of Truth 始终是 HISIEM。

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
idempotency_key TEXT PK
command_id UUID NOT NULL UNIQUE
command_type VARCHAR(128) NOT NULL
tenant_id TEXT NOT NULL
aggregate_type VARCHAR(64) NULL
aggregate_id UUID NULL
result_ref_type VARCHAR(64) NULL
result_ref_id TEXT NULL
safe_result JSONB NULL
completed_at TIMESTAMPTZ NOT NULL
```

`safe_result` 只保存重复调用时需要返回的最小安全结果，不保存 raw Tool response、凭据、Prompt 或敏感日志内容。

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
published_at TIMESTAMPTZ NULL
last_error_code VARCHAR(128) NULL
created_at TIMESTAMPTZ NOT NULL
```

Status：

```text
PENDING
PROCESSING
PUBLISHED
FAILED
```

约束：

```text
UNIQUE(event_id, destination)
```

主要索引：

```text
(status, available_at)
WHERE status IN ('PENDING', 'FAILED')
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

`SubmitApprovedResponse` 使用外部幂等边界：

```text
1. Read and validate approved Proposal
2. Derive stable submission_key
3. Check existing ResponseExecutionRef
4. Close local transaction
5. Call HISIEM SOAR using stable request identity
6. Open new transaction
7. Insert ResponseExecutionRef
8. Insert ResponseExecutionSubmitted Event
9. Insert CommandReceipt
10. Commit
```

如果 SOAR 已接受请求但 Copilot 在持久化 execution ref 前崩溃，重试必须使用相同 `submission_key`，由 HISIEM 去重并恢复同一 execution，禁止生成第二次实际响应。

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
one execution ref per proposal
unique plan revision
unique hypothesis assessment revision
evidence deduplication
confidence range
status enum validity
referential integrity
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
