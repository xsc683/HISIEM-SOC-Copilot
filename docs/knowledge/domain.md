# 知识领域 — 带版本的安全知识

本仓在 `src/hisiem_soc_copilot/domain/knowledge/` 处提供了一个**支撑性的限界上下文**。
它只回答一个问题，并拒绝其他所有问题：

> 一个可信来源说了什么、在哪个不可变版本里、那个版本哈希到多少？

它不属于 Investigation 聚合，它不是一个权限模型，也不能授权任何东西。这样切分的理由见
[product-positioning.md](../status/product-positioning.md) 与
[domain-model.md](../contracts/domain-model.md)；本文档是具体的契约。

## 1. 为什么单独一个限界上下文

调查是*在证据之上的决策过程*。知识是*参考资料*。把两者合起来，等于给一份文档改变调查状态的能力，
而那条核心不变式——Agent 只能建议、不能授权——的全部要点就是**没有任何产物可以这么做**。

所以知识是一个平级上下文，不是 Investigation 里的实体：

| 关注点 | 归属 |
|---|---|
| 一次调查的生命周期、它的证据、它的判定 | `domain/investigation` |
| 一份知识文档及其各版本的生命周期 | `domain/knowledge` |
| 哪个租户可以*读*某份文档 | `domain/knowledge`（`Visibility`） |
| 某个租户能否对任何东西*采取行动* | `domain/response`、Human Approval |

`KnowledgeDocument` 刻意**不**是 Investigation 聚合上的一个字段。检索结果到达调查的唯一方式，
是作为挂在别的东西上的引用（在后续阶段），永远不会成为一个被拥有的子实体。

## 2. 实体

### `KnowledgeDocument` — 聚合根

一条外部可标识的知识。身份是 `(source_kind, external_key)`，在一个作用域内解析。

| 字段 | 说明 |
|---|---|
| `id` | UUID |
| `source_kind` | `MITRE_ATTACK` \| `CURATED_GUIDANCE` \| `TENANT_RUNBOOK` — **不可变** |
| `external_key` | 来源自己的标识符。**不可变** |
| `visibility` | `GLOBAL` \| `TENANT` — **不可变** |
| `tenant_id` | GLOBAL 为 `None`，TENANT 为归属租户 |
| `title` | ≤ 512 字符；跟随当前版本的标题 |
| `status` | `ACTIVE` \| `RETIRED` |
| `active_version_id` | 指向当前版本的指针；首次摄取前为 `None` |
| `revision` | 领域变更计数器，随事件一起发出 |
| `lock_version` | 持久化 CAS token，由仓储拥有 |
| `created_at`、`retired_at` | `retired_at` 恰好在 status 为 RETIRED 时被设置 |

有三条规则在领域里强制，**同时**也是数据库 CHECK 约束：

1. `GLOBAL` ⟺ `tenant_id IS NULL`；`TENANT` ⟺ `tenant_id IS NOT NULL`。
2. `status = 'ACTIVE'` ⟺ `retired_at IS NULL`。
3. `revision >= 0`、`lock_version >= 0`。

`source_kind`、`external_key` 与 `visibility` 创建之后永不改变。没有任何方法会改它们。一份需要
不同身份的文档，就是另一份文档。

### `KnowledgeDocumentVersion` — 不可变

一个版本被写入一次，永不更新。内容变更**追加**一个版本，永不重写已有的版本。这正是来源可核验的
原因：一条命名了 `(document_version_id, content_hash)` 的引用始终可验证，因为事后字节和行都改不了。

| 字段 | 说明 |
|---|---|
| `id`、`document_id` | UUID |
| `version` | ≥ 1，每份文档内连续 |
| `content_hash` | 小写 SHA-256 十六进制 |
| `title` | 截至该版本的标题 |
| `normalized_content` | 非空，且**已经归一化** |
| `language` | ≤ 16 字符，默认 `en` |
| `source_version` | 上游版本，例如一次 ATT&CK 发布 |
| `metadata` | 自由格式 JSONB，只存储、永不解释 |
| `ingested_at`、`effective_at` | 时间戳 |

