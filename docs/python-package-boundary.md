# HISIEM SOC Copilot V1 — Python Package / Module Boundary

## 1. 目的

本文定义 V1 的 Python 工程骨架、模块职责、依赖方向、边界约束、Composition Root、测试结构和初始依赖集合。

核心原则：

```text
Domain model is pure.
Application owns use cases.
Agent owns orchestration, not business authority.
Infrastructure adapts external systems.
API is transport only.
Bootstrap wires everything together.
```

---

## 2. 工程总体结构

正式代码目录：

```text
HISIEM-SOC-Copilot/
├── pyproject.toml
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
│
├── src/
│   └── hisiem_soc_copilot/
│       ├── main.py
│       ├── config.py
│       ├── domain/
│       ├── application/
│       ├── contracts/
│       ├── agent/
│       ├── api/
│       ├── infrastructure/
│       └── bootstrap/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── architecture/
│   ├── e2e/
│   └── fixtures/
│
├── docs/
└── infra/
    └── docker-compose.yml
```

使用标准 `src layout`，项目根目录不直接作为 Python package。

---

## 3. `domain/`

Domain 是最内层，只表达业务模型和业务不变量。

```text
domain/
├── investigation/
│   ├── aggregate.py
│   ├── entities.py
│   ├── value_objects.py
│   ├── enums.py
│   ├── events.py
│   └── errors.py
│
├── response/
│   ├── aggregate.py
│   ├── entities.py
│   ├── value_objects.py
│   ├── enums.py
│   ├── events.py
│   ├── policy.py
│   └── errors.py
│
└── shared/
    ├── entity.py
    ├── event.py
    ├── identifiers.py
    └── errors.py
```

Domain 允许依赖：

```text
Python stdlib
typing
dataclasses
enum
datetime
uuid
```

Domain 禁止依赖：

```text
FastAPI
SQLAlchemy
Alembic
LangGraph
OpenAI SDK
HTTPX
Kafka
Redis
Celery
PostgreSQL driver
```

---

## 4. Domain Model Implementation

核心 Domain Model 推荐使用：

```text
dataclass
Enum
Value Object
explicit Aggregate methods
```

示例：

```python
@dataclass
class Investigation:
    id: UUID
    status: InvestigationStatus
    lock_version: int

    def cancel(self, actor: ActorRef, now: datetime) -> list[DomainEvent]:
        ...
```

SQLAlchemy ORM Model 不得直接作为 Domain Entity。

Domain Aggregate 负责状态转换与不变量，不能由外部层直接修改字段绕过方法。

---

## 5. `application/`

Application 定义 Use Case、Commands、Queries、Ports 和 Application Services。

```text
application/
├── commands/
│   ├── investigation.py
│   ├── evidence.py
│   ├── hypothesis.py
│   ├── response.py
│   └── approval.py
│
├── handlers/
│   ├── investigation.py
│   ├── evidence.py
│   ├── hypothesis.py
│   ├── response.py
│   └── approval.py
│
├── queries/
│   ├── investigation.py
│   ├── workspace.py
│   └── response.py
│
├── ports/
│   ├── repositories.py
│   ├── unit_of_work.py
│   ├── hisiem.py
│   ├── threat_intel.py
│   ├── knowledge.py
│   ├── soar.py
│   ├── clock.py
│   └── event_publisher.py
│
├── services/
│   ├── investigation_service.py
│   ├── evidence_service.py
│   └── response_service.py
│
└── errors.py
```

Application 可以依赖：

```text
domain
stdlib
```

Application 通过 `Protocol / ABC` 依赖外部能力。

Application 不得 import `infrastructure.*`。

---

## 6. Application Commands 与 Queries

Command 表达改变业务事实的意图，例如：

```text
StartAlertInvestigation
CancelInvestigation
ReviseInvestigationPlan
RecordEvidenceBatch
AssessHypotheses
RecordFindings
FinalizeInvestigationResult
CreateResponseProposal
RequestResponseApproval
ApproveResponse
RejectResponse
SubmitApprovedResponse
CompleteInvestigation
```

