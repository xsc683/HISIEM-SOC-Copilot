# 知识检索评估契约

`KB-GOLDEN-V1` 基线。包：`src/hisiem_soc_copilot/evaluation/knowledge/`。驱动：
`src/hisiem_soc_copilot/knowledge/evaluation.py`。

它的目的很窄，值得直说：**衡量混合检索是否挣得了它的位置**，并产出一份审查者能重新打分的文件。

## 1. 边界

评估包刻意自足。它不从任何生产层导入——`domain`、`application`、`infrastructure`、`bootstrap`——
而是通过两个注入的异步 callable 接收全部检索行为：

```python
RetrieveFn = Callable[[str, EvalQuery, str], Awaitable[Sequence[RetrievedHit]]]
ResolveFn  = Callable[[str, str], Awaitable[bool]]
```

后果：

- 套件可以在**没有数据库、没有模型、没有网络**的情况下运行与重打分。
  `tests/unit/evaluation/knowledge/` 里的 25 个测试做的正是这件事。
- 生产知识领域不得导入评估模块。驱动住在两者之外，即 `hisiem_soc_copilot.knowledge`，所以两个
  方向都不会变成依赖。
- `tests/architecture/test_evaluation_boundary.py` 强制任何生产层都不导入
  `hisiem_soc_copilot.evaluation`。唯一合法的导入者是 `hisiem_soc_copilot.knowledge`，它是
  dev/eval 表面而非生产层——测试在允许清单里显式点名它，而不是让它碰巧通过。

构建那些注入 callable 的包，是**围绕运维 CLI 所用的同一批 Application 用例**的适配器，永不是围绕
一条平行路径。因此一次基线运行演练的，正是真实摄取与真实检索所跑的那套代码：哈希、分块、
embedding profile 选择、两阶段事务、RRF、多样化、引用解析。

## 2. 夹具：`KB-GOLDEN-V1`，语料版本 `1`

| | |
|---|---|
| 套件 id | `KB-GOLDEN-V1` |
| 语料版本 | `1` |
| 租户 | `tenant-a`、`tenant-b`、`tenant-c` |
| 文档 | **18** |
| 用例 | **22** |
| 类别 | **9** |
| 已标注内容块 | **70** |
| source kind | 9 个 `TENANT_RUNBOOK`、6 个 `CURATED_GUIDANCE`、3 个 `MITRE_ATTACK` |
| 可见性 | 9 个 `GLOBAL`、9 个 `TENANT` |
| 夹具退役的 | `guidance-legacy-ssh-hardening` |
| 投毒文档 | `guidance-detection-engineering-review-checklist`、`tenant-a-soar-response-runbook` |

夹具是**封存**的：`CORPUS` 与 `CASES` 是模块常量，不在运行时生成。对语料的改动就是对套件的改动，
版本必须随之递增。

### 九个类别

| 类别 | 用例数 | 它测什么 |
|---|---|---|
| `EXACT_IDENTIFIER` | 4 | 一个字面 token（`T1110`、`sshd`、一个 CVE）必须被**词法地**找到 |
| `SEMANTIC_GUIDANCE` | 2 | 一个措辞不出现在文档里的概念性问题——只有向量通道能找到它 |
| `ATTACK_QUERY` | 3 | 从导入的知识里查 ATT&CK 技术 |
| `TENANT_RUNBOOK` | 3 | 某租户自己的 runbook 对该租户可达 |
| `SCOPE_COMPETITION` | 2 | 相同文本同时存在于全局**和**某租户下；赢的必须是对的那个 |
| `NEAR_MISS_WRONG_DOC` | 2 | 一份看似合理但错误的文档，不得顶掉对的那份 |
| `RETIRED_EXCLUSION` | 2 | 已退役文档不得出现在普通检索里 |
| `CROSS_TENANT_DENIED` | 2 | 另一个租户的文档永不返回 |
| `PROMPT_INJECTION_POISON` | 2 | 注入的指令被当作惰性 DATA 检索出来 |