`__post_init__` 强制那条让 `content_hash` 有意义的规则，并且在**每一条**构造路径上都强制它——
`create()`、mapper 加载、以及测试里手写的字面量一视同仁：

```python
compute_content_hash(normalized_content) == content_hash   # else InvalidKnowledgeVersionError
```

两种失效模式被拒绝而不是修补，而修补其中任何一种都会构成哈希的*第二个*定义：

1. `normalized_content` 不是 `normalize_knowledge_content` 的不动点。一个「归一化过的」内容再次
   归一化会变成别的东西的版本，带着一个未来对同样字节重新归一化时无法复现的哈希。
2. `content_hash` 不是与它相邻存放的内容的哈希。一行若哈希与正文互相不符，它两个都不描述，所以
   在任何人读它之前，来源就已经断了。

两项检查都走同一个领域哈希函数。mapper、仓储和迁移里都没有第二份 SHA-256。

### `KnowledgeContentChunk` — 不可变内容身份

引用的**目标**。写入一次，永不重写。

| 字段 | 说明 |
|---|---|
| `id`、`document_id`、`document_version_id` | UUID |
| `generation` | ≥ 1。对同一版本重新分块是**新**的一代；旧的一代原样保留 |
| `ordinal` | ≥ 0，在 `(document_version_id, generation)` 内唯一 |
| `heading_path` | 该内容块来自的章节路径 |
| `content` | 内容块文本。**不可变** |
| `content_hash` | 小写 SHA-256 十六进制，由 `create()` 从 `content` 派生 |
| `token_count`、`language` | 边界与元数据 |
| `chunker_version` | 由哪个分块器产出 |
| `created_at` | |

它为什么是与 embedding 行分开的实体，就是全部要点：

- 内容身份写入一次、永不重写，所以指向它的引用能跨越重新嵌入、检索投影重建、进程重启、文档退役、
  后续文档版本启用、以及一次重新分块而始终可解析。
- embedding 行是它的**可重建投影**。删掉并重建每一行 embedding，不会破坏任何引用，因为没有任何
  引用命名 embedding 行。

版本实体强制的那套 content/content_hash 自校验，在这里同样强制，位置在实体边界，并且走**同一个**
共享辅助函数：`create()` 从内容派生哈希而不是接受一个哈希，`__post_init__` 在每次加载时重新检查这
一对值。一个存储哈希不是其存储文本之哈希的内容块会被拒绝，永不修补。

### `KnowledgeChunkEmbedding` — 可重建投影

`(content_chunk_id, embedding_profile_id, embedding, indexed_at)`，在
`(content_chunk_id, embedding_profile_id)` 上唯一。

它**不带内容，也不带哈希**。这个「没有」是设计而不是疏漏：没有任何东西能被*通过*这张表引用，所以
重建它纯粹是运维动作。它是唯一一张预期会被删除并重建的知识表。

### `AttackRelease` — 钉住的发布及其权威

| 字段 | 说明 |
|---|---|
| `id`、`framework`、`source_release` | 在 `(framework, source_release)` 上唯一 |
| `content_fingerprint` | 对该发布规范技术集合的 SHA-256。仅当某个发布是迁移从早于指纹机制的既有行收养而来时为 `NULL` |
| `status` | `ACTIVE` \| `INACTIVE`。一条**按 framework 的部分唯一索引**把「每个 framework 至多一个 ACTIVE 发布」变成数据库事实 |
| `technique_count`、`created_at`、`activated_at` | |

权威住在**这里**，粒度是发布——不在技术行上。「这个 framework 的哪个发布是权威的」是一个关于某一个
发布的事实；把它按技术建模，正是此前允许两个发布同时权威的原因。`attack_technique` 依然是钉住的
技术*快照*——一技术一行——每行通过 `(framework, source_release)` 引用它的发布。不可变性规则及其
指纹见 [security-boundary.md](security-boundary.md) §10。

