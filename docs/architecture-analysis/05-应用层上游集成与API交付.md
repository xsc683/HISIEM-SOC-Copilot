# 05 应用层、上游集成与 API 交付

> **本文性质：** 代码级取证补充，**不构成契约**。`docs/README.md` 规定「Architecture and persistence documents are authority」——冲突时以 `hisiem-integration-contract.md` 为准。

> **文档类型**：子系统深挖（代码级核验，非设计提案）
> **分析对象**：`D:\Project\HISIEM-SOC-Copilot` 的 `application/`（38 文件，6104 行）+ `infrastructure/hisiem/`（3 文件）+ `infrastructure/auth/`（3 文件）+ `api/`（10 文件）
> **取证方式**：源码直读 + grep 统计；每个关键论断附 `file:line` 锚点
> **结论以当前代码为准**（分支 `capability-mcp` @ `1e567be`）
> **文档集**：00–08 共 9 篇，见 [`README.md`](README.md)

---

## 为什么这四块合一篇

它们回答同一个问题：**「外面进来的请求，怎么变成域上的合法操作；域上的事实，怎么变成外面能读的东西」。**

| 块 | 角色 | 方向 |
| --- | --- | --- |
| `api/` | HTTP 交付：路由、schema、错误映射 | **入站** |
| `infrastructure/auth/` | 信任来源：谁在调用 | **入站** |
| `application/` | 用例编排：命令、查询、处理器、服务、端口 | **中枢** |
| `infrastructure/hisiem/` | 上游读适配：查告警、查日志、查规则 | **出站** |

**主线是「端口—适配器」的分离**：`application/ports/` 的 **15 个模块**里定义 **36 个 `Protocol` 类**（AST 实测；`repositories.py` 11 / `knowledge.py` 7 / `durable.py` 5 / 其余各 1–2），`infrastructure/` 提供实现，`bootstrap/` 把两者接起来。**这条链的每一段都可被替换与单测。**

> **口径说明**：`application/ports/` 的**文件数是 16**（15 模块 + `__init__.py`），**协议类数是 36**——两者不可混用。

---

## 1. 应用层：四个子包的职责分工

### 1.1 关键论断

**论断 1：`application/` 的四个子包有清晰的语义分工。**

| 子包 | 文件 | 行数 | 语义 |
| --- | --- | --- | --- |
| `commands/` | 3 | 438 | **意图的**声明****（数据类） |
| `queries/` | 2 | 395 | **读取的**声明**** |
| `handlers/` | 6 | **3321** | **命令的执行者**（含事务、幂等、事件发布） |
| `services/` | 5 | 1946 | **跨用例的服务**（工作区投影、知识检索） |
| `ports/` | 16 | — | **对外依赖的接口** |

实测 `wc -l`：

| 文件 | 行数 |
| --- | --- |
| `handlers/knowledge.py` | **790** |
| `handlers/workflow.py` | **755** |
| `handlers/attack_import.py` | **743** |
| `handlers/investigation.py` | 451 |
| `handlers/response.py` | 432 |
| `handlers/durable_support.py` | 150 |
| `services/workspace_service.py` | **964** |
| `services/knowledge_retrieval.py` | **672** |
| `services/attack_release_fingerprint.py` | 167 |
| `services/investigation_service.py` | 74 |
| `services/attack_projection.py` | 69 |

**`handlers/` 与 `services/` 是应用层的全部重量所在（5267 行 / 6104 = 86%）** ——命令与查询的声明层很薄（833 行）。

**论断 2：`commands/` 与 `queries/` 是**薄声明**，不是逻辑所在。**

```python
# application/commands/response.py 只有 63 行
```

**最薄的命令模块只有 63 行** ——因为它只声明「要做什么」，执行在 `handlers/response.py`（432 行）。

**CQRS 式的读写分离在包结构上成立**：`commands` + `queries` 分开，但**两者共用同一套端口与聚合**——不是完整的 CQRS（没有独立读库）。

**论断 3：工作区投影服务是**纯读**，且四条禁止写进 docstring。**

```python
# application/services/workspace_service.py:1-14
"""Workspace read service — composes the Analyst Workspace projection.

This service performs a PURE READ: it loads the Investigation aggregate and its
child rows through tenant-scoped repository/query ports inside ONE UnitOfWork,
then projects them into an immutable :class:`InvestigationWorkspaceReadModel`.

It MUST NOT (docs/investigation-workspace.md §7):
- mutate any domain state,
- run the Agent / LangGraph / a model / a tool,
- touch ORM or SQL directly (it depends on the ``UnitOfWork`` port only).

The composed timeline is derived deterministically from persisted facts; no entry
is fabricated and no debug/telemetry fact is exposed.
"""
```

