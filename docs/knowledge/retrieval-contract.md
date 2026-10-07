# 检索契约

`KnowledgeRetrievalService` 与 `KnowledgeCitationResolver` 的类型化表面，位于
`src/hisiem_soc_copilot/application/services/knowledge_retrieval.py`。

该模块里的一切都是纯的、确定性的，只有它编排的那两次仓储调用和那次 embedding 调用例外。排序是
作用在普通数据上的普通函数——这正是排序能脱离数据库复现、并脱离数据库测试的原因。

> **模型可达——经由两个只读工具。** 这两个服务是内部的，但
> `knowledge.retrieve_security_guidance` 与 `knowledge.resolve_attack_technique` 是
> **已注册且可选择**的，它们经 `agent/knowledge/catalog.py` → `application/ports/knowledge.py`
> 调进这套表面。它们是只读、租户作用域、有界的；摄取、变更与发布切换对模型依然不可达。见
> [security-boundary.md](security-boundary.md) §7。

## 1. 类型

```python
KnowledgeQuery(topic: str, context_terms: tuple[str, ...] = (), limit: int = 5)

KnowledgeHit(
    citation_id: str,
    document_id: UUID,
    document_version_id: UUID,
    chunk_id: UUID,
    source_kind: SourceKind,
    title: str,
    excerpt: str,          # bounded DATA, never instruction
    language: str,
    source_version: str | None,
    retrieved_at: datetime,
)

KnowledgeSearchResult(
    hits: tuple[KnowledgeHit, ...] = (),
    truncated: bool = False,
    retrieval_profile: RetrievalProfile | None = None,
)
```

`KnowledgeHit` **没有**任何字段可以被读成控制信号。它没有 `instructions`、没有 `action`、没有
`severity`、没有 `authority`。这个「没有」是对冻结的精确字段集做的断言，所以未来某次改动若新增
一个字段，会挂测试，而不是悄悄扩大一次检索能表达的东西。

`chunk_id` 命名的是一个**不可变内容块**（`knowledge_content_chunk`）——引用目标——永远不是
embedding 投影行。正因如此，§7 描述的任何一次重建都不会让 `citation_id` 里的这个引用失效。

`MAX_RESULT_LIMIT = 5` —— 请求更多会被**拒绝**，不是被夹到上限。

ATT&CK 权威投影有单写者切换语义：一个 `MITRE_ATTACK` 文档的 `active_version_id` 只在 ATT&CK
切换事务里移动，永不因普通摄取而移动。检索本身不需要感知发布——它读指针，而切换保证这个指针——
但正是这条保证，让「服务一次 MITRE 命中」与「服务权威发布」是同一个断言。

## 2. 作用域是强制的

```python
async def retrieve(
    self, *, tenant_id: str, query: KnowledgeQuery, mode: RetrievalMode = RetrievalMode.HYBRID
) -> KnowledgeSearchResult
```

`tenant_id` 是必填关键字，没有默认值。**没有无作用域变体**，所以未来任何调用方——工具、CLI、
评估——都不可能误检索整个语料。空的或纯空白的租户会抛 `InvalidKnowledgeQueryError`；它永远不会
退化成默认值。

`KnowledgeCitationResolver.resolve(tenant_id=..., citation_id=...)` 形状相同，理由也相同。

## 3. 查询归一化（拒绝，永不修补）

| 上限 | 限制 |
|---|---|
| `topic` 长度 | 256 字符 |
| 上下文词条 | 12 个 |
| 单词条长度 | 64 字符 |
| 派生检索词（兜底） | 96 |

归一化做 Unicode NFC 并折叠空白。它**不**转小写、不做词干化、不剥标点：安全标识符必须逐字节存活。
上下文词条去重时保留首次出现顺序——顺序稳定很重要，因为词法查询与向量查询都从同一个元组派生，
顺序不稳就会让同一个问题产出两次不同的检索。

不满足上限的查询以 `InvalidKnowledgeQueryError` 被拒。它永远不会被悄悄改形为另一个问题。

