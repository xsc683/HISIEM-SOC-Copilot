# HISIEM SOC Copilot — 面试指南

**用途：** 私人复习材料。这里的每条技术主张都以 `capability-mcp` 上的代码与文档为据。实现有边界或
缺口的地方，都写明。

**怎么用。** §1–§3 是口头介绍与边界。§4 是代码地图。§5–§42 是按主题逐条的复习，格式是
*概念 → 实际实现 → 为什么 → 代价 → 追问*。§43–§44 是代码路径与失效走查。§45–§49 是设计决策、
非目标与局限。§50–§51 是题库。

---

## 1. 30 秒介绍

> HISIEM SOC Copilot 是 SIEM 之上的一层 AI 调查。一条安全告警触发一次 agent 调查，它调用一份固定
> 白名单里的四个只读工具，把每个有据的结果归一化成可追溯的 Evidence，并产出判定与响应提案。高风险
> 响应不可能由模型执行：策略约束它，人批准它，而批准被持久记录成一条幂等命令，由 HISIEM 的 SOAR
> 引擎执行。随后 Copilot 观测真实执行结果，并把它投影回分析员工作区。
>
> 工程论点是：你可以造出一个真正有用的安全 agent，而**不**给它权威——一个典型 agent 演示里让模型
> 动手的每一处，这个系统都有一个边界替代它，而每个边界都由一道可证伪的验收闸门覆盖。

---

## 2. 3 分钟介绍

**问题。** SOC 分析员手工分诊告警：拉上下文、搜日志、查检测规则、翻 runbook、形成结论、写报告。
这里面大部分是信息收集与上下文切换。LLM 能帮上大忙——但一个能在安全平台上*动手*的 agent 是负债，
因为一次提示注入或一次幻觉就会变成一个被禁用的账号或一台被隔离的主机。

**设计上的回应。** 把问题拆开：让模型做推理与信息收集，把它从每一个携带权威的决定里拿掉。

**生命周期。** 一条告警启动一次调查。一个 LangGraph 图跑有界的步骤：注水告警（系统控制，不是模型
选择），然后让模型从四个只读工具里挑。工具结果经由归一化器变成不可变 Evidence——而类型化失败产出
**零** Evidence，所以一次坏掉的调用不能被洗白成一个事实。发现与判定随之产出，判定可以产出响应提案。
随后策略决定 `DENY` 还是 `REQUIRE_APPROVAL`。需要批准时，由人决定；该决定绑定到 revision 与 hash，
所以过期批准无法授权已改变的意图。批准会写入一条持久 outbox 事件，派发器幂等地把它提交给 HISIEM
SOAR。提交与执行是**两个独立的状态机**——`SUBMITTED` 不是 `SUCCEEDED`——而不确定的结果变成
`ATTENTION_REQUIRED`，永远不会被捏造成终态。最终真相是 HISIEM 观测到的那个。

**我最希望被问到的几块：**

1. **权威分离。** 九个区分（ToolResult≠Evidence、Policy≠Human Approval、Submission≠Execution
   Success……），每一个都在代码里强制，并由验收场景把关。
2. **持久执行。** Outbox、幂等键、有界重试、显式不确定态、TOCTOU 受保护的批准。
3. **MCP 治理。** 发现、准入、选择是三件不同的事。模型可选面恰好四个只读工具；写入是被设计掉的，
   不是被沙箱关起来的。
4. **评估。** 一个 29 场景、13 闸门、不可补偿的跨平面验收包，配机器可读产物——包括它自己的可证伪性。

---

## 3. 项目边界：它是什么 / 不是什么

| 它是 | 它不是 |
|---|---|
| AI 辅助调查 | 一个 SIEM——安全数据与检测归 HISIEM |
| 证据驱动（每个发现都追溯到 Evidence） | 一个 SOAR——执行归 HISIEM SOAR |
| 受治理（确定性的只读工具白名单） | 一个自治 SOC——没有任何路径让模型授权 |
| 受人工权威把关 | 一个聊天机器人或通用安全问答 |
| 持久的（outbox、幂等、可重试） | 一个多智能体系统——刻意只有一个 agent |

**那条永久的规则：**

```text
Model proposes  →  Policy constrains  →  Human authorizes
Durable command records intent  →  HISIEM executes  →  Copilot observes
```

**可能的追问：** *「为什么单智能体？」* → 每多一个 agent，权威面就乘一层，工具选择也更难推理。这里
的约束关乎*模型可以做什么*，而一个带 4 工具面的有界 agent，远比一队互相协商的 agent 好辩护。多
智能体是刻意的非目标（§47）。

---

## 4. 仓库 / 包地图

```text
src/hisiem_soc_copilot/
├── config.py                     single typed-settings entry point
├── main.py                       process entrypoint (config → container → uvicorn)
├── domain/                       PURE (no FastAPI/SQLAlchemy/LangGraph/httpx/pydantic)
│   ├── investigation/            Investigation aggregate + state machine
│   ├── response/                 ResponseProposal + policy + approval
│   ├── knowledge/                knowledge documents/versions/chunks
│   └── shared/
├── application/                  commands, queries, handlers, ports, services
│   ├── commands/                 investigation | knowledge | response
│   ├── handlers/                 investigation | response | workflow | durable_support | knowledge | attack_import
│   ├── ports/                    16 ports (repositories, uow, hisiem, model_provider, durable, soar, trust, …)
│   ├── queries/                  investigation | workspace
│   └── services/                 investigation_service | knowledge_retrieval | workspace_service | attack_projection | …
├── contracts/                    boundary schemas (API/LLM/Tools) — Pydantic lives here
├── agent/                        LangGraph orchestration (NOT business authority)
│   ├── graph/                    state | nodes | builder | runtime | budget | tool_audit
│   ├── tools/                    registry | policy | args | executor | providers | native_provider | provider_router
│   ├── evidence/normalizer.py    ToolResult → immutable Evidence
│   └── knowledge/catalog.py      model-facing knowledge tool adapter
├── api/                          FastAPI transport only (+ routers/, schemas/)
├── infrastructure/               adapters
│   ├── persistence/  auth/  checkpoint/  embedding/  hisiem/  knowledge/
│   ├── durable/                  dispatcher | investigation_runner | response_runner
│   ├── llm/  mcp/provider.py  messaging/  observability/  soar/  threat_intel/
└── bootstrap/container.py        Composition Root
```

**最该熟悉的几个文件：**

| 文件 | 为什么 |
|---|---|
| `agent/tools/registry.py` | 确切的模型可选白名单与禁止清单 |
| `agent/tools/executor.py` | provider 路由接缝与结果成形 |
| `agent/evidence/normalizer.py` | 工具结果在哪里变成（或没能变成）Evidence |
| `agent/graph/nodes.py` | 调查的各个步骤 |
| `infrastructure/durable/dispatcher.py` | Outbox 的租约/回收/重试语义 |
| `infrastructure/durable/response_runner.py` | 提交与观测两个 runner |
| `application/services/knowledge_retrieval.py` | 混合检索与排序 |
| `domain/response/` | 策略与批准的不变式 |

---

## 5. 调查生命周期

**概念。** 一次调查是一个带状态机的聚合，不是一个聊天会话。

**实现。** `CREATED → RUNNING → {COMPLETED, FAILED, CANCELLED}`。**响应流程是一条独立的、完成后的
聚合生命周期**——响应提案不是调查的一个状态。「每 租户 + 告警 一个活跃调查」由一条**部分唯一索引**
强制，所以重复启动会在数据库处收敛，而不是依赖应用代码里「先查后写」的竞态。

**为什么。** 两个设计点：(1) 把响应与调查分开，意味着一次已完成的调查事后仍然可以产出响应、并据响应
被评判；(2) 把「一个活跃调查」规则压进部分唯一索引，使它免于竞态。

**替代方案。** 用一个聚合同时持有两者，会把调查完成与响应进度耦合起来，并让「重开提案」变成对一次
已关闭调查的改动。

**代价。** 两条生命周期意味着两件事要推理，工作区必须把它们组合起来。而那个组合正是工作区投影所做的
事。

**参考。** `domain/investigation/`、`domain/response/`、`docs/contracts/domain-model.md`、
`docs/contracts/persistence-schema.md`。

**追问：** *「你怎么防止同一个告警被并发启动两次？」* → 部分唯一索引；第二次插入失败，handler 返回
已存在的调查。

---

## 6. 领域模型

**概念。** 业务规则住在一层纯净、无框架依赖的领域里。

**实现。** `domain/` 含聚合、实体、值对象、领域事件与不变式。**没有 FastAPI、SQLAlchemy、
LangGraph、httpx 或 Pydantic**——由 `tests/architecture/` 强制，不是靠约定。持久化使用显式的
ORM↔领域 mapper，而不是直接映射领域类。

**为什么。** 两个回报：领域不变式可以脱离数据库做单元测试、跑得很快；以及领域不会意外获得一个让它
不可测或与传输耦合的依赖。

**代价。** mapper 是样板——本可以由 ORM 白送、却要你手写的代码。换来的是持久化模型与领域模型可以
各自演化（例如一个 `jsonb` 列的形态不是聚合的形态）。

**参考。** `docs/contracts/domain-model.md`、`docs/contracts/python-package-boundary.md`、
`tests/architecture/test_import_boundaries.py`。

**追问：** *「纯净性到底怎么强制的？」* → 一个架构测试走遍 import，如果 `domain/` 里出现被禁止的包
就让构建失败。

