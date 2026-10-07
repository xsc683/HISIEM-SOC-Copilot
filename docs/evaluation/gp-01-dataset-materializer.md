# GP-01 数据集物化器契约

## 1. 范围

本文档定义 GP-01 Dataset Materializer 契约。（来源：作为工作步骤 `E1-B.3` 交付；那个代号是工程史，
不是这里任何东西的名字。）

物化器把已提交的逻辑 GP-01 场景变成真实的 HISIEM 资源，并解析出它们的 provider 身份以供后续评估
使用。

```text
Committed GP-01 Scenario
        ↓
Dataset Materializer
        ↓
Real HISIEM ingestion
        ↓
Real event resolution
        ↓
Real alert resolution
        ↓
Dataset verification
        ↓
VerifiedDataset
```

物化器不跑 Copilot 调查、不决定判定、不给结果打分、也不封存评估清单。

## 2. 权威边界

物化器必须以 HISIEM 作为已物化资源的真相来源。

它不得：

- 直接写 Elasticsearch；
- 直接向 Kafka 生产；
- 直接写 `siem-alerts`；
- 写 Copilot 领域状态；
- 在本地推导 provider 寻址标识符；
- 把评估 oracle 事实暴露给 Copilot 运行时。

GP-01 事件注入必须经由 HISIEM 所用的真实 SSH 日志摄取路径进入。预定路径是：

```text
TCP SSH log input
→ HISIEM SSH parser
→ siem-events-*
→ detection pipeline
→ siem-alerts
```

只有当 provider 资源能通过 HISIEM 支持的读接口解析到时，才被认为已物化。

## 3. GP-01 逻辑数据集

GP-01 含六个语义事件和一个控制事件。

语义事件：

- `F1` —— SSH 认证失败
- `F2` —— SSH 认证失败
- `F3` —— SSH 认证失败
- `F4` —— SSH 认证失败
- `F5` —— SSH 认证失败
- `S1` —— 失败序列之后的 SSH 认证成功

`F1` 到 `F5` 必须共享：

- `source.ip`；
- `user.name`；
- `host.name`；
- `event.category = authentication`；
- `event.action = authentication_failure`；
- `event.outcome = failure`。

`S1` 必须共享同样的来源、账号与主机，并且必须满足：

- `event.category = authentication`；
- `event.action = authentication_success`；
- `event.outcome = success`；
- `S1.timestamp > max(F1..F5.timestamp)`。

场景的 ground truth 是：一次暴力破解序列之后，同一个实体出现了一次成功认证。预期判定数据属于评估
oracle，不得传给调查运行时。

## 4. watermark 控制事件

当所部署的检测运行时需要推进事件时间才能让相关窗口关闭时，物化器必须生成一个控制事件 `W1`。

`W1` 存在的唯一目的是推进检测处理。它必须：

- 被归类为 `WATERMARK_CONTROL`；
- 使用一个与 GP-01 攻击实体不同的实体；
- 使用一个不同的 `source.ip`；
- 不满足任何 GP-01 证据要求；
- 不被纳入语义 ground truth；
- 不被注入 Copilot 图状态或 prompt。

该事件仍然是一个真实的 HISIEM 事件，因此如果某次调查做了刻意宽泛的检索，它可能被观测到。评估打分
必须把它归类为控制数据，且不得允许它满足 GP-01 证据要求。

## 5. 运行身份

每次物化都创建一个唯一的 `run_id` 以及由它派生的确定性短 `run_tag`。

运行期特定的实体必须从 `run_id` 派生，好让并发或重复的评估运行不会共用同一个检测身份。

至少，绑定后的场景必须包含：

```text
run_id
run_tag
attack_source_ip
watermark_source_ip
user_name
host_name
event timestamps
rendered SSH log lines
```

必需不变式：

```text
attack_source_ip != watermark_source_ip
```

