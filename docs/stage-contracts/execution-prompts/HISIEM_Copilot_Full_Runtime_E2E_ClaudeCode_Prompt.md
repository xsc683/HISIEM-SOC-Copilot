# HISIEM + HISIEM-SOC-Copilot 全场景 Runtime E2E 测试提示词

> 目标：交给 Claude Code，对 `HISIEM` 与 `HISIEM-SOC-Copilot` 两个项目执行真实运行态、跨仓库、跨进程、跨存储、Browser-to-backend 的全场景端到端验收。
>
> 本轮不是 Stage D 封板任务。不要封板 Stage D，不要开始 Stage E，不要 commit，不要 push。

---

## 1. 测试对象与 Git 安全边界

### HISIEM

```text
D:\Project\SIEM
branch: add_frame
remote baseline: 5f46ac146a87ea17acb4834bab855863702007b9
```

本地 working tree 可能包含未提交 Stage D frontend/docs 改动，这些改动属于本次测试对象。

先记录：

```text
branch
HEAD
remote tracking SHA
git status
git diff --stat
untracked files
```

禁止：

```text
git reset
git checkout -- .
git restore .
git clean
rebase
force push
```

### HISIEM-SOC-Copilot

```text
D:\Project\HISIEM-SOC-Copilot
branch: capability-mcp
expected HEAD: 09487a72cf46a368fe82a797b7e8dabf7638e07d
```

Stage A / B / C 已通过验证，不要重新设计。

---

## 2. 本轮 E2E 定义

必须区分：

```text
L1 Unit / Component
L2 Integration
L3 Logical E2E           # FakeHisiem / FakeUnitOfWork / in-memory graph
L4 Browser Contract E2E  # Playwright + page.route()/mock API
L5 TRUE RUNTIME E2E      # 本轮核心
```

只有真实运行以下链路才算 TRUE CROSS-PROJECT E2E：

```text
Browser
→ HISIEM Vue
→ HISIEM control-api
→ PostgreSQL / Elasticsearch / Kafka / Flink
→ HISIEM BFF
→ Copilot FastAPI
→ Copilot PostgreSQL
→ real HISIEM HTTP adapter
→ Agent graph
→ Evidence / Finding / Verdict
→ Response Proposal
→ Human Approval
→ Copilot durable execution
→ HISIEM internal SOAR API
→ SOAR Worker
→ observed execution
→ Copilot Workspace
→ HISIEM Browser
```

已有 mock/in-memory E2E 只能作为回归证据，不能替代 L5。

---

## 3. Authority / Contract Sources

执行前读取实际代码与当前 contract。

### HISIEM

```text
README.md
CLAUDE.md
docs/current-status.md
docs/product-contract.md
docs/operations.md
docs/architecture.md
docs/agent-integration.md
docs/soar.md
docs/design/module-boundaries.md
docs/design/managed-detection-runtime.md
actual Controllers / Services / adapters
infra/docker-compose.yml
actual frontend router/API code
```

### Copilot

```text
current docs/
00_Four-Plane_Architecture_Contract_Freeze.md
Stage A/B/C specs
actual API routers
bootstrap/container
HISIEM adapter
SOAR adapter
Agent runtime
durable dispatcher
Workspace read model
Evidence/Finding/Verdict/Response persistence
actual configuration
```

若旧文档与代码冲突，以当前 production code + current product contract 为准。

---

## 4. 先建立 Runtime E2E Matrix，然后立即执行

每个场景记录：

```text
Scenario ID
Runtime path
Services involved
Input
Expected durable state
Expected external state
Expected UI state
Negative invariant
Execution result
Evidence
PASS / FAIL / BLOCKED / N/A
```

不要只列命令。每个场景必须回答：**它实际证明了什么？**

建立 matrix 后继续执行，不要停下来等用户确认。

---

## 5. 主动执行与安全规则

只要问题可以在本地开发环境安全解决，就自行解决并继续：

```text
diagnose → resolve → continue
```

包括但不限于：Docker 未启动、Kafka topic 未 ready、Flink job 未运行、control-api / Copilot / SOAR worker / Vite 未启动、Playwright browser 未安装、migration 未执行、本地 MCP server 未启动、deterministic model server 未启动、测试 harness 缺失、真实 production defect。