---

## 7. LangGraph 与 checkpoint 边界

**概念。** 图是编排与有界工作状态。它**不是**业务真相的来源。

**实现。** 图的状态是一个有界的跨步工作结构（`agent/graph/state.py`）。checkpoint 持久化到
`langgraph_checkpoint` schema——**由 LangGraph 拥有并迁移**，与 Alembic 拥有的 `copilot` schema
分开，连接分开、迁移归属分开。

**为什么。** checkpoint 是一个*可恢复机制*。如果它被读成领域状态，那么一次图重放、一次 checkpoint
还原或一次图重构，就可能静默重定义一次调查的业务状态。把它们分开，意味着调查的真相就是领域聚合持久
下来的那些东西。

**代价。** 恢复一个图需要重新推导领域上下文，而不是从 checkpoint 里读出来——恢复时多一点工作量，
换来单一真相来源。

**参考。** `agent/graph/`、`docs/contracts/application-commands-domain-events-langgraph-state.md`。

**追问：** *「如果 checkpoint 和领域不一致呢？」* → 领域赢。checkpoint 是工作记忆；它影响图接下来
做什么，不影响这次调查*是*什么。

---

## 8. Tool Registry

**概念。** 一份确定性白名单——模型可以选择的工具的确切集合。

**实现。**

```python
AGENT_SELECTABLE_TOOLS = {
    "hisiem.search_events",
    "hisiem.get_detection_rule",
    "knowledge.retrieve_security_guidance",
    "knowledge.resolve_attack_technique",
}
SYSTEM_CONTROLLED_TOOL = "hisiem.get_alert_context"   # never offered to the model
FUTURE_CATALOG_TOOLS = {"hisiem.get_entity_activity", "threat_intel.lookup_ip"}  # not registered
```

外加一份显式的 `FORBIDDEN_TOOLS` 清单，点名那些绝不能出现的能力类型——`execute_shell`、
`raw_http_request`、`write_alert`、`set_alert_verdict`、`block_ip`、`isolate_host`、
`start_soar_execution`、`approve_response` 等等。

**为什么。** 注册表就是模型的全部行动空间。让它显式、小、有测试覆盖，意味着「agent 能做什么？」这个
问题有一行答案。已编目但未实现的工具放在**另一个**集合里，因而永不被注册——**模型永远不能选到一个
没有执行器的工具**，而 agent 正是那样开始幻觉出能力的。

**代价。** 加一个工具需要一个执行器、一份 schema 和一条策略项——刻意比让模型随便试要麻烦。

**参考。** `agent/tools/registry.py`、`tests/unit/agent/test_tool_surface.py`、
`tests/architecture/test_knowledge_boundary.py`。

**追问：** *「为什么 `get_alert_context` 是系统控制的？」* → 图的注水步骤需要在模型决定任何事情之前
拿到告警上下文，所以直接调用它。它被从模型可见面上移除，使模型不能花一轮去重新取它、或者操纵它。

---

## 9. Tool Policy

**概念。** 一道确定性闸门，能在任何东西执行之前拒绝一次工具调用。

**实现。** `agent/tools/policy.py` 在执行器做任何事之前先对候选做策略评估。**策略 DENY 与预算耗尽
都在任何 provider 调用之前返回。**

**为什么。** 次序就是全部要点。如果策略跑在 provider 调用*之后*，一个被拒的能力依然已经在远端系统
上造成了副作用。短路意味着拒绝不带来任何后果。

**代价。** 策略必须能在不产生副作用的前提下被求值，这限制了策略可以依赖什么。

**参考。** `agent/tools/policy.py`、`agent/graph/nodes.py`。

**追问：** *「策略和批准是一回事吗？」* → 不是。策略是确定性的、系统拥有的；批准是人的决定。`DENY`
不是一次驳回——它们是来自不同权威的两个不同事实（§24、§25）。

---

## 10. Tool Budget

**概念。** 一次调查在工具调用上可以花掉多少的上限。

**实现。** `agent/graph/budget.py` 跟踪预算；执行器在调用 provider 之前检查它，所以一个失控的循环
不可能产出无界的外部调用。

**为什么。** agent 循环在构造上就是无界的——由模型决定何时停。预算正是让最坏情况变有限的东西。它在
调用*之前*强制，所以耗尽的预算不付任何外部代价。

**代价。** 预算可能砍掉一次合法的调查。缓解办法是：耗尽是一个显式、可观测的结果，而不是一片沉默。

**追问：** *「预算耗尽了会怎样？」* → 那次工具调用不会发生，图带着手上已有的东西继续。它永不捏造
结果。

---

## 11. Tool Executor

**概念。** 工具候选变成 provider 调用的那唯一一道接缝。

**实现。** `agent/tools/executor.py`：

- 查出工具并派发到正确的路径（native provider、MCP provider，或一个知识目录适配器）。
- 注入可信调用上下文——**租户与 actor 来自执行器，不来自模型的参数**。
- 构造 provider 侧参数，拒绝模型自带的租户字段。
- 把 provider 结果成形为有界的 `ToolResult`。
- 把一个没有接适配器的知识工具处理成**不可用**结果并附明确原因，而不是异常、也不是一个静默的空成功。

**为什么。** 把边界集中在这里，意味着租户注入与结果成形规则只存在一份。`provider_router.py` 选择
provider；执行器不知道某个能力来自原生代码还是 MCP。

**代价。** 模型与 provider 之间多一层——值得，因为那一层正是信任边界所在。

**参考。** `agent/tools/executor.py`、`agent/tools/provider_router.py`、
`agent/tools/native_provider.py`。

**追问：** *「一个 MCP 工具在模型看来有什么不同？」* → 没有不同。模型看到的是一份受信内部规格；
server、endpoint、transport 与凭据对它不可见。

---

## 12. ToolResult vs Evidence

**概念。** 工具响应是数据。Evidence 是有据的、带来源的事实。

**实现。** Evidence 只经归一化器产出，且只来自一次**有据的成功**。类型化失败产出**零** Evidence。
失败事实被分别记录：假成功证据只在一次类型化失败*也*产出了 Evidence 时才触发，而「失败被归一化成空」
只在后端不可用、且状态是 SUCCESS/NO_DATA 时才触发。

**为什么。** 这是证据驱动 agent 里最重要的一条反幻觉边界。没有它，一次返回 `{}` 的坏调用会变成「无
发现」，进而变成一个看着无害的判定。有了它，一次坏调用显式地就是一次坏调用。

**替代方案。** 让模型解读原始工具输出。构建更便宜；但那样判定的依据就取决于模型如何读一个错误
字符串。

**代价。** 每一种新的结果形态都需要一条归一化路径。验收场景 `XP-REL-001/002` 存在的目的就是证明失败
那一半——一次类型化失败、零 Evidence。

**参考。** `agent/evidence/normalizer.py`，闸门 `FORBIDDEN_FACTS_ABSENT`。

**追问：** *「给我一个它挡住的具体的失效模式。」* → 一个 MCP server 被停掉。能力是已准入的，调用
失败。没有这条边界，agent 会报「没有匹配事件」，并断定该告警无害。有了它，这次工具失败是一次零
Evidence 的类型化失败，判定不能建立在它上面。

---

## 13. EvidenceNormalizer 与来源

**概念。** 每个证据来源都走同一条归一化路径。

**实现。** `EvidenceNormalizer.normalize_provider_result(...)` 接收 provider 结果以及仅限关键字的
`tool_call_id`、`provider` 与 `operation`，产出带来源的不可变 Evidence 行。
`ProviderInvocationResult` 携带类型化失败分类；`tool_result_is_typed_failure` 区分类型化失败与成功。

**为什么。** 单一路径意味着来源不会在某一种来源类型上被忘记，而且来自 MCP 工具与原生工具的 Evidence
形状相同、保证相同。

**代价。** 形态确实不同的来源必须被适配进这个共同模型——这正是要点，但这也意味着新增一种来源是一次
刻意的集成，而不是一次透传。

**参考。** `agent/evidence/normalizer.py`、`docs/contracts/investigation-tool-contract.md`。

---

## 14. 知识平面

**概念。** 检索到的安全文档提供的是*支撑性上下文*——永远不是判定权威。

**实现。** 知识文档带版本；内容块**不可变**。引用目标是一个不可变内容块，所以引用能在一次
embedding 重建之后存活。ATT&CK 权威投影有单写者切换：一个 `MITRE_ATTACK` 文档的
`active_version_id` 只在切换事务里移动，永不因普通摄取而移动。

**为什么。** 版本化加上不可变，才让一条引用*在时间上有意义*。如果内容块可变，一次重新摄取之后引用
就会静默指向不同的文本。

**代价。** 不可变意味着存储随重建增长；换来的是引用是一个稳定引用。

**参考。** `docs/knowledge/domain.md`、`domain/knowledge/`。

**追问：** *「ATT&CK 投影为什么需要单写者切换？」* → 因为服务一次 ATT&CK 命中是一句关于某个权威发布
的主张。如果普通摄取能移动那个指针，一次检索就可能混进两个发布，那句话就不成立了。

---

## 15. FTS + pgvector + 混合检索 + RRF

**概念。** 把词法的精确性与语义的召回结合起来，确定性地融合。