`EXACT_IDENTIFIER` 与 `SEMANTIC_GUIDANCE` 是**混合类别**。它们存在的意义正是检验「两个通道混起来
能找到任一个单独找不到的东西」，而混合闸门把这两类里任何一次未命中都视为不合格——因为容差不能成为
「漏掉混合设计专门要处理的那个用例」的借口。

## 3. 模式

| `EvalMode` | 通道 |
|---|---|
| `LEXICAL_ONLY` | 只有 PostgreSQL FTS |
| `VECTOR_ONLY` | 只有精确 pgvector 最近邻 |
| `HYBRID` | 两者，按 RRF 融合 |

某个通道跑不起来的模式会被报成**不可用并附原因**，而不是零分。只有 `ModeUnavailableError`——由注入
的 `retrieve` 在通道本身不可用时抛出——才被那样读。其他任何异常都会向上传播并让这次运行响亮地失败：
一个真实的缺陷永远不能被报成「通道不可用」。

## 4. 指标

全部由确定性代码从原始排序算出。**没有 LLM 裁判。**

| 指标 | 定义 |
|---|---|
| `recall_at_k` | 前 *k* 名中相关文档的占比。当某用例声明没有相关文档（纯排除用例）时为 `None`，于是它被排除在均值之外，而不是被打成成功或失败。 |
| `reciprocal_rank` | 第一份相关文档的 `1 / rank`，一份都没找到时为 `0.0`。 |
| `ndcg_at_k` | 文档级排序上的二值增益 nDCG@k。`DCG = Σ 1/log2(rank+1)`，对前 *k* 名中的相关文档求和；`IDCG` 为理想排序的同一量。`IDCG` 为零时为 `0.0`。 |
| `citation_resolution_rate` | 返回命中中引用经解析器解析成功的占比。空结果集为 `0.0`——刻意不是一个空洞的 `1.0`，因为一次什么都没解析的运行什么都没证明。它报在命中数旁边，好让「空结果导致的零」与「解析器坏掉导致的零」始终可区分。 |
| `cross_tenant_leakage_count` | 既不属于查询租户也不属于 `GLOBAL` 的命中。**按命中**计数，不按文档，所以重复泄漏就是重复的数字。 |
| `forbidden_retrieval_count` | 落在此用例显式禁止的文档（已退役的，或另一个租户的）上的命中。 |
| `claim_mismatches` | 检索器声称已解析、但解析器拒绝了的命中。一个不再为真的声称会变成一个数字，而不是一次静默通过。 |

每个指标都在产物里按模式、按用例聚合。

## 5. 混合闸门

`hybrid_verdict` 返回三种判定之一，外加支撑它的比较。

| 判定 | 何时 |
|---|---|
| `NOT_RUN` | 未请求混合、向量通道被排除，或向量/混合通道跑不起来。没有可比的东西。 |
| `PASS` | HYBRID 的平均 Recall@k 在**更好的**单通道基线 `HYBRID_RECALL_TOLERANCE = 0.02` 之内，**并且**每个混合类别用例都被 HYBRID 检索到了。 |
| `FAIL` | 以上都不是，并带具体数字报告。 |

两点对审查很重要：

- **不存在任何不产出数字的 `PASS` 路径。** 这道闸门是一次测量，不是一句断言。
- **`NOT_RUN` 不是失败。** 对一次向量通道没有真东西可比的运行来说，它是正确、诚实的判定。驱动只在
  `FAIL` 时退出 `1`，所以 `NOT_RUN` 不会训练运维去无视退出码。

## 6. 产物

默认写入 `.eval-runs/knowledge/`（用 `--out` 覆盖）。文件名：

```
kb-golden-v1-k5.json                    # SEALED, deployment provider
kb-golden-v1-k5-plumbing-only.json      # SEALED, deterministic test fixture
kb-golden-v1-k5-open-corpus.json        # NOT SEALED -- see section 8.3
```

Schema：`knowledge-retrieval-eval/v1`。