Query 只读取事实，例如：

```text
GetInvestigation
GetInvestigationWorkspace
GetEvidence
GetFindings
GetAlert
SearchEvents
GetEntityContext
LookupThreatIntel
RetrieveKnowledge
GetSoarExecution
```

Tool Call 本身不是 Domain Command。

---

## 7. Repository Ports

Repository Interface 位于：

```text
application/ports/repositories.py
```

示例：

```python
class InvestigationRepository(Protocol):
    async def get(
        self,
        *,
        tenant_id: str,
        investigation_id: UUID,
    ) -> Investigation | None:
        ...

    async def add(self, investigation: Investigation) -> None:
        ...
```

公开 Repository API 的 Query 必须携带 `tenant_id`。

禁止公开：

```python
get(investigation_id)
```

这种不带 Tenant Scope 的查询接口。

---

## 8. UnitOfWork Port

```python
class UnitOfWork(Protocol):
    investigations: InvestigationRepository
    evidence: EvidenceRepository
    ...

    async def commit(self) -> None:
        ...

    async def rollback(self) -> None:
        ...
```

Application Handler 不接触 AsyncSession、Connection 或 SQL statement。

---

## 9. `contracts/`

Pydantic v2 放在边界层，不污染 Domain。

```text
contracts/
├── api/
│   ├── investigation.py
│   ├── approval.py
│   └── response.py
│
├── llm/
│   ├── plan.py
│   ├── tool_decision.py
│   ├── hypothesis.py
│   ├── finding.py
│   ├── result.py
│   └── response.py
│
└── tools/
    ├── common.py
    ├── alert.py
    ├── events.py
    └── threat_intel.py
```

用途：

```text
REST validation
LLM Structured Output
Tool argument validation
Tool result boundary validation
```

Pydantic Object 不是 Domain Entity，必须经过显式 Mapping。

---

## 10. `agent/`

Agent Package 只负责认知与编排。

```text
agent/
├── graph/
│   ├── state.py
│   ├── input.py
│   ├── output.py
│   ├── builder.py
│   ├── routing.py
│   └── nodes/
│       ├── load_investigation.py
│       ├── hydrate_alert.py
│       ├── plan.py
│       ├── decide_next.py
│       ├── execute_read_tool.py
│       ├── ingest_evidence.py
│       ├── assess.py
│       ├── finalize_result.py
│       ├── prepare_response.py
│       ├── request_approval.py
│       ├── wait_approval.py
│       ├── load_approval.py
│       ├── submit_response.py
│       └── complete.py
│
├── llm/
│   ├── provider.py
│   ├── request.py
│   ├── result.py
│   └── structured.py
│
├── tools/
│   ├── definition.py
│   ├── registry.py
│   ├── executor.py
│   └── policy.py
│
├── evidence/
│   ├── normalizer.py
│   └── dedup.py
│
├── prompts/
│   ├── planning.py
│   ├── investigation.py
│   ├── assessment.py
│   └── result.py
│
└── errors.py
```

---

## 11. Agent Dependency Rule

允许：

```text
agent
  ↓
application
  ↓
domain
```

禁止：

```text
agent
  ↓
infrastructure
```

例如以下 import 禁止：

```python
from hisiem_soc_copilot.infrastructure.hisiem import HisiemClient
```

正确路径：

```text
Agent Node
→ Application Query / Port
→ Injected Adapter
```

---

## 12. Graph Node Rule

Graph Node 必须保持 Thin。

示例：

```python
async def ingest_evidence_node(
    state: InvestigationGraphState,
    deps: AgentDependencies,
) -> dict:
    command = RecordEvidenceBatch(...)
    result = await deps.command_bus.execute(command)

    return {
        "new_evidence_ids": result.evidence_ids,
        "investigation_revision": result.revision,
    }
```

Node 禁止：