仅以下情况允许停止：

- 需要用户提供未知外部 credential
- 需要破坏性 DB 操作
- 需要删除 persistent volume
- 需要 reset/recreate 用户已有环境
- 需要修改 frozen architecture
- 需要真正外部生产服务
- 操作可能破坏用户已有非测试数据

禁止：

- `docker system prune`
- 删除 volume
- drop 整个现有 database
- truncate 非测试业务表
- 删除用户已有运行数据
- 手工修改运行容器作为配置源

测试数据使用唯一 namespace：

```text
e2e-<timestamp-or-uuid>
```

只清理由本轮创建且 ownership 明确的数据。

---

## 6. Secret Handling

不要打印：

- `.env` 内容
- API key
- bearer token
- DB password
- session token
- Authorization header

可检查“是否配置/是否非空/双端是否一致”，但输出必须 masked。

本地双服务测试优先使用 runtime-only ephemeral E2E secret，不写入 Git。

Browser 永远不能获得 Copilot service bearer 或 HISIEM internal SOAR service bearer。

---

## 7. 启动真实 Runtime Topology

根据实际配置启动并验证：

```text
HISIEM PostgreSQL
Elasticsearch
Kafka
Logstash
Flink JobManager
Flink TaskManager
Flink detection job
HISIEM control-api
必要时 detection-controller
SOAR worker
HISIEM Vue
Copilot PostgreSQL
Copilot FastAPI
必要时 deterministic model server
必要时 local MCP server
必要时 OTel Collector
```

不能只验证“端口可连”。要验证运行语义。

---

## 8. 场景 ENV — Runtime Health

验证：

- PostgreSQL ready
- Elasticsearch cluster available
- Kafka metadata/topic available
- Logstash pipeline loaded
- Flink JM/TM ready
- detection job RUNNING（需要时）
- control-api health
- SOAR worker running
- Copilot `/healthz`
- Vue reachable
- `/api/ops/health-scan`

确认 `UP / DEGRADED / DOWN` 语义正确；Logstash 仅端口可达不能判定 pipeline healthy。

---

## 9. 场景 AUTH — Browser / BFF / Service Trust

使用真实 HISIEM authentication，至少覆盖 ADMIN / ANALYST / AUDIT。

验证：

- valid login
- unauthenticated rejected
- AUDIT read allowed
- AUDIT mutation rejected
- ANALYST expected actions allowed
- tenant membership enforced

Browser 网络只能访问 HISIEM application boundary，不能直接请求 Copilot `:8000`。

直接验证 Copilot service trust：

```text
missing bearer → reject
bad bearer → reject
valid bearer + missing tenant → reject
valid bearer + missing actor → reject
valid bearer + trusted tenant/actor → accept
```

所有失败不能形成 credential oracle。

---

## 10. 场景 INGEST — Real Log Ingestion

必须从真实入口发送日志，不允许直接向 ES 插数据替代。

Positive：

```text
unique SSH/security log
→ Logstash
→ ECS event
→ Elasticsearch siem-events-*
→ Kafka siem-events
```

验证：`@timestamp`、source.ip、user.name、event action/outcome、log.source_id、原始日志语义、unique marker。

Negative：bad JSON 或 missing/invalid `@timestamp`：

```text
→ siem-events-dlq
```

不得进入正常检测分支，不得生成伪造告警。

---

## 11. 场景 DETECT — Kafka → Flink → Alert

使用真实 Kafka + Flink。

至少执行：

- single-event representative path
- window/brute-force path

优先使用 `infra/simulator/brute-force-test.sh` 或当前规则真实支持的格式。

证明：

```text
Logstash
→ Kafka offset advances
→ Flink consumes
→ detection
→ Elasticsearch siem-alerts
```

验证 alert.id、rule.id、event_count、related_events、entity、事件时间、alert.created_at。

重复/重放必须验证 deterministic convergence，不能无界制造 duplicate alert。

---

## 12. 场景 RULE — Rule Lifecycle

使用独立 E2E rule：

```text
list → detail → valid create/edit → desired state → deploy → observed runtime
```