```jsonc
{
  "schema_version": "knowledge-retrieval-eval/v1",
  "suite_id": "KB-GOLDEN-V1",
  "retrieval_profile": { "profile_id": "hybrid-v1", "rrf_k": 60, ... },
  "embedding_profile": {
    "available": true,
    "provider": "...", "model_id": "...", "dimension": 1536,
    "evidence": "PLUMBING_ONLY" | "DEPLOYMENT_CONFIGURED" | "NONE",
    "detail": "..."
  },
  "corpus_version": "1",
  "corpus": {
    "corpus_mode": "SEALED" | "OPEN_CORPUS",
    "sealed": true,
    "corpus_fingerprint": "<64 hex>",
    "expected_corpus_fingerprint": "<64 hex>" | null,
    "eligible_document_count": 17,
    "eligible_version_count": 17,
    "eligible_chunk_count": 67
  },
  "case_count": 22,
  "modes": {
    "LEXICAL_ONLY": { "available": true, "mean_recall_at_k": ..., "mean_reciprocal_rank": ..., ... },
    "VECTOR_ONLY":  { ... },
    "HYBRID":       { ... }
  },
  "citation_integrity": { "by_mode": {...}, "hits_returned": ..., "resolved_hits": ..., "claim_mismatches": ... },
  "tenant_leakage":   { "by_mode": {...}, "total": 0 },
  "forbidden_retrieval": { "by_mode": {...}, "total": 0 },
  "hybrid_gate": { "verdict": "PASS|FAIL|NOT_RUN", "detail": "...", "recall_tolerance": 0.02 },
  "per_case": [ { "mode": "...", "case_id": "...", "category": "...", "ranked_document_keys": [...], ... } ]
}
```

`write_artifact` **拒绝替换已存在的文件**，除非 `overwrite=True`。基线是证据；静默替换证据，正是
两个人最终为同一个套件引用不同数字的由来。文件以显式的 `newline="\n"` 写出，好让字节在 Windows 与
POSIX 检出上完全相同。

### 产物绝不能包含什么

embedding 向量或其任何一部分 · 凭据、API key、token、连接串 · 环境变量或宿主机路径 · 原始 HTTP
请求或响应 · 完整文档正文（只允许 ≤ 240 字符的摘录）。

任何拿到这份文件的人都能重算里面每一个数字。这正是要点。

## 7. 证据质量——`PLUMBING_ONLY` vs `DEPLOYMENT_CONFIGURED`

| `embedding_profile.evidence` | 含义 |
|---|---|
| `DEPLOYMENT_CONFIGURED` | 向量由这套部署自己的 embedding provider 产出。数字度量的是一个真实检索系统。*模型本身的语义有效性*仍然超出这套 harness 能断言的范围。 |
| `PLUMBING_ONLY` | 向量由确定性测试夹具产出。它们**不携带任何语义含义**。`VECTOR_ONLY` 与 `HYBRID` 的结果验证的是管线——分块、索引、排序、融合、引用解析——对检索质量什么都没说。 |
| `NONE` | 未配置 provider；向量通道没有运行。 |

一次 plumbing 运行会**在文件名里和 JSON 里都**被标注，所以即使有人不打开文件就引用它，「诚实标签」
依然在。一次 plumbing 运行绝不能被呈现为语义基线，而本文档也没有呈现任何一条。

## 8. 确定性

给定同一份语料快照和同一个检索 profile，一次运行是可复现的：同样的排序、同样的 ordinal、同样的
指标。打分路径里没有 sleep、没有网络调用、没有随机性、没有读时钟。产出某次运行的那些具体旋钮取值，
通过 `RetrievalProfile` 记录在每一个结果上。

### 8.1 稳定排序键

并列由**语义业务身份**打破，永不由数据库代理键打破：

```python
stable_ranking_key(view) -> (
    source_kind, visibility, tenant_id or "", external_key,
    document_version_number, chunk_generation, ordinal,
)
```

随机的文档、版本或内容块 UUID **禁止**用作排序并列打破。键的每个组成部分都能从语料本身复现，所以
两个独立摄入同样字节的数据库会以同样的方式解并列——这才让这条基线可跨库复现，而不只是在一台机器上
可重复。`visibility`/`tenant_id` 在键里，是为了当一个 `GLOBAL` 与一个 `TENANT` 文档共用
`external_key` 时它仍然是**全序**；而 `ordinal` 在 `(document_version_id, generation)` 内唯一。

