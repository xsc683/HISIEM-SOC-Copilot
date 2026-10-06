# Stage C — Capability / MCP Implementation Spec

**Status:** ACTIVE  
**Version:** 1.1  
**Architecture Authority:** `00_Four-Plane_Architecture_Contract_Freeze.md`  
**Required Base:** Stage B sealed baseline  
`ca927b31702d05f0f844dd3949cb4271ee6ba7e9`

## 模块1：目标

建立统一的 **Tool Provider Architecture**。

MCP 只是第一种 External Provider，不是新的 Tool Governance 系统。

- Provider 负责 raw capability discovery、transport、invocation。
- Admission / Registry 负责 trusted capability contract。
- ToolPolicy / ToolBudget 负责运行时治理。
- Agent 不得直接调用 MCP。
- MCP 不得建立第二套 Evidence、Audit、Authorization。
- V1 仅支持 read-only external capability。

## 模块2：Stage C 范围

### 实现

- ToolProvider abstraction
- NativeToolProvider thin adapter
- MCPToolProvider
- trusted MCP server configuration
- complete paginated capability discovery
- normalization + admission
- external/internal schema separation
- external schema fingerprint
- provider routing
- read-only enforcement
- tenant/auth propagation
- result validation + bounds
- typed provider failures
- EvidenceNormalizer integration
- existing audit integration
- Stage B OpenTelemetry integration
- deterministic MCP contract tests
- real Streamable HTTP integration tests

### 不实现

- MCP write execution
- stdio transport
- MCP Tasks
- MRTR / InputRequired interaction flow
- generic OAuth/CIMD platform
- second Tool Registry
- second authorization system
- second Evidence model
- second audit/truth store
- Stage D frontend work

## 模块3：Git Baseline Contract

Stage C implementation MUST contain:

```text
ca927b31702d05f0f844dd3949cb4271ee6ba7e9
```

执行前：

```text
verify ancestry
→ verify clean baseline
→ create/reuse Stage C feature branch
```

如果当前 HEAD 不包含 Stage B sealed baseline：

```text
DO NOT START STAGE C IMPLEMENTATION
```

不得从更早的 Knowledge-only Stage A baseline 开始。

## 模块4：Discovery / Admission Architecture

```text
Trusted MCP Server Config
          ↓
High-level MCP Client
          ↓
Complete paginated tool discovery
          ↓
Raw ProviderCapability
          ↓
Normalization
          ↓
Admission Manifest
          ↓
ToolRegistry
```

Discovery 只回答：Server 声称暴露了什么能力。

```text
Discovered
!= Admitted
!= Model-selectable
```

## 模块5：Runtime Architecture

```text
Agent
  ↓
ToolRegistry
  ↓
ToolPolicy
  ↓
ToolBudget
  ↓
ToolExecutor
  ↓
Provider Router
  ├── NativeToolProvider
  │        ↓
  │      HISIEM
  │
  └── MCPToolProvider
           ↓
      Trusted MCP Server
           ↓
   ProviderInvocationResult
           ↓
        ToolResult
           ↓
   EvidenceNormalizer
           ↓
        Evidence
```

Agent MUST NOT invoke MCP directly.

## 模块6：Authority Model

```text
MCP Server
= raw capability source

ToolProvider
= discovery / transport / invocation adapter

Admission Manifest
= trusted capability contract

ToolRegistry
= model-visible catalog authority

ToolPolicy
= runtime permission authority

ToolBudget
= resource-use authority

Human Authority
= write/high-risk authorization authority

Evidence
= durable investigation fact
```

Provider 不拥有 authorization authority。

## 模块7：ToolProvider Contract

Provider 负责：

- raw capability discovery / metadata
- provider identity
- invocation
- transport handling
- external schema observation
- provider failure normalization

Provider 不负责：

- model authorization
- tenant authorization decision
- Registry admission
- ToolPolicy
- ToolBudget
- Human Approval
- Verdict
- Response authorization

## 模块8：NativeToolProvider

采用 thin adapter，不重写 Native Tool stack。

现有 model-selectable behavior 保持不变：