验证：

- valid single_event/window accepted
- invalid DSL rejected
- bad update 不覆盖旧有效配置
- enabled/desired state 保持唯一 authority
- deploy 返回当前契约状态
- Flink 行为只在真实 apply 后改变

若 process runtime adapter 默认 disabled，先查受支持测试配置。不能安全启用则验证 disabled semantics，并将 physical deploy 标为 BLOCKED/环境限制，不伪造 PASS。

---

## 13. 场景 ALERT / CASE

基于真实检测产生的 Alert：

```text
Alert list
→ detail
→ verdict
→ status
→ audit
→ Case aggregation / creation
→ Case detail
→ timeline
→ evidence/entity linkage
```

验证：stable ID、verdict/status 独立、close 前约束、event time/created time 区分、audit actor、Case/Alert relation、timeline durability。

Negative：invalid transition、缺 verdict 关闭、无权限写操作必须被拒绝。

---

## 14. 场景 RISK / HEALTH / NOTIFICATION

使用唯一 E2E entity：

```text
criticality update → risk recalculation → resulting risk state
```

验证 audit 与 entity linkage。

同时检查 Data Health / operational health / 测试行为自然产生的通知。禁止为了“有通知”直接写 DB。

---

## 15. 场景 COPILOT-LAUNCH — Real Alert → Investigation

必须使用真实 HISIEM Alert。

```text
POST /api/alerts/{alertId}/agent-investigation
```

链路：

```text
Browser/HISIEM API
→ AlertController
→ AgentLaunchService
→ service bearer + trusted tenant/actor
→ Copilot POST /api/v1/investigations
→ Copilot PostgreSQL
```

验证：

- investigation_id
- one active investigation per tenant + alert
- repeated launch idempotent/convergent
- alert re-entry lookup active/latest
- Browser 不收到 service bearer
- body 不复制整条 Alert
- authoritative source ref 持久化
- hard refresh/re-entry 从 durable state 恢复，不靠 localStorage 作为 truth

---

## 16. 场景 COPILOT-RUN — Real Investigation

禁止：

```text
FakeHisiem
FakeUnitOfWork
httpx.MockTransport
```

必须真实：

```text
Copilot process → HisiemHttpAdapter → HISIEM control-api
```

Agent 至少真实调用：

```text
GET /api/alerts/{id}
GET /api/detection-rules/{ruleId}
POST /api/log-search
```

数据必须来自本轮真实 ingestion/detection。

LLM 为稳定性优先使用 repository-supported scripted deterministic model；若进程级 scripted provider 不足，可启动本地 deterministic OpenAI-compatible test model server，但它只能替代外部 LLM，不能 fake HISIEM、DB、BFF、tool execution、Evidence persistence。

验证真实持久链：

```text
Investigation
→ Tool Invocation Audit
→ Evidence
→ Hypothesis/Assessment
→ Finding
→ Investigation Result
```

Evidence 必须引用真实 tool invocation。Definitive MALICIOUS/BENIGN 必须有真实 observed HISIEM Evidence；Knowledge-only 不可满足 observed-fact guard。

---

## 17. 场景 KNOWLEDGE — Real Knowledge Runtime

使用真实 Copilot PostgreSQL Knowledge storage，优先通过 repository-supported fixture/ingestion 导入隔离 deterministic test knowledge。

至少验证：

```text
knowledge.retrieve_security_guidance
knowledge.resolve_attack_technique
```

验证 tenant isolation、Citation、source/version/hash、实际 retrieval mode、ATT&CK identity、Knowledge Evidence persistence、Workspace Supporting Context。

Knowledge 不能变成 Platform Fact，retrieval score/RRF 不能变成 authority rating。

需要 embedding 时使用受支持 deterministic test embedding profile，不依赖付费 provider。

---

## 18. 场景 MCP — Real Local Streamable HTTP

启动真实 local deterministic MCP server，禁止 public MCP server。

要求：official SDK、high-level client、Streamable HTTP、当前配置要求的协议策略。

配置 admitted READ_ONLY E2E capability，通过真实 Agent runtime 调用：

