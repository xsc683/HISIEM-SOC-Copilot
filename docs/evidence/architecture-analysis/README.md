# HISIEM-SOC-Copilot 架构与实现分析文档集

> **怎么读本集** —— 本集是**代码级取证层**：不是契约，也不是入门材料。
> 建议路径：先读 [`../guide/`](../../guide/) 建立整体认知 → 再读权威契约 → 想确认「代码真的是这样吗」时再读本集。
> 本集独有的是 **`file:line` 锚点与反直觉的真实形态**。`docs/README.md` 规定「Architecture and persistence documents are authority」——**冲突时以权威契约为准**；而**代码是最终事实**。
> 完整阅读地图见 [`../guide/04-想深入读哪一篇.md`](../../guide/04-想深入读哪一篇.md)。
>
> **取证基线 ≠ 当前 HEAD ≠ 当前代码语义** —— 本集基于分支 `capability-mcp` @ `1e567be` 建立，那是本组 `file:line` 取证的**原始代码基线**，此后**未重新扫描**。仓库继续演进；已检查到的后续 `*.py`/`*.sql` 改动只涉及注释、docstring 与人类可读消息文本（例如文档路径引用），未改变本集记录的源码语义。因此三件事要分开看：`1e567be` = **取证快照的代码基线**；当前 `HEAD` = **现行仓库状态**；**核验某条证据是否仍成立时，以当前代码为最终事实**。

> **分析对象**：`D:\Project\HISIEM-SOC-Copilot`（Hatchling `src/` layout，包根 `src/hisiem_soc_copilot/`；Python 3.12+ · FastAPI · LangGraph + langgraph-checkpoint-postgres · SQLAlchemy 2 async + psycopg 3 + pgvector · Alembic · OpenTelemetry · MCP SDK）
> **分析方式**：按子系统边界拆分，对**实际源码**取证——关键结论均附 `file:line` 相对路径证据。
> **取证工具链**：本仓**无 `.codegraph/` 索引**，采用**源码直读 + AST 边界测试直读 + grep 统计**；每个 `file:line` 都对应真实代码，不凭空编造行号。**部分论断在项目 venv 中实跑验证**（见 §2）。
> **取证代码基线**：分支 `capability-mcp` @ `1e567be`（本集快照；当前仓库状态见上）
> **生成日期**：2026-09-22
> **文档集**：00–08 共 9 篇。**各篇的行数与图数不在本表维护**——它们随每次编辑漂移，也不是读者需要的信息（现算：`wc -l <篇>` 与 `grep -c mermaid <篇>`）

---

## 〇、文档集导航总表

| # | 文档 | 主题 |
| --- | --- | --- |
| 00 | [00-分层架构与包边界总览.md](00-分层架构与包边界总览.md) | 十层包结构、5 个 AST 边界测试、组合根、配置入口、开关表 |
| 01 | [01-端到端关键数据流.md](01-端到端关键数据流.md) | 7 条数据流 + outbox 三分类失败 + 检查点安全 |
| 02 | [02-领域模型与调查编排.md](02-领域模型与调查编排.md) | 聚合根状态机、图状态、8 个节点 |
| 03 | [03-工具执行策略与模型Provider.md](03-工具执行策略与模型Provider.md) | 注册表与准入、五段链、Provider 架构、MCP、LLM 适配器 |
| 04 | [04-证据归一化与响应闭环.md](04-证据归一化与响应闭环.md) | 证据链、响应提案聚合、策略、HISIEM 集成 |
| 05 | [05-应用层上游集成与API交付.md](05-应用层上游集成与API交付.md) | 命令/查询/处理器/端口、HISIEM 读适配、信任边界、HTTP |
| 06 | [06-知识库与威胁情报.md](06-知识库与威胁情报.md) | 混合检索四段管线、嵌入独立性、ATT&CK |
| 07 | [07-持久化可靠性与可观测性.md](07-持久化可靠性与可观测性.md) | 持久化四层、13 迁移、三层可靠性、隐私安全埋点 |
| 08 | [08-评估体系.md](08-评估体系.md) | 预言机防火墙、封存、执行桥、失败四分类 |

### 拆篇依据

篇数由**代码实际的包边界与架构测试**决定：