一次导入是三个动作，把它们分开，才让权威声明为真，而不只是意图为真：

1. **注册。** bundle 被解析、取指纹，并按钉住的不可变规则校验；随后发布及其规范技术行在**一个短
   事务**里以 **INACTIVE** 写入。此时 `activate=True` 不激活任何东西——它记录的是这次导入具有权威
   *意图*。
2. **暂存。** 每一项技术都经一个专用的、具备 MITRE 能力的摄取 handler 摄取——同一个摄取用例，但由
   容器的 `attack_projection_ingestion_handler` 工厂构造，带上一份普通工厂无法铸造的能力——并带
   `activate_version=False`，于是它的不可变版本、内容块与 embedding 都被创建，但
   `active_version_id` **不被移动**。然后每项技术的一条绑定行记入 `attack_release_projection`。
   因此一个已暂存的发布已完整投影，同时对检索完全不可见。

   `MITRE_ATTACK` 是系统托管的。普通知识 handler 在摄取和退役两侧都以
   `SYSTEM_MANAGED_KNOWLEDGE_SOURCE` 拒绝它，`ingest-file --source-kind` 也不提供它。不存在调用方
   可控的绕过开关：能力来自 bootstrap 时的接线，永远不来自命令字段、metadata、租户、actor 或 CLI
   flag。
3. **切换**，仅当这次导入具有权威意图时。它是**一个**事务：取该 framework 的 advisory lock、
   **在任何变更之前先校验**、翻转该发布的权威、镜像 `attack_technique.active`，然后通过
   `activate_version()` 逐个移动已绑定文档的指针。因此权威与检索要么一起切换，要么都不切。

### `AttackReleaseProjection` — 发布 → 版本绑定

| 字段 | 说明 |
|---|---|
| `id`、`framework`、`source_release`、`technique_id` | 在 `(framework, source_release, technique_id)` 上唯一——自然键，也是被重试的暂存收敛到的冲突目标 |
| `document_id`、`document_version_id` | 该发布所暂存的那个确切的不可变版本 |
| `content_hash` | 暂存版本的哈希，切换时与其规范行的哈希核对 |
| `created_at` | |

这一行存在，是因为两个看起来像一个的事实确实是两个：*该发布携带了该技术的内容*，以及*该发布投影到
的不可变版本行是 V*。两个发布可以携带逐字节相同的技术内容，因而共用一个
`KnowledgeDocumentVersion`——复用是正确的，内容本来就一样——而此时单看版本行无法记录是哪个发布把
它暂存进来的。

版本上的 `source_version` 不能充当这个绑定。它记录的是哪个发布恰好**创建**了这一行，所以在共享版本
上它命名了一个发布，并静默错认了另一个。从内容哈希反推这个绑定会因同样的理由失败——它得到的是
「内容匹配的发布集合」，而不是「某个发布实际暂存了哪个投影」——而行 id 是随机的，所以没有任何推导
能重建调用方当时观察到的那个。绑定在暂存时写一次，永不重推；这正是重新激活一个更早的发布能还原它
当时暂存的那个确切版本的原因。

绑定是**来源，不是权威**。哪个发布权威由 `attack_release.status` 决定；这一行只说明某个发布的投影
*是*哪个版本，而切换正是把文档指针移到它上面。

### 为什么切换没有削弱 `activate_version`

`KnowledgeDocument.activate_version()` 对普通知识保持它只向前的契约。重新摄取历史内容依然永远不会
把文档的 active 指针往回滚——那条规则住在 `_converge_on_existing` 里，未作改动——而一次暂存摄取
无论哪个方向都不移动指针。切换是一个不同的动作，有它自己显式的语义，所以通用规则是被留着不动，而
不是被放宽来迁就它。

## 3. 归一化与哈希

```python
content_hash = SHA-256(normalize_knowledge_content(raw).encode("utf-8"))
```

`normalize_knowledge_content` 恰好做五件事，不多做：

