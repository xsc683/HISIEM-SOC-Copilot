# HISIEM-SOC-Copilot 架构与实现分析文档集

> **怎么读本集** —— 本集是**代码级取证层**：不是契约，也不是入门材料。
> 建议路径：先读 [`../guide/`](../guide/) 建立整体认知 → 再读权威契约 → 想确认「代码真的是这样吗」时再读本集。
> 本集独有的是 **`file:line` 锚点与反直觉的真实形态**。`docs/README.md` 规定「Architecture and persistence documents are authority」——**冲突时以权威契约为准**；而**代码是最终事实**。
> 完整阅读地图见 [`../guide/04-想深入读哪一篇.md`](../guide/04-想深入读哪一篇.md)。
>
> 取证锚点：分支 `capability-mcp` @ `1e567be`。其后全部提交均为文档改动（`git diff --stat 1e567be..HEAD -- '*.py' '*.sql'` 为空），**代码未变**，故本集结论仍然现行。


> **怎么读本集** —— 本集是**代码级取证层**：不是契约，也不是入门材料。
> 建议路径：先读 [`../guide/`](../guide/) 建立整体认知 → 再读权威契约 → 想确认「代码真的是这样吗」时再读本集。
> 本集独有的是 **`file:line` 锚点与反直觉的真实形态**。`docs/README.md` 规定「Architecture and persistence documents are authority」——**冲突时以权威契约为准**；而**代码是最终事实**。
> 完整阅读地图见 [`../guide/04-想深入读哪一篇.md`](../guide/04-想深入读哪一篇.md)。
>
> 取证锚点：分支 `capability-mcp` @ `1e567be`。其后全部提交均为文档改动（`git diff --stat 1e567be..HEAD -- '*.py' '*.sql'` 为空），**代码未变**，故本集结论仍然现行。


> **分析对象**：`D:\Project\HISIEM-SOC-Copilot`（Hatchling `src/` layout，包根 `src/hisiem_soc_copilot/`；Python 3.12+ · FastAPI · LangGraph + langgraph-checkpoint-postgres · SQLAlchemy 2 async + psycopg 3 + pgvector · Alembic · OpenTelemetry · MCP SDK）
> **分析方式**：按子系统边界拆分，对**实际源码**取证——关键结论均附 `file:line` 相对路径证据。
> **取证工具链**：本仓**无 `.codegraph/` 索引**，采用**源码直读 + AST 边界测试直读 + grep 统计**；每个 `file:line` 都对应真实代码，不凭空编造行号。**部分论断在项目 venv 中实跑验证**（见 §2）。
> **代码基线**：分支 `capability-mcp` @ `1e567be`
> **生成日期**：2026-09-22
> **文档集**：00–08 共 9 篇，6571 行，38 个 mermaid 图（全部经 `mermaid@11.10.1` 解析器校验）

---

## 〇、文档集导航总表

| # | 文档 | 主题 | Mermaid 块 | 行数 |
| --- | --- | --- | --- | --- |
| 00 | [00-分层架构与包边界总览.md](00-分层架构与包边界总览.md) | 十层包结构、5 个 AST 边界测试、组合根、配置入口、开关表 | 5 | 768 |
| 01 | [01-端到端关键数据流.md](01-端到端关键数据流.md) | 7 条数据流 + outbox 三分类失败 + 检查点安全 | 8 | 1261 |
| 02 | [02-领域模型与调查编排.md](02-领域模型与调查编排.md) | 聚合根状态机、图状态、8 个节点 | 3 | 551 |
| 03 | [03-工具执行策略与模型Provider.md](03-工具执行策略与模型Provider.md) | 注册表与准入、五段链、Provider 架构、MCP、LLM 适配器 | 4 | 901 |
| 04 | [04-证据归一化与响应闭环.md](04-证据归一化与响应闭环.md) | 证据链、响应提案聚合、策略、HISIEM 集成 | 4 | 782 |
| 05 | [05-应用层上游集成与API交付.md](05-应用层上游集成与API交付.md) | 命令/查询/处理器/端口、HISIEM 读适配、信任边界、HTTP | 4 | 591 |
| 06 | [06-知识库与威胁情报.md](06-知识库与威胁情报.md) | 混合检索四段管线、嵌入独立性、ATT&CK | 4 | 610 |
| 07 | [07-持久化可靠性与可观测性.md](07-持久化可靠性与可观测性.md) | 持久化四层、13 迁移、三层可靠性、隐私安全埋点 | 3 | 446 |
| 08 | [08-评估体系.md](08-评估体系.md) | 预言机防火墙、封存、执行桥、失败四分类 | 3 | 661 |
| — | **合计** | | **38** | **6571** |

### 拆篇依据（铁律 2）

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

## 〇·B、关键场景交互图（archify HTML）

**未产出。**

本环境**未安装 archify 工具**（`command -v archify` 返回未找到），因此既无法 `validate --quality showcase` 也无法 `deliver`。

按 铁律 4 与 §6 质量闸门的要求**如实标注**：**本文档集不含 archify 交互图**，全部 38 张图均为**经解析器校验的 mermaid 行内图**。不谎称已交付。

若后续需要交互图，建议的 4 个场景（按论证价值排序）：

| # | 建议类型 | 场景 |
| --- | --- | --- |
| 1 | `lifecycle` | **outbox 三分类失败 + 租约续期 + fencing**（01 篇，本项目最有价值的可靠性机制） |
| 2 | `workflow` | 调查图的 8 节点 + 条件边 + 越界预算降级（02 篇） |
| 3 | `workflow` | 「模型建议 → 策略约束 → 人工授权 → 持久命令」全链（04 篇） |
| 4 | `architecture` | **预言机防火墙**：evaluation / harness / 生产三层的穿墙点（08 篇） |

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

## 二、Mermaid 校验记录

```text
mermaid@11.10.1
块数=38 通过=38 失败=0
exit 0
```

**校验方式**（可复现）：