| 篇 | 边界性质 | 判据 |
| --- | --- | --- |
| 00 | 全集总览 | 必须有的入口篇 |
| 01 | 跨子系统数据流 | 必须有的纵切篇 |
| 02 | **域 + 图**（同一设计的两半） | `domain/` 禁框架；`agent/graph` 是它的执行侧 |
| 03 | **工具判断链** | `agent/tools` + `contracts` + `infrastructure/{llm,mcp}` |
| 04 | **响应不变式所在** | `domain/response` + `agent/evidence` + `infrastructure/soar` |
| 05 | **入站 + 中枢 + 出站读** | `api/` + `application/` + `infrastructure/{auth,hisiem}` |
| 06 | **独立受限上下文** | 有**自己的** `test_knowledge_boundary.py` |
| 07 | **基础设施四件** | `persistence` / `durable` / `checkpoint` / `observability` |
| 08 | **独立架构约束** | 有**两条**自己的边界测试，且体量超业务域 |

---

## 一、分析对象与代码事实基线

### 仓库定位表

| 维度 | 内容 | 证据 |
| --- | --- | --- |
| 项目名/包名 | `hisiem-soc-copilot` / `hisiem_soc_copilot` | `pyproject.toml:6` |
| 定位 | HISIEM 之上的 AI 调查与响应**决策层**；**agent 永不授权** | `README.md`；各篇 |
| 运行时 | **Python `>=3.12`**（venv 实测 3.13.14） | `pyproject.toml:8` |
| Web 框架 | **FastAPI** + Uvicorn | `pyproject.toml:13-14` |
| 编排 | **LangGraph** + `langgraph-checkpoint-postgres` | `pyproject.toml:19-20` |
| 持久化 | **SQLAlchemy 2 async** + **psycopg 3** + **pgvector** | `pyproject.toml:16-17,24-26` |
| 迁移 | **Alembic，13 个迁移**（+ `__init__.py`） | `alembic/versions/` |
| 模型契约 | 自有 `ModelProvider` 协议；OpenAI 兼容适配器 | `contracts/llm/`；`infrastructure/llm/openai_compatible.py` |
| 工具互操作 | **MCP SDK `>=2.2,<3`**，协议 **`2026-07-28`** | `pyproject.toml:29`；`infrastructure/mcp/provider.py:40` |
| 可观测 | **OpenTelemetry**（api/sdk/exporter + 4 个 instrumentation） | `pyproject.toml:27-32` |
| 质量门 | `pytest` + `ruff`（line 100）+ **`mypy`**（strict） | `pyproject.toml:52-70` |
| 规模（src） | **230** 个 `.py` | `find src -name '*.py' -not -path '*__pycache__*' \| wc -l` |
| 规模（tests） | **149** 个 `.py` | `find tests -name '*.py' \| wc -l` |
| 规模（评估体系） | `evaluation` **28** + `evaluation_harness` 21 = **49 个文件 / 20472 行** | 08 篇 §「为什么评估独立成篇」 |
| 架构测试 | **5 个**（`tests/architecture/`），全部 AST 扫描 | `tests/architecture/test_*.py` |
| 生产层 | **6 个**（`domain` / `application` / `agent` / `api` / `infrastructure` / `bootstrap`）——**显式枚举** | `test_evaluation_boundary.py:25-27` |
| 非生产包 | **4 个**（`contracts` / `knowledge` / `evaluation` / `evaluation_harness`） | 00 篇 §1.1 |
| 代码索引 | **无 `.codegraph/`** → 取证降级为直读 + grep | 仓库根 |

---

---

## 二、核查记录（对实际源码的事实核对）

**核查方式**：每篇成文后**回查每条 `file:line` 是否对应真实代码**，并用可复现的 `wc -l` / `grep -c` / `find` 重新计数。

### 2.1 与直觉/旧文档不同的真实形态（逐条）

以下 **17 条**是核查中发现的**代码真实形态违反命名直觉**或**与文档/常识冲突**的地方。**文档已按代码如实处理**。