**实现。** 词法通道：在带版本的内容块上做 PostgreSQL **全文检索**。语义通道：在 embedding 上做
**pgvector** 相似度。融合：在两个通道上做 **Reciprocal Rank Fusion**。`MAX_RESULT_LIMIT = 5`，
请求更多会被**拒绝，而不是被夹到上限**。排序被实现为**作用在普通数据上的普通函数**——除它编排的那
两次仓储调用与那次 embedding 调用之外，纯且确定性。

**为什么。** FTS 与向量检索失败的方式不同：FTS 漏掉转述，向量漏掉精确标识符（一个 CVE、一个规则
id）。RRF 奖励两个通道之间的一致性，而不需要在两个不可比的量纲之间做分数校准。而把排序做成纯函数，
正是让排序可复现、可脱离数据库测试的原因。

**替代方案。** 一个专用向量数据库。被否：在这个规模上，现有 PostgreSQL 上的 pgvector 足够，且能
避免第二个真相存储与第二套运维系统。

**代价。** **真实语义质量未被测量**——没有配置 embedding provider，所以混合评估证明的是接线，不是
质量。产物被标注 `PLUMBING_ONLY`。在被问到之前就主动说明这一点。

**参考。** `application/services/knowledge_retrieval.py`、
`docs/knowledge/retrieval-contract.md`、`docs/evaluation/knowledge-evaluation-contract.md`。

**追问：** *「为什么用 RRF 而不是加权分数融合？」* → 加权融合要求来自不同量纲的分数可比，而对 BM25
与余弦距离来说，这是一个没有原则性答案的校准问题。RRF 用的是名次，只需要一致性，而且确定、好测。

---

## 16. 引用复验

**概念。** 一条引用必须解析到真实、在作用域内的内容。

**实现。** 引用会被解析，并针对持久化的内容块**再次校验**。检索结果暴露的 `citation_id` 命名一个
不可变内容块。有两道闸门：`DANGLING_CITATION`（解析不了的引用）与 `CROSS_INVESTIGATION_CITATION`
（解析到当前调查/租户作用域之外的引用）。

**为什么。** 引用是让 AI 回答可审计的机制。解析不了的引用比没有引用更糟——它看起来像是有据。跨调查
引用是一次**租户隔离泄漏**，只是披着一条参考资料的皮。

**代价。** 复验每条引用要花一次查询；替代方案是去相信一条模型产出的参考。

**参考。** 闸门 `DANGLING_CITATION`、`CROSS_INVESTIGATION_CITATION`；
`application/services/knowledge_retrieval.py`。

---

## 17. 知识权威边界

**概念。** 知识可以供参考；它不能做决定。

**实现。** 知识证据携带自己的权威分级——*支撑性上下文*——与平台事实相区分。
`KNOWLEDGE_ONLY_DEFINITIVE_VERDICT` 是一道硬闸门：确定判定不能只靠知识。`KnowledgeHit` **没有**任何
可以被读成控制信号的字段（没有 `instructions`、`action`、`severity`、`authority`），而这个「没有」
是对冻结字段集的断言。

**为什么。** 一份写着「封掉这个 IP」的 runbook 不能有能力授权封掉这个 IP。冻结字段断言是强版本：
以后加这样一个字段会挂测试，而不是悄悄扩大检索能表达的东西。

**代价。** 一次只有知识的调查无法得到确定判定——这是对的，但它意味着系统必须能说出「我无法下结论」
（`INCONCLUSIVE`）。

**参考。** 闸门 `KNOWLEDGE_ONLY_DEFINITIVE_VERDICT`、`docs/knowledge/security-boundary.md`。

**追问：** *「工作区怎么呈现这一点？」* → 前端从持久化的来源类型推导出权威分级，用不同的标签渲染
知识——而验收 harness 执行的是**真实前端模块**，而不是复述那个映射。

---

## 18. MCP 发现

**概念。** 了解一个 server 自称提供了什么。

**实现。** 官方 MCP Python SDK，Streamable HTTP 传输。发现会完成全部分页工具列表、归一化原始
元数据，并对规范外部工具名加上输入 schema 与输出 schema（或一个显式的「缺失」标记）计算确定性的
**SHA-256 指纹**。

**为什么。** 发现是不可信输入。把它当作一个*提案*而不是一个事实，才让下一步（准入）有意义。

**代价。** 分页与归一化在 server 与注册表之间多了一些代码；换来的是模型永远看不到原始 server
元数据。

**参考。** `infrastructure/mcp/provider.py`。

**追问：** *「一个新发现的工具会怎样？」* → 它被发现但**未准入**——运维可见，模型不可选。启动与
配置的刷新会重新发现并重新校验；缺失的已准入工具变成不可用；指纹漂移变成 `SCHEMA_MISMATCH`。

---

## 19. MCP 准入

**概念。** 一份手写的、服务端声明，说明这个系统决定信任什么。

**实现。** 一条准入项声明：内部名、受信描述、所配置的 server 身份、外部名、内部参数/结果契约、预期
外部 schema 指纹、只读/风险分类、租户作用域，以及每能力上限。未知 server、写入/高风险能力、未准入
的动态工具一律 **fail closed**。

**为什么。** 这就是「模型能调 server 提供的任何东西」与「模型能调我们决定信任的东西」之间的差别。
模型看到的是那份受信描述——不是 server 的自我描述，那是受攻击者影响的内容。

**替代方案。** 自动准入 server 提供的一切。那会让 server 的内容（以及任何能影响它们的人）成为信任
边界的一部分。

**代价。** 加一个能力需要一条手写项和一个预期指纹——刻意制造的摩擦。

**参考。** `agent/tools/providers.py`（`AdmissionEntry`）。

**追问：** *「为什么受信描述与 server 自己的描述是分开的？」* → 因为 server 的描述是远端内容，而
远端内容是不可信数据。描述是一处 prompt 面：一段恶意的工具描述能操纵工具选择。

---

## 20. MCP schema 指纹、协议钉住、只读权威

**概念。** 三道互相独立的 fail-closed 控制。

**实现。**

| 控制 | 机制 | 失效模式 |
|---|---|---|
| schema 指纹 | 对规范名 + 输入 schema + 输出 schema 求 SHA-256 | 漂移 → `SCHEMA_MISMATCH` |
| 协议钉住 | 要求生产协议版本 | 降级 → 被拒绝，不被接受 |
| 只读权威 | `is_model_selectable` 要求 `READ_ONLY` | 写能力 → 永不可选 |
| 传输 | 只连受信配置的 endpoint；除非显式受信为内部，否则 HTTPS | 意外的重定向/换 host → 被拒绝 |
| 上限 | 全局 + 每能力结果上限 | 超大 → `RESULT_TOO_LARGE`，**永不截断** |

**为什么。** 每道控制挡的是不同的攻击。指纹意味着一个 server 不能在一个已准入名下静默改变某工具
做什么。钉住意味着一次协商不能被降级到更弱的协议。只读分类意味着一个写能力不只是被劝阻——它在结构
上不可选。

**代价。** 一次合法的 schema 变更变成一次显式的运维动作（更新预期指纹）。这就是预期的成本。

**参考。** `infrastructure/mcp/provider.py`；闸门 `UNADMITTED_MCP_SELECTED`、`WRITE_MCP_SELECTED`。

**追问：** *「为什么超大结果要拒绝而不是截断？」* → 一个被截断的结果是一个静默不完整、却依然看起来
完整的事实。显式的 `RESULT_TOO_LARGE` 可恢复；一个被截断的事实会毒化判定。

---

## 21. 租户边界

**概念。** 租户作用域由服务端断言，永不由模型或客户端断言。

**实现。** 租户与 actor 经所配置的 `TrustedContextProvider` 来自受信请求上下文。模型的参数不能声明
租户；模型自带的租户字段会被**拒绝**。provider 侧参数构造在需要时注入受信租户上下文。
`ProviderInvocationContext` 由执行器注入。仓储读取是租户作用域的。跨租户泄漏是一道硬闸门
（`CROSS_TENANT_LEAK`，2 个场景）。

**为什么。** 租户是那样一个字段：模型的一次错误会变成一次安全事件；它同时也是模型天然想填的那个
字段。让模型在结构上不可能提供它，比校验它提供的东西更强。

**代价。** 每个 provider 与仓储都必须接受注入的上下文，比从参数里读一个字段略多一些接线。

**参考。** `application/ports/trust.py`、`agent/tools/executor.py`、闸门 `CROSS_TENANT_LEAK`。

---

## 22. 提示注入处理

**概念。** 所有远端与检索到的内容都是**数据**，永远不是指令。

**实现。** 工具结果与检索到的知识都被归一化成数据，不携带控制语义。`KnowledgeHit` 没有承载指令的
字段。工具面是一份固定白名单，所以注入的文本无法让一个新工具存在。租户由服务端提供，所以注入的文本
无法扩大作用域。判定与策略决定不是模型权威的。抗注入性由安全场景里的**禁止事实**来衡量，而不是被
断言。

**为什么。** 这里的注入防御是*架构性的*，不是基于过滤器的。过滤器是一场必输的博弈；把模型的权威拿掉
不是。即便一个被完美注入的模型，也选不到未准入工具、改不了租户作用域、授权不了响应，也造不出一条能
解析的引用。

**代价。** 架构限制了 agent 能做什么，而这正是预期的取舍。基于内容的检测不在尝试范围内。

**参考。** `XP-SEC-001/002` 里的闸门 `FORBIDDEN_FACTS_ABSENT`；
`docs/knowledge/security-boundary.md`。