```bash
T="$TEMP/mermaid-check"; mkdir -p "$T" && cd "$T"
echo '{"name":"mc","private":true,"type":"module"}' > package.json
npm install --silent --no-audit --no-fund mermaid@11.10.1 jsdom
node ~/.claude/skills/code-level-architecture-docs/scripts/validate-mermaid.mjs \
     "D:/Project/HISIEM-SOC-Copilot/docs/architecture-analysis"
```

**分篇块数**：00×5、01×8、02×3、03×4、04×4、05×4、06×4、07×3、08×3 = **38**。

**校验过程中修正的语法问题（2 处）**：

1. **`flowchart` 节点标签含未转义的双引号** —— 01 篇写 `ToolResult` 相关标签时触发解析失败；改为全部标签双引号包裹 + 内部引号转义。
2. **08 篇误留了一个占位节点**（`HAL0["x"]`）—— 在核查阶段发现并移除（见 §3.1）。

### 实测执行过的验证（**本文档集唯一非静态的取证**）

除 mermaid 解析器校验外，**唯一实际执行过代码验证的一处**是 MCP provider：

```bash
$ ./.venv/Scripts/python.exe -m pytest \
    tests/unit/agent/test_mcp_provider.py \
    tests/integration/test_mcp_streamable_http.py -q
24 passed in 1.21s
```

**如实标注**：**除此之外，本文档集的所有论断均为静态取证**（源码直读 / AST 测试直读 / grep 计数），**未运行完整测试套件、未运行 mypy、未启动服务**。§3 的「核心实现结论表」中标 `[已执行]` 的行表示有实际运行证据。

---

## 三、核查记录（对实际源码的事实核对）

**核查方式**：每篇成文后**回查每条 `file:line` 是否对应真实代码**，并用可复现的 `wc -l` / `grep -c` / `find` 重新计数。

### 3.1 核查修正汇总

**共修正约 90 处**，其中**实质错误 9 处**（会误导读者）、其余为计数错误与锚点偏移。

**本集于 2026-09-22 经过一轮独立核证**：5 个核证 agent 逐条读代码取证，每条附 `file:line`；**关键结论另经本人独立复核**（不径信 agent 输出）。核证覆盖本集全部 96 条待核实项。

**9 处实质错误**（按严重度）：

| # | 初稿写的 | 代码真相 | 篇 |
| --- | --- | --- | --- |
| 1 | `DENYING_VERDICT` 判「非恶意」→ 拒绝 | **写反了**：判 `None`/`INCONCLUSIVE`；**`BENIGN` 反而进入 `REQUIRE_APPROVAL`** | 04 |
| 2 | 「进审批必须过五道闸门」列为**代码强制不变式** | 该方法**生产零调用**（死代码）；实际强制在 `handlers/response.py:115-157` 内联 | 04 |
| 3 | `EVENT_PLAN_CROSSES_YEAR_BOUNDARY` = 「构造跨年场景」 | 是**拒绝码**——跨年即拒（fail-closed） | 08 |
| 4 | downgrade guard =「ATT&CK 版本不能倒退」 | 是 **Alembic schema 降级守卫**；导入侧**明确支持**回到旧版本 | 06 |
| 5 | `continue` / `finalize_without_response` 是聚合方法 | 两者都**不是方法名**，且运行期**不可达** | 02 |
| 6 | 预算「任何一维超限都会终止」 | **6 个上限只有 4 个生效**（`max_tool_calls_per_step` 无强制点、`max_llm_tokens` 显式「暂不计量」） | 00 |
| 7 | 「放 `contracts/` 会产生循环」 | **依赖图不构成障碍**——`contracts/tools/types.py` 是叶、`contracts/` 无 AST 约束。**把风格选择说成了技术必然** | 03 |
| 8 | `next_action` 取值 `CALL_TOOL`/`ASSESS` | 实际常量是 **`EXECUTE_TOOL`/`CONVERGE`**（`CALL_TOOL` 在源码中不存在） | 02 |
| 9 | 「执行桥比预言机大得多」 | **方向相反**（9708 < 10764）；基于错误的 5504 行得出 | 08 |

**外加 5 处计数错误**：`infrastructure` 76→**68**、`llm` 11→**10**、`contracts` 5→**7**、`evaluation` 32→**28**、`evaluation_harness`+`evaluation` 38→**49**；以及 `UnitOfWork` 「11 个 repository」→**21 个属性**、Alembic 「14 迁移」→**13**、`application/ports` 「16 个 Protocol」→**36 个协议类**（16 是文件数）。

**错误的三类根因**：

1. **从间接证据推断**（4 处）—— 用 docstring 推断实现（normalizer 的哈希）、用代码注释推断取值（`next_action`）、用包名匹配推断守卫生效（`evaluation_harness` 的 `_` vs `.`）。
2. **置信于「统一口径」的假设**（3 处）—— 阶段编号假设只有一套、预算假设六维都生效、包放置假设被依赖图所迫。
3. **计数口径混用**（5 处）—— 文件数 vs 类数（16 vs 36）、端口数 vs UoW 属性数（11 vs 21）、含/不含 `__init__.py`（14 vs 13）。

**锚点偏移**（约 70 处）已逐一修正。**最集中的一处**：00 篇有 7 个 `config.py` 行号全错，同一根因（`sed` 窗口输出手工加基址，偏差 299 行）——**教训：行号必须用 `grep -n <pattern> <file>` 取绝对值，不要从窗口输出换算。**

### 3.2 与直觉/旧文档不同的真实形态（逐条）

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
| 12 | **`evaluation`（28 文件 / 10764 行）+ `evaluation_harness`（21 文件 / 9708 行）合计 49 文件 / 20472 行，而业务域 `domain` 是 29 文件——评估体系的代码量约为业务域的 1.7 倍** | 08 篇 §「为什么评估独立成篇」 | 「先建可信评估、再建功能」的直接体现 |
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

