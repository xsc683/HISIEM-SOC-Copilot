# HISIEM SOC Copilot V1 — Investigation Tool Contract

## 1. 目的

本文定义 V1 Investigation 可调用能力的 Tool Contract，包括：

- Tool 分类与权限。
- 统一输入/输出边界。
- Tenant / Auth / Budget 注入规则。
- HISIEM Read Tool 契约。
- 外部 Evidence Tool 契约。
- Tool Result → Evidence 转换规则。
- 错误、截断、重试和安全边界。

V1 Tool 只服务于：

```text
Alert-driven read-only Investigation
```

---

## 2. 总体原则

```text
Model chooses candidate operation.
System binds authority.
Tool executor validates policy.
Provider returns data.
Evidence normalizer creates Evidence.
```

标准调用链：

```text
LLM Tool Candidate
      ↓
Schema Validation
      ↓
Authenticated Scope Binding
      ↓
Tool Policy / Budget Validation
      ↓
Provider Adapter
      ↓
Typed Tool Result
      ↓
Evidence Normalizer
      ↓
Evidence
```

LLM 不得直接调用 Provider SDK、HTTP Client、Database 或 Elasticsearch。

---

## 3. Tool 风险边界

V1 Agent Registry 只允许：

```text
READ_ONLY
```

禁止注册：

```text
WRITE
SIDE_EFFECT
ARBITRARY_HTTP
SHELL
SCRIPT
RAW_ES_DSL
DATABASE_QUERY
```

所有安全环境变更必须走：

```text
InvestigationResult
→ ResponseProposal
→ Policy
→ Human Approval
→ HISIEM SOAR
```

不得通过 Investigation Tool 绕过该链路。

---

## 4. Tool 分类

### System-controlled Reads

由 Graph / Application 决定，不允许模型自由选择：

```text
hisiem.get_alert_context
```

用于 authoritative hydration。

### Agent-selectable Read Tools

V1 Allowlist：

```text
hisiem.search_events
hisiem.get_entity_activity
hisiem.get_detection_rule
threat_intel.lookup_ip
knowledge.retrieve_security_guidance
knowledge.resolve_attack_technique
```

未注册名称全部拒绝。

---

## 5. ToolExecutionContext

以下 Context 由系统注入，不属于模型参数：

```text
ToolExecutionContext

investigation_id
tenant_id
source_alert_ref
correlation_id
tool_call_id
budget_state
```

Provider Credential、Actor Authority、Tenant Scope 由 Infrastructure / Application 决定。

模型参数中禁止：

```text
tenant_id
actor
role
permission
authorization header
service credential
provider base URL
```

---

## 6. Tool Candidate

模型只产生：

```text
ToolCandidate

tool_name
arguments
reason_summary?
```

`reason_summary` 仅用于可观察性，不具备授权语义。

每个 Tool 使用独立严格 Schema。

禁止通用：

```text
execute(tool_name, dict[str, Any])
```

作为最终执行边界。

---

## 7. Tool Result Envelope

统一输出：

```text
ToolResult

tool_call_id
tool_name
status
fetched_at

data
source_refs[]
truncated
continuation?

error?
```

Status：

```text
SUCCESS
NO_DATA
PARTIAL
REJECTED
UNAVAILABLE
```

`data` 必须是该 Tool 的 Typed Result，而不是任意 Provider JSON。

---

## 8. Error Contract

标准 Error Code：

```text
INVALID_ARGUMENT
POLICY_REJECTED
RESOURCE_NOT_FOUND
UPSTREAM_UNAVAILABLE
TIMEOUT
RATE_LIMITED
UPSTREAM_REJECTED
INTERNAL_ERROR
```

错误返回只保存：

```text
code
safe_message
retryable
```

不得向 LLM、Domain Event 或用户暴露：

```text
credentials
internal stack trace
raw upstream error body
sensitive provider configuration
```

---

## 9. Retry

Tool Executor 只允许对明确 retryable 的基础设施错误进行 bounded retry。

禁止：

```text
unbounded retry
LLM-controlled retry count
retry policy from tool result content
```

所有 retry 计入 Investigation Budget。

---

## 10. Budget

每次 Tool 调用必须计入：

```text
tool_calls_used
steps_used
time budget
```

可选 Provider 还可计入：

```text
cost budget
rate-limit budget
```

达到预算时 Tool Executor 拒绝新调用，Graph 转入 Finalize。

预算耗尽优先形成：

```text
COMPLETED + INCONCLUSIVE
```

而不是 `FAILED`。

---

# 11. `hisiem.get_alert_context`

类型：

```text
SYSTEM_CONTROLLED / READ_ONLY
```

作用：

> 根据 Investigation.source_alert_ref 重新获取 HISIEM 权威 Alert。

模型无输入权限。

System Input：

```text
source_alert_ref
```

Provider Call：

```text
GET /api/alerts/{address_id}
```

Output：

```text
AlertContext

resource_ref
business_id?
created_at?
status?
rule_id?
rule_name?
rule_type?
severity?
description?
risk_score?
entity?
case_id?
rule_tags[]
event_count?
related_events[]
entity_refs[]
```