```text
hisiem.search_events
hisiem.get_detection_rule
knowledge.retrieve_security_guidance
knowledge.resolve_attack_technique
```

System-controlled tools 继续保持 system-controlled。

## 模块9：MCP Client / Transport

使用：

- Official MCP Python SDK
- high-level MCP Client
- Streamable HTTP transport

默认不以 `ClientSession` / manual `initialize()` 作为架构中心。

Low-level session API 只有在 high-level Client 无法满足真实需求时才允许使用。

Stage C V1 不支持 stdio transport。

## 模块10：MCP Protocol Policy

Production V1：

```text
required protocol:
2026-07-28
```

Legacy downgrade：

```text
FAIL CLOSED BY DEFAULT
```

不得依赖 SDK silent fallback 让旧协议服务器进入生产。

未来 legacy 支持必须具备：

- trusted per-server config
- explicit protocol allowlist
- security review
- regression coverage
- contract update

Unsupported protocol：

```text
PROTOCOL_ERROR
```

## 模块11：MCP Server Identity

Configured `server_id` 是 Copilot trust identity，只能来自 Trusted Runtime Configuration。

Server-reported `name`、`version`、`server_info`、`instructions` 只属于 untrusted consistency / diagnostic metadata。

不得控制：

- trust
- admission
- authorization
- model-visible identity
- tenant routing
- risk classification

Unknown/unconfigured server：

```text
FAIL CLOSED
```

## 模块12：Endpoint Security

Production remote MCP endpoint 默认必须使用 HTTPS。

例外：

```text
explicit trusted internal transport
```

Endpoint destination 只能来自 trusted config。

模型、用户输入、Tool args、MCP result 不能控制：

- scheme
- host
- port
- transport
- redirect target

安全要求：

- unexpected cross-origin redirect → reject
- unexpected host change → reject
- no model-controllable SSRF path
- production egress allowlist preferred

## 模块13：Authentication

只实现当前 admitted server 实际需要的 auth mode。

Credentials 只能来自 existing trusted secret/config mechanism，且永远不能进入：

- Model
- ToolResult
- Evidence
- Audit payload
- Logs
- Telemetry

上游支持 scope 时：

```text
production credential
→ least privilege
→ read-only
```

Admission classification 不能替代 upstream permission restriction。

## 模块14：Capability Discovery

Discovery 必须遍历完整 pagination：

```text
list tools
→ collect page
→ next cursor?
   ├── yes → continue
   └── no  → complete catalog
```

不能假设第一页就是完整 Catalog。

Server description / annotations 永远是 untrusted metadata。

## 模块15：Catalog Refresh

V1：

```text
provider startup
→ full discovery + revalidation
```

以及：

```text
configured refresh
→ full rediscovery + revalidation
```

变化处理：

```text
new tool
→ discovered but unadmitted

missing admitted tool
→ unavailable

schema drift
→ SCHEMA_MISMATCH
```

Remote Catalog 不得自动修改 model-visible Registry。

## 模块16：Admission Manifest

Trusted Admission Manifest 至少定义：

- internal tool name
- trusted model-visible description
- server_id
- external tool name
- internal input contract
- expected external schema fingerprint
- classification
- risk
- tenant scope
- result contract
- result bounds

分类：

```text
model-selectable
system-controlled
future catalog
forbidden
```

Dynamic new tool 默认：

```text
NOT model-selectable
```

## 模块17：External vs Internal Schema

### External Provider Schema

来自 MCP Server：

```text
external tool name
inputSchema
outputSchema / explicit absent state
```

### Internal Model-visible Schema

来自 Copilot Admission：

```text
trusted internal tool name
trusted description
model-visible arguments only
```

Protected runtime field 不能暴露给模型。

Invocation：

```text
validated model args
+
trusted runtime context
        ↓
provider argument builder
        ↓
external invocation args
        ↓
external schema validation
        ↓
MCP call
```

## 模块18：External Schema Fingerprint

字段：

```text
external_schema_fingerprint
```

计算：

```text
external tool name
+
canonical inputSchema
+
canonical outputSchema / explicit ABSENT
        ↓
deterministic canonical JSON
        ↓
SHA-256
```