### 3.3 核心实现结论表

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
| **进审批五道闸门** | 动作已注册 / 有目标 / **有证据** / 策略已完成 / 策略非 DENY | `aggregate.py:89-103` |
| **混合检索** | 两路独立候选（各 20）+ **RRF `k=60`** + **每文档 ≤ 2 条**；**排序是纯函数** | `knowledge_retrieval.py:1-19`；`config.py:415-418` |
| **检索权威立场** | 「**摘要是数据，不是指令**；引用是待重验的参考，不是授权」 | `knowledge_retrieval.py:16-19` |
| **嵌入独立性** | 与对话 LLM **完全独立**；未配置时**明确告知**不静默降级；**无 ANN 索引** | `config.py:452,459-460`；`pyproject.toml:24-26` |
| **持久化四层** | ORM(9) → Mapper(6) → Repository(6, **3140 行**) → UnitOfWork | 07 篇 §1 |
| **事务边界** | 领域行与事件/outbox/回执**同事务**；outbox claim/mark **各自独立短事务** | `durable.py:1-8` |
| **检查点隔离** | 独立 `database_url` + 独立 `schema_name`（`langgraph_checkpoint`）；**不是真相** | `config.py:43,56`；`state.py:7-8` |
| **可观测隐私** | 敏感 HTTP 字段**在 server hook 处替换**（早于 processor/exporter） | `observability/bootstrap.py:22-31` |
| **工具遥测** | 是包在工具链外的**装饰器**（`agent` 禁导入 `infrastructure`，所以埋点不可能由 agent 自己做） | `observability/tools.py:1-12`；`test_import_boundaries.py:45` |
| **预言机防火墙** | 生产只收到 **4 字段 launch projection**；oracle / events / control events / sealed object **永不穿过** | `evaluation_harness/harness.py:6-9` |
| **评估隔离** | dispatcher 必须在 `Container.open()` **之前**关闭；harness 手动 `drain_once` | `harness.py:14-16` |
| **评估失败分类** | 4 类，**「无效失败」（provider 瞬时）不算产品失败** | `evaluation_harness/classification.py` |
| **来源分离** | `dataset_*` 来自清单记录的代码版本；`execution_*` 是当前 HEAD/worktree | `harness.py:18-20` |
| **内容寻址出现四次** | 语料身份 / ATT&CK 发布指纹 / 响应内容哈希 / （SIEM 侧）检测计划哈希 | 08 篇 §3.2 |
| **跨仓库契约** | Copilot → `/api/internal/soar/executions` + 服务令牌 + `X-Tenant-ID`；HISIEM 侧 fail-closed | 04 篇 §4.3；SIEM 03 篇 §4 |

---

## 四、关键开关与运行模式

### 4.1 影响行为的关键开关

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

### 4.2 Agent 预算（6 维上限，全部 `ge=1`）

| 上限 | 默认 | 证据 |
| --- | --- | --- |
| `agent_budget.max_steps` | `20` | `config.py:119` |
| `agent_budget.max_tool_calls` | `30` | `config.py:120` |
| `agent_budget.max_tool_calls_per_step` | `4` | `config.py:121` |
| `agent_budget.max_llm_calls` | `20` | `config.py:122` |
| `agent_budget.max_llm_tokens` | `20_000` | `config.py:125` |
| `agent_budget.max_duration_seconds` | `600` | `config.py:126` |

**任何一维超限都会终止** ——且 `max_llm_calls` 有保留槽不变量（03 篇 §2.3 论断 5）。

### 4.3 知识与检索参数

| 参数 | 默认 | 证据 |
| --- | --- | --- |
| `knowledge.max_document_bytes` | `2_000_000` | `config.py` |
| `knowledge.max_chunks_per_document` | — | `config.py:79` |
| `knowledge.max_chunk_chars` | `8_000` | `config.py:86` |
| `knowledge.lexical_candidate_limit` | `20` | `config.py:415` |
| `knowledge.vector_candidate_limit` | `20` | `config.py:416` |
| **`knowledge.rrf_k`** | **`60`** | `config.py:417` |
| `knowledge.max_hits_per_document` | `2` | `config.py:418` |

### 4.4 MCP 结果边界

| 参数 | 默认 | 证据 |
| --- | --- | --- |
| `result_bounds.timeout_seconds` | `30.0` | `providers.py:32` |
| `result_bounds.max_items` | `100` | `providers.py:33` |
| `result_bounds.max_serialized_bytes` | `256_000` | `providers.py:34` |
| `result_bounds.max_text_chars` | `32_000` | `providers.py:35` |
| `result_bounds.max_depth` | `8` | `providers.py:36` |

**per-capability 边界不得超全局**（`providers.py:43-49`，配置期抛异常）。

---

## 五、待核实清单

**96 条待核实项在 2026-09-22 的核证轮中处理**（94 条解答、2 条初稿有误已更正），事实已并入各篇正文。分布：00×8、01×10、02×10、03×12、04×10、05×12、06×12、07×10、08×12。

> **更正（2026-09-23）**：本行原先写「**全部**核证完毕」，这个措辞**过度声明**了。逐篇复核发现 **07 篇 §6 当时仍有 9 条未决**；本轮已就其中 2 条给出代码级解答（见本篇 §6 的第 2、9 条），**其余 7 条仍未决**，已在该节明确标注。核证记录应该准确到「哪些还没做」，否则它本身就是一处不可靠的断言。

### 5.1 最高优先级的 6 条（建议优先取证）

| # | 待核实 | 为什么重要 | 见 |
| --- | --- | --- | --- |
| 1 | **`httpx2` 未在 `pyproject.toml` 声明** | **间接依赖当直接依赖用**——`mcp` 若换 HTTP 客户端即 ImportError。建议提升为显式依赖 | 03 篇 §3.2 论断 6 |
| 2 | **`_running` 锁字典只增不减** | `dispatcher.py:142`；长期运行的内存增长是否有清理路径 | 01 篇 §9 |
| 3 | **`auditSafeConfig` 对应物的脱敏规则** | 工具审计进持久层前剔除哪些字段（漏一个则敏感值入库） | 03 篇 §8 |
| 4 | **`EXECUTABLE_ACTION_KEYS` 是 4 个动作中的哪几个** | 域允许清单（4 个）与**可执行子集**的差集未取证 | 04 篇 §7 |
| 5 | **`dedup hash` 与 `content hash` 的字段构成** | 决定证据幂等与不可变性 | 04 篇 §7 |
| 6 | **`observability/tools.py` 装饰器的装配点** | 装饰后 `ToolExecutor` 身份是否仍能被 `NativeToolProvider` 识别 | 07 篇 §6 |

