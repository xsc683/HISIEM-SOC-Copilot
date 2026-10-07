# HISIEM SOC Copilot — 架构总览

一套图优先的、简洁的系统模型。本文档只负责视觉导航；权威细节在
[`domain-model.md`](../contracts/domain-model.md)、
[`application-commands-domain-events-langgraph-state.md`](../contracts/application-commands-domain-events-langgraph-state.md)、
[`persistence-schema.md`](../contracts/persistence-schema.md) 与
[`python-package-boundary.md`](../contracts/python-package-boundary.md)。

两张规范全图（按平面的架构图 + 端到端数据流）见 [`architecture-diagrams.md`](architecture-diagrams.md)。

> 平面模型一共命名了 **十一个** 平面；「四平面」指的是本仓闭环的那四个。两个数字不是 1:1
> 映射——[`architecture-diagrams.md`](architecture-diagrams.md) §1 有解释。

图 1–8，按它们层层叠加的顺序排列。

---

## 图 1 — 调查与授权链

整个系统收在一张图里。

```mermaid
flowchart TD
    HISIEM["HISIEM<br/>观测到的平台事实"]
    KNOW["知识平面<br/>支撑性上下文"]
    INV["Investigation"]
    EVID["Evidence"]
    FIND["Finding"]
    RESULT["Investigation Result / Verdict"]
    PROP["Response Proposal"]
    POL["Policy<br/>DENY | REQUIRE_APPROVAL"]
    HUM["Human Approval"]
    CMD["Durable Command"]
    SOAR["HISIEM SOAR"]
    OBS["观测到的执行结果"]
    WS["工作区重建"]

    HISIEM --> INV
    KNOW --> INV
    INV --> EVID
    EVID --> FIND
    FIND --> RESULT
    RESULT --> PROP
    PROP --> POL
    POL --> HUM
    HUM --> CMD
    CMD --> SOAR
    SOAR --> OBS
    OBS --> WS
    WS -.->|"分析员自行决策"| HUM
```

```text
模型提案  →  策略约束  →  人授权
持久命令记录意图  →  HISIEM 执行  →  Copilot 观测
```

**模型的影响力止于何处。** `Response Proposal` 以上的每一步都受模型影响。从 `Policy`
往下都是确定性的、由人做的、或被观测到的：策略是一个函数，批准是一个人，命令持久且幂等，
执行发生在 HISIEM，而观测到的结果才是最终事实。

---

## 图 2 — 工具与证据路径

模型选中的一次工具调用，如何变成（或没能变成）Evidence。

```mermaid
flowchart LR
    LLM["LLM 候选<br/>工具 + 参数"]
    REG["ToolRegistry<br/>已准入的只读白名单"]
    POL["ToolPolicy"]
    BUD["ToolBudget"]
    EXE["ToolExecutor"]
    NAT["Native provider"]
    MCP["MCP provider"]
    TR["ToolResult"]
    NORM["EvidenceNormalizer"]
    EV["不可变 Evidence"]

    LLM --> REG
    REG --> POL
    POL --> BUD
    BUD --> EXE
    EXE --> NAT
    EXE --> MCP
    NAT --> TR
    MCP --> TR
    TR -->|"仅 grounded 成功"| NORM
    NORM --> EV
    TR -.->|"类型化失败"| X["无 Evidence"]
```

**两处值得注意的性质：**

- **策略与预算在任何 provider 调用之前短路。** 能力被拒或预算耗尽时直接返回，根本不碰
  provider。
- **类型化失败不产出 Evidence。** 那条虚线边正是 `ToolResult ≠ Evidence` 的要点：一次失败、
  空结果或未 grounded 的工具调用，不能被洗白成一个事实。它就是用来挡住「假成功证据」的——
  那种 agent 的 Evidence 看起来有据、实则来自一次坏掉调用返回空结果的失效模式。

---

## 图 3 — MCP 准入与调用

发现不是准入，provider 不是权威。

