# GP-01 清单封存器契约

## 1. 范围

本文档定义 GP-01 Manifest Sealer 契约。（来源：作为工作步骤 `E1-B.4` 交付；那个代号是工程史，不是
这里任何东西的名字。）

封存器把一份已校验的物化数据集转换成一成不变的评估清单，供 Golden Path 评估 harness 与评分器消费。

```text
VerifiedDataset
      ↓
ManifestBuilder
      ↓
Canonical Manifest Payload
      ↓
ManifestSealer
      ↓
Immutable SealedManifest
```

封存器不摄取日志、不解析 HISIEM 资源、不跑 Copilot 调查、不调 LLM，也不决定调查结果。

物化器（`E1-B.3`）是唯一负责证明「provider 资源存在且满足 GP-01 物化不变式」的那一步。

## 2. 输入权威

封存器必须只接受由数据集物化器校验边界产出的 `VerifiedDataset`。

它必须拒绝：

- 未校验的 `MaterializationDraft`；
- 部分解析的 provider 资源；
- 不稳定的来源告警；
- 有歧义的来源告警；
- 本地推断出来的 provider 标识符；
- 场景必需不变式失败过的数据集。

API 应当通过类型分离让非法构造难以发生：

```text
MaterializationDraft
        ↓ verify
VerifiedDataset
        ↓ seal
SealedManifest
```

`seal(unverified_dataset)` 不是一个受支持的操作。

## 3. 封存器纯净边界

`ManifestSealer` 不得执行 provider 或模型 IO。

它不得：

- 调 HISIEM；
- 调 ModelProvider；
- 跑 LangGraph；
- 创建或更新 Copilot 领域状态；
- 直接查询 Elasticsearch；
- 执行检测逻辑；
- 改动已物化的数据集。

期望的切分是：

```text
MaterializationVerifier
        ↓
VerifiedDataset
        ↓
ManifestBuilder
        ↓
CanonicalManifest
        ↓
ManifestSealer
        ↓
filesystem persistence
```

对同样的显式输入值，`ManifestBuilder` 与 `ManifestSealer` 应当是确定性的。

## 4. 清单的用途

封存清单有三项职责：

1. 标识一次 GP-01 评估运行所用的那些确切真实 HISIEM 资源；
2. 携带评分器所需的私有评估 oracle；
3. 以密码学方式检出封存之后评估记录的改动。

它不是可运行的 Copilot 载荷，且不得被当作调查上下文。

## 5. 清单 schema

清单必须带版本。

推荐的顶层 schema：

```json
{
  "schema_version": "gp-eval-manifest/v1",
  "scenario": {},
  "run": {},
  "scope": {},
  "entities": {},
  "events": [],
  "control_events": [],
  "source_alert": {},
  "oracle": {},
  "code": {},
  "integrity": {}
}
```

对规范含义做非向后兼容的改动时，必须升 schema 版本。

## 6. 场景身份

清单必须标识物化这次运行时所用的确切场景定义。

必需字段：

```text
scenario.id
scenario.version
scenario.source_file_sha256
scenario.semantic_sha256
```

`source_file_sha256` 是已提交场景源文件那串确切字节的 SHA-256。

`semantic_sha256` 是规范解析后 `ScenarioSpec` 表示的 SHA-256。

这两个哈希含义不同，不得混为一谈：

- 源哈希检出字节级编辑；
- 语义哈希检出与无关源格式无关的场景含义变化。

## 7. 运行身份

清单必须记录：

```text
run.run_id
run.materialized_at
run.sealed_at
```

时间戳在哈希之前必须使用同一种规范的 RFC 3339 UTC 表示。

清单可以包含复现或诊断一次评估所必需的其他有界运行期元数据，但不得包含凭据或进程环境转储。

## 8. 作用域与实体

清单必须保留解析 provider 资源所用的评估作用域。

至少：

```text
scope.provider = hisiem
scope.tenant_id
```

GP-01 实体块应当包含用于语义打分的归一化攻击实体：

```text
entities.source_ip
entities.user_name
entities.host_name
```

这些值是评估事实，不是授权声明。它们不得被用来在 Copilot 运行时内部确立租户或行为者权威。

## 9. 事件引用

清单里存储的每个语义事件都必须源自那份已校验的 provider 数据集。

一条语义事件条目应当包含：

```text
role
classification = GROUND_TRUTH
provider_ref.provider
provider_ref.index
provider_ref.document_id
timestamp
event_action
event_outcome?
source_ip
user_name
host_name
payload_sha256
```

只应当存储评估与关联所需的、有界的归一化事实。

清单不得把完整的原始 Elasticsearch 文档复制成它的常规表示。

provider 引用必须是从 HISIEM 解析到的确切 `_index` 与 `_id` 值。它们不得由逻辑 role 或哈希重新
生成。

## 10. 控制事件隔离

watermark/控制事件必须与语义 ground-truth 事件分开存储。

示例表示：