### 5.2 分篇清单

**00 篇（8 条）**：`_build_mcp_providers` 的类型擦除原因；`knowledge` 包完整职责；`contracts/api` 内容（**实测为空命名空间**）；`messaging/` 与 `threat_intel/` 空包；`python-package-boundary.md` 各节与代码的一致性；`evaluation` 与 `evaluation_harness` 的完整分工；`agent_budget` 六个上限的强制点。

**01 篇（10 条）**：`_running` 字典清理；`claim_batch` 的 SQL；`renew_lease` 的 token 校验 SQL；destination 是否同表；`tool_audit` 的「by-key」具体键；`RecordEvidenceBatch` 的 dedup key；submit 幂等键取值；`attention_required` 的工作区可见性；知识检索 tenant 强制的签名；MCP 在五段链中的位置。

**02 篇（10 条）**：`InvestigationPhase` 取值集；`_bump()` 与 `lock_version` / `revision` 的分工；`continue` 的调用点；`_model_consult` 的异常分类；`_repeat_budget_for` 阈值；`assess` 的 183 行逻辑；`finalize_result` 与 `complete` 为何是两个节点；`GraphRuntime` 的 57 行内容；`_deadline_passed` 与预算对象的关系。

**03 篇（12 条）**：`providers.py` 是否更适合放 `contracts/`；`_FAILURE_CODES` 与 `ProviderFailureCode` 的一致性；`max_depth` 的执行点；`external_schema_fingerprint` 的规范化算法；`ProviderInvocationResult` 字段；`compute_fingerprint` 的校验点；`item_count` / `serialized_size` / `json_depth` 实现；`InputRequiredRoundsExceededError` 的捕获点；`executor.py` 492 行的内部分支；`scripted.py` 的确定性策略；`llm/schemas.py` 的作用；`contracts/llm/errors.py` 完整层次。

**04 篇（10 条）**：`EXECUTABLE_ACTION_KEYS` 成员；`ResponseSubmissionStatus` 完整取值；`validate_for_approval` 的调用点；`action_allowed` 延迟导入原因；两个哈希的字段构成；`EvidenceSourceType` 取值；submit 幂等键；`soar/adapter.py` 端点与重试；`SoarSettings.tenant_header` 缺失是否有意；`ThreatIntelPort` 接入计划。

**05 篇（12 条）**：4 个多行装饰器端点的具体路径；`/lookup` 的查询键与去重语义；`attack_import.py` 与 `knowledge.py` 职责；`durable_support.py` 作用；`queries/workspace.py` 与 `services/workspace_service.py` 分工；`api/schemas/workspace.py` 字段；`application/errors.py` 层次；`header_provider.py` 的信任判定；`api/errors.py` 异常映射表；`ClockPort` 消费点。

**06 篇（12 条）**：`cli.py` 命令集；`knowledge/evaluation.py` 与 `evaluation/knowledge/` 分工；chunker 算法；`max_chunks_per_document` 值；三个 chunk token 参数；`ChunkerProfile` 构成；`_validate_chunker_profile`；`corpus_identity.py` 算法；`metrics.py` 指标集；`mitre_stix.py` 解析范围；`attack_import.py` 完整流程；`ThreatIntelPort` 接入计划。

**07 篇（10 条）**：装饰器装配点；`_REDACTED_HTTP_ATTRIBUTES` 完整清单；`metrics.py` 指标集；`checkpoint/postgres.py` 的 schema 管理；`unit_of_work.py` 的端口聚合方式；`repositories/knowledge.py` 结构；`orm/knowledge.py` 表清单；标准埋点完整清单；迁移的 downgrade 路径；`log_correlation` 关联字段。

**08 篇（12 条）**：阶段编号体系（E1-B / E1-C / E3 / E4 / E5）；`ledger.py` 记录内容；`cross_plane/gates.py` 判定项；`oracle.py` 预期表达；`injector.py` 注入机制；`materializer.py` 物化流程；`quality_harness.py` 判据；`score.py` 评分算法；`time_plan.py` 时间线构造；`metrics.py` 指标集；评估环境变量清单；`verifier.py` 验证规则。

---

## 六、阅读顺序建议

### 路径 A — 理解全貌（约 50 分钟）

1. **本 README** —— 建立索引与可信度判断
2. **`00`** —— 十层包结构、5 个 AST 边界测试、开关表（**TL;DR 是整集电梯陈述**）
3. **`01` §0 主链总览** —— 一张图看完全链路
4. **`01` §2 outbox 三分类失败** —— 本文档集里最有价值的一节
5. 按兴趣深挖 `02`–`08`

### 路径 B — 面试 / 技术评审准备（约 2 小时）

1. **`00` §2 包边界如何强制** —— 讲清「边界是测试断言，不是文档约定」
2. **`04` §3 响应策略** —— 讲清「Agent 建议，不能授权」的**结构性**保证（`PolicyDecision` 只有 2 值）
3. **`01` §2 outbox 三分类失败 + 耗尽钩子** —— 讲清「传输预算 ≠ 业务含义」
4. **`08` §1 预言机防火墙** —— 讲清「评估预期永不进运行时」
5. **README §3.2 反直觉真实形态** —— 17 条最容易在评审中被追问的点

### 路径 C — 改代码前必读（按改动类型）

| 要改什么 | 先读 |
| --- | --- |
| 加层 / 改依赖方向 | `00` §2 + `tests/architecture/test_import_boundaries.py` 的 `BOUNDARIES` 字典 |
| 加工具 | `03` §1（注册表 + 准入 + **`is_model_selectable` 合成属性**） |
| 改图 | `02` §3 + `01` §3（**检查点安全**：工具 + 证据必须同节点） |
| 加响应动作 | `04` §2（**五道闸门** + 内容哈希三字段） |
| 改 outbox / 可靠性 | `01` §2 + `07` §2（**事务边界分工**） |
| 加表 / 迁移 | `07` §1（四层） |
| 加 HTTP 端点 | `05` §4（**`api` 四条禁导入**） |
| 改检索 | `06` §1（**排序必须是纯函数**） |
| 改评估 | `08` §1（**预言机防火墙**）+ 两条边界测试 |