```text
SQLAlchemy Session
raw SQL
direct ORM update
manual commit
direct HISIEM database access
direct Aggregate field mutation
```

Graph Node 只做 orchestration adapter：准备输入、调用 Application、返回最小 Graph State delta。

---

## 13. LangGraph State Boundary

Graph State 使用 `TypedDict`，保存跨步骤真正需要持续的 bounded working state。

建议字段：

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

不得进入 Graph State：

```text
System Prompt
Formatted Prompt
Full LLM conversation history
Chain-of-Thought
Full Tool Result
Full Evidence collection
Full Investigation Aggregate
Full Domain Event history
HTML / UI representation
Authentication token
API keys
Authorization claims supplied by model
```

V1 不使用 `MessagesState` 作为核心 State。

---

## 14. `agent/llm` Provider Boundary

`agent/llm/provider.py` 定义轻量 Provider Protocol。

具体实现放：

```text
infrastructure/llm/
```

例如：

```text
OpenAIProvider
CompatibleProvider
```

依赖关系：

```text
Agent
  ↓
ModelProvider Protocol

Infrastructure
  ↓
Provider Implementation
```

Agent 不绑定具体模型 SDK。

---

## 15. Tool Boundary

`agent/tools/` 定义：

```text
ToolDefinition
ToolRegistry
ToolExecutor
ToolPolicy
```

LLM 只产生 Tool Candidate。

执行链：

```text
Model Tool Candidate
→ Schema Validation
→ Authenticated Scope Binding
→ Resource Scope Validation
→ Budget / Tool Policy
→ Adapter Execution
→ ToolResult
→ Evidence Normalizer
```

Tenant、Actor、Authorization Scope 不允许由模型自由填写。

---

## 16. `infrastructure/`

Infrastructure 负责所有外部实现。

```text
infrastructure/
├── persistence/
│   ├── database.py
│   ├── unit_of_work.py
│   ├── orm/
│   │   ├── base.py
│   │   ├── investigation.py
│   │   ├── evidence.py
│   │   ├── hypothesis.py
│   │   ├── finding.py
│   │   ├── response.py
│   │   ├── events.py
│   │   └── operations.py
│   ├── mappers/
│   │   ├── investigation.py
│   │   ├── evidence.py
│   │   └── response.py
│   └── repositories/
│       ├── investigation.py
│       ├── evidence.py
│       ├── hypothesis.py
│       └── response.py
│
├── checkpoint/
│   ├── postgres.py
│   └── setup.py
│
├── hisiem/
│   ├── client.py
│   ├── mapper.py
│   └── adapter.py
│
├── soar/
│   └── adapter.py
│
├── threat_intel/
│   └── adapter.py
│
├── knowledge/
│   └── adapter.py
│
├── llm/
│   ├── openai.py
│   └── compatible.py
│
├── messaging/
│   ├── outbox.py
│   └── dispatcher.py
│
└── observability/
    ├── tracing.py
    └── metrics.py
```

---

## 17. ORM Boundary

SQLAlchemy ORM Model 只存在：

```text
infrastructure.persistence.orm
```

不得从该目录泄漏到：

```text
domain
application
agent
api
```

Repository 返回 Domain Entity 或 Application Read DTO，不返回 ORM Entity。

---

## 18. Mapper Boundary

使用显式 Mapper：

```text
ORM
 ↕
Domain
```

例如：

```text
InvestigationRow
→ InvestigationMapper
→ Investigation
```

禁止依赖 `Pydantic from_attributes=True` 将 ORM Model 直接当作 Domain Model。

API Serialization 使用 Pydantic，但输入应来自 Application Read Model，而不是 ORM Entity。

---

## 19. `api/`

FastAPI 只负责 Transport。

```text
api/
├── app.py
├── dependencies.py
├── errors.py
├── routers/
│   ├── investigations.py
│   ├── approvals.py
│   └── events.py
└── schemas/
    └── common.py
```

Router 执行链：

```text
HTTP
→ Authenticate
→ Build Trusted Context
→ Application Command / Query
→ HTTP Response
```