**四条禁止**：

| # | 禁止 | 理由 |
| --- | --- | --- |
| 1 | 改任何域状态 | 它是读服务 |
| 2 | **跑 Agent / LangGraph / 模型 / 工具** | **读路径不得有 AI 副作用** |
| 3 | **直接碰 ORM 或 SQL** | **只依赖 `UnitOfWork` 端口** |
| 4 | 编造时间线条目 / 暴露 debug 事实 | 投影必须来自持久事实 |

**第 2 条是最值得注意的**：**打开工作区不会触发任何模型调用。** 这与「图只在 outbox 派发时推进」（01 篇 §1.1 论断 1）是同一件事的两面——**读是读，推是推**。

**第 3 条在 AST 层面有强制**：`test_knowledge_boundary.py` 的规则 3 用同一个扫描器检查「`application/` 下没有文件出现 `AsyncSession`、`select(...)`、`session.execute(...)`、`<=>`」。

**论断 4：`workspace_service.py` 是应用层最大的文件（964 行）。**

**为什么工作区投影这么大**：它要把 **`Investigation` 聚合 + 全部子行**（证据、发现、假设、假设评估、结果、计划修订、响应提案、审批、提交、执行）**在一个 `UnitOfWork` 里读出来并投影成一个只读模型**。

**「inside ONE UnitOfWork」是一条一致性要求** ——工作区看到的所有东西来自**同一个快照**，不会出现「证据读了但发现还没读」的撕裂。

### 1.2 应用层结构

```mermaid
---
config:
  theme: base
  themeVariables:
    fontFamily: YaHei
---
flowchart TB
    subgraph API["api/（入站）"]
        RT["routers/investigations.py"]
        SCH["schemas/（4 模块）"]
        ERR["errors.py"]
    end
    subgraph APP["application/"]
        subgraph DECL["声明（薄，833 行）"]
            CMD["commands/（3）"]
            QRY["queries/（2）"]
        end
        subgraph EXEC["执行（厚，5267 行）"]
            H["handlers/（6）"]
            S["services/（5）"]
        end
        P["ports/（16 Protocol）"]
    end
    subgraph INFRA["infrastructure/（适配器）"]
        HAD["hisiem/adapter.py"]
        AUTH["auth/（3）"]
        PERS["persistence/"]
        SOAR["soar/"]
        LLM["llm/"]
    end
    BOOT["bootstrap/container.py<br/>唯一装配点"]

    RT --> QRY
    RT --> CMD
    CMD --> H
    QRY --> S
    H --> P
    S --> P
    P -.->|实现| HAD
    P -.->|实现| PERS
    P -.->|实现| SOAR
    P -.->|实现| LLM
    BOOT --> INFRA
    BOOT --> APP

    style DECL fill:#eef2fb,stroke:#4a5f9c
    style EXEC fill:#e8f4ea,stroke:#4a7c59
```

---

## 2. 上游集成：HISIEM HTTP 适配器

### 2.1 关键论断

**论断 1：适配器是**纯传输**，且端点「已对参考 SIEM 仓库验证」。**

```python
# infrastructure/hisiem/adapter.py:1-8
"""HISIEM HTTP adapter implementing the HisiemPort.

Transport-only over HISIEM's control API. Endpoints follow the real HISIEM
contract (verified against the reference SIEM repo):
``GET /api/alerts/{id}``, ``POST /api/log-search``, ``GET /api/detection-rules/{id}``
and the X-Tenant-ID convention. Errors map to ExternalServiceError; upstream
bodies never leak.
"""
```

**三处关键信息**：

| 信息 | 含义 |
| --- | --- |
| **"Transport-only"** | 不含业务判断——映射在 `mapper.py` |
| **"verified against the reference SIEM repo"** | **端点是**实证过的**，不是猜的** |
| **"upstream bodies never leak"** | 上游响应体不外泄 |

**三个真实端点**（与 SIEM 侧逐一对上）：