### 路径 D — 只关心某一条链路

| 链路 | 读 |
| --- | --- |
| HTTP 请求 → 调查创建 → 图推进 | `01` §1 + `05` §4 + `02` §3 |
| 工具调用 → 证据落库 | `01` §4–§5 + `03` §2 |
| 结论 → 响应提案 → 人工审批 → HISIEM 执行 | `01` §6 + `04` 全篇 |
| 知识检索 | `06` §1 |
| 可靠性与恢复 | `01` §2 + `07` §2 |
| 评估运行 | `08` §2 |

---

## 修订记录

| 版本 | 日期 | 变更 | 作者 |
| --- | --- | --- | --- |
| 1.0 | 2026-09-22 | 首版。9 篇，6571 行，38 个 mermaid 块全部经 `mermaid@11.10.1` 校验通过。核查修正 **13 处**（其中 7 处为 `config.py` 行号锚点，同一根因），记录 **17 条**反直觉真实形态，**96 条**待核实。MCP 测试实跑 **24 passed**（唯一非静态取证）。**archify 交互图未产出**（本环境无该工具，如实标注）。 | code-level-architecture-docs skill |

---

## 附录：核证记录（2026-09-22）

> 2026-09-22 的核证轮在各篇正文里留下了自我更正的痕迹（`⚠️` 块、含「初稿」「更正」的叙述）。为了让正文只陈述事实，这些痕迹已从正文搬出、**逐字保留**在下方；正文对应位置改以平实语气陈述同一事实。

### 原 00-分层架构与包边界总览.md

*（§1.2 规模实测 表下块引用）*

>
> **⚠️ 本表的 5 个计数已按 2026-09-22 核证更正**：初稿的 `infrastructure 76` / `llm 11` / `contracts 1+4` / `evaluation 32` / `38 文件` 全部有误。其中 `infrastructure 76` 与同一行给出的子包明细相加（= 68）**自相矛盾**。

*（§4.1 论断 6 表头括号标记）*

**六个配置维度，但只有 4 个真正被强制**（2026-09-22 实证更正）：

*（§4.1 论断 6 表下块引用）*

> **⚠️ 初稿写「任何一维超限都会终止」是错的。** 真实强制维度是 **4 个**，另外 2 个是**惰性配置**（声明了但当前不生效）。**这本身值得记录**：它说明「配置项存在」不等于「约束生效」。

*（待核实 节末块引用）*

> **本节已按 2026-09-22 的核证结果收尾**：6 条已解答、**2 条初稿有误**（`python-package-boundary.md` 只到 §39，§63-69/§88 属仓库外的 brief；`agent_budget` 六上限只有 4 个被强制）——两处均已在上文更正。**0 条仍未定。**
>
> **核证方式**：8 个独立核证 agent 逐条读代码取证，每条附 `file:line`；关键结论另经本人独立复核（不径信 agent 输出）。

### 原 01-端到端关键数据流.md

*（待核实 节末块引用）*

> **本节已按 2026-09-22 的核证结果收尾**：10 条**全部已解答**（含 `_running` 锁字典**确无清理路径**、`claim_batch` **不用 `FOR UPDATE SKIP LOCKED` 而是乐观 CAS** 且 `commit()` 在批循环之外、`renew_lease` 的三条件 fencing、`destination` 与 outbox **同一张表**、工具审计键 `tool:<name>:<fingerprint>`、submit 幂等键 `response:<tenant>:<proposal>`、`ATTENTION_REQUIRED` 的三处工作区可见性、tenant 是**强制关键字参数**且有 AST 测试、MCP 落在五段链第 5 段内部）。
>
> **核证方式**：8 个独立核证 agent 逐条读代码取证，每条附 `file:line`；关键结论另经本人独立复核（不径信 agent 输出）。

### 原 02-领域模型与调查编排.md

*（§1 论断 2 状态迁移表行内交叉引用）*

| `RUNNING` | `continue` | `RUNNING`（自环，**运行期不可达——见下方更正**） |
| `RUNNING` | `finalize_without_response` | `COMPLETED`（**迁移串，不是方法名——见下方更正**） |

*（§1 论断 2 表下块引用）*

> **⚠️ 实证更正：这两条迁移在运行期都不可达，且两个名字都不是方法。**
>
> **聚合的真实方法是**（`aggregate.py`）：`start`(`:109`) / `update_phase`(`:122`) / `complete_without_response`(`:131`) / `link_response_proposal`(`:136`) / `cancel`(`:165`) / `fail`(`:173`)。
>
> **`continue` 全仓只出现一次**——就是 `_TRANSITIONS` 里的这一行声明（`aggregate.py:32`）。聚合**没有 `continue()` 方法**，`_transition()` 只被 `complete_without_response` / `cancel` / `fail` 调用。
>
> **应用侧推进迭代走的是 `inv.update_phase(command.phase)`**（`application/handlers/workflow.py:129`），**既不查 `_TRANSITIONS` 也不发 terminated 事件**。
>
> **`finalize_without_response` 只是迁移表里的字符串动作名**，真实方法叫 `complete_without_response`。
>
> **所以 `_TRANSITIONS` 的实际作用范围比它看起来窄**——它是 `complete_without_response` / `cancel` / `fail` 三个方法的守卫表；`update_phase` 走的是另一条不受它约束的路径。**这本身是一条值得注意的设计事实**（阶段推进与生命周期迁移是两套东西）。

*（§1 论断 4 代码块首行锚点（原锚点有误））*

```text
# aggregate.py:66-77
```

*（§1.3 图状态代码块行内注释（原为交叉引用））*

next_action: str | None  # 代码常量实际取值见下方更正

*（§1.3 表下块引用）*