| # | 反直觉真实形态 | 证据 | 影响 |
| --- | --- | --- | --- |
| 1 | **`PolicyDecision` 只有两个取值**——`ALLOW_AUTOMATIC` **在类型系统里不存在**，不是「存在但不用」 | `domain/response/enums.py:20-24` | **「Agent 不能授权」是结构性保证**，不是约定。加它必须新增枚举值，那是一次显眼的改动 |
| 2 | **调查聚合**刻意没有**审批/执行状态**——「an Investigation never transitions because a proposal was approved, rejected, or executed」 | `domain/investigation/aggregate.py:38-41` | 调查的职责是「查清楚」，响应是「处置」；混在一起会让「查完但没批」变成模糊状态 |
| 3 | **`REJECTED`（人拒绝）与 `DENIED`（策略拒绝）是两个不同状态** | `aggregate.py:29-35` | 让「为什么没执行」有准确答案 |
| 4 | **提案人刻意不进内容哈希**——「provenance is not part of the approvable contract, so it can never be used to make an approval hash match or mismatch」 | `aggregate.py:53-59` | 换提案人不该让已有审批失效 |
| 5 | **`sealer` 刻意没有 `seal(draft)` API**——「sealing an unverified dataset is structurally impossible」 | `evaluation/sealer.py:4-7` | 与第 1 条同一种手法：**不给那个能力** |
| 6 | **`domain` 连 `pydantic` 都不能导入** | `test_import_boundaries.py:32`；`:97-101` 二次断言仅 stdlib 可导入 | 域模型是纯 dataclass；schema 在 `contracts/` |
| 7 | **`api` 不得导入 `agent`**——唯一一条**同层横向**的禁止 | `test_import_boundaries.py:46-51` | HTTP 处理与 AI 编排之间隔了一层可测试的用例边界 |
| 8 | **`httpx2` 未在 `pyproject.toml` 声明却被直接 import**——它来自 `mcp>=2.2,<3` 的传递依赖（`Requires-Dist: httpx2>=2.5.0`） | `infrastructure/mcp/provider.py:18`；`mcp-2.2.0.dist-info/METADATA` | **间接依赖被当直接依赖用**：`mcp` 若换 HTTP 客户端即 ImportError。建议提升为显式依赖 |
| 9 | **outbox 的 claim/mark 绝不放在图/LLM/网络事务里** | `persistence/repositories/durable.py:5-8` | 图的运行可能几分钟，**长事务会占住连接且租约对别的 worker 不可见** |
| 10 | **预算计数进检查点，崩溃重启不重置为满额** | `agent/graph/budget.py:8-12` | 若不进检查点，**反复崩溃 = 无限预算** |
| 11 | **`execute_and_ingest` 把工具调用与证据落库合成**一个**节点** | `agent/graph/builder.py:9-13,79` | 分开会让崩溃丢证据或重复执行工具 |
| 12 | **评估体系的体量超过业务域**：按文件数 49 vs 29（约 1.7 倍），按行数 20472 vs 3107（约 6.6 倍）——**两种度量口径差得很远，引用时必须说明是哪一个** | 08 篇 §「为什么评估独立成篇」 | 「先建可信评估、再建功能」的直接体现 |
| 13 | **「生产层」是显式枚举的 6 元素集合**，不是「src 下所有包」——**新包默认不受边界约束** | `test_evaluation_boundary.py:25-27`；`test_cross_plane_boundary.py:38-42` | 取舍是「宁可漏也不误伤」，靠 code review 兜底 |
| 14 | **一条守卫曾因 `_` 不是 `.` 而静默失效**——`_reaches(target, "evaluation")` **不匹配** `evaluation_harness` | `test_cross_plane_boundary.py:5-12` | 作者**在测试 docstring 里承认了这一点**并补上显式断言 |
| 15 | **知识边界测试带**阳性对照**扫描器**——「the SAME scanner is then pointed at the infrastructure repository as a positive control」 | `test_knowledge_boundary.py:15-17` | 防「一个什么都没扫到的扫描器让规则永远通过」 |
| 16 | **HTTP 敏感字段在 server hook 处替换**，**不是导出前过滤** | `observability/bootstrap.py:22-24` | 敏感值**从不进入可观测管线**（导出前过滤会让它短暂存在于 span 对象里） |
| 17 | **GP-01 评估的就是 SIEM 上那条**真实**规则——五项逐值吻合** | `evaluation/contracts.py:43-47` vs `SIEM/infra/rules/rule-ssh-brute-force-001.yaml` | 见下方专表 |

**第 17 条的实证对照**（这是两个仓库之间的一处硬连接）：