用途：检测 canonical representation drift，不作为 semantic JSON Schema equivalence proof。

Unexpected fingerprint change：

```text
SCHEMA_MISMATCH
→ capability unavailable
→ fail closed
```

Internal contract 独立版本化。

## 模块19：Read-only V1

Read-only authority 来自 trusted Admission Policy。

不能根据以下信息推断：

- tool name
- `readOnlyHint`
- server description
- provider annotation

Write/high-risk capability 不得 model-selectable。

未来 write path：

```text
Response Proposal
→ Policy
→ Human Approval
→ Durable Execution
```

禁止：

```text
Agent → MCP write → execute
```

## 模块20：Trusted Tenant Context

Tenant 只来自 existing Trusted Runtime Context。

Capability scope：

```text
GLOBAL_READ_ONLY
TENANT_SCOPED
```

TENANT_SCOPED 缺失 tenant：

```text
FAIL CLOSED
```

如果 external schema 需要 tenant：

```text
model-visible args
+
trusted tenant
→ provider-side injection
```

禁止 fallback 到 GLOBAL/default tenant。

## 模块21：Runtime Invocation Contract

必须保持：

```text
Agent
→ ToolRegistry
→ ToolPolicy
→ ToolBudget
→ ToolExecutor
→ Provider Router
→ MCPToolProvider
→ MCP Server
→ ProviderInvocationResult
→ ToolResult
→ EvidenceNormalizer
```

硬性约束：

```text
Policy DENY
→ provider not invoked
```

```text
Budget exhausted
→ provider not invoked
```

## 模块22：MCP Result Processing

处理顺序固定：

```text
protocol / result type
        ↓
InputRequired / unsupported interaction?
        ↓
is_error?
        ↓
supported content type?
        ↓
structured contract validation
        ↓
result bounds
        ↓
ProviderInvocationResult
```

不能先信任 `structured_content`。

## 模块23：Terminal Result Policy

Stage C V1 只支持：

```text
complete terminal result
```

不支持：

```text
InputRequiredResult
MRTR
MCP Tasks
```

统一：

```text
UNSUPPORTED_INTERACTION
```

不实现：

- elicitation
- sampling callback loop
- MRTR state machine
- task lifecycle/polling

## 模块24：is_error Handling

如果：

```text
is_error = true
```

则必须：

```text
REMOTE_TOOL_ERROR
```

绝不能形成 successful ToolResult。

## 模块25：Result Contract

V1 支持：

- validated structured JSON
- explicitly allowed bounded text

Structured content 必须再次经过 trusted internal result contract validation。

不支持：

- image
- audio
- binary
- arbitrary resource attachment
- executable/local-resource content

统一：

```text
INVALID_RESULT
```

所有 MCP content：

```text
UNTRUSTED DATA
```

Prompt-injection-like result 不得改变 Policy / Authorization / Human Authority。

## 模块26：Result Bounds

采用：

```text
Global Hard Limits
+
Per-capability Limits
```

至少限制：

- timeout
- item count
- serialized bytes
- individual text size
- nested depth

Per-capability limit 不得超过 Global Hard Limit。

超限：

```text
RESULT_TOO_LARGE
```

或 contract-defined safe truncation。

禁止 silent truncation。

## 模块27：Failure Taxonomy

```text
TIMEOUT
UNAVAILABLE
AUTH_FAILURE
RATE_LIMITED

REMOTE_TOOL_ERROR
PROTOCOL_ERROR
UNSUPPORTED_INTERACTION

SCHEMA_MISMATCH
INVALID_RESULT
RESULT_TOO_LARGE

PROVIDER_ERROR
```

Agent 只能看到 safe typed failure。

不得暴露 raw stack trace、SDK exception repr、credential、authenticated raw error response。

MCPProvider 不建立第二套 hidden retry policy。

## 模块28：Evidence Path

Evidence-producing MCP capability 必须走：

```text
ProviderInvocationResult
→ ToolResult
→ EvidenceNormalizer
→ existing immutable Evidence
```

禁止新增：

```text
MCPEvidence
ExternalEvidence
ProviderEvidence
```

## 模块29：Audit