> **⚠️ 实证更正：`next_action` 的实际取值与代码注释不符。**
>
> `state.py:74` 的注释写 `# "CALL_TOOL" | "ASSESS"`，但**代码里的常量是**（`agent/graph/nodes.py:85-86`）：
>
> ```python
> EXECUTE_TOOL = "execute_and_ingest"
> CONVERGE = "assess"
> ```
>
> **写入点是 `nodes.py:475/502/514/525`，`CALL_TOOL` 在源码中根本不存在**（只有那一行陈旧注释）。
>
> **`builder.py:34-44` 的两个路由函数读的正是这两个字符串**——所以注释是**过期的**，代码是权威的。**初稿照抄了注释，因而继承了这处代码内部的注释/实现落差。**

*（§6 待核实 引导句）*

**本节已按 2026-09-22 的核证结果重写**：初稿的 10 条**全部已解答**（事实已并入上文），无遗留项。

*（§6 待核实 解答表）*

| 初稿待核实项 | 解答 |
| --- | --- |
| `InvestigationPhase` 完整取值 | **5 个**：`HYDRATING` / `PLANNING` / `INVESTIGATING` / `VERIFYING` / `FINALIZING`（`enums.py:43-50`）。同文件另有 9 个 StrEnum |
| `_bump()` 与 `lock_version` / `revision` 的分工 | `_bump()` **只递增 `revision`**；`lock_version` 由 repository 独占管理，域方法永不自增 |
| `continue` 动作的调用点 | **无调用点**——是 `_TRANSITIONS` 里的死声明；迭代推进走 `update_phase` |
| `_model_consult` 的异常分类 | **两类**：`ModelConfigurationError` **重抛**（部署错误必须暴露）；其余 `ModelProviderError` 子类（Unavailable / RateLimited / Timeout / Refusal / OutputValidation）**一律降级为 `None`**（`nodes.py:112-127`） |
| `_repeat_budget_for` 的重试上限 | `_MAX_SAME_FAILING_CALL_RETRIES = 2`（`nodes.py:139`）；指纹不同则重置为 `(0, None)` |
| `assess` 的 183 行逻辑 | 先 `change_phase(VERIFYING)` → `can_call_llm()` 才消费槽并调模型 → 映射规则确定：候选未命中 → `UNRESOLVED`；`SUPPORTED`/`CONTRADICTED` 必须存在同向 relation **且**引用的证据在本调查可解析（知识证据不参与 grounding）否则降级；findings 必须引用本调查真实 evidence id 才落库 |
| `finalize_result` 与 `complete` 为何是两个节点 | **两个命令 + 两次独立事务 + 硬序**：`finalize_result` 落不可变 `InvestigationResult` 并写回 `result_id`，**要求聚合仍为 RUNNING**；`complete` 只做 `complete_without_response()`。即「先持久化结果（要求 RUNNING），再由另一事务关闭生命周期」 |
| `GraphRuntime` 的 57 行内容 | `UnitOfWorkFactory` Protocol + `GraphRuntime`（**8 个 keyword-only 依赖**：uow_factory / workflow_handler / model / executor / normalizer / registry / hisiem / tenant_id）+ `new_unit_of_work()` |
| `_deadline_passed` 与 `RuntimeBudget` 的关系 | **复用**——`_deadline_passed` 就是 `RuntimeBudget.from_state(state).deadline_exceeded`；同一谓词还被 `can_call_llm` / `can_consult_decide` 复用 |
| `domain/response/` 的 9 个文件 | 确认 9 个（属 04 篇范围） |

*（§6 一处锚点更正）*

### 一处锚点更正

初稿 §1 论断 4 的代码块标为 `aggregate.py:66-77`，实际 `def start` 位于 **`aggregate.py:109-120`**；`HYDRATING` 的赋值点是 **`:113`**（初稿 §6 待核实 1 曾误引 `:70`，那是 `cancelled_at`）。

### 原 03-工具执行策略与模型Provider.md

*（§7 边界表下块引用）*

> **⚠️ 初稿给的理由是错的。** 初稿写「放到 `contracts/` 会产生循环」——**实证不成立**：
>
> - `contracts/tools/types.py` 是**叶模块**，只 import `dataclasses` + `typing`（`:14-15`）；
> - `contracts/` 全包**零处** import `agent`；
> - `tests/architecture/test_import_boundaries.py` 的 `BOUNDARIES` 字典里**根本没有 `contracts` 条目**——`contracts` 内部**没有 AST 约束**。
>
> **所以「循环」不是事实障碍。** 是否把 `providers.py` 迁进 `contracts/` 纯属**包放置取舍**——**初稿把一个风格选择说成了技术必然**。

*（待核实 节末块引用）*

> **本节已按 2026-09-22 的核证结果收尾**：12 条**全部已解答**（含 `_FAILURE_CODES` 与 `ProviderFailureCode` 逐值一致、`external_schema_fingerprint` 用 `sort_keys` 紧凑 JSON 取 SHA-256、`max_depth` 与 `max_items` 同点执行、`InputRequiredRoundsExceededError` 只在 `invoke()` 捕获、`executor.execute` 的 8 个分支、`scripted` 是**有状态调用游标**、`schemas.py` 是严格 wire 边界、`contracts/llm/errors.py` 是 7 类单继承链）。
>
> **核证方式**：8 个独立核证 agent 逐条读代码取证，每条附 `file:line`；关键结论另经本人独立复核（不径信 agent 输出）。

### 原 04-证据归一化与响应闭环.md

*（§1 论断 3 标题行）*

**论断 3：两个哈希不是 normalizer 算的——初稿在此处误信了 docstring。**

*（§1 论断 3 表下块引用）*

> **⚠️ 实证更正**：`EvidenceNormalizer` **本身不计算**这两个哈希。它只算 `query_fingerprint`（`normalizer.py:291-297`）。**两个哈希的定义在 `domain/investigation/content.py`，调用点在 `application/handlers/workflow.py`**（dedup 在 `:266-273`，content 在 `:706`）。
>
> **normalizer 的 docstring 说它「computes the dedup/content hashes」，与实现不符**——初稿忠实引用了 docstring，因而继承了这处**代码注释与实现之间的落差**。
>
> **dedup key 的输入是四元组** `{provider, operation, raw_reference, resource_address}`（`content.py:62-68`），经 `sort_keys=True, separators=(",",":")` 的规范 JSON 取 SHA-256；**采集时间刻意不入 key**（否则同一观测每次采集都会产生新证据）。知识检索有特例：`knowledge` + `retrieve_security_guidance` 时用 `raw_reference["citation_identity"]` 替换整个 raw_reference，并剔除 `retrieval_mode` / `retrieval_profile_id` / `retrieved_at`（`content.py:45-61`）。