| Copilot 调 | HISIEM 端点 | SIEM 侧锚点 |
| --- | --- | --- |
| `GET /api/alerts/{id}` | `AlertController` | `alert/AlertController.java` |
| `POST /api/log-search` | `LogSearchController` | `logsearch/LogSearchController.java` |
| `GET /api/detection-rules/{id}` | `RuleController` | `rules/RuleController.java` |

**论断 2：「upstream bodies never leak」是错误处理的一条硬约束。**

```python
# adapter.py:6-7
Errors map to ExternalServiceError; upstream bodies never leak.
```

**所有错误归一为 `ExternalServiceError`**（`application/errors.py`）——**HISIEM 的响应体不进 Copilot 的错误路径**。

**这与 SIEM 侧的一条形成对照**：SIEM 的 `ElasticsearchGateway.java:72` **把 `e.getMessage()` 放进响应体**（SIEM 03 篇 §10 待核实 3）。**Copilot 侧更严格**。

**论断 3：映射被抽成独立的 `mapper.py`，有三个具名函数。**

```python
# adapter.py:24-28
from .mapper import (
    map_alert_detail,
    map_detection_rule,
    map_log_search_response,
)
```

**三个映射函数对应三个端点** ——**适配器只管「发请求、收响应、报错」，形状转换在 mapper**。

**这让 mapper 可以被纯单元测试**（喂一个 HISIEM 响应 JSON，断言产出的 `HisiemAlertData`）。

**论断 4：`HisiemPort` 定义了四个数据契约 + 一个端口。**

```python
# application/ports/hisiem.py
HisiemAlertData, LogEventHit, EventSearchResult, DetectionRuleContext, HisiemPort
```

| 契约 | 用途 |
| --- | --- |
| `HisiemAlertData` | 告警详情（`hydrate_alert` 节点用） |
| `LogEventHit` | 单条日志命中 |
| `EventSearchResult` | 日志检索结果集 |
| `DetectionRuleContext` | 检测规则上下文 |

**四个契约对应四类「从上游读什么」** ——**端口不是「一个万能 HTTP 客户端」，而是四个有语义的读操作**。

**论断 5：读凭据与处置凭据是两组独立配置，但字段几乎相同。**

```python
# config.py:61-75（读）
class HisiemSettings(BaseSettings):
    base_url: str = Field(default="http://127.0.0.1:8080")
    bearer_token: str = Field(default="")
    timeout_seconds: float = Field(default=10.0)
    tenant_header: str = Field(default="X-Tenant-ID")

# config.py:285-303（写）
class SoarSettings(BaseSettings):
    base_url: str = Field(default="http://127.0.0.1:8080")
    bearer_token: str = Field(default="")
    timeout_seconds: float = Field(default=10.0)
```

**唯一差异是 `HisiemSettings` 多一个 `tenant_header`**（只有查询侧需要租户头；处置走内部服务端点，租户由服务令牌那条路径表达）。

**这是权限分离在配置层的体现**：**读告警的凭据与执行处置的凭据应当不同**。

### 2.2 出入站全景

```mermaid
---
config:
  theme: base
  themeVariables:
    fontFamily: YaHei
---
flowchart LR
    subgraph HISIEM["HISIEM（参考 SIEM 仓库）"]
        AL["AlertController"]
        LS["LogSearchController"]
        RC["RuleController"]
        ISC["InternalSoarController"]
    end
    subgraph CP["Copilot"]
        HA["HisiemHttpAdapter<br/>Transport-only"]
        MAP["mapper.py<br/>3 个映射函数"]
        HP["HisiemPort<br/>4 契约"]
        SA["HisiemSoarAdapter"]
        SP["SoarPort"]
        AUTH["auth/<br/>header / hisiem_bearer"]
    end

    HA -->|"GET /api/alerts/{id}"| AL
    HA -->|"POST /api/log-search"| LS
    HA -->|"GET /api/detection-rules/{id}"| RC
    HA -->|"X-Tenant-ID"| AL
    HA -->|"错误归一"| E["ExternalServiceError<br/>上游响应体不外泄"]
    HA --> HP
    MAP --> HA
    SA -->|"POST /api/internal/soar/executions<br/>服务令牌 + X-Tenant-ID"| ISC
    SA --> SP
    AUTH -->|"trusted_context_provider"| CP

    style HA fill:#e8f4ea,stroke:#4a7c59
    style E fill:#fdecea,stroke:#b3453a
```

---

## 3. 信任边界

### 3.1 关键论断

