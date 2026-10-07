# 文档地图

本目录存放 HISIEM SOC Copilot 的**权威技术文档**。
**你永远不该需要猜「先打开哪一篇」。** 按你的目标在下面找。

> **第一次来？** 先读[根 README](../README.md)，再读 [`guide/`](guide/)——四篇「先讲项目、再讲实现」的导览。想要视觉模型就回到
> [`architecture-overview.md`](status/architecture-overview.md)（八张图）；想深挖某一个具体领域时回到本页。

---

## 目录地图

| 目录 | 装什么 | 什么时候读 |
| --- | --- | --- |
| `README.md` | 本图 | 第一次来 |
| [`status/`](status/) | **现在什么是真的**：产品定位、架构总览与两张规范图 | 你要看当前全貌 |
| [`contracts/`](contracts/) | **什么是必须成立的**：领域模型、持久化 schema、包边界、工具/MCP 契约、provider 契约、上游集成边界、工作区投影、可观测性、V1 范围 | 你要改行为或改接口 |
| [`operations/`](operations/) | 怎么把本地运行环境起起来 | 你要把它跑起来 |
| [`knowledge/`](knowledge/) | 知识子系统：领域、检索契约、安全边界、运维步骤 | 你在做知识相关的工作 |
| [`evaluation/`](evaluation/) | 四项评估职责：closure、知识基线、数据集物化、清单封存 | 你在做评估相关的工作 |
| [`guide/`](guide/) | 先讲项目的入门材料 | 你刚接触这个项目 |
| [`evidence/`](evidence/) | **代码级取证**：每条论断都带 `file:line` | 你想核实「代码真的是这样吗」 |
| [`interview/`](interview/) | 面试复习材料 | — |

## 从这里开始

| 文档 | 读它是为了 | 体量 |
|---|---|---|
| [`guide/`](guide/) | **第一次来就从这里开始。** 四篇导览：这层决策层解决什么问题、一次调查的完整旅程、以及为什么 agent 不能自己授权 | 4 篇 |
| [`architecture-overview.md`](status/architecture-overview.md) | 视觉模型——调查/授权链、工具与证据路径、MCP 准入、知识路径、持久执行、真相边界、评估、跨仓边界 | 8 张图 |
| [`architecture-diagrams.md`](status/architecture-diagrams.md) | 两张规范全图——按平面组织的架构图，以及端到端调查数据流 | 2 张图 |
| [`product-positioning.md`](status/product-positioning.md) | 产品是什么、不是什么；目标用户与边界 | 11 KB |
| [`product-scope.md`](contracts/product-scope.md) | 用户旅程、范围与非目标、完成的定义 | 17 KB |
| [`interview/INTERVIEW_GUIDE.md`](interview/INTERVIEW_GUIDE.md) | 复习材料：从 30 秒自我介绍到深入追问 | 大 |

---

## 架构

| 文档 | 读它是为了 | 体量 |
|---|---|---|
| [`python-package-boundary.md`](contracts/python-package-boundary.md) | 分层模型——域的纯净性、application 的端口/UoW、agent 编排、纯传输的 API——以及依赖方向规则 | 22 KB |
| [`application-commands-domain-events-langgraph-state.md`](contracts/application-commands-domain-events-langgraph-state.md) | 命令、领域事件，以及 LangGraph 状态与领域状态的关系（以及它*不是*领域状态） | 35 KB |
| [`model-provider-contract.md`](contracts/model-provider-contract.md) | provider 契约：有界调用、结构化输出、类型化失败、fail-closed 行为 | 14 KB |
| [`investigation-workspace.md`](contracts/investigation-workspace.md) | 分析员工作区的投影契约 | 9 KB |
| [`evidence/architecture-analysis/README.md`](evidence/architecture-analysis/README.md) | **代码级证据层**（9 篇）：按子系统的 `file:line` 取证、反直觉的真实形态、以及边界**实际**的样子 | — |

> 证据层**不是**权威。它回答「代码真的是这样吗」——上面的契约回答「代码必须是什么样」。
> 两者冲突时，**以契约为准**，而**代码是最终事实**；此时需要更新的正是那份证据文档。

---

## 领域与持久化

| 文档 | 读它是为了 | 体量 |
|---|---|---|
| [`domain-model.md`](contracts/domain-model.md) | 聚合、实体、值对象、不变式——纯净的内核 | 28 KB |
| [`persistence-schema.md`](contracts/persistence-schema.md) | 表、约束、索引，以及 ORM↔领域的映射；乐观锁 | 38 KB |

---

## 知识