| Copilot 常量 | 值 | SIEM 规则 YAML |
| --- | --- | --- |
| `GP01_RULE_ID`（`contracts.py:43`） | `rule-ssh-brute-force-001` | `id: rule-ssh-brute-force-001` |
| `GP01_RULE_KEY_FIELD`（`:44`） | `source.ip` | `keyField: source.ip` |
| `GP01_RULE_CONDITION`（`:45`） | `authentication_failure` | `condition.value: authentication_failure` |
| `GP01_RULE_THRESHOLD`（`:46`） | `5` | `threshold: 5` |
| `GP01_RULE_WINDOW_MINUTES`（`:47`） | `5` | `windowMinutes: 5` |

**五项全部一致。** 所以**评估通过的语义是「针对真实规则端到端正确」**，而不是「针对人造靶子正确」。

**另有 3 处「同名不同义」值得单列**（易误读，但不是错误）：

| 概念 | 含义 A | 含义 B |
| --- | --- | --- |
| **`reconcile`** | SIEM 的 `DetectionRuntimeService.reconcileDesiredStates`（收敛**视图**） | SIEM 的 `FlinkRuntimePort.apply/stop`（执行**物理动作**） |
| **`manifest`** | Flink 的 `runtime-manifest.json`（**文件**） | 控制面的 `detection_runtime_manifest`（**表**） |
| **`Graph state` vs `Domain state`** | 图状态：跨步工作状态，**只支持故障恢复** | 域状态：**真相**（`state.py:7-8`） |

### 2.2 核心实现结论表