不同的运行身份应当派生出不同的攻击实体，好让此前的检测抑制状态无法污染一次新的运行。

## 6. 时间计划

逻辑场景必须在物化时绑定到过去的时刻。它不得生成未来事件。

推荐计划：

```text
anchor = floor(now - safe_history_offset)

F1 = anchor + 10s
F2 = anchor + 20s
F3 = anchor + 30s
F4 = anchor + 40s
F5 = anchor + 50s
S1 = anchor + 70s
W1 = anchor + 7m
```

确切偏移量可以是配置常量，但下列不变式是强制的：

- 所有失败事件都在所配置的暴力破解检测区间内；
- `S1` 发生在所有失败事件之后；
- `W1` 发生在相关检测窗口关闭边界之后；
- 在注入开始时，所有生成的时刻都已过去。

SSH syslog 渲染必须使用所部署 HISIEM 解析器预期的时区。对当前 GP-01 环境，这是 `Asia/Shanghai`。

由于 SSH syslog 形式不携带年份，物化器必须拒绝任何跨越自然年边界的时间计划，而不是去猜解析器的
年份补全行为。

失败码：

```text
EVENT_PLAN_CROSSES_YEAR_BOUNDARY
```

## 7. 逻辑身份与 provider 身份

场景身份与 HISIEM provider 身份是两个不同的概念。

像 `F1` 这样的逻辑事件身份不得被当作 Elasticsearch 文档 id。

一条已解析的事件引用必须使用 HISIEM 返回的值：

```text
provider = hisiem
index = real _index
document_id = real _id
```

来源告警引用必须使用 HISIEM 告警详情 API 实际接受的标识符：

```text
provider = hisiem
resource_type = alert
address_id = real HISIEM alert addressing id
business_id = optional business alert id
```

对当前 HISIEM 实现，告警寻址用的是 Elasticsearch 文档 `_id`。物化器必须从 HISIEM 解析它，且不得从
`alert.id`、`run_id`、事件 id、时间戳或哈希推断它。

## 8. 物化器模型

评估包应当暴露与下列等价的 provider 中立类型：

```text
ScenarioSpec
RunIdentity
BoundScenario
LogicalEvent
RenderedEvent
InjectedEvent
ResolvedEvent
ResolvedAlert
MaterializationDraft
VerifiedDataset
```

`ResolvedEvent` 必须只包含用来证明场景身份与供后续打分所需的、有界的归一化字段，包括：

```text
logical_role
provider
index
document_id
timestamp
event_category
event_action
event_outcome
source_ip
user_name
host_name
message_fingerprint
```

它不得把完整的 Elasticsearch 文档持久化成常规评估表示。

`ResolvedAlert` 应当包含：

```text
provider
address_id
business_id?
rule_id
rule_name?
source/entity
created_at
event_count
status
related_event_refs[]
```

## 9. 状态机

物化必须使用一个显式状态机：

```text
NEW
 ↓
PREFLIGHTED
 ↓
EVENTS_RENDERED
 ↓
EVENTS_INJECTED
 ↓
EVENTS_RESOLVED
 ↓
ALERT_RESOLVED
 ↓
VERIFIED
 ↓
MATERIALIZED
```

失败状态：

```text
FAILED
INDETERMINATE
```

对于无法证明服务端结果的非幂等注入，必须有 `INDETERMINATE`。

## 10. 预检

在所有预检通过之前，不得发生任何写入。

必需检查：

### 10.1 HISIEM 可达性

所配置的 HISIEM 控制/读表面必须可达。

### 10.2 租户有效性

评估租户必须能用所配置的可信测试凭据读取。

### 10.3 检测规则契约

所部署的 SSH 暴力破解规则必须匹配 GP-01 所需的场景假设。至少核验生效的规则身份、启用状态、关键
字段、失败谓词、阈值与检测窗口。

任何实质性的语义不匹配都必须以如下失败：

```text
RULE_CONTRACT_MISMATCH
```