```json
{
  "role": "W1",
  "classification": "WATERMARK_CONTROL",
  "excluded_from_ground_truth": true,
  "provider_ref": {
    "provider": "hisiem",
    "index": "...",
    "document_id": "..."
  }
}
```

控制事件不得：

- 出现在 `oracle.required_evidence_roles` 里；
- 满足语义证据要求；
- 仅仅因为出现在清单里就被注入 Copilot prompt 或图状态；
- 被评分器计入失陷证据。

评分器必须理解 `GROUND_TRUTH` 与 `WATERMARK_CONTROL` 之间的区别。

## 11. 来源告警引用

清单必须绑定用来启动调查的那个确切来源告警。

必需表示：

```text
source_alert.provider = hisiem
source_alert.resource_type = alert
source_alert.address_id
source_alert.business_id?
source_alert.rule_id
source_alert.event_count
```

`source_alert.address_id` 必须是 HISIEM 告警详情 API 实际接受的标识符。

对当前 HISIEM 告警实现来说，这就是 Elasticsearch 告警文档的 `_id`。

封存器不得从 `alert.id`、场景 id、哈希或事件 id 推 `address_id`。

业务告警 id 可以只作为可选的展示/关联元数据保留。

## 12. oracle

清单可以包含确定性打分所需的私有 GP-01 oracle。

最小 oracle：

```text
oracle.expected_verdict = MALICIOUS
oracle.facts[]
oracle.required_evidence_roles[]
```

推荐的 GP-01 语义事实：

```text
FAILURE_SEQUENCE
  at least five SSH authentication failures
  same attack source
  same target account
  same target host

POST_FAILURE_SUCCESS
  SSH authentication success exists
  same source
  same account
  same host
  success occurs after the failure sequence
```

oracle 应当描述事实与证据要求，而不是规定模型的措辞。

打分不得要求某个确切生成的句子，例如某个固定的 Finding 字符串。

这才让 GP-01 成为一次有据可依的调查评估，而不是一场 prompt 背诵测验。

## 13. oracle 隔离

oracle 隔离是一条硬架构不变式。

评估 harness 可以读封存清单，但 Copilot 调查只能收到标识真实来源资源所需的那点生产安全启动信息。

期望的流：

```text
SealedManifest
      ↓
Evaluation Harness
      ↓ extract only launch ref
ExternalResourceRef(
    provider="hisiem",
    resource_type="alert",
    address_id=<real address id>,
    business_id=<optional>
)
      ↓
Copilot Investigation
```

禁止的流：

```text
oracle → ModelProvider
oracle → system/user prompt
oracle → LangGraph state
oracle → ToolResult
oracle → Evidence
oracle → Finding candidate
oracle → InvestigationResult
```

生产应用不得导入评估 oracle 包。

仓库架构测试应当强制一条单向依赖：

```text
evaluation
    ↓
production public contracts
```

并禁止：

```text
production domain/application/agent/infrastructure/api
    ↓
evaluation oracle
```

## 14. 评估启动视图

harness 应当为调查启动暴露一个 `SealedManifest` 的专用投影，让意外的 oracle 传播在结构上难以发生。

等价类型：

```text
EvaluationLaunchRef
  provider
  resource_type
  address_id
  business_id?
```

启动器不得把完整的清单对象传给生产调查代码。

## 15. 规范化

清单完整性依赖一份项目自有的、带版本的规范表示。

`gp-eval-manifest/v1` 的规范化必须至少定义：

```text
encoding: UTF-8
object keys: lexical sorted order
separators: compact JSON separators
numbers: finite JSON numbers only
NaN/Infinity: prohibited
timestamps: canonical RFC 3339 UTC
list ordering: deterministic by schema meaning
Unicode: no implementation-dependent re-encoding
```

一个合适的 Python 序列化原语等价于：

```python
json.dumps(
    payload,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
)
```

列表顺序必须在序列化之前就确定。`sort_keys=True` 不会让数组顺序变成确定性的。

推荐的确定性排序：

```text
events           → logical role order F1..F5,S1
control_events   → logical role
oracle.facts     → declared ScenarioSpec order
required roles   → declared ScenarioSpec order
related refs     → stable provider-reference order where semantic order is irrelevant
```

## 16. 完整性哈希

清单必须携带一个 SHA-256 完整性摘要。

哈希规则：

```text
manifest_sha256 =
SHA256(
  canonical_json(
    manifest with integrity.manifest_sha256 omitted
  )
)
```

哈希不得包含它自己。

规范化标识符必须被纳入被哈希的载荷，例如：

```text
integrity.canonicalization = json-sort-keys-v1
```

校验器必须按该 schema 版本的规范化规则重算摘要，并在不匹配时拒绝。

## 17. 代码修订

一份封存基准记录应当标识它生成时所处的代码修订。

推荐字段：

```text
code.git_commit
code.dirty
```

一份意在作为权威评估记录的清单应当要求：

```text
code.dirty = false
```

脏工作树可以产出一份被显式标为非权威的开发产物，但它不得被标成与一份干净的封存基准记录等价。

## 18. 密钥排除

清单、物化草稿、规范载荷、封存日志与诊断都不得包含：