| 主题 | 结论 | 证据来源 |
| --- | --- | --- |
| **核心不变式** | **Model proposes → Policy constrains → Human authorizes → Durable command records intent → Platform executes → Copilot observes；agent 永不授权** | `README.md`；04 篇全篇。**结构性强制的三处**：`PolicyDecision` 只有 2 值、提案状态机无 `CREATED→APPROVED` 边、策略只产出 `DENY`/`REQUIRE_APPROVAL` |
| **十层包结构** | 6 生产层 + 4 非生产包；**边界由 5 个 AST 测试强制**，不靠文档约定 | `tests/architecture/test_*.py`；00 篇 §1–§2 |
| **`domain` 纯净性** | 禁 `pydantic` / `sqlalchemy` / `langgraph` / `fastapi` / `httpx` / `alembic`；**仅 stdlib 可导入** | `test_import_boundaries.py:22-34,97-101` |
| **`api` 边界** | 禁 `agent` / `langgraph` / `infrastructure.persistence` / `infrastructure.hisiem` | `test_import_boundaries.py:46-51` |
| **唯一组合根** | `Container`（717 行）——只有它知道全部具体实现 | `bootstrap/container.py:1-7,75` |
| **组合根的资源管理** | 显式回收「无请求作用域」的 `UnitOfWork`；5 类异步资源由 lifespan 持有 | `container.py:89-95`；`container.py:78-88` |
| **配置入口** | 单一 `config.py`（516 行），13 个 `Settings` 分组；**7 个关键开关默认关闭/不信任/假实现** | `config.py:494-511`；00 篇 §7 |
| **outbox 三分类失败** | 解析失败**永不**消耗死信预算（永远重试）；不可重试立即死信；可恢复退避重试 | `dispatcher.py:10-15,196-213,236-255` |
| **耗尽钩子** | **destination 决定死信的业务含义**，且**先写业务事实再死信**；钩子失败则保持可重试 | `dispatcher.py:71-86,325-346` |
| **fencing** | 租约续期被 token fence；续期失败被忽略（结算时校验） | `dispatcher.py:226-235,268-304` |
| **有界重试** | 指数退避 ≥2s 起、**上限 120s**——次数无限但速率有界 | `dispatcher.py:373-376` |
| **图拓扑** | 8 个节点，唯一回环是 `decide_next ⇄ execute_and_ingest`；**边只读确定性标记，不问模型** | `builder.py:75-107,15-17` |
| **检查点安全** | 工具调用 + 证据落库**合为一个节点**；`Domain wins over checkpoint` | `builder.py:9-13,47-51` |
| **预算** | 单一权威值对象；**进检查点不重置**；**为收敛保留 2 个 LLM 槽**；耗尽走确定性降级 | `budget.py:1-22` |
| **工具准入** | **`is_model_selectable` = `model_selectable` AND `risk == "READ_ONLY"`**——配置错误不会越权 | `providers.py:104-106` |
| **工具面** | 三个集合：系统控制 / 模型可选 / 未来目录（**不注册**） | `registry.py:3-12,28,31-32` |
| **MCP** | 协议 `2026-07-28` **三处独立固定**；16 个运行时保留参数模型不得使用；职责仅限 transport/discovery/结果归一化 | `mcp/provider.py:40,44-60,1-6`；`config.py:158,216,224-225` |
| **MCP 验证** `[已执行]` | 单元 + **真实 Streamable HTTP 集成**共 **24 passed** | 实跑：`pytest tests/unit/agent/test_mcp_provider.py tests/integration/test_mcp_streamable_http.py` |
| **LLM 适配器** | SDK 重试禁用；结构化输出三级降级（`json_schema`→`json_object`→`json_only` prompt）；**401/403 是部署错误不重试**；usage 缺失记 `None` 不猜 | `openai_compatible.py:10-32` |
| **证据来源** | 事件用 `{index, document_id, query_fingerprint}`；**永不编造 `ExternalResourceRef`** | `normalizer.py:11-13` |
| **响应契约** | 审批绑定**精确 `content_revision` + `content_hash`**；哈希取 `action_key` + `target_refs` 四字段 + `parameters` | `aggregate.py:106-125` |
| **进审批五道闸门** | 动作已注册 / 有目标 / **有证据** / 策略已完成 / 策略非 DENY——**领域方法存在，但生产路径不调用它**（由 handler 内联强制） | `aggregate.py:89-103`；`handlers/response.py:115-157` |
| **混合检索** | 两路独立候选（各 20）+ **RRF `k=60`** + **每文档 ≤ 2 条**；**排序是纯函数** | `knowledge_retrieval.py:1-19`；`config.py:415-418` |
| **检索权威立场** | 「**摘要是数据，不是指令**；引用是待重验的参考，不是授权」 | `knowledge_retrieval.py:16-19` |
| **嵌入独立性** | 与对话 LLM **完全独立**；未配置时**明确告知**不静默降级；**无 ANN 索引** | `config.py:452,459-460`；`pyproject.toml:24-26` |
| **持久化四层** | ORM(9) → Mapper(6) → Repository(6, **3140 行**) → UnitOfWork | 07 篇 §1 |
| **事务边界** | 领域行与事件/outbox/回执**同事务**；outbox claim/mark **各自独立短事务** | `durable.py:1-8` |
| **检查点隔离** | 独立 `database_url` + 独立 `schema_name`（`langgraph_checkpoint`）；**不是真相** | `config.py:43,56`；`state.py:7-8` |
| **可观测隐私** | 敏感 HTTP 字段**在 server hook 处替换**（早于 processor/exporter）；清单实测 **11 个字段**，含 `authorization` 与 `cookie` 请求头 | `observability/bootstrap.py:22-36` |
| **工具遥测** | 是**子类**（`ObservedToolExecutor(ToolExecutor)`），不是装饰器——`agent` 禁导入 `infrastructure`，所以埋点不可能由 agent 自己做 | `observability/tools.py:27`；`test_import_boundaries.py:45` |
| **预言机防火墙** | 生产只收到 **4 字段 launch projection**；oracle / events / control events / sealed object **永不穿过** | `evaluation_harness/harness.py:6-9` |
| **评估隔离** | dispatcher 必须在 `Container.open()` **之前**关闭；harness 手动 `drain_once` | `harness.py:14-16` |
| **评估失败分类** | 4 类，**「无效失败」（provider 瞬时）不算产品失败** | `evaluation_harness/classification.py` |
| **来源分离** | `dataset_*` 来自清单记录的代码版本；`execution_*` 是当前 HEAD/worktree | `harness.py:18-20` |
| **内容寻址出现四次** | 语料身份 / ATT&CK 发布指纹 / 响应内容哈希 / （SIEM 侧）检测计划哈希 | 08 篇 §3.2 |
| **跨仓库契约** | Copilot → `/api/internal/soar/executions` + 服务令牌 + `X-Tenant-ID`；HISIEM 侧 fail-closed | 04 篇 §4.3；SIEM 03 篇 §4 |

---

## 三、关键开关与运行模式

### 3.1 影响行为的关键开关