`address_id` 必须来自 Investigation 的 ExternalResourceRef。

不得使用返回内容中的其他 ID 替换它。

`alert.analyst_verdict` 可以读取为 HISIEM Context，但不得映射为 Copilot Verdict。

---

## 12. Alert Related Events

Alert 中已有的 bounded `related_events` 在 hydration 时直接进入 AlertContext，并由 Evidence Normalizer 选择性转换为 Evidence。

V1 不单独注册：

```text
get_related_events
```

避免同一数据存在两套读取语义。

如果未来 related events 需要分页/独立 API，再增加独立 Tool Contract。

---

# 13. `hisiem.search_events`

类型：

```text
AGENT_SELECTABLE / READ_ONLY
```

Provider：

```text
POST /api/log-search
```

模型输入：

```text
from
to
logic
conditions[]
limit?
sort?
```

Condition：

```text
field
operator
value
```

V1 Agent 允许字段：

```text
event.category
event.action
event.outcome
event.type
source.ip
destination.ip
related.ip
user.name
host.name
log.source_id
message
event.original
```

实际执行前还必须确认字段和 Operator 存在于 HISIEM：

```text
GET /api/log-search/fields
```

Copilot Allowlist 必须是 HISIEM Catalog 的子集。

禁止模型生成 Elasticsearch DSL。

---

## 14. Search Operator

允许：

```text
is
contain
exist
is_one_of
not_is
not_contain
not_exist
not_is_one_of
```

最终可用 Operator 由 HISIEM Field Catalog 再校验。

Logic：

```text
AND
OR
```

---

## 15. Event Search Bounds

Copilot 必须保持比 Provider 能力更有界。

V1 默认：

```text
limit = 100
```

最大：

```text
limit <= 200
```

单次 Agent Tool Search 默认围绕调查时间窗执行。

Copilot Tool Policy 默认限制：

```text
single call time span <= 24h
```

更宽调查必须拆分为多个 bounded query，并受整体 Investigation Budget 限制。

无论 Copilot 配置如何，均不得超过 HISIEM Provider Hard Limits：

```text
max span = 90 days
max conditions = 20
max values per condition = 50
max value length = 512
max page size = 200
max result window = 10000
```

V1 Tool 不向模型开放任意 `page`。

需要更多数据时应缩小时间范围或调整调查条件，而不是无界翻页。

---

## 16. Event Search Result

Output：

```text
EventSearchResult

items[]
total
returned
from
to
took_ms
truncated
```

每个 Item 归一化为：

```text
SecurityEventObservation

provider_event_id?
provider_index?
timestamp?
event_category?
event_action?
event_outcome?
event_type?
source_ip?
destination_ip?
related_ip[]
user_name?
host_name?
log_source_id?
message?
event_original?
```

Provider 返回的其他字段不得默认全部进入 Graph State。

`truncated = total > returned`。

---

## 17. Event Provenance

当前 HISIEM Log Search 返回：

```text
_id
_index
```

但没有公开独立 Event Detail API。

因此 Event Evidence 的 Provenance 使用：

```text
source = HISIEM_LOG_SEARCH
raw_reference = {
  index,
  document_id,
  query_fingerprint
}
```

在 HISIEM 提供稳定 Event Address API 前，不得虚构一个可寻址的 Event `ExternalResourceRef`。

---

# 18. `hisiem.get_entity_activity`

类型：

```text
AGENT_SELECTABLE / READ_ONLY
```

作用：

> 针对 Alert 中已知 IP / User / Host 查询 bounded activity。

模型输入：

```text
entity_type
entity_value
from
to
category?
outcome?
limit?
```

Entity Type：

```text
IP
USER
HOST
```

映射：

```text
USER → user.name
HOST → host.name
IP   → source.ip / related.ip 的受控查询组合
```

该 Tool 由 Adapter 生成结构化 `log-search` 请求。

模型不得自行选择底层字段组合或 DSL。

Output 与 `EventSearchResult` 相同，并按 Event `_id` 去重。

---

## 19. Entity Scope

`entity_value` 必须来自至少一个已存在的可信上下文：

```text
AlertContext
Existing Evidence
Validated Finding context
```

模型不得凭空引入任意组织内部 User/Host 作为调查目标。

外部 IP Candidate 可以来自已有 Evidence / Threat Intel 关联，但仍需 Tool Policy 校验。

---

# 20. `hisiem.get_detection_rule`

类型：

```text
AGENT_SELECTABLE / READ_ONLY
```

Provider：

```text
GET /api/detection-rules/{rule_id}
```

输入：

```text
rule_id
```

`rule_id` 必须来自当前 AlertContext 或经过系统解析的已知 Rule Reference。

Output：

```text
DetectionRuleContext

rule_id
name?
type?
status?
version?
tags[]
logic_summary?
```

Adapter 应返回 bounded investigation context，不把任意执行代码或平台内部配置作为 Agent 指令。

---

# 21. `threat_intel.lookup_ip`

类型：