*（§2.1 论断 8 表下块引用）*

> **⚠️ 但必须说明一处实证结果**：全仓 grep 该方法的调用点，**只有定义（`aggregate.py:89`）与一条单元测试**（`tests/unit/domain/test_response_proposal.py:133`），**生产路径从不调用它**。
>
> **生产路径的等价前置条件由 handler 内联强制**（`application/handlers/response.py:115-157`）：COMPLETED 状态、result 存在、证据服务端解析、目标服务端派生、动作可执行、策略已算。
>
> **所以本篇 §5 不变式表第 13 条已在下方标注为「领域方法存在但生产未调用」**——把「已定义但未调用」的校验列为「代码强制的不变式」是**高估**。

*（§2.1 论断 9 前结论句）*

**所以 V1 的响应面比我初稿写的窄得多：4 个域动作里只有 1 个真能执行。**

*（§3 论断 3 表下块引用）*

> **⚠️ 这一条初稿写反了，已按代码更正。** 初稿写成「调查判定『不是威胁』时被拒」，与代码**正好相反**：**`BENIGN`（明确判为良性）反而进入 `REQUIRE_APPROVAL`**。
>
> **代码的真实意图是「低置信度不允许绕过人工门」**（`policy.py:75` 的行内注释原文：*「Low-confidence/unknown verdicts never bypass the human gate in V1」*）——**拒绝的不是「清白」，而是「不确定」**。

*（待核实 节末块引用）*

> **本节已按 2026-09-22 的核证结果收尾**：10 条**全部已解答**（含可执行动作**只有 1 个**、`ResponseSubmissionStatus` 5 值与 `ResponseExecutionStatus` 是**两套独立生命周期**、`validate_for_approval` **生产零调用**、两个哈希定义在 `domain/investigation/content.py` 且 normalizer 不算它们、SOAR 适配器**零重试**（重试在上层 dispatcher）、`HisiemSettings.tenant_header` **全仓无消费点**）。
>
> **核证方式**：8 个独立核证 agent 逐条读代码取证，每条附 `file:line`；关键结论另经本人独立复核（不径信 agent 输出）。

### 原 05-应用层上游集成与API交付.md

*（篇首计数更正块引用）*

> **一处计数更正**：初稿写「16 个 Protocol」——16 是**文件数**（15 模块 + `__init__.py`），不是协议数；**实际定义的协议是 36 个**。

*（待核实 节末块引用）*

> **本节已按 2026-09-22 的核证结果收尾**：12 条**全部已解答**（含 8 个端点的完整路径表、`/lookup` 的 3 个必填 Query 参数 + DB 局部唯一索引保证至多一条 active、`durable_support.py` 提供 exactly-once 命令、`queries/workspace.py` 是纯数据层而 `services/workspace_service.py` 是执行层、`api/errors.py` 5 个 handler + 完整映射表、`HeaderTrustedContextProvider` **无签名或令牌校验**（dev/test only）、`ClockPort` 只有 3 个生产消费点）。
>
> **核证方式**：8 个独立核证 agent 逐条读代码取证，每条附 `file:line`；关键结论另经本人独立复核（不径信 agent 输出）。

### 原 06-知识库与威胁情报.md

*（§3.1 论断 3 行）*

**「downgrade guard」初稿误读为业务规则，已按代码更正。**

*（§3.1 论断 3 表下块引用）*

> **⚠️ 与初稿相反的实证**：**导入侧明确支持回到旧版本**——`attack_import.py:326` 与 `application/ports/knowledge.py:433` 都写「re-activating an older release restores its own projection」。全仓 `application/` 与 `domain/knowledge/` 下 **grep 不到任何 release 版本先后比较逻辑**。
>
> 所以「ATT&CK 发布不能降级」**不是**业务规则；守卫保护的是**数据库 schema 的可逆性**，不是**数据的新旧顺序**。

*（§6 待核实 引导句）*

**本节已按 2026-09-22 的核证结果重写**：初稿的 12 条中 **11 条已解答**（事实已并入上文），仅 1 条仍未定。

*（§6 已解答 小节标题）*

### 已解答（初稿的待核实项，事实已并入正文）

*（§6 已解答 表）*