1. 去掉开头的 BOM（编辑器的产物，不是内容）；
2. 把 `\r\n` 与孤立的 `\r` 折叠成 `\n`；
3. 应用 Unicode NFC；
4. 去掉每一行末尾的空白；
5. 去掉无意义的尾部空行，并以恰好一个 `\n` 结尾（空输入归一化为空字符串）。

它**不得**转小写、剥标点或做词干化。`T1110`、`sshd`、`authentication_failure` 以及 CVE 形状的
token 都是安全标识符；改动它们就改动了文档的含义。

**后果：** 同一份文档从 Windows 检出（`CRLF`）和从 Linux 检出（`LF`）摄取，产出同一个
`content_hash`，因此不会产生第二个版本。这由 `tests/unit/knowledge/test_domain_knowledge.py` 直接
断言。

## 4. 枚举（冻结）

`SourceKind` = `MITRE_ATTACK`、`CURATED_GUIDANCE`、`TENANT_RUNBOOK`
`Visibility` = `GLOBAL`、`TENANT`
`DocumentStatus` = `ACTIVE`、`RETIRED`
`EmbeddingProfileStatus` = `ACTIVE`、`RETIRED`
`DistanceMetric` = `COSINE`（当前唯一取值）

这里刻意**没有** `PUBLIC`/`PRIVATE`/`ORG`/`GROUP`/`USER`/`CONFIDENTIAL`。那些是授权模型，而知识
不携带授权。`Visibility` 是一个**作用域**——谁的检索可以读这一行——不是一份许可。

## 5. 生命周期

```
            create()                     retire()
  (none) ──────────────► ACTIVE ──────────────────► RETIRED
                           │  ▲
       activate_version()  │  │  (no path)
                           ▼  │
                    active_version_id 只向前移动
```

- `ACTIVE → RETIRED` 合法，且只发生一次。`RETIRED → ACTIVE` **不支持**，也没有方法。对已退役文档
  调 `retire()` 会抛 `KnowledgeDocumentStateError`；摄取用例接住这个状态并改报
  `already_retired=True`，于是重复退役在用例边界上是幂等的，同时不削弱聚合。
- `activate_version()` 把 `active_version_id` 向前移动，并在 RETIRED 文档上被拒绝。重新激活已是
  active 的版本，对幂等重放来说是合法的空操作——但它仍然发出审计事件，所以账本记录了该版本被再次
  确认。
- 「只向前」是普通摄取路径的规则，不是这个聚合方法自身的性质。唯一的刻意例外是 ATT&CK **切换**：
  它把已绑定文档指到其发布所暂存的版本，即使那个版本比当前正在服务的版本更旧——还原一个更早的发布
  就是还原它的投影，而 §2 给了那个动作它自己显式的语义。
- 激活总是与版本及其内容块的持久化写在**同一个事务**里，所以指针永远不会引用一个检索投影缺失的
  版本。

## 6. 分块配置（冻结）

分块是一个*检索投影*，不是领域真相。这份配置是一个领域值对象，因为它的身份随每个内容块一起走：

```python
ChunkerProfile(
    chunker_version="structure-aware-v1",
    target_tokens=600,
    max_tokens=800,      # hard ceiling 800
    overlap_tokens=80,   # must be < target_tokens
)
```

分块*算法*住在 `infrastructure/knowledge/chunker.py`，经 `application/ports/chunking.py` 抵达。
领域只拥有这份配置及其不变式。改了算法而不改 `chunker_version`，会静默重写那些内容并未改变的检索
投影，所以 `chunker_version` 随每个内容块持久化，并由检索 profile 回显。

内容块 ordinal 与内容哈希是确定性的：同样的归一化内容加同样的 profile，产出同样的数量、同样的
ordinal、同样的每块哈希，与文件路径和行尾无关。