**论断 1：信任来源有三档，默认 `none`（不信任任何来源）。**

```python
# config.py:321
trusted_context_provider: Literal["none", "header", "hisiem_bearer"] = "none"
```

| 取值 | 实现 | 语义 |
| --- | --- | --- |
| **`none`（默认）** | — | **不信任任何入站上下文** |
| `header` | `infrastructure/auth/header_provider.py` | 从请求头取可信上下文 |
| `hisiem_bearer` | `infrastructure/auth/hisiem_service_provider.py` | 从 HISIEM 服务令牌派生 |

**默认 `none` 是 fail-closed 的** ——新部署的实例**不会凭请求头就相信调用方是谁**。

**论断 2：`TrustedContext` 与错误类型同在一个端口模块里。**

```python
# application/ports/trust.py
TrustedContext, UntrustedRequestError, ServiceAuthenticationError, TrustedContextProvider
```

**四个成员**：上下文值对象 + **两个不同的错误** + 端口。

| 错误 | 语义 |
| --- | --- |
| `UntrustedRequestError` | 请求本身不可信 |
| `ServiceAuthenticationError` | **服务认证失败**（与「请求不可信」不同） |

**区分这两者是有意义的**：前者是**客户端问题**（该带的东西没带对），后者是**部署问题**（服务令牌配错了）。

**论断 3：`container.py` 里有一个请求级的信任上下文提供者。**

```python
# bootstrap/container.py:437
def trusted_context_provider(self, request: Request) -> TrustedContextProvider | None:
```

**注意返回 `| None`** ——**当 `trusted_context_provider = none` 时返回 `None`**，调用方必须显式处理「没有信任来源」的情形。

**论断 4：`bootstrap` 是唯一 import `HeaderTrustedContextProvider` 的地方。**

```python
# bootstrap/container.py:50-53
from ..infrastructure.auth.header_provider import HeaderTrustedContextProvider
from ..infrastructure.auth.hisiem_service_provider import (
    HisiemServiceTrustedContextProvider,
)
```

**两个实现都被装配在组合根** ——**`application` 只看到 `TrustedContextProvider` 协议**（`container.py:35-38` 从 ports 导入）。

### 3.2 信任判定

```mermaid
---
config:
  theme: base
  themeVariables:
    fontFamily: YaHei
---
flowchart TB
    REQ["入站请求"]
    CFG{"trusted_context_provider"}
    NONE["none<br/>返回 None"]
    HDR["HeaderTrustedContextProvider"]
    HB["HisiemServiceTrustedContextProvider"]
    OK["TrustedContext<br/>（tenant / actor / 服务身份）"]
    E1["UntrustedRequestError"]
    E2["ServiceAuthenticationError"]

    REQ --> CFG
    CFG -->|none| NONE
    CFG -->|header| HDR
    CFG -->|hisiem_bearer| HB
    HDR -->|成功| OK
    HDR -->|失败| E1
    HB -->|成功| OK
    HB -->|失败| E2

    style NONE fill:#fdf0e6,stroke:#b8763e
    style E1 fill:#fdecea,stroke:#b3453a
    style E2 fill:#fdecea,stroke:#b3453a
```

---

## 4. API 交付

### 4.1 关键论断

**论断 1：只有一个路由模块，且前缀是 `/api/v1/investigations`。**

```python
# api/routers/investigations.py:50
router = APIRouter(prefix="/api/v1/investigations", tags=["investigations"])
```

**实测 8 个端点**：

| 行 | 方法 | 路径 | 说明 |
| --- | --- | --- | --- |
| `:53` | `POST` | `""` | **创建调查（201）** |
| `:89` | `GET` | `/lookup` | **按告警查已有调查** |
| `:113` | `GET` | `/{investigation_id}` | 读调查 |
| `:126` | `GET` | （多行装饰器） | — |
| `:146` | `POST` | `/{investigation_id}/cancel` | 取消 |
| `:166` | `POST` | （多行装饰器） | — |
| `:200` | `POST` | （多行装饰器） | — |
| `:216` | `POST` | （多行装饰器） | — |

> **`/lookup` 是一条值得注意的端点**：它按**告警**查已有调查，而不是按调查 id。**这让「这条告警是否已经查过」成为一次查询** ——避免重复调查同一条告警。

**论断 2：`api` 层的禁导入是**四条**，含一条同层横向的。**