**追问：** *「如果注入的内容改变了判定呢？」* → 它可以影响判定——模型是在那段文本上推理。它做不到的
是*授权*任何东西：判定仍然无法在没有策略与人批准的情况下执行。这就是那条刻意划下的线。

---

## 23. 响应提案生命周期

**概念。** 响应提案是模型的建议，被表达成一个有自己的生命周期的一等聚合。

**实现。** `ResponseProposal` 经由 `POST /api/v1/investigations/{id}/response-proposals` 从一次已完成
的调查创建。它携带一条策略决定，并在需要批准时携带一个 `ApprovalRequest`。批准/驳回是分开的端点。
响应生命周期**独立于调查生命周期**。

**为什么。** 把提案做成聚合，意味着批准、驳回与执行状态都是带自己跃迁的持久事实——而不是调查上某个
后续步骤可以覆盖的字段。

**代价。** 工作区投影里有更多状态要组合。

**参考。** `domain/response/`、`application/handlers/response.py`。

---

## 24. 策略

**概念。** 一个确定性决策函数，与人的同意分开。

**实现。** `evaluate_response_policy` 返回 `DENY` 或 `REQUIRE_APPROVAL`。`DENY` 不产出可派发命令。
策略是确定性的系统代码。

**为什么。** **Policy ≠ Human Approval** 这个区分值得捍卫：策略决定是一次*规则适用*，人的决定是
*同意*。把它们合并，要么让系统可以声称一个它从未拿到的人工批准，要么让人能推翻一条系统本被要求执行
的策略。

**代价。** 要通过两道闸门而不是一道——对一个带副作用的动作来说，这是预期的成本。

**参考。** `domain/response/`，闸门 `EXECUTION_WITHOUT_APPROVAL`。

**追问：** *「策略 DENY 和人工驳回是一回事吗？」* → 不是。`DENY` 是系统基于策略拒绝；驳回是人拒绝。
工作区把它们呈现为不同的事实，验收场景也让它们保持分开。

---

## 25. 人工批准

**概念。** 对一个带副作用的响应的显式人工同意。

**实现。** 一个 `ApprovalRequest` 由一个带 `APPROVE`/`REJECT` 的 `ApprovalDecision` 回答。批准与驳回
是不同端点、不同持久决定。一次驳回**不能创建可派发命令**。不变式：*「Agent 只能建议，不能授权。」*

**为什么。** 这是整个系统的核心安全属性。LLM 的输出可能出错或被操纵；人的决定是那个人对一个带副作用
动作承担责任的检查点。

**代价。** 人工延迟处在响应的关键路径上。这正是要点——但它意味着系统必须把「等待一个人」建模成一个
真实、持久的状态，而它确实这么做了。

**参考。** `domain/response/`、`application/handlers/response.py`，闸门
`EXECUTION_WITHOUT_APPROVAL`、`SUBMISSION_TREATED_AS_SUCCESS`。

**追问：** *「如果分析员批准之后意图变了呢？」* → §26。

---

## 26. revision / hash 的 TOCTOU 保护

**概念。** 一次批准授权的是一个*具体*意图，不是一份通用许可。

**实现。** 批准决定绑定到它所批准提案的 **revision 与 hash**。一份过期批准——它绑定的 revision 不再
匹配当前提案——**无法授权已改变的意图**。

**为什么。** 这补上了一个真实的 TOCTOU 缺口。没有它：分析员审阅提案 v1 并批准；在批准与派发之间，
提案（或它背后的证据）变了；派发执行了人从未看过的东西。把决定绑定到 revision，把这个变成一次拒绝。

**代价。** 任何对提案的改动都会让批准失效、需要新的决定。正确，而且略烦人——对一个安全控制来说，
这正是对的方向。

**参考。** `domain/response/`、`XP-AUTH-003`（过期绑定 + 可证伪性）。

**追问：** *「你怎么证明这道闸门不是空摆设？」* → E3 的工作加了一条过期绑定适配器路径，让一次过期
授权真的会在 `EXECUTION_WITHOUT_APPROVAL` 上 FAIL，而不是因为字段缺失被跳过。

---

## 27. 持久执行

**概念。** 一条响应命令能在进程重启之后存活。

**实现。** `infrastructure/durable/`——一个**派发器**、一个**调查 runner**，以及**响应 runner**
（一个提交 runner 与一个观测 runner）。工作由持久化状态驱动，而不是由进程内回调驱动。

**为什么。** 「人已批准」与「SOAR 已执行」之间的间隔可能是几秒，也可能是几小时。如果那个间隔活在
进程内存里，一次重启就会静默丢掉这条命令。持久性让这条命令成为一个持久事实，而不是一次在途函数调用。

**代价。** 一整套派发器机械（租约、重试、死信）代替了一次函数调用。

**参考。** `infrastructure/durable/`、
`docs/contracts/application-commands-domain-events-langgraph-state.md`。

---

## 28. 事务性 outbox

**概念。** 把「打算发布」这件事记录进与状态变更同一个数据库事务。

**实现。** 一次批准在同一个事务里把一条 `response_execution_queued` 领域事件写进 **outbox**。派发器
从 outbox 发布。Outbox 行携带**租约归属、租约到期、回收、fencing token 与死信**语义，另有一个
outbox **trace context** 列携带原始的 `traceparent`。

**为什么。** 双写问题：提交了状态变更却没发布 → 响应永不发生；发布了却没提交 → 响应为一条从未被
记录的决定而发生。Outbox 让发布成为提交的一个后果。

租约那些细节才是真正的工程所在：一个朴素的 outbox 会在发布者中途死掉时漏消息。带到期 + 回收的租约
意味着一条被遗弃的消息会被重试；**fencing token** 阻止一个恢复过来的属主在其租约已被回收之后重复
发布。

**代价。** 至少一次发布，所以消费方必须幂等——这就是幂等键存在的原因（§29）。

**参考。** `infrastructure/durable/dispatcher.py`，迁移 `*_outbox_lease_*`、
`*_outbox_lease_fencing_token`、`*_outbox_trace_context`。

**追问：** *「为什么要 fencing token，光有租约不够吗？」* → 租约限定的是一个属主*何时*可以行动；它
阻止不了一个被挂起的属主在到期之后行动。fencing token 才是让存储能拒绝那个过期属主写入的东西。

---

## 29. 幂等

**概念。** 一个逻辑业务意图，无论要尝试多少次。

**实现。** 提交带键：`submission_key = response:<tenant>:<proposal>`。一个**CommandReceipt** 是
作用域化的，并记录请求指纹（迁移 `*_command_receipt_scoped_idempotency`、
`*_command_receipt_request_fingerprint`）。并发的同键请求会收敛。

**为什么。** 至少一次投递加上重试，意味着同一条命令会被尝试不止一次。幂等正是让这件事安全的东
西。键从*业务身份*（哪个租户、哪个提案）派生，而不是从尝试计数派生——所以一次重试与一次重复派发被
认作同一个意图。

**代价。** 需要一个收据存储和细心的作用域化。更难的那部分是*作用域*：一个作用域太宽的收据会吞掉一个
真正新的意图；作用域化版本是一次正确性修复。

**参考。** `infrastructure/durable/`、`XP-REL-004`、针对真实 PostgreSQL 的 E3 聚焦持久性集成测试。

**追问：** *「你怎么证明是一个逻辑意图、而不是两个？」* → 聚焦持久性集成测试针对真实数据库驱动真实
重试，并断言只有一个持久意图。

---

## 30. 重试 / 退避

**概念。** 有界重试、有界耐心，以及一个显式的终态结果。

**实现。** 派发器常量：`_MAX_ATTEMPTS = 10`、`_MAX_BACKOFF_SECONDS = 120`。近似指数的有界退避。
`ResponseSubmitExhaustionHandler` 处理耗尽的情形。

**为什么。** 无界重试是活锁。有界重试需要为「到界时会发生什么」给出一个诚实答案——而这里的答案不是
「失败」（§31）。

**代价。** 有界尝试意味着一个暂时坏掉的下游可能被放弃。缓解是：放弃是*显式且可见*的，不是静默的。

**参考。** `infrastructure/durable/dispatcher.py`。

**追问：** *「你在这里找到哪两个持久性 bug？」* → 一次派发器 resolver 失败会把一条本该重试的命令
**送进死信**，而尝试计数器有一个 **off-by-one**。两个都是持久性闭合时发现的，并用回归测试修掉——
这是「显而易见的实现其实是微妙错误的」的好例子。

---

## 31. ATTENTION_REQUIRED

**概念。** 一个显式的不确定态。

**实现。** `ResponseSubmissionStatus` 包含 `ATTENTION_REQUIRED`（迁移
`979070495d4f_add_attention_required_submission_state`）。真实的 `ResponseSubmitExhaustionHandler`
在第 10 次尝试、且此前是不确定失败之后记录它。时间线携带 `SUBMISSION_ATTENTION_REQUIRED`，且
**没有** `SUCCEEDED`/`FAILED` 状态。

**为什么。** 响应系统里最危险的失效是一次*猜测*。如果一次提交超时，系统确实不知道那个动作有没有
发生。标成 `FAILED` 可能导致重试一件已经发生过的事；标成 `SUCCEEDED` 则声称了一个没人观测到的结果。
`ATTENTION_REQUIRED` 说的是真话：*我们试过了，结果未知，必须有人来看。* 工作区必须以那种方式渲染
它——把它呈现成终态成功或 provider 拒绝，两者都会 FAIL 各自的闸门。