```text
discovery
→ catalog
→ admission
→ ToolRegistry
→ Policy
→ Budget
→ MCPToolProvider
→ MCP server
→ ProviderInvocationResult
→ ToolResult
→ EvidenceNormalizer
→ Evidence
```

同时验证：

```text
unadmitted dynamic tool → not selectable
schema drift → SCHEMA_MISMATCH
is_error → REMOTE_TOOL_ERROR
timeout → typed failure
oversized → RESULT_TOO_LARGE
prompt-injection-like text → DATA only
secret → absent from Evidence/Audit/Telemetry
```

---

## 19. 场景 RESPONSE — Human Authority

基于真实完成的 Investigation + Evidence，通过真实 HISIEM Workspace 创建 Response Proposal：

```text
Browser → HISIEM BFF → Copilot proposal → Policy
```

使用安全 E2E SOAR Playbook，不允许真实外部破坏动作。

验证 target 来自 persisted Investigation source alert。

Browser 不得指定 tenant / actor / target / provider credential。

注入 `target / tenant_id / actor / credential` 必须 fail closed。

---

## 20. 场景 APPROVE / REJECT

### Reject

```text
Proposal → WAITING_APPROVAL → Human Reject → persisted decision
```

必须：

```text
ZERO durable execution command
ZERO HISIEM SOAR execution
```

### Approve

```text
Proposal → Policy REQUIRE_APPROVAL → Human Approve → Durable Command → submission
```

验证：

```text
expected_revision + expected_content_hash
```

stale/wrong revision/hash → `409` + ZERO execution。

并发/重复 approval 不得产生 duplicate execution intent。

---

## 21. 场景 SOAR — Copilot → HISIEM Execution

Approve path 必须真实进入：

```text
Copilot durable execution
→ HisiemSoarAdapter
→ POST /api/internal/soar/executions
→ HISIEM InternalServiceAuthFilter
→ SoarService
→ HISIEM PostgreSQL
→ SOAR worker
→ execution state
```

验证 service bearer、X-Tenant-ID、Idempotency-Key、real execution_id、target、playbook snapshot、node state、final status。

然后：

```text
Copilot observe
→ GET /api/internal/soar/executions/{id}
→ update execution reference
→ Workspace
→ Browser
```

最终 UI 必须显示 HISIEM observed execution truth。submission success 不能冒充 execution success。

---

## 22. 场景 IDEMPOTENCY

同 submission/idempotency key 重试：

```text
same logical command → same HISIEM execution
```

不得产生 second side effect。

同 lifecycle message_id：一个 matching playbook 只产生一个 execution；若有多个 matching playbook，则允许每个 playbook 各一个。

---

## 23. 场景 DURABILITY / RESTART

只重启本轮安全启动的进程，不删 DB/volume。

至少验证：

```text
Copilot pending durable command
→ restart Copilot
→ resumes
→ no duplicate execution
```

可行时再验证：

```text
SOAR pending/running
→ restart SOAR worker
→ durable state resumes
```

验证 state/idempotency survives restart，execution truth 不重置，Workspace 可重构。

---

## 24. 场景 FAILURE / RECOVERY

执行受控 failure injection，并恢复后继续。

### HISIEM unavailable to Copilot

```text
→ typed external failure
→ no fabricated Evidence
→ no state corruption
→ uncertainty/INCONCLUSIVE where appropriate
```

恢复后新的 investigation 必须正常。

### Copilot unavailable to HISIEM

Browser/BFF 必须得到 bounded error（例如当前契约的 503），不泄露 raw upstream body/credential，且 HISIEM 其他功能仍可用。恢复后 re-entry 成功。

### MCP unavailable

必须是 typed UNAVAILABLE/TIMEOUT，不可 fake success。

禁止用 mocked HTTP response 代替 runtime failure。

---

## 25. 场景 ATTENTION_REQUIRED

若仓库有安全可控的 test retry/backoff 配置，则构造：

```text
approved command
→ transient/uncertain HISIEM submission failures
→ retry budget exhausted
→ ATTENTION_REQUIRED
```

语义必须是“提交结果不确定，需要人工处理”，不能说 provider rejected，也不能生成不存在的 external execution id。UI 进入终态后按当前契约停止轮询。