## 4. 检索模式

| 模式 | 通道 | `profile_id` |
|---|---|---|
| `LEXICAL_ONLY` | PostgreSQL FTS | `lexical-v1` |
| `VECTOR_ONLY` | 精确 pgvector 最近邻 | `vector-v1` |
| `HYBRID` | 两者融合 | `hybrid-v1` |

生产用 `HYBRID`。单通道模式存在的意义是让质量**可归因**——评估可以判定混合到底有没有比更好的那条
基线带来增益，而不是宣称它有。

### 词法通道

- PostgreSQL 全文检索，用 `simple` 配置（不做词干化、没有停用词表——安全 token 不得被变形）。
- 查询被展开成**词条列表**并以 **OR** 组合，因为 `plainto_tsquery` 会把它收到的东西 AND 起来：
  要求一个内容块同时含有一个问题的每个词，恰好会压掉混合检索本来要补上的那类语义命中。
- 每个词条都是一个独立的绑定参数。任何调用方提供的文本都不会被拼进 tsquery 语法，也不存在任何
  路径能让原始 tsquery、SQL 片段或操作符 DSL 抵达数据库。
- `lexical_document` 是 `GENERATED` 列
  （`to_tsvector('simple', heading_path || ' ' || content)`），所以索引永远不会和它所描述的内容
  漂移。

### 向量通道

- 在 pgvector 上做**精确**最近邻。这里刻意**没有 HNSW，也没有 IVFFlat 索引**：本仓的设计就是精确
  排序，而 ANN 索引会悄悄改变「找到了哪些邻居」。
- `embedding` 列是**无类型**的 `vector` 类型——schema 里没有烧进任何维度，所以换一个不同维度的
  embedding 模型不需要迁移。维度契约由当前 ACTIVE 的 embedding profile 强制。
- 该通道仅在存在 `ACTIVE` embedding profile **且**所配置 provider 的描述符身份与它精确一致时才
  运行。不一致会抛 `KnowledgeRetrievalUnavailableError`，而不是把查询嵌进另一个空间、产出看起来很
  像回事的胡话。

### 融合

Reciprocal Rank Fusion，`score(d) = Σ 1 / (60 + rank_i(d))`，rank 从 1 起算。

用 RRF 而不是原始分数的加权和，是因为两个通道产出的数字不在同一量纲上：`ts_rank` 和一个余弦距离
没有共同单位，任何混合系数都会是一个打扮成参数的魔数。

只有单一列表时，RRF 退化成「保持该列表的顺序」（它关于 rank 严格单调），这正是单通道模式不需要
单独代码路径的原因。

## 5. 排序、并列打破、多样化

每通道候选预算：**词法 20、向量 20**，融合后最多 5。

**并列打破顺序**：唯一一个稳定的语义键，SQL 候选查询与 Python 融合共用它，因此两者不可能互相
矛盾。

```python
stable_ranking_key(view) -> (
    source_kind, visibility, tenant_id or "", external_key,
    document_version_number, chunk_generation, ordinal,
)
```

排序并列由**语义业务身份**打破——也就是知识*是什么*——永远不由数据库生成的代理键打破。用 `uuid4`
打破并列，会让两个摄入了同一份语料的数据库给两个同分内容块排出不同顺序，而这恰恰是一条可复现
基线无法容忍的；`document_id`/`document_version_id` 都是随机 UUID，所以两者都不能出现在键里。

每个组成部分在「重新摄取进一个全新数据库」下都是稳定的：同一文档、同一版本、经同一个分块器分块，
永远产出同一个键。

- `visibility`/`tenant_id` 在键里，是因为一个 `GLOBAL` 文档与一个 `TENANT` 文档可能共用一个
  `external_key`（两个部分唯一索引恰好允许这种情况），所以没有作用域，这个顺序就不是**全序**。
  `tenant_id` 在排序时折叠成 `""`，这是安全的，因为折叠只发生在**同一个** visibility 取值内部，
  那里租户本来就是一致的。