**代价。** 一个需要人工跟进的状态，按设计如此。

**参考。** `ResponseSubmitExhaustionHandler`、`XP-REL-004`、闸门 `SUBMISSION_TREATED_AS_SUCCESS`。

**追问：** *「为什么不干脆无限重试？」* → 因为对一个不确定的带副作用动作无限重试，正是把它做两次的
方式。不确定性最终必须浮到人面前。

---

## 32. 提交 ≠ 执行成功

**概念。** 两个独立的状态机。

**实现。**

| `ResponseSubmissionStatus` | 含义 |
|---|---|
| `PENDING` / `RETRYING` | 尚未交出 / 正在重试 |
| `SUBMITTED` | 已交出。**不含任何结果声明。** |
| `FAILED_DEFINITIVE` | 确定性失败 |
| `ATTENTION_REQUIRED` | 不确定——需要人 |

| `ResponseExecutionStatus` | 含义 |
|---|---|
| `QUEUED` / `RUNNING` | 已受理、进行中 |
| `SUCCEEDED` / `FAILED` | **观测到的**终态结果 |

外加一个指向 provider 执行的 `ResponseExecutionRef`，以及一个 provider 执行状态的投影
（`*_response_execution_lifecycle` 迁移）。

**为什么。** 这类集成里最常见的 bug，是把「我发出去了」当成「它成功了」。让两个状态机分开，使这个
错误*不可表示*：不存在任何一个提交状态取值意味着成功。

**代价。** 工作区必须呈现两个状态及其关系，比一个布尔值要多做 UI——而这正是分析员需要的那个区分。

**参考。** `domain/response/`，迁移 `b7c2d41a90ef_response_execution_lifecycle`、
`c8f1a93d5b27_separate_investigation_response_lifecycles`。

**追问：** *「提交之后、观测到结果之前，工作区显示什么？」* → 「已提交——等待结果」，且**没有**捏造
的外部执行 id。`XP-UX-001` 要求执行面事实存在，但禁止把本地提交状态呈现成观测到的结果。

---

## 33. HISIEM 执行真相

**概念。** 执行系统拥有执行记录。

**实现。** 观测 runner 轮询并把 provider 执行状态对账成一个观测状态。**HISIEM 观测到的执行状态是
最终执行真相。** `XP-AUTH-005` 是运行时集成的：真实 HISIEM 控制 API、真实 SOAR worker、真实
Kafka，一个真实 provider 执行 id 抵达终态。

**为什么。** Copilot 对它提交了什么有一个*信念*；HISIEM 对发生了什么有*记录*。如果 Copilot 的信念
能赢，那么一次丢失的响应或一次分叉的重试，就会产出一个自信地报告错误结果的系统。

**代价。** 在观测循环运行期间，Copilot 的视图可能落后于真相——这是正确行为，也是轮询持续到终态的原因。

**参考。** `infrastructure/durable/response_runner.py`、`XP-AUTH-005`。

**追问：** *「如果 Copilot 与 HISIEM 不一致呢？」* → HISIEM 赢。这就是这条边界的定义，也正是「没有
第二个执行真相」的意思。

---

## 34. OpenTelemetry

**概念。** 跨一条异步、持久边界的分布式追踪。

**实现。** `setup_telemetry`（只写一次的 SDK 全局量）、`start_span`、`linked_worker_span`
（SpanKind.CONSUMER，一个**新的根 span，带一条指向原始 trace 的 Link**）、`bind_log_context`
（5 个仅日志字段）、`capture_traceparent` / `validate_traceparent`（W3C v00），以及 FastAPI、
httpx、SQLAlchemy 与 psycopg 的自动埋点。

**为什么异步这个细节重要。** 追踪一个 HTTP 请求是常规操作。追踪*30 秒后在另一个进程里恢复*的工作
不是。一次朴素的续接会在新进程里造出一个没有父节点的子 span——一条断掉的 trace。这个实现在入队时
持久化 `traceparent`，并在恢复时创建一个**链接到**原始 trace 的**新根 span**。这份工作被正确归属到
当时把它入队的那个请求，同时不假装自己是同一个 span——假装会是对因果关系的撒谎。

**代价。** 两条互相链接的 trace，而不是一条连续的 trace——这就是一条异步边界诚实的形状。

**参考。** `infrastructure/observability/`、`docs/contracts/observability.md`、迁移
`b6c2a4d19f30_outbox_trace_context`。

**追问：** *「为什么不直接传播父上下文？」* → 因为 worker 不是那个 HTTP 请求调用栈的延续；它是一条
*由它引起*的独立根。Link 建模的是这件事；父子边声称的是更强的东西，会产出歪曲这个系统的 trace。

---

## 35. 遥测数据安全

**概念。** 遥测绝不能被变成一条数据外泄通道。

**实现。** `sanitize_metric_attributes` 配一份标签键允许清单，以及**fail-closed 的整体观测拒绝**——
一条携带非允许键的观测会被整条拒绝，而不是被部分记录。`FORBIDDEN_TELEMETRY_ATTRIBUTE_KEYS` 与
`FORBIDDEN_METRIC_LABEL_KEYS` 枚举了不允许出现的东西。原始 prompt、completion、完整工具结果、密钥、
embedding 与思维链永不被发出。评估产物在写出之前会做密钥扫描。

**为什么。** 两个理由，第二个才是有意思的那个。基数（cardinality）是通常的理由——把租户 id 当
metric 标签会让序列数爆炸。但更强的理由是**隐私**：遥测会被复制、导出、留存，还常常被送到第三方
后端。一个出现在 span 属性里的 prompt 或凭据，已经离开了你的信任边界。fail-closed 之所以重要，是
因为部分脱敏会把被禁止的值原样留下。

**代价。** 因为不带上载荷，可调试性降低了。缓解是：关联性（id、状态、耗时）被保留，而内容不保留。

**参考。** `infrastructure/observability/`、`XP-OBS-002`、`XP-SEC-003`（闸门 `SECRET_LEAK`），以及
`tests/`。

**追问：** *「为什么 fail-closed，而不是把那一个坏键丢掉？」* → 丢掉那个键会保留这条观测，却静默
丢弃一个调用方以为被记录下来的字段。拒绝整条观测让这个错误变得可见。对一个隐私控制来说，可见的失败
胜过静默的部分合规。

---

## 36. Collector 故障语义

**概念。** 丢掉遥测不得改变任何业务结果。

**实现。** 没有任何业务路径读取 span、trace id 或 collector 状态。`XP-REL-005` 是运行时集成的：
OTel Collector 在**两个真实 Copilot worker 进程**之间被停掉再重启，而持久化的业务结果**完全相同**。
做出判定的闸门是 `TELEMETRY_CHANGED_BUSINESS_STATE`。

**为什么。** 这是把「遥测 ≠ 业务真相」这条边界变成可执行的。人很容易*相信*遥测是旁路的，却意外与之
耦合——比如用一个 span 上下文去关联一条命令，或者在 exporter 挂掉时让请求失败。这个场景证明了这个
解耦，而不是断言它。

**代价。** 没有什么值得说的——全部要点就是遥测在构造上是可选的。

**参考。** `XP-REL-005`、`docs/contracts/observability.md`。

**追问：** *「你怎么让它可测的？」* → 通过在真实进程里，比较同一份工作在 collector 开着与关掉时的
*持久化业务结果*。

---

## 37. 分析员工作区

**概念。** 分析员看到的调查视图，是持久化真相的一个投影。

**实现。** `application/services/workspace_service.py` 与 `queries/workspace.py` 构建这个投影，经
`GET /api/v1/investigations/{id}/workspace` 提供。UI 住在 **HISIEM 仓**
（`HISIEM/web/`），因为 HISIEM 拥有平台的 web 应用。前端的权威推导住在
`web/src/utils/copilot.js`（`evidenceAuthority`、`AUTHORITY_LABELS`、
`proposalSubmissionNeedsAttention`）。

**它必须保住的东西：**

| 要求 | 含义 |
|---|---|
| 权威分级 | 平台事实 vs 知识上下文 vs 模型推导的发现 vs agent 判定 vs 策略 vs 人工决定 vs 执行结果 |
| 服务端真相优先 | 刷新时，过期快照被更新的服务端状态覆盖 |
| 重建 | 一次全新加载精确复现持久化真相 |
| 不发明任何东西 | 不呈现持久化生命周期没有记录的批准/执行/提交状态 |
| 永不渲染内部物 | 没有思维链、prompt 或图 checkpoint |

**为什么 UI 在另一个仓。** HISIEM 拥有平台的 web 应用以及分析员的会话、路由与认证。把 Copilot 的 UI
放进 HISIEM 的控制台是一致的选择——而且它值得显式说明，否则它看起来像个错误。

**代价。** 为一个功能引入一条跨仓边界。缓解办法是：验收 harness 执行真实前端模块，所以评估与 UI
不会漂移。

**参考。** `docs/contracts/investigation-workspace.md`、`XP-UX-001`。

---

## 38. 刷新与过期重建

**概念。** 工作区是一个投影，所以重新加载必须复现真相，而过期客户端必须输给服务端。

**实现。** `XP-UX-002` 在真实投影上测两件事：
`WORKSPACE_RECONSTRUCTED_FROM_DURABLE_STATE`（仅凭持久化状态构建的第二次投影，精确复现持久化真相
——同样的权威分级、发现、判定、策略、决定、提交与执行状态）与
`WORKSPACE_STALE_OVERRIDDEN_BY_REFRESH`（过期快照不同，而刷新落在服务端真相上）。