Router 禁止：

```text
Router → Repository
Router → SQLAlchemy
Router → LangGraph Node
Router → HISIEM Client
```

---

## 20. Initial API Surface

工程骨架预留：

```text
POST /api/v1/investigations
GET  /api/v1/investigations/{id}
GET  /api/v1/investigations/{id}/workspace
POST /api/v1/investigations/{id}/cancel
GET  /api/v1/investigations/{id}/events
POST /api/v1/approvals/{id}/approve
POST /api/v1/approvals/{id}/reject
```

Approval Actor 必须来自 Authentication Context，不得放入 Request Body。

---

## 21. `bootstrap/`

Composition Root：

```text
bootstrap/
├── container.py
├── lifespan.py
├── graph.py
└── workers.py
```

只有 Bootstrap 允许同时知道 Domain、Application、Agent、Infrastructure 和 API 的具体实现。

示例绑定：

```text
HisiemPort
← HisiemHttpAdapter

ModelProvider
← OpenAIProvider

UnitOfWork
← SqlAlchemyUnitOfWork

Graph
← AsyncPostgresSaver
```

---

## 22. `main.py`

`main.py` 只负责：

```text
load config
build container
build FastAPI app
register lifespan
```

禁止出现：

```text
business logic
SQL queries
prompt
tool implementation
graph node logic
```

---

## 23. Dependency Direction

固定依赖方向：

```text
              domain
                ▲
                │
           application
            ▲       ▲
            │       │
         agent    contracts
            ▲       ▲
            │       │
     infrastructure api
            ▲       ▲
             \     /
             bootstrap
```

语义：

```text
domain
↑
application
↑
agent

inner abstractions
↑
infrastructure implementations

application/contracts
↑
api

everything
↑
bootstrap
```

没有任何内层 Package 允许 import 外层 Implementation。

---

## 24. Forbidden Imports

以下全部禁止：

```text
domain → application
domain → agent
domain → infrastructure
domain → api

application → agent
application → infrastructure
application → api

agent → infrastructure
agent → api

infrastructure → api

api → infrastructure.persistence.orm
```

架构测试必须自动检查主要边界。

---

## 25. Configuration

唯一配置入口：

```text
config.py
```

配置使用 typed settings。

主要配置域：

```text
ApplicationSettings
DatabaseSettings
LangGraphSettings
HisiemSettings
LLMSettings
AgentBudgetSettings
ObservabilitySettings
```

Secret 不进入 Graph State、Domain Event、Command、Tool Result 或日志。

---

## 26. Database Configuration

逻辑配置：

```text
COPILOT_DATABASE_URL
LANGGRAPH_DATABASE_URL
```

二者可以指向同一 Database，但使用不同 Schema 与不同 Pool。

```text
COPILOT_DATABASE_URL
→ copilot

LANGGRAPH_DATABASE_URL
→ langgraph_checkpoint
```

---

## 27. Infrastructure Initialization

部署顺序：

```text
1. PostgreSQL available
2. create required schemas
3. alembic upgrade head
4. initialize LangGraph checkpoint schema
5. application health check
6. start API / workers
```

生产启动时不得由多个 API Worker 竞态执行业务 Migration。

Migration 必须是独立 Deployment Step。

---

## 28. `tests/`

```text
tests/
├── unit/
│   ├── domain/
│   ├── application/
│   └── agent/
│
├── integration/
│   ├── persistence/
│   ├── hisiem/
│   ├── checkpoint/
│   └── api/
│
├── architecture/
│   └── test_import_boundaries.py
│
├── e2e/
│   └── test_ssh_bruteforce_investigation.py
│
└── fixtures/
```

---

## 29. Domain Tests

不启动数据库。

重点验证：

```text
state transition
Finding Evidence invariant
INCONCLUSIVE invariant
Approval invariant
Response policy
terminal state
content hash binding
```

---

## 30. Persistence Integration Tests

使用真实 PostgreSQL。