SQL 候选查询也恰好按这些列排序，通过 `_stable_order_columns()` 与 `COLLATE "C"`，所以数据库的排序
与 Python 的 RRF 融合不可能互相矛盾——向量通道也一样。

因此先前那条注意事项已作废。它说的是：排序敏感的指标对某套部署可复现，但跨部署只是*指示性*的，
因为并列是按 `uuid4` 打破的。这不再成立，而
`tests/integration/persistence/test_knowledge_persistence.py` 里的双全新库测试断言的正是相反的一面：
两个独立摄入的数据库有完全相同的 LEXICAL 排序、完全相同的确定性夹具 VECTOR/HYBRID 排序，以及完全
相同的 `Recall`/`MRR`/`nDCG`。

### 8.2 封存语料前置条件

在计算任何指标之前，这次运行会从数据库推导出**租户可见、ACTIVE、合格语料**，并与封存夹具的预期事实
集比对。不一致会以 `CORPUS_PRECONDITION_FAILED` 失败，并列出每一处差异——意外的合格文档、缺失的
预期文档、变过的内容哈希、变过的版本、变过的内容块投影——而不是产出一个看着挺像回事的分数。

这很重要，因为分数在这里是最危险的输出。在错误语料上量出来的排序不只是没用；它是一个会被人引用的
数字。差异清单是有界的，且截断会被说明而不是静默发生，所以一条被截断的消息永远不会被误当成完整的。

默认是**封存**。处在竞争作用域里的环境文档不被容忍，它们是前置条件失败。

### 8.3 `--allow-ambient-corpus` 是一次不同的测量

对于想要「这个数据库现在返回什么」的运维，存在一个显式逃逸口。它不是一次降低了标准的 SEALED 运行：

- 前置条件被完全跳过。
- 产物记录 `corpus_mode: "OPEN_CORPUS"` 与 `sealed: false`，而 `expected_corpus_fingerprint` 为
  `null`，因为这次运行什么都没断言。
- 产物文件名带 `-open-corpus`。
- **不产出任何 hybrid 闸门判定**，所以一次开放语料运行永远不可能打印
  `KB-GOLDEN-V1 baseline PASS`。

它可以说数据库当前返回了什么。它永远不能说「这就是 KB-GOLDEN-V1 基线」，因为这次运行没有任何东西
能确立语料就是那份夹具。

### 8.4 语料指纹

```python
CORPUS_FINGERPRINT_SCHEMA = "knowledge-corpus-fingerprint/v1"

corpus_fingerprint(facts) -> SHA256(canonical JSON of the SORTED fact set)
```

每条事实是一个合格语料内容块，归约成可复现的语义事实：

```
visibility, tenant_id, source_kind, external_key,
document_version_number, document_content_hash,
chunk_generation, ordinal, chunk_content_hash
```

`GLOBAL` 文档的 `tenant_id` 是 `""` 而不是 `None`，所以规范形式是全的，排序也是真正的全序。事实集在
哈希之前被**去重并排序**，所以指纹在构造上就与顺序无关——而一份对三个租户作用域可见的 `GLOBAL` 文档
只贡献一条事实，不是三条。

刻意缺席的（因为它们会让两份相同的语料看起来不同）：**行 UUID、时间戳、数据库主机、embedding
向量**——以及凭据，凭据根本不该被写下来。

两个独立数据库摄入同一份夹具，产出同一个指纹。这正是这个指纹存在的目的：让那句话可核查——而且它是
被直接断言的。

## 9. 怎么跑

```bash
python -m hisiem_soc_copilot.knowledge.cli evaluate            # all three modes, k=5
python -m hisiem_soc_copilot.knowledge.cli evaluate --mode HYBRID --mode LEXICAL_ONLY
python -m hisiem_soc_copilot.knowledge.cli evaluate --skip-ingest
python -m hisiem_soc_copilot.knowledge.cli evaluate --embedding-provider deterministic-test-only
python -m hisiem_soc_copilot.knowledge.cli evaluate --allow-ambient-corpus   # OPEN_CORPUS
```