**为什么。** 重建是对照**持久化真相**量的，永不参照客户端恰好看到的东西——浏览器里的临时内存不是
输入。这才让工作区是一个投影，而不是一个带同步问题的缓存。

**代价。** 一次重载总要花一趟服务端往返。

**参考。** `XP-UX-002`、评估 harness 里的 `cross_plane_workspace.py`。

**追问：** *「可证伪的那一半是什么？」* → 一次落在过期快照上的刷新，或一次与服务端真相不同的重建，
都会 FAIL `EXPECTED_FACTS_PRESENT`。

---

## 39. GP-01

**概念。** 在一个封存的、可复现的数据集上做端到端调查正确性。

**实现。** 一个已提交的逻辑场景被物化成**真实的 HISIEM 资源**（数据集物化器），它们的 provider 身份
被解析，结果被**封存**进一份清单（清单封存器）。一次真实模型运行针对封存清单执行；工具/证据质量被
评估；一个**确定性正确性评分器**给它打分；一个**有界可重复性收集器**给尝试分类；一个套件汇总做聚合。
GP-01 闭合要求 **3/3 次有效正确性通过**。

**为什么。** 先封存，后打分。如果输入可以漂移，分数就没有意义——你永远不知道一次变化来自系统还是
数据。封存清单正是让一次通过可复现的东西。

**代价。** 数据集变化时需要重新封存——刻意的摩擦，并且有一条成文的「绝不为了一个无关改动重新封存
GP-01」规则。

**参考。** `docs/evaluation/`、`src/hisiem_soc_copilot/evaluation/`。

**追问：** *「为什么是 3/3 而不是通过率？」* → 因为一个随机系统 3 次过 2 次，并不显然比 0 次通过更
好——它意味着方差。要求全部有效尝试都通过，让这个声明是二值的、也是诚实的。

---

## 40. KB-GOLDEN-V1

**概念。** 知识子系统的评估基线。

**实现。** 一份带语料版本的版本化语料。检索模式（`HYBRID`、`LEXICAL_ONLY`、`VECTOR_ONLY`）配确定性
打分，一个在打分之前从数据库推导合格语料的封存语料前置条件，以及一个给「基线取自哪个确切数据库
状态」命名的语料指纹。结果：相关闸门上 **679 通过 / 0 跳过**。

**为什么。** 两个机制值得知道：**前置条件**意味着一次跑在错误数据库状态上的运行会拒绝产出数字，而
不是产出一个悄悄错误的数字；**指纹**意味着基线只有连同它所来自的状态才有意义。

**那处诚实的缺口。** `REAL EMBEDDING EVALUATION NOT RUN`。没有配置 embedding provider，所以语义那
一半从未被演练过。`VECTOR_ONLY` 那一行打出接近随机的召回，是**接线**的正面信号（向量通道确实在调
provider，而不是降级成语义词法），对检索质量则毫无信号。产物被标注 `PLUMBING_ONLY`，不得被引用成
语义基线。

**参考。** `docs/evaluation/knowledge-evaluation-contract.md`。

**追问：** *「闭合它需要什么？」* → 配置一个真实 embedding provider 并重跑；harness 不需要改动。
这就是设计意图——夹具是替身，不是替代品。

---

## 41. XP-01

**概念。** 跨平面验收：把系统的不变式变成可执行、可证伪的东西。

**实现。** 9 个族共 29 个场景（Authority 5、Capability 1、Knowledge 4、MCP 5、Observability 2、
Reliability 5、Security 3、Tenant 2、Workspace 2）。13 道硬闸门。3 个执行 profile：26 个确定性、
3 个运行时集成（真实 HISIEM + Kafka、真实 OTel Collector）。每个场景产出一份
`cross-plane-gate-results/v1` 产物。聚合是 `cross-plane-suite-results/v1`，而且是**不可补偿**的。

**为什么不可补偿。** 因为分数会招致优化分数。「29 个里 28 个通过，加权 0.96」掩盖了究竟哪条不变式
破了。这个聚合里**任何地方都没有分数、权重或通过百分比**——一个 FAIL、一个缺失场景、一个未知场景，
或一个缺失的闸门结果，会让整个套件失败。而否定那一半是针对真实产物测的，所以这个聚合是真的能挂的。

**三个说明设计被想透了的细节：**

1. **运行时证据永不被顶替。** 每个运行时集成场景在聚合里*同时*保留自己那份确定性产物。
2. **确定性被强制。** 跨多次读取逐字节相同，与输入顺序无关，身份里没有时间戳或 run id。
3. **产物在原子写入之前做密钥扫描**，而冻结的扫描是子串扫描——这就是为什么有一条不变式键叫
   `credential_marker_absent`，而不是任何包含那个标记本身的名字。

**代价。** 维护 29 个场景是实打实的工作。换来的是「agent 没有批准就不能执行」这样的主张，由一份
机器可读产物支撑，而不是一段话。

**还值得说的一点：** 它是在**不改动任何 HISIEM 生产代码**、也不改动本仓生产层的前提下建起来的。不变式
变得可测，而没有被测系统被改动——这是我最想强调的部分。