至少覆盖：

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

---

## 31. Concurrency Test

必须存在两个并发 `StartInvestigation` 针对同一 Tenant + Alert 的测试。

结果必须：

```text
exactly one Active Investigation
```

不能只依赖 Application 层 `SELECT then INSERT`，必须由 PostgreSQL Partial Unique Index 最终收敛。

---

## 32. LangGraph Persistence Test

真实 PostgreSQL Checkpointer：

```text
START
→ execute several nodes
→ checkpoint
→ simulate process restart
→ resume same thread_id
→ continue
```

并验证 Domain State 仍为 Source of Truth。

---

## 33. HITL Recovery Test

```text
Request Approval
→ interrupt
→ persist ApprovalDecision
→ simulate process restart
→ resume
→ reload immutable Decision
→ continue exactly once
```

---

## 34. Side-effect Recovery Test

模拟：

```text
SOAR accepts request
→ Copilot crashes before persisting execution ref
→ retry SubmitApprovedResponse
```

必须得到：

```text
same submission_key
same SOAR execution
no duplicate response
```

---

## 35. Architecture Test

至少自动检查：

```text
domain 不 import sqlalchemy/langgraph/fastapi
application 不 import infrastructure
agent 不 import infrastructure
api 不 import ORM models
```

架构边界不得只存在于文档。

---

## 36. V1 Initial Dependencies

Runtime Core：

```text
fastapi
uvicorn
pydantic
pydantic-settings
sqlalchemy
psycopg
alembic
langgraph
langgraph-checkpoint-postgres
httpx
```

Test / Quality：

```text
pytest
pytest-asyncio
ruff
mypy
```

暂不提前加入：

```text
Celery
Kafka client
pgvector
reranker
multi-agent framework
```

这些依赖仅在对应能力进入实现阶段后增加。

---

## 37. Module Ownership Summary

| Module | Owns |
|---|---|
| `domain` | Entities, Aggregates, Value Objects, Invariants, Domain Events |
| `application` | Commands, Queries, Handlers, Ports, UoW contracts |
| `contracts` | API/LLM/Tool boundary schemas |
| `agent` | LangGraph orchestration, LLM/tool coordination, bounded working state |
| `api` | HTTP transport and authenticated request context |
| `infrastructure` | PostgreSQL, HISIEM, SOAR, LLM, external provider implementations |
| `bootstrap` | Dependency wiring and process startup |

---

## 38. 最终工程边界

```text
              FastAPI
                 │
                 ▼
            Application
                 │
          ┌──────┴──────┐
          ▼             ▼
       Domain        LangGraph
          ▲             │
          │             │
          └──────┬──────┘
                 │ Ports
                 ▼
           Infrastructure
          ┌──────┼──────────┐
          ▼      ▼          ▼
      PostgreSQL HISIEM     LLM
                           / Tools
```

Persistence：

```text
Domain Fact
→ copilot PostgreSQL schema
→ Alembic-owned
```

Runtime：

```text
Graph Working State
→ langgraph_checkpoint schema
→ LangGraph-owned
```

External Security Facts：

```text
Alert / Event / Case / SOAR
→ HISIEM-owned
```

---

## 39. 冻结决策

| 项目 | 决策 |
|---|---|
| Python Layout | `src/` layout |
| Domain Model | Pure Python dataclass / Value Object |
| Boundary Validation | Pydantic v2 |
| ORM Exposure | Infrastructure only |
| Application DB Access | UnitOfWork / Repository ports |
| Agent Runtime | LangGraph |
| Graph State | TypedDict / bounded working state |
| Graph Node | Thin orchestration adapter |
| LLM Provider | Own lightweight Protocol |
| Infrastructure Imports | Forbidden from inner layers |
| API Role | Transport only |
| Composition Root | `bootstrap/` |
| Architecture Boundary | Enforced by tests |
| Long-term Memory | Not part of V1 |
| Chat Model | Not part of V1 |
| Multi-Agent | Not part of V1 skeleton |