| 文档 | 读它是为了 |
|---|---|
| [`knowledge/domain.md`](knowledge/domain.md) | 知识领域：文档、版本、不可变内容块 |
| [`knowledge/retrieval-contract.md`](knowledge/retrieval-contract.md) | 类型化检索面：查询、命中、排序、强制的租户作用域 |
| [`knowledge/security-boundary.md`](knowledge/security-boundary.md) | 知识的安全边界与模型可触达面 |
| [`evaluation/knowledge-evaluation-contract.md`](evaluation/knowledge-evaluation-contract.md) | `KB-GOLDEN-V1` 基线：语料、模式、评分、前置条件 |
| [`knowledge/operations.md`](knowledge/operations.md) | 运维步骤：供给、pgvector、摄取、检索、评估 |

> **先读那处诚实的缺口：** 本仓**没有配置任何嵌入 provider**，所以混合闸门的语义那一半**从未被跑过**。混合评估证明的是**接线**——融合、排序、并列打破、引用解析与评分——**不是检索质量**。产物一律标注 `PLUMBING_ONLY`。
> 见 [`evaluation/knowledge-evaluation-contract.md`](evaluation/knowledge-evaluation-contract.md)。

---

## 能力与 MCP

| 文档 | 读它是为了 |
|---|---|
| [`investigation-tool-contract.md`](contracts/investigation-tool-contract.md) | 工具契约：模型可选面、参数与结果契约、类型化失败 |
| [`hisiem-integration-contract.md`](contracts/hisiem-integration-contract.md) | 通往 HISIEM 的边界：读什么、怎么读、在什么租户/授权规则下读 |

**模型可选面恰好是四个只读工具**，外加一个模型永远不能调用的系统控制工具。未知 server、未准入的动态工具、写能力与 schema 漂移一律 fail closed。确切的集合与规则见
[`investigation-tool-contract.md`](contracts/investigation-tool-contract.md) §3–§4——**契约是权威**；根 README 只是概述它。

---

## 可观测性

| 文档 | 读它是为了 |
|---|---|
| [`observability.md`](contracts/observability.md) | span、跨持久边界的上下文传播、metric 标签安全性、遥测数据策略 |

**真相边界：** 没有任何业务路径读取 span、trace id 或 collector 状态。**运行中途停掉 collector，持久化的业务结果完全相同。**

---

## 评估

| 文档 | 读它是为了 |
|---|---|
| [`evaluation/gp-01-dataset-materializer.md`](evaluation/gp-01-dataset-materializer.md) | GP-01 场景如何变成真实的 HISIEM 资源 |
| [`evaluation/gp-01-manifest-sealer.md`](evaluation/gp-01-manifest-sealer.md) | 清单封存——钉住的、可复现的评估输入 |
| [`evaluation/evaluation-closure-contract.md`](evaluation/evaluation-closure-contract.md) | closure：确定性评分器、有界可重复性、套件聚合 |
| [`evaluation/knowledge-evaluation-contract.md`](evaluation/knowledge-evaluation-contract.md) | `KB-GOLDEN-V1`——知识基线 |

**跨平面验收（XP-01）**——29 个场景、13 道不可补偿的硬闸门、一个确定性聚合——写在[根 README §12](../README.md#12-评估) 里，并由 `tests/` 里的闸门本身作为证据。

---

## 运行时验证

| 文档 | 读它是为了 |
|---|---|
| [`local-integrated-runtime.md`](operations/local-integrated-runtime.md) | 起完整的本地拓扑：端口、profile、启动脚本、双进程 worker 装配 |

---

## 工程史

工程过程记录——阶段规格、验收报告、执行提示词——已于 **2026-10-06 删除**。它记录的是工作如何排序（试了什么、按什么顺序、什么通过了），而不是设计本身，也没有独立的读者价值。
**没有丢失任何东西**：当前规则在 `contracts/`，当前状态与风险在 `status/`，验收证据就是 `tests/` 里的那些闸门。需要历史版本时用 git。

因此**阶段术语（「Stage A–E」、「E1-C2」）已不再出现在本文档集的任何地方**；如果你在代码注释里遇到它，那是内部工程史，不是产品模型。

---

## 本文档集的约定

- 文档描述**当前**行为，并**显式**写明 **implemented / verified / not implemented / out of scope**，而不是暗示。
- 凡有边界，边界就写下来——**包括那些有代价的边界**。
- **架构与持久化文档是权威**：实现跟随它们，`tests/architecture/` 用机制强制分层规则。**记录下来的偏离不会让文档变错**——规范仍然成立，缺口作为 **Implementation Gap** 登记在受影响的那份文档里，而不是被抹平。当前唯一一处未闭合的例子见
  [`persistence-schema.md`](contracts/persistence-schema.md) §4（规范要求带时区的 `TIMESTAMPTZ`；ORM 层自始至终是 naive 的）。
- 验收产物是机器可读的、由真实运行产生的；**没有任何东西是从叙述里誊抄的**。