**参考。** [README §12](../../README.md#12-评估)、`docs/evaluation/`。

---

## 42. 架构测试

**概念。** 边界由机械方式强制，不是靠约定。

**实现。** `tests/architecture/`——`test_import_boundaries.py`、`test_knowledge_boundary.py`、
`test_evaluation_boundary.py`、`test_cross_plane_boundary.py`、`test_test_isolation.py`。封存时
**285 通过**。具体来说：生产层永不导入评估包；领域保持纯净；模型可选工具面恰好是声明的 4 工具集；
XP-01 包里没有 IO/时钟导入。

**为什么。** 只写在散文里的边界会腐坏。工具面那条断言是最锋利的例子：它意味着*模型的全部行动空间*
不能在测试不失败的情况下改变——而这正是一个安全相关表面上想要的性质。

**代价。** 当确实需要一个合法依赖时，架构测试偶尔会不方便——这正是要点，因为它迫使边界讨论显式发生。

**参考。** `tests/architecture/`、`docs/contracts/python-package-boundary.md`。

**追问：** *「举一个边界测试抓到东西的例子。」* → 模型可选面测试把它钉在恰好四个工具上，所以加第五
个工具而不注册它（或注册一个而没有执行器）会让构建失败。

---

## 43. 重要代码路径

**路径 A —— 一条告警变成判定。**

```text
POST /api/v1/investigations
  → StartAlertInvestigation command → handler
  → partial unique index: one Active Investigation per (tenant, alert)
  → investigation row (CREATED)
  → durable investigation runner picks it up
  → LangGraph: hydrate (system-controlled get_alert_context)
              → model selects a tool (4-tool allowlist)
              → ToolPolicy → ToolBudget → ToolExecutor → provider
              → ToolResult → (grounded only) EvidenceNormalizer → Evidence
              → findings → verdict
  → investigation COMPLETED (or INCONCLUSIVE / FAILED)
```

**路径 B —— 一个判定变成已执行的响应。**

```text
POST .../response-proposals             → ResponseProposal + evaluate_response_policy
                                        → DENY | REQUIRE_APPROVAL
POST .../response-approvals/{id}/approve → ApprovalDecision bound to revision + hash
                                        → (same transaction) outbox event
                                          response_execution_queued
  → dispatcher: lease → submit runner (idempotency key response:<tenant>:<proposal>)
      → HISIEM SOAR
      → PENDING → RETRYING → SUBMITTED | FAILED_DEFINITIVE | ATTENTION_REQUIRED
  → observe runner → ResponseExecutionRef → observed execution status
      → QUEUED → RUNNING → SUCCEEDED | FAILED   (final truth: HISIEM)
  → workspace projection rebuilt from persisted state
```

**路径 C —— 知识检索。**

```text
KnowledgeQuery(topic, context_terms, limit ≤ 5) + tenant_id (required)
  → FTS channel  +  vector channel (pgvector)
  → RRF fusion → bounded KnowledgeHit set
  → citation → citation revalidation
  → validated Knowledge Evidence (authority class: supporting context)
  → finding
```

---

## 44. 失效模式走查

| 失效 | 行为 | 机制 |
|---|---|---|
| 工具调用失败 / provider 挂掉 | 类型化失败，**零 Evidence**；判定不能建立在它上面 | §12、`XP-REL-001/002` |
| 工具返回超大结果 | `RESULT_TOO_LARGE`，永不截断 | §20 |
| 模型选中一个未准入工具 | 不可选——这个面是一份固定白名单 | §8、`XP-MCP-002` |
| 模型选中一个写能力 | 结构上不可能（要求 `READ_ONLY`） | §20、`XP-MCP-003` |
| MCP server 改了某工具的 schema | `SCHEMA_MISMATCH`；该工具变成不可用 | §20 |
| 模型自带租户字段 | 被拒绝；租户由服务端注入 | §21 |
| 模型试图授权一次批准 | 不存在这样的能力 | §25、`XP-AUTH-001` |
| 工具结果里有提示注入 | 改不了作用域、工具面、策略，也造不出引用 | §22 |
| 一条引用解析不了 | `DANGLING_CITATION` 让场景失败 | §16 |
| 一条引用跨了调查 | `CROSS_INVESTIGATION_CITATION` 让场景失败 | §16 |
| 仅凭知识给出确定判定 | `KNOWLEDGE_ONLY_DEFINITIVE_VERDICT` 失败 | §17 |
| 人驳回 | 不创建任何可派发命令 | §25 |
| 批准之后提案变了 | 过期批准无法授权新意图 | §26、`XP-AUTH-003` |
| 提交超时 | `ATTENTION_REQUIRED`——一个显式不确定态 | §31 |
| 重复投递之后的重试提交 | 经幂等键收敛到一个逻辑意图 | §29 |
| 派发器 resolver 失败 | 该命令**不**进死信；它会被重试 | §30 |
| 执行中途进程重启 | outbox 是持久来源；工作恢复，无重复意图 | §27–§29 |
| OTel Collector 被停掉 | 持久化业务结果**完全相同** | §36、`XP-REL-005` |
| 一个密钥进入遥测属性 | 该观测被**整条拒绝** | §35 |
| 前端持有过期状态 | 刷新时服务端真相优先 | §38 |
| 工作区声称一次没被记录的执行 | `WORKSPACE_INVENTED_AUTHORITY` 让场景失败 | §37 |

---

## 45. 最难的设计决策（十个）

1. **把提交状态与执行状态分开。** 两个状态机，第一个的任何一个取值都不蕴含成功。防住了那个经典的
   「我发出去了，所以它成功了」bug。
2. **用 `ATTENTION_REQUIRED` 而不是猜一个终态。** 不确定性是一个真实结果，必须可表示。
3. **把批准绑定到 revision + hash。** 补上批准与派发之间的 TOCTOU。
4. **幂等键从业务身份派生，不从尝试身份派生。** 让重试与重复投递成为同一个意图。
5. **幂等收据必须*作用域化*。** 作用域化是一次正确性修复——作用域太宽会吞掉一个真正新的意图。
6. **让类型化工具失败产出零 Evidence。** 最强的一条反幻觉边界。
7. **把发现与准入当作两件事。** server 的自我描述是不可信输入。
8. **把写入设计掉，而不是沙箱化。** 一个只读的模型面不存在沙箱逃逸这一类 bug。
9. **用 Link 而不是父子关系建模异步 trace 边界。** 诚实的因果关系；父子边会歪曲这个系统。
10. **让验收聚合不可补偿。** 它消除了「套件过了但某条不变式破了」这一整类情况。

---

## 46. 考虑过的替代方案

| 决策 | 选定 | 替代方案 | 为什么不选替代方案 |
|---|---|---|---|
| 工具面 | 固定的 4 工具只读白名单 | 动态 MCP 发现 + 自动准入 | server 的内容会变成信任边界的一部分 |
| 写入 | 完全不可被模型选择 | 沙箱化的写执行 | 沙箱逃逸是一类 bug；拿掉能力就消除了这一类 |
| 证据 | 类型化失败 → 零 Evidence | 让模型自行解读原始结果 | 依据会取决于模型怎么读一个错误字符串 |
| 批准绑定 | revision + hash | 基于时间的批准有效期 | 时间窗内意图变了，窗口就授权了已改变的意图 |
| 幂等键 | `response:<tenant>:<proposal>` | 每次尝试一个 id | 每次尝试一个 id 会让每次重试都是新意图 |
| Outbox 租约 | 租约 + 到期 + 回收 + fencing | 只有租约 | 被挂起的属主能在到期后行动；fencing 拒绝过期写入 |
| 重试终态 | `ATTENTION_REQUIRED` | N 次之后 `FAILED` | `FAILED` 是一句关于没人观测到的副作用的声明 |
| 执行真相 | HISIEM 观测到的状态 | Copilot 自己的提交记录 | Copilot 的记录是信念；HISIEM 的是记录 |
| 向量存储 | 现有 PostgreSQL 上的 pgvector | 专用向量数据库 | 在这个规模上白搭第二套运维系统与第二个真相存储 |
| 排序 | 基于名次的 RRF | 加权分数融合 | BM25 与余弦分数的校准没有原则性答案 |
| 知识权威 | 只作支撑性上下文 | 知识可以支撑确定判定 | 一份写着「封掉这个 IP」的 runbook 不能授权封 IP |
| 遥测标签 | fail-closed 整条观测拒绝 | 丢掉非允许的键 | 隐私控制上的静默部分合规 |
| Agent 拓扑 | 一个 agent | 多智能体 / A2A | 让要防守的权威面乘上一层 |
| 评估判定 | 不可补偿 | 加权分数 | 分数会招致优化分数 |
| UI 不变式的测试方式 | 执行真实前端模块 | 在测试里复述那个映射 | 测试会通过，而 UI 已经漂移 |

---

## 47. 刻意的非目标

以决策表述，不是以缺口表述：

- **多智能体系统与 A2A 协议。** 一个带界工具面的调查 agent。额外的 agent 会让权威面乘一层，并让工具
  选择更难推理、更难设闸。
- **第二个 RAG 框架、向量数据库、可观测性栈或评估平台。** 每个关注点恰好一个归属。给其中任何一个
  加第二套平台，都会造出四平面契约明确禁止的平行架构问题。
- **模型路由器。** 一个 provider 契约，可插拔，没有路由层。
- **投机性的沙箱化。** 写路径是被设计掉的，不是被关起来的。
- **本仓里的前端。** 工作区 UI 属于平台仓。
- **基于模型的授权。** 永不。策略是确定性的；批准是人的。

---

## 48. 当前局限

**按设计正确，但值得说明**

- 提交状态与执行状态刻意分开，所以工作区显示两个状态——呈现更复杂，而它正是那条安全属性的来源。
- 工作区的权威分级是**客户端侧**从持久化来源类型推导的（`E5-OBS-01`）。一个服务端字段会更简单；前端
  推导是通过执行真实模块来测量的。这是一个有记录、被接受的观察。
- 在任何 provider 执行存在之前，工作区的执行面事实就是本地提交状态（`E5-OBS-02`）——要求被呈现为一次
  提交，禁止被呈现为观测到的结果。

**未测量或部分**

- **语义检索质量未测量**（没有配置 embedding provider；产物是 `PLUMBING_ONLY`）。
- **七个可选 span 操作未被发出**（`queue.wait`、`alert.hydrate`、`native.call`、`embedding`、
  `postgres.fts`、`pgvector.search`、`retrieval.merge`）。验收只在运行时真正走过的操作上跑
  （`OBS-001`）。
- **MCP provider 失败分类粒度**较粗（`DEFECT-005`，低，不阻塞；有界类型化失败，不产出假成功
  Evidence）。
- **依赖数据库的测试跳过守卫加固**（`TEST-INFRA-001`，仅测试基础设施）。
- **真实世界的模型波动。** *打分*的确定性是被强制的；真实模型输出的确定性不是，也不可能。GP-01 的
  回应是 3/3 要求与一个有界可重复性收集器，而不是一个假设。

**运维**

- 评估验收运行是一条被驱动的命令，不是接进流水线的 CI 作业。
- 三个运行时集成场景需要真实 HISIEM 栈和一个 Collector；它们不在场时会带明确原因跳过，所以那 26 个
  确定性场景在哪里都能跑。

---

## 49. 这套东西要怎么产品化

按风险排序，不按工作量。

1. **模型治理。** prompt/版本钉住配一套经过评估的变更流程；一个真实 provider 矩阵而不是一个；在工具
   预算之外，还有每次调查的成本与延迟预算。
2. **语义检索。** 配置一个真实 embedding provider、重跑 `KB-GOLDEN-V1`，发布一条真正的语义基线——
   补上最大的那个测量缺口。
3. **批准运维。** 待处理批准的升级与过期策略；对 `ATTENTION_REQUIRED` 条目的通知与 SLA（今天它们是
   显式的，但被动的）。
4. **多租户加固。** 按租户的限流、配额与吵闹邻居隔离；租户作用域的静态加密。
5. **知识生命周期。** 带评审/批准的知识来源摄取管线、来源背书，以及一条尊重引用稳定性的语料刷新
   流程。
6. **CI 集成。** 让 29 场景验收包成为每次构建的闸门，并记录聚合的 `suite_identity`，同时让那 26 个
   确定性场景在每次改动上跑。
7. **运维可观测性。** 把 checkpoint 耗时、派发器积压、outbox 年龄、提交延迟与 `ATTENTION_REQUIRED`
   计数接进告警，配 SLO。
8. **灾难恢复。** 针对 PostgreSQL（含 outbox 与收据）做过测试的恢复，并对在途响应有成文的 RPO/RTO。
9. **审计导出。** 覆盖整条权威链（谁提议、策略说了什么、谁批准、提交了什么、观测到什么）的防篡改
   审计轨迹。
10. **模型成本/性能。** 检索结果缓存；为分析员反馈做调查流式输出；在同一份验收包上评估更小的模型。

---

## 50. 可能的面试问题

**开场**

1. *一句话说，这个系统是什么？* → §1。
2. *为什么一个 AI agent 需要这么多机械？* → 因为一个有写权限的 agent 是负债；这些机械才让 agent
   既**可用**、又安全。
3. *最有意思的是哪部分？* → 权威模型与持久执行路径。

**Agent 工程**

4. *这个 agent 究竟能做什么？* → 恰好 4 个只读工具，外加一次系统控制的上下文获取。§8。
5. *你怎么阻止它幻觉出能力？* → 未实现的工具在另一个集合里，永不被注册；这个面由架构测试断言。
   §8、§42。
6. *工具失败你怎么处理？* → 类型化失败，零 Evidence。§12。
7. *提示注入怎么处理？* → 架构层面：把权威拿掉，注入就没有可操纵的东西。§22。

**RAG / 知识**

8. *检索是怎么工作的？* → FTS + pgvector + RRF，纯确定性排序。§15。
9. *为什么用 RRF？* → 基于名次的融合避开了分数校准。§15。
10. *知识能产出判定吗？* → 不能——一道硬闸门。§17。
11. *检索质量被测过吗？* → 没有。先说这个缺口。§40。

**权威 / 安全**

12. *给我讲一遍 agent 建议封一个 IP 时会发生什么。* → §43 路径 B。
13. *什么阻止批准变过期？* → revision + hash 绑定。§26。
14. *你怎么证明没有批准就不执行？* → `EXECUTION_WITHOUT_APPROVAL`，外加一个真的会让它失败的过期
    绑定用例。§26、§41。
15. *策略 vs 批准？* → 规则适用 vs 同意。§24、§25。

**可靠性**

16. *提交超时了怎么办？* → `ATTENTION_REQUIRED`。§31。
17. *你怎么避免执行两次？* → 幂等键 + 作用域化收据 + fencing token。§28、§29。
18. *`SUBMITTED` 是什么意思？* → 只是「已交出」。§32。
19. *谁说了算？* → HISIEM 观测到的状态。§33。
20. *讲讲你找到的那些持久性 bug。* → resolver 失败导致死信；尝试计数 off-by-one。§30。

**评估**

21. *你怎么知道这些是能用的？* → GP-01、KB-GOLDEN-V1、XP-01。§39–§41。
22. *为什么不可补偿？* → 分数会招致优化分数。§41。
23. *这个聚合会失败吗？* → 会，而且否定那一半是针对真实产物测的。

**设计判断**

24. *你会怎么改？* → §46、§49。
25. *什么是刻意没做的？* → §47——并且准备好为每一条辩护。

---

## 51. 深度追问

### D1. 「为什么 `ATTENTION_REQUIRED` 是一个状态而不是一个错误？」

因为世界的真实状态是*未知*，而每一个现成的错误状态都是一句**声明**。`FAILED` 声称那个动作没有发生
——但一次超时可能意味着它发生了、只是响应丢了。`SUCCEEDED` 声称它发生了。两者都是对一个真实系统上
副作用的猜测，而在这里猜错，就是你封一个 IP 封两次、或者把一台主机留在未隔离状态的方式。

`ATTENTION_REQUIRED` 是唯一说出实际真相的状态：*我们尝试过，结果不确定，需要人来看。* 工作区被禁止
把它渲染成终态成功或 provider 拒绝，而两种呈现都会 FAIL 各自的闸门。§31、`XP-REL-004`。

**追问：「这不就是把活儿推给人吗？」** → 是——刻意的。替代方案是把*风险*推给一个不知道自己摊上了这件
事的人，那更糟。产品化的答案是升级与通知，好让人尽快知道（§49）。

### D2. 「你怎么保证模型不能执行任何东西？」

分层防守，而且这些层是结构性的、不是行为性的：

1. **不存在这样的能力。** 注册表里没有任何执行响应的工具。禁止清单显式点名它们（`block_ip`、
   `isolate_host`、`start_soar_execution`、`approve_response`……），而它们不在可选集合里。
2. **写入在 provider 层不可选。** `is_model_selectable` 要求 `READ_ONLY`，所以即便某个 MCP server
   提供写能力，也到不了模型面前（§20）。
3. **策略是确定性的、系统拥有的。** 模型不评估策略（§24）。
4. **批准是人对一个独立端点的决定。** 不存在任何代码路径让模型输出变成一次批准（§25）。
5. **批准绑定到 revision 与 hash**，所以它授权的是一句具体意图（§26）。
6. **命令是持久且幂等的**，所以它不可能被签两次（§27–§29）。
7. **以上全部都被设闸**，在 `XP-AUTH-001/002/003` 与 `EXECUTION_WITHOUT_APPROVAL` 里。

**一句话版本：** *从模型输出到副作用之间不存在任何代码路径。那条路径上的每一条边都要经过一个模型
无法影响的组件——而每条边都有一个验收场景。*

### D3. 「为什么发现与准入是分开的？」

因为发现是远端的、受攻击者影响的内容，而准入是本地的、由人写下的决定。一个 server 想什么时候加
工具、改描述、改 schema 都可以。如果发现蕴含准入，那么**控制了 server 就控制了 agent 的行动空间**
——包括工具*描述*，那是 prompt 面。

把它们分开意味着：一个新工具被发现（运维看得见），但在有人写下一条带受信描述、契约、风险分级、租户
作用域与预期指纹的准入项之前，它不可选。已准入名下的 schema 变更会变成 `SCHEMA_MISMATCH`，而不是
静默改变的行为。§18–§20。

**追问：「那不是很多手工活吗？」** → 是，而这就是功能。那一步手工正是人决定信任什么的地方，而能力
足够少，所以这个成本有界。

### D4. 「为什么幂等键不是每次尝试一个 UUID？」

因为幂等关乎**业务身份**，不是尝试身份。每次尝试一个 UUID 会让每次重试都是一个全新意图——而那正是
幂等本该防住的那个 bug。`response:<tenant>:<proposal>` 说的是「这个租户对这个提案的响应」，所以一次
重试、一次重复投递与一次重新派发都收敛到一个意图。

微妙之处在于收据的**作用域**。太宽，它会吞掉一个合法的新意图（对同一个提案的第二份、确实不同的
响应）；太窄，它去不了重。把这个作用域做对是一次正确性修复，而不是初版设计——这是「用了幂等」与
「幂等是对的」之间差别的一个诚实例子。§29。

### D5. 「outbox 和 exactly-once 有什么区别？」

Outbox 给出的是**至少一次**发布，并保证与已提交事务有一条*因果链*。它不给 exactly-once。它解决的是
双写问题——「提交成功的话，发布发生了吗？」——办法是让发布成为提交的后果，而不是第二个独立动作。

要让投递 exactly-once，需要一个事务性 broker 或去重——而这里的去重正是幂等键在消费方提供的东西。所以
组合是：outbox 负责因果保证，幂等消费方负责重复容忍。说 outbox 单独提供「exactly-once」就是过度
声称。§28、§29。

### D6. 「为什么工作区在另一个仓？」

因为 HISIEM 拥有平台的 web 应用——控制台、分析员的会话、路由与认证。Copilot 工作区是那个控制台里的
一个页面，不是一个独立应用。把它放进 Copilot 仓，要么得复制平台的前端基础设施，要么得建第二个控制台。

代价是一条跨仓的功能边界，而缓解办法很具体：验收 harness **执行真实前端模块**
（`web/src/utils/copilot.js`），而不是在测试里复述它的权威映射。所以评估与 UI 不会漂移——一次破坏
不变式的推导改动会让验收运行失败，而不只是另一个仓里的一个单元测试失败。§37。

**追问：「那 Copilot 仓看起来是不是不完整？」** → 在解释之前它看起来是这样，这正是 README 要显眼地
说明这一点的原因。README 跨仓那一节的 §3。

### D7. 「你声称没有第二个真相来源。说服我。」

这个主张是：没有任何组件的本地状态能被提升为业务真相。逐个检查候选：

| 候选 | 为什么它赢不了 |
|---|---|
| LangGraph checkpoint | 工作记忆；永不被读成领域状态 |
| OTel span / trace id | 没有业务路径读 span；由 collector 故障等价比对证明 |
| 前端状态 | 从持久化状态推导；刷新时服务端赢；`WORKSPACE_INVENTED_AUTHORITY` 是一道硬闸门 |
| MCP 调用状态 | provider 局部；不是持久执行记录 |
| 原始 ToolResult | 不可信数据；只在有据成功时经归一化器变成 Evidence |
| Copilot 的提交记录 | 是信念；HISIEM 观测到的状态才是最终 |
| 评估产物 | 从真实运行*产出*、由聚合消费；它们从不回写 |

最强的一条证据是 `XP-REL-005`：在两个真实 worker 进程之间停掉 Collector，持久化的业务结果**完全
相同**。这是把遥测边界变得可执行，而不是被断言。§36、D5。

### D8. 「如果从头来过，你会怎么改？」

按确信程度排序的诚实回答：

1. **先补上 embedding 缺口。** 先建了混合检索、却无法测量它的语义质量，是一个排序错误——测量很便宜，
   而这个缺口是项目里最大的诚实漏洞（§40、§49）。
2. **把权威分级放到服务端。** 我在前端从持久化来源类型推导证据权威分级。它是对的，也是被测量过的，
   但一个服务端字段本可以消掉一处跨仓耦合（§48、`E5-OBS-01`）。
3. **从一开始就让验收包成为 CI 闸门。** 它今天是一条被驱动的命令；聚合稳定的 `suite_identity` 本就是
   设计成每次构建记录下来的，早点接上本可以更早抓到回归。
4. **为 `ATTENTION_REQUIRED` 加升级机制。** 状态是对的，但它是被动的——没有人被通知。显式是必要的，
   但不是充分的。

我**不会**改的：单 agent 拓扑、4 工具只读面、不可补偿聚合，以及把工作区留在平台仓。每一条都是我宁可
辩护、也不愿重来的决定。