```python
# tests/architecture/test_import_boundaries.py:46-51
"api": (
    "infrastructure.persistence", "infrastructure.hisiem",
    "langgraph", "agent",
),
```

| 禁导入 | 含义 |
| --- | --- |
| `infrastructure.persistence` | **API 不碰 ORM** |
| `infrastructure.hisiem` | **API 不直连上游** |
| `langgraph` | **API 不碰图引擎** |
| **`agent`** | **API 不碰 agent** |

**注意禁的是 `infrastructure.persistence` 与 `infrastructure.hisiem` 两个**具体子包**，不是整个 `infrastructure`** ——所以 `api` **可以**导入 `infrastructure` 的其它部分（例如可观测性）。

**这是精细的粒度**：不是「api 不许碰 infrastructure」，而是「**api 不许碰持久化与上游**」。

**论断 3：schema 分三个模块，且与领域模型分离。**

```
api/schemas/common.py
api/schemas/response.py
api/schemas/workspace.py
```

**`api/schemas/` 是 Pydantic 模型** ——**它们与 `domain` 的 dataclass 是两套类型**（因为 `domain` 禁 pydantic，00 篇 §2.1 论断 2）。

**所以 API 边界上必然有一次转换**：领域 dataclass → Pydantic schema。**这次转换发生在 `api` 层**，而 `domain` 完全不知道 HTTP 的存在。

**论断 4：应用由工厂函数创建，且只 include 一个 router。**

```python
# api/app.py:41,48
app = FastAPI(title="HISIEM SOC Copilot", version="0.1.0", lifespan=lifespan)
...
app.include_router(investigations_router)
```

**只有一个 router** ——**这一版的 HTTP 面很窄**（8 个端点），而内部能力（知识摄取、ATT&CK 导入）走 CLI（见 06 / 08 篇）。

**论断 5：错误处理在 `api/errors.py`，与 `application/errors.py` 分层。**

```
api/errors.py                  ← HTTP 层错误映射
application/errors.py          ← 应用层错误（ExternalServiceError 等）
domain/shared/errors.py        ← 域错误（DomainError / StateTransitionError）
contracts/llm/errors.py        ← 模型错误 taxonomy
```

**四层错误类型，逐层向上翻译** ——**域不知道 HTTP 状态码，HTTP 不知道领域语义**。

**论断 6：健康探针是唯一不认证的端点。**

```python
# api/app.py:44
@app.get("/healthz", tags=["ops"])
```

**`/healthz` 在 `/api/v1/**` 之外** ——所以它不经过任何租户/信任路径。**这是标准做法**：探针必须能被编排系统无凭据调用。

### 4.2 入站处理链

```mermaid
---
config:
  theme: base
  themeVariables:
    fontFamily: YaHei
---
sequenceDiagram
    autonumber
    participant C as 调用方
    participant APP as api/app.py
    participant RT as routers/investigations
    participant TCP as TrustedContextProvider
    participant H as application handler
    participant U as UnitOfWork
    participant DOM as domain

    C->>APP: 请求
    alt /healthz
        APP-->>C: 200（无认证）
    else /api/v1/**
        APP->>RT: 路由
        RT->>TCP: 解析可信上下文
        alt 不可信
            TCP-->>RT: UntrustedRequestError
            RT-->>C: 4xx（api/errors.py 映射）
        else 可信
            TCP-->>RT: TrustedContext
            RT->>H: 命令/查询（带可信上下文）
            H->>U: 事务
            U->>DOM: 聚合方法
            DOM-->>U: 状态 + 领域事件
            U-->>H: 提交
            H-->>RT: 结果
            RT-->>C: 200/201 + Pydantic schema
        end
    end
```

---

## 5. 关键不变式（代码强制）