```text
AGENT_SELECTABLE / READ_ONLY / EXTERNAL_EVIDENCE
```

输入：

```text
ip
```

只允许合法 IP literal。

禁止：

```text
URL
hostname
arbitrary query language
provider selection by model
```

Output：

```text
IpReputation

indicator
verdict
confidence?
labels[]
first_seen?
last_seen?
provider
references[]
fetched_at
```

Verdict 归一化为：

```text
MALICIOUS
SUSPICIOUS
BENIGN
UNKNOWN
```

Threat Intel Verdict 只是 External Evidence，不是 Copilot Investigation Verdict。

Provider 不可用时返回 `UNAVAILABLE`；Investigation 应允许继续。

---

# 22. `knowledge.retrieve_security_guidance`

类型：

```text
AGENT_SELECTABLE / READ_ONLY / EXTERNAL_EVIDENCE
```

输入：

```text
topic
context_terms[]?
limit?
```

V1：

```text
limit <= 5
```

Output：

```text
KnowledgeResult

documents[]
```

Document：

```text
document_id
title
excerpt
source
version?
```

知识内容始终：

```text
DATA_ONLY
```

不得将 Runbook / RAG 文档中的命令解释为授权动作。

---

# 23. `knowledge.resolve_attack_technique`

类型：

```text
AGENT_SELECTABLE / READ_ONLY / VALIDATION
```

输入：

```text
technique_id
```

例如：

```text
T1110
T1078
```

Output：

```text
AttackTechniqueRef

framework
technique_id
name
version_or_source?
```

只有 Resolver 成功返回的 Technique 才能进入最终 `InvestigationResult.attack_mappings`。

模型自由生成但未 Resolve 的 ID 不得持久化为正式 ATT&CK Mapping。

---

## 24. ToolResult → Evidence

ToolResult 不直接等于 Evidence。

固定链：

```text
Typed ToolResult
      ↓
Evidence Normalizer
      ↓
Deduplication
      ↓
RecordEvidenceBatch
      ↓
Immutable Evidence
```

Evidence Normalizer 负责：

```text
select bounded observations
attach provenance
attach collection timestamp
create raw reference
calculate content_hash / dedup key
remove provider-only noise
```

模型不得自行设置：

```text
Evidence provenance
source_tool_call_id
collected_at
resource authority
```

---

## 25. Evidence Summary

模型可以对已生成 Evidence 产生：

```text
summary
```

但必须保留原始：

```text
observation
provenance
raw_reference
```

Summary 不能覆盖 Source-derived Observation。

---

## 26. Tool Data Instruction Trust

所有 Tool Result 内容均：

```text
DATA_ONLY
```

包括：

```text
event.original
message
alert.description
Threat Intel text
Knowledge excerpts
Rule text
```

Tool 内容可以影响：

```text
Plan
Hypothesis
Finding Candidate
Verdict Candidate
```

但不能改变：

```text
Tool policy
Tenant scope
Authorization
Approval
Domain state directly
Side-effect permission
```

---

## 27. Tool Observability

每次 Tool Invocation 至少记录：

```text
tool_call_id
investigation_id
tool_name
safe arguments
status
started_at
finished_at
retry count
error code?
result metadata
```

默认不持久化完整 Tool Result。

Domain 需要的事实由 Evidence 持久化。

---

## 28. Sensitive Data

日志和 Evidence 可能包含敏感安全数据。

Tool / Trace / Event 必须避免重复扩散：

```text
credentials
session tokens
API keys
full unrestricted raw logs
provider secrets
```

必要的 `event.original` 只作为调查 Evidence 的 bounded observation 保存，并遵守后续 retention/redaction policy。

---

## 29. V1 Golden Path Tool Set

SSH Brute Force → Possible Account Compromise 的最小工具闭环：

```text
hisiem.get_alert_context
        ↓
hisiem.get_detection_rule
        ↓
hisiem.search_events / get_entity_activity
        ↓
threat_intel.lookup_ip (if provider available)
        ↓
knowledge.resolve_attack_technique
```

典型 Evidence：

```text
repeated SSH failures
successful authentication after failures
post-login privileged activity
source IP reputation
validated ATT&CK references
```

缺失 Threat Intel 或 Knowledge Provider 不得使整个 Investigation 自动 `FAILED`。

---

## 30. V1 明确禁止的 Tool

```text
execute_shell
run_script
raw_http_request
raw_elasticsearch_query
write_alert
set_alert_verdict
close_alert
create_case
modify_case
block_ip
disable_user
isolate_host
start_soar_execution
approve_response
```

这些能力不得作为 Investigation Read Tool 注册。

---

## 31. 最终契约

```text
Model
  ↓ candidate only
Tool Registry
  ↓ allowlisted read capability
Tool Policy
  ↓ tenant / scope / budget validation
Provider Adapter
  ↓ authoritative or external data
Evidence Normalizer
  ↓ grounded immutable evidence
Domain
```

最高规则：

> **Agent 可以自主选择“下一步读取什么”，但不能选择“以谁的权限读取”、不能绕过 Provider 查询边界，也不能通过 Tool 改变安全环境。**