`--skip-ingest` 通过 `find_by_external_key` 从数据库重新推导语料键映射，而不是相信一份内存里的映射，
并以一条可据以行动、列出任何缺失键的消息失败。它给一个确实存在的语料打分，否则它就不跑。

## 10. 已记录的那次基线

上面十五节描述的是这套 harness **能**做什么。这一节记录真正跑过什么，因为两者并不相同，而这个差别
正是要点。

| | |
|---|---|
| 已执行的运行 | 一次，针对 `127.0.0.1:5434` |
| embedding 证据 | `PLUMBING_ONLY`——确定性测试夹具，显式传入 |
| 产物 | `.eval-runs/knowledge/kb-golden-v1-k5-plumbing-only.json` |
| 语料 | 18 份文档（9 份 `GLOBAL`，`tenant-a`/`tenant-b`/`tenant-c` 各 3 份）、18 个版本、70 个已标注内容块——其中**17 / 17 / 67 合格**，因为 `guidance-legacy-ssh-hardening` 已退役 |
| 合格语料指纹 | `6eee7f56c8a01f50…`（`SEALED`） |
| 用例 | 22 个封存；21 个被打分，1 个被排除（那个 `unanswerable` 用例） |

```
corpus: SEALED fingerprint=6eee7f56c8a01f50 documents=17 versions=17 chunks=67
  LEXICAL_ONLY  recall@5=0.952 mrr=0.952 ndcg=0.924 citation_resolution=1.000 leaks=0 forbidden=0 scored=21
  VECTOR_ONLY   recall@5=0.381 mrr=0.248 ndcg=0.265 citation_resolution=1.000 leaks=0 forbidden=5 scored=21
  HYBRID        recall@5=0.976 mrr=0.750 ndcg=0.797 citation_resolution=1.000 leaks=0 forbidden=1 scored=21
hybrid gate: PASS
```

`HYBRID` 的 `MRR`/`nDCG@5` 略低于闭合前的数字（`0.782` / `0.828`），因为并列现在按语义身份打破，
而不是按 `uuid4`——正是这个改动把「在这台机器上可重复」变成了「跨数据库可复现」。`LEXICAL_ONLY` 与
`VECTOR_ONLY` 未变。

在这次闭合之下，运行的语料不再取决于运维自律：封存前置条件（§8.2）在打分之前从数据库推导合格语料，
并且如果一个环境文档出现在竞争作用域里就**以 `CORPUS_PRECONDITION_FAILED` 失败**。基线依然只有连同
它所取自的那个数据库状态才有意义；差别在于，跑在错误状态上的一次运行现在会拒绝产出数字，而不是产出
一个悄悄错误的数字——而上面那个指纹，就是给那个状态命名的方式。

**真实 embedding 评估未运行。** 本仓没有配置 embedding provider，所以混合闸门里语义的那一半——
§5 要求的「HYBRID 在真正的语义用例上站得住」——从未被演练过。上面 `VECTOR_ONLY` 那一行，是随机向量
*应该*打出的分数：接近随机的召回。那是**接线**的正面信号（向量通道确实在调 provider，而不是静默
降级成词法），对检索质量则毫无信号。

因此这里混合闸门的 `PASS` 只意味着一件具体的事：融合、排序、并列打破、引用解析与打分端到端接线
正确，且是在一个「一个通道有意义、另一个是噪声」的语料上。把夹具换成一个真 provider，才会把它变成
一句质量陈述——而 harness 本身不需要任何改动就能做到。

plumbing 的数字可以被引用为一次端到端管线检查。它们不可以被引用为语义基线，而本文档集中的任何东西
都没有把它们当作基线引用。

**闭合时状态：`REAL EMBEDDING EVALUATION NOT RUN`。** 配置语义 provider 这一步被刻意排除在本次闭合
的必做任务之外，所以上面那次确定性夹具运行至今仍是唯一一次运行。它不得被当作一次语义运行来替代，而
它产出的产物继续被标注为 `PLUMBING_ONLY`。