分块器变更会为同一个 `document_version_id` 产出**新一代**，而不是重写。`CHUNK_GENERATION_INITIAL
= 1`；普通检索只选某版本上存在的最高代，旧代原样留在盘上——这正是重新分块之前捕获的引用仍可解析的
原因。**当前没有任何路径会删除历史代。** 这里刻意不提供「破坏性重新分块」操作。

## 7. 事件

三条只追加的审计事实，与对应行在同一个事务里记录：

| `event_type` | 发出者 | 载荷 |
|---|---|---|
| `knowledge_document_created` | `KnowledgeDocument.create()` | `source_kind`、`external_key`、`visibility` |
| `knowledge_document_version_ingested` | `activate_version()` | `document_version_id`、`version`、`content_hash` |
| `knowledge_document_retired` | `retire()` | *（空）* |

这些是审计事实，**不是**异步副作用触发器。**没有为它们接任何 outbox 目的地。** 它们的存在是为了
让账本永远不可能与状态不一致，而不是为了让下游有什么可以响应。

## 8. 错误

全部派生自 `KnowledgeError`（它本身是 `DomainError`），并带有稳定的机器可读 `code`。消息面向运维，
绝不含密钥或原始文档正文。

| 类 | `code` |
|---|---|
| `InvalidKnowledgeDocumentError` | `INVALID_KNOWLEDGE_DOCUMENT` |
| `InvalidKnowledgeScopeError` | `INVALID_KNOWLEDGE_SCOPE` |
| `InvalidKnowledgeVersionError` | `INVALID_KNOWLEDGE_VERSION` |
| `InvalidKnowledgeChunkError` | `INVALID_CONTENT_CHUNK` |
| `KnowledgeDocumentStateError` | *（来自 `StateTransitionError`）* |
| `InvalidMitreBundleError` | `INVALID_MITRE_BUNDLE` |
| `InvalidContentHashError` | `INVALID_CONTENT_HASH` |
| `InvalidEmbeddingVectorError` | `INVALID_EMBEDDING_VECTOR` |
| `InvalidCitationError` | `INVALID_CITATION` |
| `KnowledgeBoundsExceededError` | `KNOWLEDGE_BOUNDS_EXCEEDED` |

## 9. 导入边界

`domain/knowledge` **只**导入标准库——`hashlib`、`re`、`unicodedata`、`dataclasses`、`datetime`、
`enum`、`uuid`、`typing`——外加 `domain/shared`。没有 SQLAlchemy、pgvector、FastAPI、Pydantic、
OpenAI、HTTPX 或 LangGraph。这由 `tests/architecture/test_knowledge_boundary.py` 断言，它用 `ast`
解析每个文件，而不是 grep 文本。

同一个文件还断言反方向：`application/` 或 `domain/knowledge/` 下没有任何文件出现
`AsyncSession`、`text(`、`select(`、`session.execute`、`cosine_distance` 或 `<=>` 操作符。原始 SQL
与向量查询只存在于 `infrastructure/persistence/repositories/knowledge.py`。

## 10. 知识不是什么

重述这条不变式，因为它是最容易被侵蚀的那一条：

- `KnowledgeDocumentVersion` 是**带版本的知识真相**。
- `KnowledgeContentChunk` 是**不可变内容身份**——引用目标。
- `KnowledgeChunkEmbedding` 是**可重建的检索投影**。
- 一个 embedding 是**可重建索引**。
- 一个 `AttackRelease` 是外部语料的**权威钉住快照**，而它的权威不超出「规范行就是从这次发布来的」
  这个范围。
- 一个 `AttackReleaseProjection` 是**发布 → 版本绑定**——来源，不是权威。它记录某个发布暂存了哪个
  不可变版本，并且不授予任何东西：它不能激活、不能批准，也不能被读作「哪个发布权威」的声明。
- 一个检索分数是**排序信号**。
- 一条引用是**已复验的引用**。

以上没有一个是业务权威。知识不能授权、不能批准、不能执行、不能改租户、不能改策略、不能创建 Verdict、
也不能调用 SOAR。`is_visible_to()` 决定谁的*检索*可以读某一行；它不授予别的任何东西。