```mermaid
flowchart TD
    DISC["MCP 发现<br/>分页工具列表"]
    NORM["归一化<br/>规范名 + 输入/输出 schema"]
    FP["schema 指纹<br/>SHA-256"]
    ADM["可信准入<br/>手写、服务端"]
    REG["注册表<br/>模型可见面"]
    PB["策略 + 预算"]
    EXE["执行器"]
    PROV["MCP provider<br/>Streamable HTTP"]
    RES["类型化结果<br/>或类型化失败"]
    EVN["EvidenceNormalizer"]
    EV["不可变 Evidence"]

    DISC --> NORM --> FP --> ADM
    ADM -->|"已准入 + 只读"| REG
    ADM -.->|"未准入：可发现，不可选"| OPS["仅运维可见"]
    REG --> PB --> EXE --> PROV --> RES
    RES -->|"grounded"| EVN --> EV
    RES -.->|"类型化失败"| NONE["无 Evidence"]
```

| 边界 | 规则 |
|---|---|
| 发现 ≠ 准入 | 新发现的工具对运维可见，但**不**是模型可选的 |
| Provider ≠ 权威 | 服务端不能自行扩大自己的信任；准入是一条服务端声明 |
| 指纹漂移 | 规范名 + 输入 schema + 输出 schema → 一旦变化即 `SCHEMA_MISMATCH` |
| 协议 | 钉住的生产协议版本；降级**被拒绝**，不会被静默接受 |
| 传输 | 只连受信配置的 endpoint；除非显式标记为 internal，否则要求 HTTPS；重定向/换 host 被拒绝 |
| 租户 | 服务端注入；模型自带的租户字段被拒绝 |
| 写入 | `is_model_selectable` 要求 `READ_ONLY`——**MCP V1 的写入永不是模型可选** |

---

## 图 4 — 知识路径

检索产出的是*支撑性上下文*，永远不是判定权威。

```mermaid
flowchart LR
    Q["KnowledgeQuery<br/>租户作用域，强制"]
    FTS["PostgreSQL<br/>全文检索"]
    VEC["pgvector<br/>语义相似度"]
    RRF["RRF 融合"]
    HIT["KnowledgeHit<br/>有界，无控制信号"]
    CIT["Citation"]
    REV["Citation 复验"]
    KEV["已复验的知识证据"]
    FIND["Finding"]

    Q --> FTS
    Q --> VEC
    FTS --> RRF
    VEC --> RRF
    RRF --> HIT
    HIT --> CIT
    CIT --> REV
    REV -->|"可解析"| KEV
    REV -.->|"悬空 / 跨调查"| DROP["拒绝"]
    KEV --> FIND
```

| 区分 | 含义 |
|---|---|
| **知识上下文 ≠ 平台事实** | 知识条目是解释与理解；它*不是*观测到的事实。工作区给两者打的是不同的权威标签 |
| **知识 ≠ 判定权威** | 仅凭知识无法产出确定判定——这是一道硬验收闸门 |
| 租户是强制的 | `tenant_id` 是必填关键字，**没有默认值，也没有无作用域变体** |
| 命中不带控制信号 | `KnowledgeHit` 没有 `instructions`、`action`、`severity` 或 `authority` 字段，并对冻结的字段集有断言 |
| 引用要复验 | 解析不了、或跨调查解析的引用，一律拒绝 |

---

## 图 5 — 响应与持久执行

「建议」在哪里变成「已执行」，以及每一步如何各自失败。

```mermaid
flowchart TD
    VER["Verdict"]
    PROP["Response Proposal"]
    POL{"Policy"}
    DENY["DENY<br/>无可派发命令"]
    REQ["REQUIRE_APPROVAL"]
    AR["Approval Request"]
    AD["Approval Decision<br/>绑定 revision + hash"]
    REJ["REJECTED<br/>无可派发命令"]
    OUT["Outbox 事件<br/>response_execution_queued"]
    DISP["Dispatcher"]
    SUB["Submit Runner<br/>幂等键"]
    S1["PENDING / RETRYING"]
    S2["SUBMITTED"]
    S3["ATTENTION_REQUIRED"]
    S4["FAILED_DEFINITIVE"]
    SOAR["HISIEM SOAR"]
    OBSR["Observe Runner"]
    EX["QUEUED / RUNNING"]
    TERM["SUCCEEDED / FAILED<br/>在 HISIEM 中观测到"]

    VER --> PROP --> POL
    POL -->|"deny"| DENY
    POL -->|"require approval"| REQ --> AR --> AD
    AD -->|"approve"| OUT
    AD -->|"reject"| REJ
    OUT --> DISP --> SUB
    SUB --> S1
    S1 --> S2
    S1 --> S3
    S1 --> S4
    S2 --> SOAR
    SOAR --> OBSR --> EX
    EX --> TERM
```