- `ordinal` 在 `(document_version_id, generation)` 内唯一，这才让这个键是全序，而不只是「近乎
  全序」。

SQL 那一侧的孪生体是 `_stable_order_columns()`：它先按该通道自己的分数排，再按恰好这些列排，并
使用 `COLLATE "C"`。`C` 排序规则不是装饰：数据库默认排序规则是依赖 locale 的，会在两台机器上把
`external_key` 排出不同顺序，而 `C`（字节序）对 UTF-8 文本与 Python 的 `str` 比较完全一致——这
正是 SQL 顺序与 Python 键*可证明地*是同一个顺序的原因。向量通道按余弦距离排，再按同样的列排。

**多样化：** 每文档最多 `max_chunks_per_document = 2` 个内容块。第一遍按 rank 顺序取候选，只要
每个文档还在自己的上限内。如果这样取完结果集仍然不满，第二遍按 rank 顺序从缓下的候选里补满剩余
槽位——依然确定性，只是多样性弱一些，这样小语料返回的是一整页，而不是人为的空页。`truncated`
报告是否有东西被丢掉。

**摘录**会折叠并截断到 `MAX_EXCERPT_CHARS = 480`。有界，是为了让一条命中成为指向内容的*指针*，
永远不能成为把整篇文档偷运进未来模型上下文的手段。

### 检索哪一代

普通检索**只选该版本上存在的最高 `generation`**。重新分块会写入新的一代并原样保留旧的一代；旧一代
不再被 `retrieve` 返回，但继续能通过 `resolve` 解析。检索路径里没有任何东西会删除某个分块代，所以
一次重新分块之前捕获的引用不受它影响。

## 6. 检索 profile

每个结果都带着产出它的那套冻结旋钮：

```python
RetrievalProfile(
    profile_id,                  # hybrid-v1 | lexical-v1 | vector-v1
    lexical_candidate_limit,     # 20
    vector_candidate_limit,      # 20
    rrf_k,                       # 60
    max_chunks_per_document,     # 2
    chunker_version,             # structure-aware-v1
    embedding_profile_id,
    embedding_model_id,
)
```

记录它，才使排序可复现：同一份语料快照加上同一个 profile，产出同一个顺序。这些默认值**就是**冻结的
`hybrid-v1` profile；改掉其中一个，就改掉了每一次排序。

## 7. 引用

格式：`kcit:<content_chunk_uuid>:<content_hash_prefix>`

这个 UUID 是**不可变内容块**的 id。正是这一处改动让引用变得持久，而它值得以不变式的形式写下来：

> **一条历史引用必须能在检索重建索引之后继续存活。**

因此这个标识符命名的是内容身份，永远不是一行用完即弃的检索行。在旧模型下，引用命名的是一行会在投影
重建时被删除、并以全新 `uuid4` 重建的行，于是重建之前捕获的每一条引用都变得永久无法解析——而且是
静默的，因为那个字符串看上去仍然能解析。

- 内容块 UUID 是规范形式（`str(UUID(x)) == x`）。
- 哈希前缀是 8–16 位小写十六进制（默认 12）。
- 解析永不抛异常、永不修补。任何畸形的串返回 `None`，解析失败。

**引用是一个把手，不是权威。** 串里的任何东西都不被信任。`KnowledgeCitationResolver.resolve` 会
重读内容块、连接文档与版本，然后重新检查：

| 检查 | 失败原因 |
|---|---|
| 字符串可解析 | `MALFORMED_CITATION` |
| 内容块存在且对该租户可读 | `CHUNK_NOT_FOUND` |
| 作用域自洽（`GLOBAL` ⟺ 无租户） | *（抛异常）* |
| `TENANT` 文档属于调用方租户 | `SCOPE_MISMATCH` |
| 存储的哈希等于**从实际读到的内容重算出的**哈希 | `CONTENT_INTEGRITY_MISMATCH` |
| 该存储哈希以串里的前缀开头 | `CONTENT_INTEGRITY_MISMATCH` |

