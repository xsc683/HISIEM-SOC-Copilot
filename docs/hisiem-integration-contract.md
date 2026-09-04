# HISIEM SOC Copilot V1 — HISIEM Integration Contract

## 1. 目的

本文定义 HISIEM 与 HISIEM SOC Copilot V1 之间的启动、身份、资源引用、数据读取、幂等和错误边界。

V1 仅覆盖：

```text
HISIEM Alert
→ Start Investigation
→ SOC Copilot Investigation
```

Case-driven Investigation 不属于 V1。

---

## 2. Source of Truth

HISIEM 始终拥有：

```text
Alert
Event
Case
Detection Rule
Tenant
User / RBAC
SOAR Playbook
SOAR Execution
```

SOC Copilot 始终拥有：

```text
Investigation
PlanRevision
Evidence
Hypothesis
Finding
InvestigationResult
ResponseProposal
Approval
```

Copilot 不复制 HISIEM Alert/Event/Case/SOAR 为自己的业务实体。

---

## 3. 主启动链

V1 目标调用链：

```text
Browser
  ↓
HISIEM Alert Detail
  ↓
POST /api/alerts/{id}/agent-investigation
  ↓
HISIEM Backend
  ↓
POST SOC Copilot /api/v1/investigations
  ↓
Investigation ID
  ↓
Investigation Workspace
```

浏览器不得获得 HISIEM→Copilot 服务凭据。

HISIEM Backend 负责：

- 用户认证。
- Tenant membership 校验。
- `ADMIN` / `ANALYST` 启动权限校验。
- 向 Copilot 建立可信服务请求。

Copilot 不重新信任浏览器声明的 Tenant、Actor 或 Role。

---

## 4. ExternalResourceRef

Copilot 使用统一外部资源引用：

```text
ExternalResourceRef
provider
resource_type
address_id
business_id?
```

HISIEM Alert V1 固定：

```text
provider = hisiem
resource_type = alert
```

### `address_id`

`address_id` 必须是 HISIEM API 实际用于资源寻址的 ID。

对于当前 HISIEM Alert：

```text
GET /api/alerts/{address_id}
```

当前实现中该值对应 Elasticsearch Alert Document `_id`。

不得假设：

```text
alert.id == address_id
```

### `business_id`

`business_id` 可保存 HISIEM 返回的 `alert.id`，仅用于业务展示和关联。

所有实际 API 调用必须使用 `address_id`。

---

## 5. Investigation 创建 API

目标接口：

```text
POST /api/v1/investigations
```

请求 Body：

```json
{
  "source_alert_ref": {
    "provider": "hisiem",
    "resource_type": "alert",
    "address_id": "<hisiem-alert-address-id>",
    "business_id": "<optional-alert-id>"
  }
}
```

Body 中禁止出现：

```text
tenant_id
actor identity
role
permissions
alert body
prompt
SOAR authority
```

启动 Investigation 不接受任意 Prompt。

---

## 6. Trusted Launch Context

Application 只接收已经由可信 Provider 建立的：

```text
TrustedContext

tenant_id
actor_subject
actor_display_name?
roles / permissions?
```

生产环境必须先完成 HISIEM→Copilot 服务认证，再建立 TrustedContext。

允许的实现方式包括：

```text
service credential + validated identity propagation
signed service token
mTLS-bound service identity
other authenticated server-to-server mechanism
```

本文不固定具体认证协议，但固定以下规则：

> 未经过服务认证的普通 HTTP Header 不具备身份权威。

开发/测试 Header Provider 不属于生产 Integration Contract。

---

## 7. Idempotency

HISIEM→Copilot 创建请求必须携带稳定：

```text
Idempotency-Key
```

同一个用户启动动作发生网络重试时必须复用同一个 Key。

规则：

```text
same Idempotency-Key
→ same logical result

new Idempotency-Key
+ same tenant + same alert
+ active Investigation exists
→ return existing active Investigation
```

数据库仍必须最终保证：

```text
at most one active Investigation
per tenant + alert address_id
```

---

## 8. 创建响应

成功响应最小结构：

```json
{
  "investigation_id": "<uuid>",
  "status": "RUNNING",
  "created": true
}
```

如果返回已有 Active Investigation：

```json
{
  "investigation_id": "<uuid>",
  "status": "RUNNING",
  "created": false
}
```

API Response 不承担 UI Base URL 配置。

如 HISIEM 需要跳转 Workspace，由 HISIEM 服务端根据配置构造 UI 地址。

---

## 9. Authoritative Hydration

创建请求只传 Resource Reference。

Copilot 创建 Investigation 后必须重新读取：

```text
GET HISIEM /api/alerts/{address_id}
```