**两个状态机，刻意分开：**

| 提交状态 | 含义 |
|---|---|
| `PENDING` / `RETRYING` | 还没交出去，或正在重试 |
| `SUBMITTED` | 已经交出去了。**这句话不说明结果。** |
| `ATTENTION_REQUIRED` | 试过了，结果**不确定**，必须有人来看 |
| `FAILED_DEFINITIVE` | 确定性失败——不是从超时里猜出来的 |

| 执行状态 | 含义 |
|---|---|
| `QUEUED` / `RUNNING` | HISIEM 已受理，进行中 |
| `SUCCEEDED` / `FAILED` | HISIEM 观测到了**终态**结果 |

**这张图要保护的不变式：**

- 拒绝不可能产生一条可派发命令。
- 批准绑定到 revision 与 hash——**过期批准无法授权已改变的意图**（TOCTOU）。
- 重试保持**同一个逻辑持久意图**（幂等键 `response:<tenant>:<proposal>`）。
- 不确定的提交**永不**被假定成 `SUCCEEDED` 或 `FAILED`。
- `ATTENTION_REQUIRED` 始终是一个显式的不确定态，永不被压平成一个终态。
- 重启或恢复**不产生重复的业务意图**。

---

## 图 6 — 真相边界图

什么*不*允许变成业务真相。

```mermaid
flowchart TD
    subgraph NOTTRUTH["不是业务真相"]
        CP["LangGraph checkpoint<br/>有界工作状态"]
        SPAN["OTel span / trace id<br/>遥测"]
        FE["前端状态<br/>呈现"]
        MCPST["MCP 调用状态<br/>provider 本地"]
        RAW["原始 ToolResult<br/>不可信数据"]
        CKPT["Checkpoint = 审计真相？否"]
    end

    subgraph TRUTH["业务真相"]
        DOM["领域聚合<br/>+ 不变式"]
        DB[("持久化状态<br/>PostgreSQL")]
        HT["HISIEM 观测到的<br/>执行结果"]
    end

    CP -.->|"永不被读作"| DOM
    SPAN -.->|"永不被读作"| DB
    FE -.->|"永不覆盖"| DB
    MCPST -.->|"永不变成"| DOM
    RAW -.->|"必须经归一化"| DOM
    HT -->|"就是"| DB
```

| 非真相 | 为什么它不能被提升 |
|---|---|
| **LangGraph checkpoint** | 图状态是跨步的工作记忆；重启不得重定义这次调查 |
| **OTel span / trace id** | 遥测丢失或 collector 被停掉，不得改变任何业务结果 |
| **前端状态** | UI 的呈现完全从持久化状态推导，自己不发明任何东西；刷新时以服务端真相为准 |
| **MCP 调用状态** | provider 对某次调用的本地视图，不是持久的执行记录 |
| **原始 ToolResult** | 不可信数据——它只有经归一化器、且只在 grounded 成功时才变成 Evidence |

> **HISIEM 观测到的执行结果是唯一的最终执行真相。**

---

## 图 7 — 评估

三条基线，一个不可补偿的验收包。

```mermaid
flowchart TD
    subgraph GP01["GP-01 — 调查正确性"]
        MAT["物化器<br/>真实 HISIEM 资源"]
        SEAL["封存清单"]
        RUN["execute_real_model_run"]
        QUAL["工具-证据质量"]
        SCORE["确定性评分器"]
        REP["有界可重复性<br/>尝试分类"]
        SUMM["套件汇总"]
        MAT --> SEAL --> RUN --> QUAL --> SCORE --> REP --> SUMM
    end

    subgraph KB["KB-GOLDEN-V1 — 知识"]
        KBC["带版本的语料"]
        KBR["检索 / 引用 / 排序"]
        KBC --> KBR
    end

    subgraph XP["XP-01 — 跨平面验收"]
        S29["29 个场景<br/>9 个族"]
        G13["13 道不可补偿闸门"]
        AGG["cross-plane-suite-results/v1<br/>一个确定性聚合"]
        S29 --> G13 --> AGG
    end
```