| 开关 | 默认 | 作用 | 证据 |
| --- | --- | --- | --- |
| **`llm.provider`** | **`scripted`** | 模型提供方；`scripted` = 确定性假模型 | `config.py:95` |
| `llm.zdr` | `true` | Zero Data Retention（发 `x-cmd-zdr: 1`） | `config.py:104` |
| `llm.structured_output_mode` | `auto` | 三级降级探测 | `config.py:107` |
| **`app.enable_dispatcher`** | **`false`** | outbox 派发（调查推进） | `config.py:272` |
| **`app.enable_response_worker`** | **`false`** | 响应观测 worker | `config.py:276` |
| `app.response_observe_interval_seconds` | `15.0` | 响应观测轮询间隔 | `config.py:282` |
| **`observability.tracing_enabled`** | **`false`** | OTel 追踪 | `config.py:137` |
| `observability.trace_sample_ratio` | `1.0` | 采样率 | `config.py:139` |
| **`mcp.enabled`** | **`false`** | MCP provider | `config.py:215` |
| `mcp.refresh_interval_seconds` | `0.0`（不刷新） | MCP 工具重发现 | `config.py:217` |
| **`auth.trusted_context_provider`** | **`none`** | 入站信任来源；`none` = 不信任 | `config.py:321` |
| **`embedding.provider`** | **`unconfigured`** | 嵌入提供方 | `config.py:470` |
| `embedding.normalization` | `NONE` | 向量归一化 | `config.py:478` |
| `embedding.distance_metric` | `COSINE`（**唯一取值**） | 距离度量 | `config.py:479` |
| `app.debug` | `false` | 调试模式 | `config.py:266` |
| `app.api_host` / `app.api_port` | `0.0.0.0` / `8000` | 监听地址 | `config.py:268-269` |

> **默认值画像**：**7 个关键开关默认「关闭 / 不信任 / 假实现」**——`scripted` LLM、关闭的 dispatcher、关闭的响应 worker、关闭的追踪、关闭的 MCP、不信任的 auth、未配置的 embedding。
>
> **这不是保守，而是「默认不产生外部副作用」**：新克隆的仓库起来后**不会调用真模型、不会推 outbox、不会连 MCP**。

### 3.2 Agent 预算（6 维上限，全部 `ge=1`）

| 上限 | 默认 | 证据 |
| --- | --- | --- |
| `agent_budget.max_steps` | `20` | `config.py:119` |
| `agent_budget.max_tool_calls` | `30` | `config.py:120` |
| `agent_budget.max_tool_calls_per_step` | `4` | `config.py:121` |
| `agent_budget.max_llm_calls` | `20` | `config.py:122` |
| `agent_budget.max_llm_tokens` | `20_000` | `config.py:125` |
| `agent_budget.max_duration_seconds` | `600` | `config.py:126` |

**六个上限里只有 4 个真正被强制**：`max_steps` / `max_tool_calls` / `max_llm_calls` / `max_duration_seconds` 有强制点；**`max_tool_calls_per_step` 无任何强制点、`max_llm_tokens` 显式「暂不计量」**（详见 00 篇 §4 论断 6）。`max_llm_calls` 另有保留槽不变量（03 篇 §2 论断 5）。**「配置项存在」不等于「约束生效」。**

### 3.3 知识与检索参数

| 参数 | 默认 | 证据 |
| --- | --- | --- |
| `knowledge.max_document_bytes` | `2_000_000` | `config.py` |
| `knowledge.max_chunks_per_document` | — | `config.py:79` |
| `knowledge.max_chunk_chars` | `8_000` | `config.py:86` |
| `knowledge.lexical_candidate_limit` | `20` | `config.py:415` |
| `knowledge.vector_candidate_limit` | `20` | `config.py:416` |
| **`knowledge.rrf_k`** | **`60`** | `config.py:417` |
| `knowledge.max_hits_per_document` | `2` | `config.py:418` |

### 3.4 MCP 结果边界

| 参数 | 默认 | 证据 |
| --- | --- | --- |
| `result_bounds.timeout_seconds` | `30.0` | `providers.py:32` |
| `result_bounds.max_items` | `100` | `providers.py:33` |
| `result_bounds.max_serialized_bytes` | `256_000` | `providers.py:34` |
| `result_bounds.max_text_chars` | `32_000` | `providers.py:35` |
| `result_bounds.max_depth` | `8` | `providers.py:36` |

**per-capability 边界不得超全局**（`providers.py:43-49`，配置期抛异常）。

---