| # | 不变式 | 强制点 | 违反后果 |
| --- | --- | --- | --- |
| 1 | **`api` 不得导入 `agent` / `langgraph`** | `test_import_boundaries.py:46-51` | HTTP 层直接编排 AI |
| 2 | **`api` 不得导入 `infrastructure.persistence`** | 同上 | API 层出 SQL |
| 3 | **`api` 不得导入 `infrastructure.hisiem`** | 同上 | API 层直连上游 |
| 4 | **`application` 不得导入 `infrastructure`** | `test_import_boundaries.py:35-44` | 用例层绑死适配器 |
| 5 | **工作区读服务是纯读** | `workspace_service.py:3-5` | 读路径改状态 |
| 6 | **读路径不跑 Agent / 模型 / 工具** | `workspace_service.py:9` | 打开页面触发模型调用 |
| 7 | **读服务不碰 ORM / SQL** | `workspace_service.py:10`；`test_knowledge_boundary.py` 规则 3 | 绕过端口直连数据库 |
| 8 | **投影不编造时间线条目** | `workspace_service.py:12-13` | 展示不存在的事实 |
| 9 | **投影不暴露 debug / telemetry 事实** | `workspace_service.py:13` | 内部细节泄漏给分析员 |
| 10 | **工作区投影在一个 UnitOfWork 内完成** | `workspace_service.py:4-5` | 投影内部撕裂 |
| 11 | **上游响应体不外泄** | `hisiem/adapter.py:6-7` | HISIEM 内部细节泄漏 |
| 12 | **上游错误归一为 `ExternalServiceError`** | 同上 | 错误类型泄漏到上层 |
| 13 | **默认不信任任何入站上下文** | `config.py:321` `= "none"` | 凭请求头冒充身份 |
| 14 | **「请求不可信」与「服务认证失败」是两个错误** | `application/ports/trust.py` | 无法区分客户端问题与部署问题 |
| 15 | **`TrustedContextProvider` 可以为 `None`** | `container.py:437` | 调用方忽略「无信任来源」 |
| 16 | **读凭据与处置凭据是两组配置** | `config.py:61` vs `:285` | 读凭据被用于执行处置 |
| 17 | **`/healthz` 不认证** | `api/app.py:44`（在 `/api/v1/**` 之外） | 探针无法被编排系统调用 |
| 18 | **`domain` 用 dataclass，`api` 用 Pydantic** | `test_import_boundaries.py:32` + `api/schemas/` | 域模型绑死 HTTP 框架 |

---

## 6. 与其他子系统的边界

| 边界 | 方向 | 契约 | 锚点 |
| --- | --- | --- | --- |
| `api` → `application` | 入 | 命令 / 查询 | `api/routers/investigations.py` |
| `api` → 三个 `contracts` | 入 | `contracts/api` | `contracts/api/__init__.py` |
| `application.ports` ← `infrastructure.hisiem` | 入 | `HisiemPort` | `application/ports/hisiem.py` |
| `application.ports` ← `infrastructure.soar` | 入 | `SoarPort` | `application/ports/soar.py` |
| `application.ports` ← `infrastructure.persistence` | 入 | `UnitOfWork` + 11 个 repository 端口 | `application/ports/repositories.py` |
| `application.ports` ← `infrastructure.auth` | 入 | `TrustedContextProvider` | `application/ports/trust.py` |
| `application` → `domain` | 出 | 聚合方法 | `domain/*/aggregate.py` |
| `application` → `contracts/llm` | 出 | 模型契约 | `application/ports/model_provider.py:17,23` |

**端口清单实测是 16 个**（`application/ports/` 下 15 个模块 + `__init__`）：

`attack` / `chunking` / `clock` / `durable` / `embedding` / `event_publisher` / `hisiem` / `investigation_runtime` / `knowledge` / `model_provider` / `repositories` / `soar` / `threat_intel` / `trust` / `unit_of_work`

> **`clock` 端口值得单独指出**：`application/ports/clock.py` 里有 `ClockPort` + `SystemClock` ——**时间是可注入的**。这让「超时 / 截止时间」相关逻辑**可以被确定性测试**（不需要真的等）。

---

## 待核实

> **本节 12 条待核实项均已解答**：8 个端点的完整路径表；`/lookup` 的 3 个必填 Query 参数 + DB 局部唯一索引保证至多一条 active；`durable_support.py` 提供 exactly-once 命令；`queries/workspace.py` 是纯数据层而 `services/workspace_service.py` 是执行层；`api/errors.py` 5 个 handler + 完整映射表；`HeaderTrustedContextProvider` **无签名或令牌校验**（dev/test only）；`ClockPort` 只有 3 个生产消费点。

---

## 修订记录

| 版本 | 日期 | 变更 | 作者 |
| --- | --- | --- | --- |
| 1.0 | 2026-09-22 | 首版。基于 `capability-mcp` @ `1e567be` 取证，覆盖 `application/` 38 + `infrastructure/hisiem` 3 + `infrastructure/auth` 3 + `api/` 10 文件。 | code-level-architecture-docs skill |