| 初稿待核实项 | 解答 |
| --- | --- |
| `knowledge/cli.py` 的子命令集 | **7 个**：`doctor` / `ingest-file` / `import-attack` / `search` / `resolve-citation` / `retire` / `evaluate`（`cli.py:96-174`）。`ingest-file` 的 `--source-kind` 排除 `MITRE_ATTACK`；另有全局 `--embedding-provider` |
| `knowledge/evaluation.py` 与 `evaluation/knowledge/` 的分工 | **纯测量 vs 接线**：`evaluation/knowledge/` 是无环境依赖的密封包（只经注入的 async callable 拿检索行为）；`knowledge/evaluation.py` 是唯一适配器，把语料真落库、经 `KnowledgeRetrievalService.retrieve` 检索、经 `KnowledgeCitationResolver.resolve` 重验引用 |
| chunker 分块算法 | **结构驱动、逐行走**：ATX 标题只更新 heading 栈（不作内容输出）、标题链以 `" > "` 随块携带、围栏块整块保留、列表 run 整块保留、其余按段落切；块按 `target_tokens` 打包；**只有超 `max_tokens` 的围栏块才按行切**。token 是 `estimate_tokens` 近似（`_CHARS_PER_TOKEN = 6`）。越界是**拒绝**（抛 `KnowledgeBoundsExceededError`）不是截断 |
| `max_chunks_per_document` / 三个 chunk token 参数 | `512` / `target=600` / `max=800` / `overlap=80`（`domain/knowledge/value_objects.py:141-146`）。关系由 `ChunkerProfile.__post_init__` 强制：`max_tokens >= target_tokens`、`max_tokens <= 800`、`overlap_tokens < target_tokens` |
| `ChunkerProfile` 构成 | frozen dataclass，四个字段：`chunker_version`（`"structure-aware-v1"`）、`target_tokens`、`max_tokens`、`overlap_tokens`（`value_objects.py:156`） |
| `_validate_chunker_profile` 校验 | `@model_validator(mode="after")`：先构造一次 `ChunkerProfile` 复用 domain 规则，再**额外**查 `chunk_max_tokens > 800`（与 domain 重复，冗余但无害） |
| `corpus_identity` 与 release fingerprint 是否同一手法 | **是**——都是「SHA-256 over canonical JSON」，都把集合 `sorted(set(...))` 后 `json.dumps(sort_keys=True, separators=(",",":"))`，各带 schema tag 混入哈希。**但刻意不共享实现**：`attack_release_fingerprint.py:27-30` 明说共享会让一个函数的含义取决于调用方 |
| `metrics.py` 指标集 | **三种都有且更全**：`recall_at_k`、`reciprocal_rank`（MRR）、`ndcg_at_k`（binary-gain nDCG）；另有 `citation_resolution_rate`、`cross_tenant_leakage_count`、`forbidden_retrieval_count` |
| `mitre_stix.py` 解析范围 | **只有 `attack-pattern`**（`mitre_stix.py:278` 跳过其它 type）；tactic 只读 technique 自身的 `kill_chain_phases`，platforms 读 `x_mitre_platforms`；限定 `enterprise-attack` 域；revoked / deprecated / 重复 id 进 `skipped_ids` |
| `attack_import.py` 导入流程 | **四步**：(1) 前置拒绝（名非空、framework 匹配、≤64 MiB，解析失败则**一个字节都不写**）；(2) `_persist_release`（单事务内 pin 指纹或校验既有 pin）；(3) `_stage_documents`（逐 technique 走 `KnowledgeIngestionHandler.ingest`，projection 行**一个短事务**幂等落库）；(4) 若申请权威则 `_cutover`（cutover 锁 → 校验绑定与可检索性 → flip 权威 → 逐文档移动指针） |
| `ThreatIntelPort` 是否有实现 | **无**。只有 Protocol 定义（`application/ports/threat_intel.py:8`），`infrastructure/threat_intel/` 是空命名空间；`threat_intel.lookup_ip` 只在 `FUTURE_CATALOG_TOOLS`（`registry.py:45`）且被测试断言**不在**模型可选面 |

*（§6 一处锚点更正）*

### 一处锚点更正

初稿 §6 曾引 `config.py:79/91/98/105/125/143` 指向 chunker 配置——**这些行号全部落在 `LLMSettings` / `AgentBudgetSettings` / `MCPResultBoundsSettings` 区域**。真实位置是 `config.py:378 / 390 / 397 / 404 / 424 / 442`。（正文 §1 与 §2 里的 `config.py:415-418`、`config.py:452` 等锚点是准确的。）

### 原 07-持久化可靠性与可观测性.md

*（§1 论断 3 块引用（计数更正））*

> **计数更正**：`alembic/versions/` 下 14 个 `.py` 里**只有 13 个是迁移**，第 14 个是 `__init__.py`。**13 个迁移全部有真实的 `downgrade()` 实现**（无一是 `pass`），其中 3 个是**带守卫的降级**（先跑 `_refuse_unsafe_downgrade()`，不满足则抛 `P3ADowngradeUnsafeError`）。

### 原 08-评估体系.md

*（篇首计数更正块引用）*

> **⚠️ 本节开篇的计数已按 2026-09-22 核证更正**：初稿写 `evaluation` 32 文件 / 5504 行、合计 53 文件 / 15212 行——实测 **28 文件 / 10764 行**、合计 **49 / 20472**。（同篇 §2 的逐文件行数表是**准确的**。）

*（§3.1 论断 4 标题行（原方向写反））*

**论断 4：`evaluation_harness` 有 21 个文件、9708 行——比 `evaluation` 大得多。**

*（§3.1 论断 4 结论句）*

**「执行桥」比「预言机」略小**（9708 < 10764）——**初稿基于错误的 5504 行得出「执行桥更大」的结论，方向恰好相反。**

*（§3.1 论断 5 表下块引用）*

> **⚠️ 初稿说「与 E1-B / E1-C 是同一套编号体系」——实证不成立**：项目里并存**三套**编号：①宏观阶段 A–E（Knowledge closure / Observability / Capability-MCP / Analyst experience / Cross-plane acceptance）；②Stage E 内部子阶段 **E0–E7**（E1 = XP-01 contract & gate model；E3 = Authority & reliability；E4 = Observability；E5 = Workspace；E6 = 聚合；E7 = 封存）；③**GP-01 评估轨道** `E1-B.3` / `E1-B.4` / `E1-C0`…`E1-C6`。
>
> **`E1-B` / `E1-C` 属第三套（GP-01 轨道）**，其提交（`e5c9119` 2026-09-06、`06b1435` 2026-09-09）比 Stage E/E1 的基线 `d4cfc8e`（2026-09-17）**早 11 天**；Stage E 自己的 E0 审计还把 `GP-01 (E1-C3)` 列为**既有依赖**。

*（§3.1 细节 2 标题行）*

**2. `EVENT_PLAN_CROSSES_YEAR_BOUNDARY` 初稿理解写反了——它是**拒绝码**。**

*（待核实 节末块引用）*

> **本节已按 2026-09-22 的核证结果收尾**：9 条已解答、**3 条初稿有误**（阶段编号是**三套并存**不是一套；`score.py` **没有评分算法**只是确定性布尔门；`EVENT_PLAN_CROSSES_YEAR_BOUNDARY` 是**拒绝码**）——三处均已在上文更正。**0 条仍未定。**
>
> **核证方式**：8 个独立核证 agent 逐条读代码取证，每条附 `file:line`；关键结论另经本人独立复核（不径信 agent 输出）。