若当前 runtime 没有安全方式加速 retry，不要修改 production retry semantics，只为测试。标记：

```text
RUNTIME E2E BLOCKED BY TESTABILITY
```

并保留已有 deterministic evidence。

---

## 26. 场景 TENANT ISOLATION

使用：

```text
tenant-e2e-a
tenant-e2e-b
```

验证：

```text
A investigation → B cannot read
A proposal → B cannot approve
A SOAR execution → B cannot query
```

BFF tenant 必须来自 HISIEM validated context；body/header spoof 不能越过 trust boundary。

不要把 HISIEM 尚未承诺的全 ES document-level multi-tenancy 当作已经实现的 contract。

---

## 27. 场景 SECURITY NEGATIVE

真实 HTTP 边界验证：

- missing session
- invalid user token
- wrong role
- bad HISIEM→Copilot service bearer
- bad Copilot→HISIEM internal SOAR bearer
- missing tenant
- tenant spoof
- actor spoof
- response target injection
- protected field injection
- wrong approval path/body mismatch
- stale revision/hash
- unknown MCP server
- MCP endpoint/model SSRF attempt

状态码应按实际 contract 返回 400/401/403/409 等。

错误中不得出现 bearer、secret、raw provider response、DB DSN、stack trace、upstream credential。

---

## 28. 场景 UI — TRUE Browser E2E

保留已有 mock Playwright，但另建 runtime browser harness。

Runtime harness 禁止：

```text
page.route(... route.fulfill ...)
Mock API
hard-coded Workspace fixture
```

允许 `page.on('request')` 只观察网络，不改响应。

真实登录并浏览：

```text
/overview
/logs
/alerts
/alerts/{real-e2e-alert}
/cases
/copilot/investigations/{real-investigation}
/soar/executions
```

验证：Platform Fact、Knowledge Context、Model-derived Finding、AI Investigation Verdict、Analyst Decision、execution truth、Evidence→Finding navigation、approval/reject、submitted != succeeded、hard refresh reconstruction、stale/error state、narrow viewport。

Browser 不得直接请求 `http://127.0.0.1:8000`，只能通过 HISIEM BFF。

---

## 29. 场景 OBSERVABILITY

如果有 local Collector config，启动真实 Collector。

验证真实链路出现：

```text
investigation.run
graph.invoke
tool.execute
knowledge.retrieve
mcp.call
investigation.persist
response.submit
response.observe
durable.dispatch
```

验证 W3C trace 传播、durable async link/new-root 语义。

检查无 credentials、raw prompt、full model response、full ToolResult、full Evidence、SQL bind、embedding vectors，也不能把高基数 IDs 放到 metric labels。

临时使 Collector unavailable 后，业务流程仍应成功：

```text
Telemetry outage != business outage
```

---

## 30. 场景 SIEM 独立 SOAR

除 Copilot response path 外，覆盖 HISIEM 自身 SOAR 主旅程：

```text
Playbook create
→ graph edit
→ publish
→ enable
→ manual/lifecycle trigger
→ execution
```

至少覆盖 Human approval、Wait、basic action；Parallel/Join 与 Loop/Loop End 在安全可行时覆盖。

验证 graph snapshot、node I/O、approval resume、Wait 不忙轮询、lease、fencing、idempotency。

---

## 31. 数据一致性检查

每个主要场景至少交叉验证两层事实：

```text
Alert: ES document ↔ HISIEM API
Investigation: Copilot DB ↔ Copilot API/Workspace
SOAR: HISIEM PG ↔ HISIEM API ↔ Copilot observed reference
Browser: UI ↔ backend durable truth
```

UI 显示成功不能单独证明 backend 成功；API 200 也不能证明最终业务执行成功。

---

## 32. 测试缺口与 Production Defect 处理

重要 runtime contract 没有 harness 时，创建最小 deterministic runtime harness。优先放在临时工作目录或 repository-approved system-test 目录，不修改 production architecture。

若发现真实 production defect：

```text
prove defect
→ smallest correct fix
→ add permanent regression test
→ rerun failed scenario
→ rerun dependent scenarios
→ continue full matrix
```

不要只汇报 bug 后停止。

只能修改被证明有缺陷的项目。