评估不得为了让 GP-01 适配一条变过的检测规则而静默调整它。

### 10.4 运行冲突

在注入之前，检索有界的 GP-01 时间/实体作用域。

- 已经属于同一个 `run_id` 的资源进入对账/续跑；
- 与另一个运行身份冲突的资源以 `RUN_IDENTITY_COLLISION` 失败。

### 10.5 时间有效性

时间计划必须通过过去时刻、检测窗口与年边界这三组不变式。

## 11. 注入协议

注入顺序是固定的：

```text
F1
F2
F3
F4
F5
S1
W1
```

调用方不得重排事件。

每次尝试注入都必须记录有界的审计数据：

```text
logical_role
attempted_at
payload_sha256
socket_target
write_status
```

密钥与授权材料不得记录。

渲染出的日志行应当携带一个只属于物化器的关联指纹，使用那些在 HISIEM 事件表示里能存活下来的字段，
例如 host、timestamp、来源地址、进程 id 与 action。这个指纹只是解析辅助；它不会变成 provider 身份。

## 12. 非幂等 TCP 规则

TCP 注入在结果有歧义之后不得盲目重试。

如果客户端在一次写入尝试之后无法确定某个渲染事件是否被接受：

```text
state = INDETERMINATE
```

用同一个 `run_id` 重跑必须默认为对账与解析。它不得自动重发一个已经尝试过的事件。

如果既有运行无法对账到一个无歧义的 provider 数据集，该运行作废，并且需要一个新 `run_id`。

这条规则防止一次不确定的重试，把五事件的失败序列变成六事件序列。

## 13. 事件解析

注入之后，事件必须经由 HISIEM 结构化日志检索 API 解析。常规物化器行为禁止直接查询
Elasticsearch。

对 `F1..F5`、`S1` 与 `W1` 各一次：

```text
0 valid matches  → continue bounded polling
1 valid match    → resolve provider reference
>1 valid matches → AMBIGUOUS_EVENT
```

解析必须至少校验：

- `_index`；
- `_id`；
- `@timestamp`；
- `event.category`；
- `event.action`；
- `event.outcome`（若存在）；
- `source.ip`；
- `user.name`；
- `host.name`；
- `log.source_id`（若存在）；
- 关联指纹字段（若存在）。

七个事件必须全部解析成功，才能开始封存告警。

## 14. 告警解析

告警解析只在事件解析成功之后开始。

物化器必须使用 HISIEM 告警 API，并且必须把候选告警与当前运行对照校验。候选筛选必须包含预期的检测
规则与攻击实体，并且在可用时应当包含当前运行的时间与关联事件约束。

解析语义：

```text
0 valid candidates  → continue bounded polling
1 valid candidate   → resolve
>1 valid candidates → AMBIGUOUS_SOURCE_ALERT
```

物化器不得通过挑最新告警、最高风险分或任意第一条结果来解决歧义。

选定候选之后，它必须用已解析的寻址标识符读取该告警详情，并再次核验同样的那些不变式。

## 15. 告警稳定屏障

当检测管线可能继续更新同一条逻辑告警时，第一条可见告警不一定是稳定的。

在校验之前，物化器必须建立一个有界的稳定屏障。一个合适的实现是重复读取，直到在连续配置次数次观测
中看到同一个稳定指纹。

指纹应当包含：

```text
address_id
rule_id
source/entity
event_count
related-event identity set
status
```

如果在配置的截止时间之前无法建立稳定：

```text
ALERT_NOT_STABLE
```

数据集不得被校验或封存。

## 16. 数据集校验

只有当所有强制不变式都成立时，物化器才能产出 `VerifiedDataset`。

### 16.1 事件不变式

```text
count(F1..F5) = 5

∀F:
  action = authentication_failure
  same source
  same account
  same host

S1:
  action = authentication_success
  same source
  same account
  same host
  timestamp > max(failure timestamps)

W1:
  classification = WATERMARK_CONTROL
  source != attack source
```