从 HISIEM 获取权威 Alert Context。

禁止：

```text
HISIEM launch request
→ copy full alert body
→ persist as Copilot Alert
```

Hydrated Alert 仅形成 bounded runtime context 和 Evidence source，不改变 HISIEM Source-of-Truth 所有权。

---

## 10. HISIEM Read Authentication

Copilot 调用 HISIEM Read API 时必须使用 HISIEM 能够认证和授权的服务身份或受控委托身份。

HISIEM 负责最终验证：

```text
authenticated principal
+
tenant membership
+
read permission
```

Copilot 发送的 Tenant Context 必须来自当前 Investigation 的可信 `tenant_ref`，不得来自 LLM Tool Arguments。

任何模型生成内容均不得决定：

```text
tenant
principal
authorization scope
service credential
```

---

## 11. Alert Hydration Contract

Provider：

```text
HISIEM
```

Endpoint：

```text
GET /api/alerts/{address_id}
```

Copilot 最少保留以下 bounded context：

```text
address_id
business alert.id?
alert.created_at
alert.rule_id
alert.rule_name
alert.type
alert.severity
alert.description
alert.risk_score
alert.entity
alert.status
alert.case_id?
rule.tags[]
event_count
related entity fields
bounded related_events
```

`alert.analyst_verdict` 可以作为 HISIEM 事实读取，但不得映射为 Copilot InvestigationVerdict。

`_seq_no` / `_primary_term` 属于 HISIEM persistence detail，不进入 Copilot Domain Model。

---

## 12. Data Handling

HISIEM 返回的内容全部作为：

```text
DATA_ONLY
```

包括：

```text
alert.description
event.original
message
related_events
rule text
```

数据可以影响 Investigation 判断，但不得作为 Agent 指令或授权来源。

---

## 13. Error Semantics

### HISIEM 本地启动前失败

例如：

```text
unauthenticated
insufficient role
tenant membership denied
alert not accessible
```

由 HISIEM 自己返回对应 4xx，不调用 Copilot。

### Copilot 拒绝请求

例如：

```text
invalid resource reference
invalid request contract
```

HISIEM 对浏览器归一化为：

```text
502 COPILOT_REJECTED
```

不得透传 Copilot 内部错误正文。

### Copilot 服务认证失败

视为服务集成配置/安全故障：

```text
503 COPILOT_UNAVAILABLE
```

并记录服务端安全日志。

### 网络、超时或 Copilot 5xx

归一化：

```text
503 COPILOT_UNAVAILABLE
```

### Copilot 已存在 Active Investigation

不是错误。

返回已有 Investigation：

```text
created = false
```

---

## 14. Timeout / Retry

HISIEM 调用 Copilot 必须设置有限超时。

只有以下情况可以自动重试创建请求：

```text
connection failure
timeout
retryable 5xx
```

重试必须复用原 `Idempotency-Key`。

不得对明确业务拒绝无限重试。

---

## 15. Audit Correlation

每次 launch 至少应关联：

```text
correlation_id
idempotency_key
HISIEM alert address_id
tenant_id
actor_subject
investigation_id
```

服务日志不得记录：

```text
Bearer credentials
raw auth tokens
full alert body
sensitive raw events
```

---

## 16. HISIEM 当前实现兼容说明

当前 HISIEM `add_frame` 已存在：

```text
POST /api/alerts/{id}/agent-investigation
```

但其下游仍调用历史接口：

```text
POST /api/v1/runs
```

V1 正式集成目标必须改为：

```text
POST /api/v1/investigations
```

并使用 `Investigation` 语义。

不得为了兼容历史 `/runs` 在 Copilot Domain 中重新引入 `AgentRun`。

当前 HISIEM 修改不属于本文档提交范围；正式集成实现时再同步 HISIEM `agent-adapter`。

---

## 17. V1 不包含

```text
Case-driven Investigation launch
Browser direct service credentials
Alert body replication
Generic Prompt launch
Cross-tenant launch
Multi-SIEM routing
Automatic alert status/verdict update
SOAR execution through launch API
```

---

## 18. 最终契约

```text
Authenticated HISIEM User
        ↓
HISIEM validates RBAC + Tenant
        ↓
Authenticated server-to-server launch
        ↓
TrustedContext + Alert ExternalResourceRef
        ↓
SOC Copilot Investigation
        ↓
Copilot re-hydrates authoritative Alert from HISIEM
```

最高规则：

> **HISIEM 负责证明“谁、在哪个 Tenant、针对哪个 Alert 发起调查”；Copilot 负责调查本身。资源数据和身份权威都不得由模型或浏览器自行声明。**
