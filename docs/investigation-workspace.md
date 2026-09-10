# Investigation Workspace — P1 规范契约

## 1. 目的与范围

本文是 **P1 SOC Investigation Workspace 垂直切片**的唯一规范契约，覆盖：

```text
HISIEM Alert
→ 启动 / 重新进入 AI 调查
→ Investigation Workspace（概览 / 证据 / 调查过程 / 时间线）
→ Result / Verdict / Uncertainty
→ 刷新后仍然存在的持久化体验
```

P1 的目标是从「可运行的调查引擎」推进到「可被分析师使用的产品」。它不改变 GP-01 评测、不重跑基准、不新增 Agent 工具/提示词/评分器。

范围外（出现即 STOP）：P2 Response Approval、SOAR 执行集成、分析师覆盖结论、案件协作/评论/任务、多告警调查、GP-02、Threat Hunt、Notebook、Chat、WebSocket 基础设施、关联图、任何新 Agent 能力。

---

## 2. 架构与信任边界

```text
Browser
  ↓  (HISIEM session, 浏览器只访问 HISIEM)
HISIEM Web
  ↓
HISIEM authenticated API boundary
  ↓  (可信服务间请求)
HISIEM Copilot BFF / adapter
  ↓
SOC Copilot Workspace API
```

固定规则：

- 浏览器 **不得**直接用伪造身份头调用 Copilot 特权 API。
- 浏览器 **不得**获得 HISIEM→Copilot 服务凭据。
- P1 **不引入第二个独立登录**。
- 开发/测试 Header Provider 不是生产浏览器认证。

---

## 3. 归属（冻结）

HISIEM 拥有：`Alert` / `Event` / `Case` / `Detection Rule` / `Tenant` / `User-RBAC` / `SOAR`。

Copilot 拥有：`Investigation` / `PlanRevision` / `Evidence` / `Hypothesis` / `Assessment` / `Finding` / `InvestigationResult`（后续 `ResponseProposal` / `Approval`）。

规则：

- Copilot 业务行 **绝不**复制进 HISIEM 持久层。
- HISIEM Alert/Event **绝不**作为新业务实体复制进 Copilot。
- 引用始终是引用（`ExternalResourceRef`）。

---

## 4. Workspace 读模型

Workspace 是一个 **不可变投影**：在一次 UnitOfWork 内完成多次租户受限的端口读取后关闭，不从 router 直接做 ORM 序列化。P1 **不做持久化迁移**，读模型完全由既有持久行组装。

`GET /api/v1/investigations/{investigation_id}/workspace` 返回：

```text
investigation          InvestigationHeader
source_alert_ref       SourceAlertRefSchema
plan_revisions[]       PlanRevisionSchema
evidence[]             EvidenceSchema
hypotheses[]           HypothesisSchema
findings[]             FindingSchema
result                 ResultSchema | null
tool_activity[]        ToolActivitySchema
timeline[]             TimelineEntrySchema
```

关键约束：

- `plan_step_state.status` 由运行时按原样投影，读模型不改写。
- `result` 仅在终局结论存在时非空；进行中不得伪造。
- 每个读取边界都做租户作用域；跨租户 / 未知 investigation 一律 `404`。

---

## 5. 活动时间线

时间线是 **产品活动时间线**，其条目只能由持久事实支撑，排序为：

```text
occurred_at ASC + 稳定次级键
```

绝不暴露 LangGraph 节点名、checkpoint、prompt、思维链。时间线 **不做任何合成计算**，仅按持久 `kind` 归类展示。

---

## 6. 安全保证（必须可证明）

- Tenant A 不能读取 / 取消 Tenant B 的 Investigation（真实 PG 测试证明跨租户 `404`）。
- 浏览器不能通过 body / header 覆盖 tenant 或 actor。
- HISIEM→Copilot 服务凭据永不进入浏览器。
- Workspace API 不暴露任何 API Key / token / password / DSN。
- 不返回原始模型请求/响应；不返回思维链；不返回 LangGraph checkpoint 状态。

### API 响应安全（显式排除）

响应 DTO 为显式 Pydantic 模型，不使用通用 ORM 序列化；不得出现：