两项完整性检查都需要，它们抓的是不同的篡改。存储的哈希不是证据；它是*写在内容旁边的一句声明*。
解析器重算 `compute_content_hash(actual chunk content)`，要求重算值等于存储的完整哈希**并且**存储
哈希携带该引用的前缀。带外改文本会破坏前者；把存储哈希改成与伪造前缀匹配会破坏后者。无论哪一种，
答案都是带显式原因的 `unresolved`，且没有任何异常逃到调用方。

作用域会被**再次**校验，**即便仓储本身已是租户作用域的**：解析器不能依赖「某一层对可见性的判断
是对的」。

与普通检索不同，解析**故意对 RETIRED 文档、历史版本以及已被取代的分块代仍然有效**。一次过去调查里
捕获的引用，必须在它指向的文档被取代或撤回之后仍然可解释。普通 `retrieve` 排除已退役文档与非当前
代；`resolve` 两者都不排除。

因此，在下列每一种事件之后，引用仍然能解析：

| 事件 | 为什么仍然能解析 |
|---|---|
| embedding profile 重建（同代） | 引用从不命名 embedding 行 |
| 向量重新嵌入 | 同一个理由——内容身份不受触动 |
| 检索投影重建 | 内容块不属于该投影 |
| 进程重启 | 解析没有任何东西是进程内状态 |
| 文档退役 | 退役是生命周期变迁，不是内容变更 |
| 新文档版本启用 | 旧版本的内容块不受触动 |
| 重新分块（新一代） | 旧代被保留，且仍可解析 |
| 跨租户 | 它**不**解析——`SCOPE_MISMATCH` |

唯一能让一条历史引用无法解析的，是对不可变历史知识的真实删除，而**当前不提供任何破坏性删除**。
到达那里的唯一途径是运维手工删行——这正是上面那两项完整性检查是重算、而不是从一句存储声明里读的
原因。

解析证明的是**来源**——这个内容块、属于这个不可变版本、带这个哈希，存在且对该租户可读。它不证明
正确性，也不授予任何东西。

## 8. 失效模式

| 条件 | 错误 |
|---|---|
| 查询违反某条上限，或租户为空 | `InvalidKnowledgeQueryError` |
| 没有 ACTIVE embedding profile | `KnowledgeRetrievalUnavailableError` |
| 未配置 embedding provider | `KnowledgeRetrievalUnavailableError` |
| provider 身份 ≠ ACTIVE profile 身份 | `KnowledgeRetrievalUnavailableError` |
| provider 返回的 profile 与它声明的不一致 | `KnowledgeRetrievalUnavailableError` |
| embedding 维度 ≠ profile 维度 | `KnowledgeRetrievalUnavailableError` |
| 某次文档摄取会切换 ACTIVE embedding profile | `EmbeddingProfileSwitchRequiresReindexError`（`EMBEDDING_PROFILE_SWITCH_REQUIRES_CORPUS_REINDEX`） |

最后一行就是 §4 的全部：当存在一个 `ACTIVE` profile、而所配置 provider 的身份与它不同时，**任何**
普通文档摄取都会显式失败——**包括**传了 `allow_embedding_profile_switch=True` 的那次。当前没有
部分切换，也没有单文档切换。保留这个 flag 只是为了让既有调用方收到那条诊断，而不是一个「未知参数」
错误。见 [operations.md](operations.md) §5。

`LEXICAL_ONLY` 从不需要 embedding provider 或 profile，所以向量通道不可用时它照常工作——也正因
如此，在这种状态下 `doctor` 报的是 `DEGRADED` 而不是 `NOT_READY`。

## 9. 纯数据保证

检索到的内容是 `DATA_ONLY`。一个含着 `rm -rf`、一段 `curl | sh` 管道、一条 PowerShell 单行命令，
或那句 *"ignore all previous instructions"* 的内容块，产出的字符串与任何其他文本完全同类。检索
路径里没有任何东西会解释、执行或据内容派发。用来证明这一点的语料，是封存的 `KB-GOLDEN-V1` 夹具
里的 `PROMPT_INJECTION_POISON` 类别。