### 16.2 检测不变式

```text
source alert exists
source alert rule matches GP-01 rule
source alert entity matches attack entity
source alert represents the required failure threshold
```

如果 HISIEM 暴露了关联事件引用，校验器应当把它们与已解析的失败事件引用交叉核对。

### 16.3 寻址不变式

```text
source_alert.address_id == actual HISIEM alert API addressing id
```

### 16.4 隔离不变式

已解析的 provider 资源必须无歧义地属于当前物化身份，且不得混入另一次运行的事件。

## 17. 物化草稿

当前运行应当维护一份可变的本地运行账本：

```text
.eval-runs/
  gp-01/
    <run_id>/
      materialization.json
```

草稿记录状态、已尝试的注入、解析进度，以及续跑/对账所需的失败诊断。

它不是评估清单，且不得被评分器当作 ground truth 消费。

生成的 `.eval-runs/` 产物应当排除在 Git 之外。

## 18. 续跑语义

一次续跑操作可以：

- 读 `materialization.json`；
- 向 HISIEM 查询已经尝试过的资源；
- 完成缺失的事件解析；
- 完成告警解析；
- 重跑数据集校验。

一次续跑操作不得自动重新注入此前已尝试过的事件。

## 19. 错误分类

实现应当暴露与下列等价的类型化失败：

```text
PreflightError
RuleContractMismatch
RunIdentityCollision
EventInjectionError
InjectionOutcomeIndeterminate
EventResolutionTimeout
AmbiguousEventError
AlertResolutionTimeout
AmbiguousSourceAlertError
AlertNotStableError
DatasetInvariantViolation
```

评估失败必须保留足够的有界结构化上下文以供诊断，同时不持久化密钥或原始凭据材料。

## 20. 包边界

推荐的包形状：

```text
src/hisiem_soc_copilot/evaluation/
├── contracts.py
├── scenario_loader.py
├── identity.py
├── time_plan.py
├── injector.py
├── hisiem_reader.py
├── materializer.py
├── verifier.py
└── cli.py
```

评估包可以依赖生产的公开契约与 adapter。生产领域、application、图与 provider 包不得依赖评估 oracle
代码。

## 21. 测试契约

默认的单元测试与集成测试不得改动真实的 HISIEM 部署。

真实物化测试必须被显式启用，例如：

```text
RUN_HISIEM_DATASET_EVAL=1
```

活的 GP-01 物化器测试必须证明：

1. `F1..F5` 解析到真实的 HISIEM 事件文档；
2. `S1` 解析到一个真实的成功事件；
3. `W1` 作为一个独立的控制事件解析到位；
4. 真实的 SSH 暴力破解告警出现；
5. 告警引用使用实际的 HISIEM 寻址 `_id`；
6. 告警无歧义地与当前运行关联；
7. `DatasetVerifier` 返回 `VerifiedDataset`。

单元测试必须至少覆盖：

- 确定性时间计划；
- 失败窗口不变式；
- `S1` 的次序；
- `W1` 的实体分离；
- 年边界拒绝；
- 按 `run_id` 的确定性身份绑定；
- 固定的注入顺序；
- 不确定 TCP 结果之后不重试；
- 不注入的续跑；
- 歧义事件拒绝；
- 歧义告警拒绝；
- 禁止从 `alert.id` 推 `address_id`；
- `S1` 不匹配攻击实体时的校验失败。

## 22. 完成闸门

只有当真实环境证明下列全部时，物化器才算完成：

```text
Logical GP-01
→ real SSH-log ingestion
→ seven resolved HISIEM events
→ real SSH brute-force alert
→ exact real alert addressing id
→ DatasetVerifier PASS
→ VerifiedDataset
```

一个没有这些证明就跑完的脚本，不足以宣称这个阶段完成。