---

## 33. 变更失效规则

若修改 HISIEM backend：重跑 affected Java tests + root Maven tests + 依赖该 boundary 的 cross-project runtime E2E。

若修改 Flink：重跑 Flink tests/package + real detection E2E。

若修改 frontend：重跑 lint + unit + build + existing Playwright + runtime Browser E2E。

若修改 Copilot production code：重跑 focused tests + mypy + Ruff + `git diff --check` + full pytest + affected runtime E2E。

若修改 cross-project API contract：两个 repo 相关测试 + 真实 cross-service scenario 全部重跑。

---

## 34. Static / Regression Gates

真实 runtime matrix 完成后补常规门禁。

### HISIEM

```text
Maven Spotless
root Maven tests
Flink Spotless
Flink clean package
frontend lint
frontend unit
frontend build
existing Playwright
```

### Copilot

```text
strict mypy
Ruff
git diff --check
architecture tests
full pytest
```

已有 fresh evidence 且相关代码未变可复用；发生修复则必须重新执行受影响 gate。

---

## 35. 测试清理

停止只由本轮启动、且确认不会影响用户工作的临时 process。

不要无条件 `docker compose down`。

可删除本轮 temporary Playwright runtime specs、screenshots、logs、stub model server、local MCP server、temporary harness。

API-created E2E entities 只有 ownership 精确可确认时才清理。不要直接删业务 DB row，除非仓库明确提供 test teardown。

---

## 36. 最终 Scope Review

确认没有：

- Stage E implementation
- Stage D sealing
- unrelated refactor
- alternate MCP stack
- frontend-only business truth
- test-only production bypass
- disabled auth/tenant checks
- weakened approval boundary
- hard-coded test credential
- committed secret
- permanent mock replacing runtime integration
- destructive DB/docker operation

并确认 HISIEM 本地未提交 Stage D 改动仍存在，没有被 reset/clean/restore 覆盖。

---

# 37. 必须生成 Markdown 测试报告

测试完成后，必须真正写入 `.md` 文件，不能只在终端打印。

优先输出路径：

```text
D:\Project\HISIEM-SOC-Copilot\FULL_RUNTIME_E2E_TEST_REPORT.md
```

若当前会话不允许安全写入该目录，则输出到当前安全工作目录：

```text
<safe-working-dir>\FULL_RUNTIME_E2E_TEST_REPORT.md
```

最终终端回复必须给出实际绝对路径。

## 37.1 报告头

```markdown
# HISIEM + HISIEM-SOC-Copilot Full Runtime E2E Test Report

- Date:
- Tester: Claude Code
- HISIEM branch:
- HISIEM HEAD:
- HISIEM remote SHA:
- Copilot branch:
- Copilot HEAD:
- Environment:
- Test namespace:
```

## 37.2 顶层状态

```markdown
## Final Status

- HISIEM RUNTIME E2E: PASS | FAIL | BLOCKED
- COPILOT RUNTIME E2E: PASS | FAIL | BLOCKED
- CROSS-PROJECT E2E: PASS | FAIL | BLOCKED
- PRE-SEAL SYSTEM GATE: PASS | FAIL
```

只有核心真实 runtime path 全部通过才能 `PRE-SEAL SYSTEM GATE: PASS`。

Unit / Integration / Mock Playwright 全绿，也不能替代 TRUE RUNTIME E2E。

## 37.3 Runtime Topology

```markdown
## Runtime Topology

| Component | Runtime | Endpoint/Port | Real/Mock | Result |
|---|---|---|---|---|
```

至少列出 HISIEM PG、ES、Kafka、Logstash、Flink JM/TM/job、control-api、detection-controller、SOAR worker、Vue、Copilot PG、Copilot FastAPI、deterministic model server、local MCP server、OTel Collector（实际使用者）。

## 37.4 Scenario Matrix

```markdown
## Scenario Matrix

| ID | Scenario | Runtime Path | Result | Durable Evidence | External Evidence | UI Evidence | Notes |
|---|---|---|---|---|---|---|---|
```

至少覆盖：