```text
CMD_API_KEY / Authorization / Bearer
HISIEM service credential
DB DSN
raw prompt / model request / model response
chain-of-thought
LangGraph checkpoint state
environment variables
```

---

## 7. 重新进入与启动

主链复用既有 `AlertDetailView` → `POST /api/alerts/{id}/agent-investigation` → HISIEM 后端 → Copilot `POST /api/v1/investigations` → `investigation_id` / `redirectUrl`。

- 不新建第二套启动实现。
- 跳转最终解析到 HISIEM 自有认证路由 `/copilot/investigations/{investigation_id}`。
- 同一 tenant + alert 至多一个 ACTIVE Investigation。

重新进入状态由 **服务端权威查询** 决定（不由前端本地状态决定）：

```text
GET /api/v1/investigations/lookup?provider=&resource_type=&address_id=
→ { active, latest }
```

```text
无调查            → 「交给 Agent 调查」
存在 active       → 「继续 Agent 调查」
仅有已完成/失败/取消 latest → 「查看 Agent 调查」（可选「重新调查」）
```

P1 不实现删除。

---

## 8. Workspace API

Copilot 侧：

```text
GET  /api/v1/investigations/{investigation_id}            # 既有，保持向后兼容
GET  /api/v1/investigations/{investigation_id}/workspace  # 只读投影
GET  /api/v1/investigations/lookup                        # 重新进入查询
POST /api/v1/investigations/{investigation_id}/cancel     # 取消
```

Workspace 查询是只读的：不产生变更、不调用模型/Graph/工具、不执行响应副作用。

HISIEM BFF 侧：

```text
GET  /api/agent-investigations/{id}
GET  /api/agent-investigations/{id}/workspace
POST /api/agent-investigations/{id}/cancel
GET  /api/alerts/{id}/agent-investigation                 # 重新进入查询
```

BFF 以服务端派生的 `X-Tenant-ID` / `X-Actor-Subject` 转发，**绝不**透传上游错误正文，上游状态归一化为安全错误码（404 / 403 / 409 / 502 / 503）。

---

## 9. 前端 Workspace

单一 Workspace 页面（路由 `/copilot/investigations/:investigationId`），四个页签：

```text
概览 / 证据 / 调查过程 / 时间线
```

无聊天面板。

- 概览优先级：Verdict → Findings → Uncertainties → ATT&CK → Response Recommendations。
- INCONCLUSIVE 视觉醒目；FAILED ≠ INCONCLUSIVE。
- Finding → Evidence 引用芯片按 **持久 ID** 解析（绝不做文本匹配），点击打开证据抽屉。
- 证据抽屉展示 Provenance / Provider Reference / Integrity。
- Response recommendations 只读展示；P1 无 Execute/Block/Isolate/Run SOAR/Approve/Reject 按钮。
- 进行中约 2–3s 轮询：无重叠请求、页面隐藏暂停、终态停止、支持手动刷新、卸载时销毁定时器；临时失败保留上次快照并标注过期，**不空白页面**。
- 失败渲染已获得的事实；INCONCLUSIVE 不是错误页。

---

## 10. 向后兼容

以下必须保持不变：

```text
POST / GET / cancel  /api/v1/investigations
E1-C1..C6 评测命令
GP-01 scorer
工具/证据 lineage
provider 行为
```

---

## 11. 质量自检（P1 收尾）

```text
1  浏览器不直接对 Copilot 认证            → 否
2  HISIEM 持久化 Copilot 数据             → 否
3  Copilot 复制 HISIEM Alert/Event        → 否
4  Workspace 查询产生变更                 → 否
5  Workspace 查询调用模型/Graph/工具      → 否
6  Evidence 不可变                       → 是
7  Finding→Evidence 按 ID                 → 是
8  引用芯片定位到确切 Evidence            → 是
9  最终 Result 与部分 RUNNING 分离        → 是
10 FAILED 与 INCONCLUSIVE 区分            → 是
11 UI 暴露 CoT/原始 prompt                → 否
12 P1 执行响应副作用                      → 否
13 错误租户可读/取消                      → 否
14 刷新后可重建                           → 是
15 告警页可重新进入                       → 是
16 既有 E1-C1..C6 测试保持绿色            → 是
17 GP-01 保持封存未修改                   → 是
```