- `HISIEM_BEARER_TOKEN`；
- `CMD_API_KEY`；
- `Authorization` 头；
- 连接密钥；
- 原始环境转储；
- provider 请求密钥。

生成出的本地清单可以包含评估作用域取值，例如 tenant id 与合成实体，前提是确定性评估需要它们。

生成出的评估运行产物不应被提交进 Git。

## 19. 持久化

推荐的生成布局：

```text
.eval-runs/
  gp-01/
    <run_id>/
      materialization.json
      manifest.json
```

`.eval-runs/` 应当被 Git 忽略。

已提交的仓库包含的是场景与契约，不是生成出的 provider 数据集或运行期清单。

## 20. 原子封存

封存必须在文件系统边界上原子。

必需顺序：

```text
build canonical manifest bytes
↓
write temporary file
↓
flush and fsync file
↓
atomic rename/replace into manifest.json when target is absent
↓
optionally fsync containing directory where supported
```

最终封存文件绝不能被观测成一份部分写出的 JSON 文档。

## 21. 不可变性与幂等

一次运行已有清单之后：

```text
existing bytes == newly computed bytes
→ idempotent success

existing bytes != newly computed bytes
→ SEAL_CONFLICT
```

封存器不得静默覆盖一份不同的封存清单。

改动已校验数据集、oracle、场景身份、来源告警引用、代码修订、规范化版本，或任何其他被哈希的字段，
都需要一个新的有效封存结果；而当它代表的是一次不同的评估执行时，还需要一个新的运行身份。

## 22. 校验 API

评估包应当提供与下列等价的操作：

```text
build_manifest(VerifiedDataset, ScenarioOracle, CodeRevision)
canonicalize_manifest(manifest)
compute_manifest_sha256(manifest)
seal_manifest(manifest, path)
verify_sealed_manifest(path)
```

`verify_sealed_manifest` 必须在返回可信封存对象之前，同时校验 schema 不变式与完整性哈希。

## 23. 错误分类

实现应当暴露与下列等价的类型化失败：

```text
ManifestNotVerifiedError
ManifestSchemaError
ManifestCanonicalizationError
ManifestIntegrityError
ManifestSealConflict
ManifestPersistenceError
OracleIsolationViolation
```

这些错误应当携带有界的诊断上下文，且不得暴露密钥。

## 24. 包边界

推荐新增：

```text
src/hisiem_soc_copilot/evaluation/
├── manifest.py
├── sealer.py
├── oracle.py
└── launch_projection.py
```

评估包可以为执行调用生产的公开接口。生产包必须对 oracle 与封存清单表示保持无感知。

## 25. CLI 契约

建议命令：

```text
python -m hisiem_soc_copilot.evaluation.cli seal <run_id>
python -m hisiem_soc_copilot.evaluation.cli verify-manifest <run_id>
```

一条便利的准备命令可以组合 B.3 与 B.4：

```text
python -m hisiem_soc_copilot.evaluation.cli prepare GP-01
```

它的内部语义仍是：

```text
preflight
→ materialize
→ resolve
→ verify dataset
→ seal manifest
```

物化与封存即使被一条便利命令一起暴露，也必须在内部保持为两个分开的契约。

## 26. 测试契约

单元测试必须至少覆盖：

1. 未校验的草稿不能被封存；
2. 同样的显式已校验输入产出逐字节相同的规范载荷；
3. 篡改清单会改变或使 `manifest_sha256` 失效；
4. 一份已存在的不同清单不能被覆盖；
5. `W1` 不出现在语义 oracle 要求里；
6. 所有事件 provider 引用都源自 `VerifiedDataset`；
7. 来源告警 `address_id` 从已解析的 provider 引用复制，永不从业务 id 派生；
8. 规范列表顺序是确定性的；
9. NaN/Infinity 被拒绝；
10. 密钥字段无法经类型化清单模型被序列化；
11. 启动投影不含任何 oracle 数据；
12. 生产包不导入评估 oracle 模块。

一次活的端到端评估准备测试必须证明：

```text
real GP-01 materialization
→ VerifiedDataset
→ sealed manifest
→ integrity verification PASS
→ source alert launch projection contains exact real HISIEM address_id
```

活的准备测试本身不给模型质量打分；打分属于后续的 Golden Path 评估阶段。

## 27. 完成闸门

只有当下列全部成立时，封存器才算完成：

```text
VerifiedDataset
→ canonical versioned manifest
→ immutable atomic persistence
→ SHA-256 integrity verification
→ oracle isolated from production investigation context
→ deterministic launch projection
```

由此得到的架构是：

```text
GP-01 Scenario
      ↓
Real HISIEM Materialized Dataset
      ↓
VerifiedDataset
      ↓
SealedManifest
      ├─ launch projection ─→ Real Copilot Investigation
      └─ private oracle ────→ Evaluation Scorer
```

只有在这条边界被证明之后，封存的 GP-01 数据集才可以作为权威的「真实 HISIEM + 真实 ModelProvider」
Golden Path 评估输入使用。