复用现有 invocation/audit mechanism。

可记录：

- internal tool identity
- provider_type
- configured server identity
- investigation identity
- trusted tenant scope
- attempt
- status
- safe error category
- external schema fingerprint
- timestamps

不得记录 credentials、authorization headers、secret transport data、raw provider exceptions。

## 模块30：Observability

复用 Stage B。

Trace：

```text
tool.execute
└── mcp.call
```

Metrics：

```text
mcp.calls
mcp.duration
mcp.errors
```

低基数 metric labels：

- tool_name
- tool_provider
- result
- error_category
- server_category

禁止 metric labels：

- server_id
- tenant_id
- investigation_id
- trace_id
- tool_invocation_id
- schema fingerprint

## 模块31：Test Infrastructure

### In-process Contract Tests

覆盖：

- discovery
- pagination
- admission
- schema separation
- schema fingerprint
- dynamic tool
- write tool
- is_error
- InputRequired
- malformed result
- oversized result
- prompt-injection payload

### Real Streamable HTTP Integration

至少验证一次：

```text
Copilot MCPToolProvider
→ Streamable HTTP
→ deterministic MCP test server
```

覆盖：

- protocol
- transport
- auth propagation
- timeout
- unavailable server
- endpoint / redirect policy
- real tool invocation
- `mcp.call` telemetry

不得依赖公网 MCP Server 完成 acceptance。

## 模块32：Required Validation

必须验证：

- Stage C HEAD contains sealed Stage B baseline
- trusted server discovery
- unknown server rejected
- configured server_id is trust authority
- self-reported identity cannot elevate trust
- full pagination handled
- dynamic tool not automatically model-selectable
- admitted read-only tool callable
- forbidden/write tool hidden
- protocol policy enforced
- silent legacy downgrade impossible
- model cannot select server / credential / endpoint
- tenant spoof rejected
- missing trusted tenant fails closed
- external/internal schema remain distinct
- schema drift fails closed
- `is_error` → `REMOTE_TOOL_ERROR`
- InputRequired/MRTR → `UNSUPPORTED_INTERACTION`
- Tasks unsupported
- malformed protocol → `PROTOCOL_ERROR`
- malformed result → `INVALID_RESULT`
- oversized result → `RESULT_TOO_LARGE`
- timeout → `TIMEOUT`
- unavailable → `UNAVAILABLE`
- Policy DENY → provider not invoked
- Budget exhausted → provider not invoked
- prompt injection remains DATA
- EvidenceNormalizer path unchanged
- telemetry secret-free
- Native Tool regressions green
- Knowledge Tool regressions green
- Stage B observability regressions green

还需运行 repository-defined：

- architecture tests
- strict mypy
- ruff
- `git diff --check`

DB / migration suites仅在 Stage C 修改 persistence semantics 时运行。

## 模块33：Acceptance

Stage C PASS 必须证明：

```text
Trusted MCP Configuration
→ high-level MCP Client
→ complete paginated Discovery
→ Normalization
→ Explicit Admission
→ ToolRegistry
→ ToolPolicy
→ ToolBudget
→ ToolExecutor
→ MCPToolProvider
→ terminal successful MCP result
→ ProviderInvocationResult
→ ToolResult
→ EvidenceNormalizer
→ Evidence
```

同时满足：

- Stage C 包含 sealed Stage B baseline
- protocol downgrade policy enforced
- unknown server fail closed
- dynamic tool 不自动注册
- schema drift fail closed
- external/internal schema 分离
- write/high-risk tool 对 Agent 不可用
- tenant 不可 spoof
- upstream credential 最小权限
- endpoint / redirect / SSRF boundary 生效
- `is_error` 不能成为 success
- MRTR / InputRequired / Tasks fail closed
- result bounds 生效
- Policy / Budget 不可绕过
- real Streamable HTTP verified
- existing Evidence / Audit boundaries reused
- Stage B OTel contract intact
- Native Tool behavior stable

只有全部通过：

```text
STAGE C: PASS
```

任一 required invariant 未验证：

```text
STAGE C: FAIL
```

Stage C 完成后停止，不进入 Stage D。