**XP-01 的聚合为什么长成这样：**

| 性质 | 含义 |
|---|---|
| 不可补偿 | 里面任何地方都不存在分数、权重或通过百分比。一个 FAIL → 套件 FAIL |
| 受完整性检查 | 缺失、未知或失败的场景让套件失败；缺少任一必需闸门结果的场景同样让它失败 |
| 确定性 | 跨多次读取逐字节相同，与输入顺序无关，身份里不含时间戳或 run id |
| 密钥安全 | 在原子写入**之前**先做密钥扫描；不含原始 prompt、completion、ToolResult 或遥测载荷 |
| 可证伪 | 已证明：直接无效的测量会让每一道闸门失败 |

---

## 图 8 — 跨仓边界

两个仓库，一个系统。

```mermaid
flowchart LR
    subgraph PLAT["HISIEM — 安全平台"]
        ING["摄取"]
        DET["检测"]
        AL["告警"]
        CASES["案件 + 运营数据"]
        EXEC["确定性 SOAR 执行"]
        WEB["分析员 web 应用<br/>web/ — Vue 控制台"]
    end

    subgraph COP["HISIEM SOC Copilot — 决策层"]
        INV["AI 调查"]
        TOOLS["受治理的工具使用<br/>4 个只读工具"]
        EV["证据 + 知识上下文"]
        VER["发现 + 判定"]
        PROP["响应提案"]
        HUMAN["人工授权流程"]
        OBS["执行观测<br/>工作区投影"]
    end

    AL -->|"告警上下文"| INV
    CASES -->|"只读<br/>经工具契约"| TOOLS
    INV --> TOOLS --> EV --> VER --> PROP --> HUMAN
    HUMAN -->|"持久命令"| EXEC
    EXEC -->|"观测到的结果<br/>= 最终真相"| OBS
    OBS -->|"投影"| WEB
    WEB -->|"分析员读取<br/>并决策"| HUMAN
```

| HISIEM 拥有 | Copilot 拥有 |
|---|---|
| 安全事件摄取 | AI 辅助调查 |
| 流处理与检测 | 受治理的工具使用 |
| 告警与运营数据 | 证据组织与知识上下文 |
| 案件管理 | 发现与判定 |
| **确定性 SOAR 执行与执行记录** | 响应提案与人工授权流程 |
| 托管分析员 web 应用 | 执行观测与工作区投影 |

**Copilot 永远不是第二个 SIEM，也永远不是第二个 SOAR。**

> **分析员工作区的 UI 实现在 HISIEM 仓。** HISIEM 拥有平台的 web 应用，所以面向分析员的 UI
> ——证据权威分级、发现、判定、策略决策、人工决策、提交与执行状态——住在 `HISIEM/web/`。
> 本仓不含任何前端。验收 harness 直接执行真实前端模块（`web/src/utils/copilot.js`），而不是
> 复述它的权威映射，因此两者不会漂移。

---

## 相关文档

| 主题 | 文档 |
|---|---|
| 领域模型与不变式 | [`domain-model.md`](../contracts/domain-model.md) |
| 命令、事件、LangGraph 状态 | [`application-commands-domain-events-langgraph-state.md`](../contracts/application-commands-domain-events-langgraph-state.md) |
| 持久化 schema 与约束 | [`persistence-schema.md`](../contracts/persistence-schema.md) |
| 分层边界及其强制 | [`python-package-boundary.md`](../contracts/python-package-boundary.md) |
| 工具契约 | [`investigation-tool-contract.md`](../contracts/investigation-tool-contract.md) |
| 模型 provider 契约 | [`model-provider-contract.md`](../contracts/model-provider-contract.md) |
| 知识平面 | [`knowledge/`](../knowledge/) |
| 评估契约 | [`evaluation/`](../evaluation/) |
| 可观测性 | [`observability.md`](../contracts/observability.md) |
| 本地一体化运行时 | [`local-integrated-runtime.md`](../operations/local-integrated-runtime.md) |