```text
ENV
AUTH
INGEST
DETECT
RULE
ALERT
CASE
RISK
COPILOT-LAUNCH
COPILOT-RUN
KNOWLEDGE
MCP
RESPONSE
APPROVE
REJECT
SOAR
IDEMPOTENCY
DURABILITY
FAILURE/RECOVERY
ATTENTION_REQUIRED
TENANT
SECURITY
UI
OBSERVABILITY
SIEM-SOAR
```

## 37.5 Evidence Classification

必须单列：

```markdown
## Evidence Classification

### TRUE RUNTIME E2E
### Integration Evidence
### Logical E2E
### Browser Contract E2E
```

明确哪些 PASS 是真实 runtime，哪些只是 regression evidence。

## 37.6 Production Defects

发现 defect 时：

```markdown
## Production Defects Found

### DEFECT-001 — <title>

- Severity:
- Repository:
- Component:
- Reproduction:
- Root Cause:
- Fix:
- Regression Test:
- Runtime Revalidation:
- Status:
```

没有则写：

```text
No production defects found during this run.
```

不要隐藏已修复 defect。

## 37.7 Blocked / Skipped

```markdown
## Blocked / Skipped Scenarios

| Scenario | Status | Exact Reason | Existing Evidence | What Is Required |
|---|---|---|---|---|
```

禁止只写 “skipped”。

## 37.8 Security Boundary Validation

至少记录：

- Browser → HISIEM
- HISIEM → Copilot
- Copilot → HISIEM internal SOAR
- tenant boundary
- actor boundary
- approval TOCTOU
- MCP admission/schema/endpoint boundary
- secret leakage
- error sanitization

## 37.9 Data Consistency Validation

至少包含：

```text
Alert: ES ↔ HISIEM API
Investigation: Copilot DB ↔ API/Workspace
SOAR: HISIEM PG ↔ HISIEM API ↔ Copilot observed reference
Browser: UI ↔ durable backend truth
```

## 37.10 Failure and Recovery Validation

记录：

```text
injected failure
expected behavior
observed behavior
data integrity
recovery result
duplicate side effect check
```

## 37.11 Regression Gates

```markdown
## Regression Gates

### HISIEM
| Gate | Result | Evidence |
|---|---|---|

### Copilot
| Gate | Result | Evidence |
|---|---|---|
```

## 37.12 Final Git State

必须写：

```markdown
## Final Git State
```

分别记录两个仓库：branch、HEAD、`git status --short`、`git diff --stat`。

明确：production code 是否被修改、是否存在 uncommitted fixes、是否有 temporary artifacts。

固定：

```text
Commit: NO
Push: NO
```

## 37.13 Final Conclusion

```markdown
## Final Conclusion

### Proven by real runtime
- ...

### Proven only by integration/logical/mock tests
- ...

### Remaining blockers
- ...

### Pre-seal decision
PASS | FAIL
```

不得使用模糊措辞。

---

## 38. 最终终端回复格式

最终聊天/终端只简洁输出：

```text
HISIEM RUNTIME E2E: ...
COPILOT RUNTIME E2E: ...
CROSS-PROJECT E2E: ...
PRE-SEAL SYSTEM GATE: ...

Test report:
<absolute path to FULL_RUNTIME_E2E_TEST_REPORT.md>

Production fixes:
<none | short summary>

Blocked scenarios:
<none | short summary>

Git:
HISIEM <status>
Copilot <status>

Commit: NO
Push: NO
```

不要把完整报告再次复制到聊天，完整结果以 Markdown 报告为准。

---

## 39. 最终禁止项

不要：

- commit
- push
- 封板 Stage D
- 开始 Stage E
- 用 mock 替代真实 runtime
- 弱化认证/tenant/approval 让测试通过
- 修改 production retry semantics 只为测试
- 删除用户 volume
- 清空用户数据库
- reset 用户未提交代码
- 打印 secret
- 把 API 200 当最终执行成功
- 把 submission success 当 SOAR success
- 把 telemetry 当 business truth

本轮唯一目标：

```text
真实证明两个项目从 SIEM 数据产生
到 Copilot 调查
到人工授权
到 HISIEM SOAR 执行
再回到 Workspace/UI
整个系统链路在真实 runtime 下正确工作。
```
