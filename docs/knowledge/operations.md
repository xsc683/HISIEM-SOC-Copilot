# 知识子系统运维

如何供给、验证、摄取、检索与评估知识子系统。

**本文档中的任何步骤都不需要删数据卷、重置数据库或重新封存 GP-01。** 知识子系统的迁移只添加自己
的表；它们不删除任何先于它们存在的东西。闭合版迁移把每一个既有内容块向前复制，并**保留它的 `id`**，
所以无论升级还是回滚，都不会丢弃已经存下的知识。

> **知识 Agent 工具是活的。** `knowledge.retrieve_security_guidance` 与
> `knowledge.resolve_attack_technique` **已注册且模型可选**——模型能通过这两个有界、只读的工具读取
> 这个子系统（见 [security-boundary.md](security-boundary.md) §7）。
>
> 但下面这些依然是**运维流程**：供给、摄取、发布切换与评估对模型不可达，模型只能查询。

## 1. 前置条件

| 要求 | 说明 |
|---|---|
| PostgreSQL 16 **带 pgvector** | 由 `infra/docker-compose.yml` 提供，镜像是 **`pgvector/pgvector:pg16`**——一个钉住的 tag，不是 `postgres:16`，也不是 `latest`。见 §2.1。 |
| `copilot` schema | 在全新数据卷上由 `infra/postgres-init/01-copilot-schema.sql` 为你创建。在既有数据卷上可能需要手工执行一条语句——见 §2.3。 |
| Python 环境 | Windows 上是 `.venv/Scripts/python.exe` |
| `COPILOT_DATABASE_URL` | 默认 `postgresql+psycopg://copilot:copilot@127.0.0.1:5433/copilot` |

连接通过 `search_path` 钉在 `copilot` schema 上，所以该 schema 必须**先存在**，Alembic 才能在里
面记录任何东西。这个细节正是 §2 里两种令人困惑的失效模式的来源。

**永远不要 `docker compose down -v`。** 数据卷是 `copilot_pgdata`；它装着这套部署里的每一次调查、
每一行证据、每一份知识文档。本文档里的每个流程都是针对**已经存在**的那个数据卷的。

## 2. pgvector 与 `copilot` schema

pgvector 是一个**基础设施前置条件**。它是 PostgreSQL 扩展，不是应用能替你装的 Python 依赖，也无法
加到一个不自带它的服务器镜像上。

### 2.1 随仓提供的镜像

`infra/docker-compose.yml` 跑的是 **`pgvector/pgvector:pg16`**——上游 PostgreSQL 16 加上 pgvector
扩展，而且 tag 是**钉住的**，不是 `latest`。朴素的 `postgres:16` 镜像**不**自带 pgvector，所以在那个
镜像上做一次全新克隆，会在第一个迁移处就死于 `type "vector" does not exist`，再怎么配置也救不回来。
这就是默认 compose 文件用 pgvector 镜像的原因。这个服务的其他部分都没变：同样的
`container_name`、同样的 `5433:5432` 端口映射、同样的 `POSTGRES_USER`/`POSTGRES_DB`、同样的
`copilot_pgdata` 数据卷。HISIEM 自己在 `5432` 上的 PostgreSQL 不受影响。

自带二进制**不**等于扩展已经可用。扩展是**按数据库**创建的，不是按镜像创建的，所以第一次
`alembic upgrade head` 依然会发出 `CREATE EXTENSION IF NOT EXISTS vector`。这里没有任何地方假定
超级用户：迁移会先确认扩展是否存在，若角色被允许则尝试创建，否则**在任何表存在之前**就带着一条可
据以行动的消息显式失败。

### 2.2 全新克隆

```bash
docker compose -f infra/docker-compose.yml up -d
# wait for the healthcheck (pg_isready -U copilot -d copilot)
.venv/Scripts/python.exe -m alembic upgrade head
```

有两件事让这条路径端到端可用，而它们在已经存在的部署上都不是自动的：

- `infra/postgres-init/01-copilot-schema.sql` 在集群初始化期间创建 `copilot` **schema**——但只对
  空数据目录生效（§2.3）。
- 第一次 `alembic upgrade head` 在该 schema **里**创建 `vector` 扩展，于是这个类型在钉住的
  `search_path` 下能解析（§2.4）。

### 2.3 既有数据卷上的 `copilot` schema

`docker-entrypoint-initdb.d` **只在**数据目录为空时运行——对全新的 `copilot_pgdata` 恰好一次。既有
数据卷永远不会重跑它，所以既有部署可能是「有 `copilot` 数据库、没有 `copilot` schema」。症状在任何
迁移跑起来之前就出现：

```
psycopg.errors.InvalidSchemaName: no schema has been selected to create in
[SQL: CREATE TABLE alembic_version (...)]
```

以数据库管理员身份修一次：

```sql
CREATE SCHEMA IF NOT EXISTS copilot;
```

它里面的每一个对象依然归 Alembic 所有。这里创建的只是一个空命名空间，而那恰恰是 Alembic 无法替
自己做的事——它需要这个命名空间先存在，才能记录自己的版本表。

### 2.4 既有数据库上的扩展

以数据库管理员身份执行**一次**：

```sql
CREATE EXTENSION IF NOT EXISTS vector SCHEMA copilot;
```

然后 `alembic upgrade head` 正常继续。不触碰任何数据。

### 2.5 把既有数据卷升级到 pgvector 镜像

这是安全路径，且不触碰任何数据：

```bash
docker compose -f infra/docker-compose.yml stop postgres
# edit infra/docker-compose.yml: image: pgvector/pgvector:pg16
docker compose -f infra/docker-compose.yml up -d
# wait for the healthcheck, then once, as an administrator:
#   CREATE SCHEMA IF NOT EXISTS copilot;                  -- only if 2.3 applied
#   CREATE EXTENSION IF NOT EXISTS vector SCHEMA copilot;
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli doctor
```

新容器挂载的是**同一个** `copilot_pgdata` 数据卷，所以既有的每一行都还在——没有 `down -v`，没有
重置，没有重新封存。`pgvector/pgvector:pg16` 就是上游 PostgreSQL 16 加上扩展，所以它和上一个镜像
跑的是同一套服务器：磁盘格式不变，也不涉及数据目录升级步骤。`docker-entrypoint-initdb.d` 在非空
数据卷上不会重跑，这正是 2.3 与 2.4 要手工执行的原因。

### 2.6 如果 pgvector 已经装在 `public`

这就是那个会产出干巴巴、毫无帮助的 `type "vector" does not exist` 的失效模式，因为一个
`search_path=copilot` 的连接看不到它。`doctor` 会专门检出这种情况并报告：

```
[FAIL] vector_extension: the vector extension is installed but not visible on
       this connection's search_path; run: ALTER EXTENSION vector SET SCHEMA copilot;
```

修一次：

```sql
ALTER EXTENSION vector SET SCHEMA copilot;
```

然后重跑迁移——或者把连接指到一个包含 `public` 的 `search_path`，如果你的部署更偏好那样。两种都
可行；推荐前者，因为它让类型由钉住的 search path 解析。

### 2.7 迁移无法继续时会说什么

```
The PostgreSQL 'vector' extension (pgvector) is required by this migration and is
not installed, and this role may not create it. Ask a database administrator to run
`CREATE EXTENSION IF NOT EXISTS vector SCHEMA copilot;` in this database once, then
re-run `alembic upgrade head`. Nothing was changed by this failed run.
```

失败的一轮是原子的：检查跑在任何 `CREATE TABLE` 之前。

### 2.8 本机既有数据库的实测状态

这里有两个相关数据库，它们**不可互换**。

| 数据库 | 状态 | 用途 |
|---|---|---|
| `127.0.0.1:5433` | 运维的 Copilot 数据库。PostgreSQL 16.15。修订 `979070495d4f`（P2）——落后当前 head（`ed6af82d9b13`、`c41f7b2e9d08`、`a5e93c07fd21`、`b6c2a4d19f30`）**四个**修订。`pg_available_extensions` 里既没有 `vector` 也没有任何 pgvector 包，所以在这个按现有打包的服务器上 `CREATE EXTENSION vector` **不可能**成功。 | **只读。** 对只做读取的 `doctor` 是安全的。在它的服务器镜像带上 pgvector 之前（§2.5），绝不要对它跑 `alembic upgrade`/`downgrade` 或任何 DDL。 |
| `127.0.0.1:5434` | 支撑 pgvector 的测试数据库，供知识子系统集成套件使用。 | 由测试迁移、演练并反复升降。 |

这就是知识子系统集成测试硬编码 `127.0.0.1:5434`、而不遵从 `COPILOT_DATABASE_URL` 的原因：一个会写
运维数据库的测试是缺陷，不是便利。

因此把 `5433` 提升到当前状态是一次**多步**运维动作，不是一步：把该服务器切到 pgvector 镜像
（§2.5）、若 §2.3 适用则执行 `CREATE SCHEMA`、跑上面那一次性的 `CREATE EXTENSION`，然后
`alembic upgrade head`。在那之前，对 `5433` 跑 `doctor` 会报 `NOT_READY`，`vector_extension` 与
`knowledge_schema` 为 FAIL——这是正确的、非破坏性的答案，也是它在不触碰任何东西的前提下给出的答案。
下面是本次闭合时**实测**的输出：

```
knowledge doctor: NOT_READY
  database: postgresql+psycopg://copilot:***@127.0.0.1:5433/copilot
  [OK] database: connected
  [FAIL] vector_extension: the vector extension is not installed in this database; see docs/knowledge/operations.md for the one-time prerequisite
  [FAIL] knowledge_schema: missing tables: attack_release, attack_technique, embedding_profile, knowledge_chunk_embedding, knowledge_content_chunk, knowledge_document, knowledge_document_version (run: alembic upgrade head)
  [FAIL] active_embedding_profile: not checked: the knowledge schema is missing (run: alembic upgrade head)
  [WARN] embedding_provider: no embedding provider configured (EMBEDDING_PROVIDER=unconfigured): lexical retrieval only
```

`attack_release_authority`、`attack_release_projection` 与 `legacy_chunk_table` 没有出现在那份清单
里，而不是被报成失败：它们查询的表在缺失的 schema 下并不存在，而报告说明的是「某项检查为何没有
跑」，而不是把同一个原因重复一遍。

## 3. 迁移

```bash
.venv/Scripts/python.exe -m alembic heads
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m alembic check
```

知识子系统的链条是 `979070495d4f`（P2 响应生命周期迁移）→ `ed6af82d9b13`（最初的知识子系统 schema）
→ `c41f7b2e9d08`（闭合修订：不可变内容块与 ATT&CK 发布模型）→ `a5e93c07fd21`（发布 → 知识投影绑定
与 fail-closed 降级守卫）→ **`b6c2a4d19f30`**（head；该修订新增的可空 outbox `traceparent`
诊断上下文列）。

`ed6af82d9b13` 已发布且**严格不可修改**，所以闭合版的 schema 变更都以堆在它上面的新修订形式到来。
这次升级是纯追加的，在一个已经带有原知识子系统表的数据库上是安全的：它把每个既有内容块复制进新的不可变
对，并**保留它的 `id`**——这正是升级之前铸出的一个 `kcit:` 把手能解析到「现在持有其内容的那一行」
的原因。embedding 行拿到新的代理 id，而这之所以安全，恰恰因为那张表是可重建投影。

### 验证一次迁移循环

```bash
.venv/Scripts/python.exe -m alembic downgrade -1
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m alembic check
```

在该修订的 head 上，`downgrade -1` 只移除那可空的 outbox `traceparent` 列。`downgrade -2` 退回
穿过 `a5e93c07fd21`，在跑完降级守卫之后**只撤销它自己创建的东西**——`attack_release_projection`
表（见下文**降级安全**）。下一步，`downgrade -3`（或显式指定 `ed6af82d9b13`），才是撤销
`c41f7b2e9d08` 所创建内容的那一步：`knowledge_content_chunk`、`knowledge_chunk_embedding`、
`attack_release`，以及它加到 `attack_technique` 上的外键。每一个知识子系统之前的对象——
`investigation`、`domain_event`、`outbox_message`、`command_receipt`、`orchestration_binding`、
`tool_invocation`、`response_proposal` 以及 LangGraph checkpoint schema——都不受触动，
`knowledge_chunk` 也不受触动，它由 `ed6af82d9b13` 创建、因而归它所有。让它继续站着的意义在于：
`downgrade -1` 之后再 `upgrade head` 会收敛，而不是丢掉那些升级本须回填的行。`vector` 扩展被刻意
**不**删除：它可能早于知识子系统存在，别的 schema 也可能依赖它，删掉它会是一次远远超出本迁移范围的破坏性
变更。

有一个值确实无法还原：一个与它自己的发布相矛盾的遗留 `attack_technique.active` 标志。升级拒绝把
这样一个 framework 的行当作权威，所以也就没有权威可以放回去——而从「闭合版本来就为退役它而存在的
那个标志」重新推一个权威出来，恢复的是歧义，不是信息。

`alembic check` 必须在升级之后、以及降级/升级循环之后都报告无漂移。

### 降级安全

`a5e93c07fd21` 在 `downgrade` 前面放了一道 **fail-closed 守卫**。它的前驱 `c41f7b2e9d08` 在下行时
无条件 drop 掉 `knowledge_content_chunk`、`knowledge_chunk_embedding` 与 `attack_release`，并在
上行时不再更新遗留的 `knowledge_chunk` 表——所以一旦这套部署写穿过新表，降级越回它就会把那些工作
静默毁掉。一条迁移要么无损，要么必须拒绝。

守卫在**第一个 `op.drop_*` 之前**运行，所以一次拒绝意味着那一轮什么都没改。它抛
`P3A_DOWNGRADE_UNSAFE`，逐类别点名其行数——永不涉及任何内容：

| 类别 | 统计什么 |
|---|---|
| `NEW_CONTENT_CHUNKS` | 没有闭合前 `knowledge_chunk` 行的内容块；旧 schema 无处安放它们 |
| `MULTIPLE_CHUNK_GENERATIONS` | 位于旧 schema 没有对应列的那一代的内容块，于是它陈旧的 generation-1 行会被当作当前行重新呈现 |
| `PROJECTION_CHANGED` | 旧单向量行无法表示的 embedding 行；把陈旧向量当作当前向量还原会是错的，而不只是有损 |
| `MUTATED_CONTENT` | 存储内容已不再匹配的闭合前内容块；旧 schema 会把陈旧字节当作当前内容服务 |
| `PINNED_ATTACK_RELEASE` | 携带内容指纹的发布，而旧 schema 没有这一列 |
| `ATTACK_PROJECTION_BINDING` | 任何发布 → 投影绑定；旧 schema 无法表达某个发布暂存了哪个版本 |

每个判定都被选成在「升级过但此后未被写入」的数据库上为**零**，所以紧邻的往返仍然通过：

```bash
# from a pre-closure database: upgrade, then one step back
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m alembic downgrade -1
```

一道连*这个*都挡住的守卫才是 bug，而不是守卫。

**那位残留，直说。** 守卫必须住在新修订里，因为 `c41f7b2e9d08` 是冻结的、不得编辑。因此它会拦下任何
**从这个 head 开始**的降级——`downgrade -1` 与 `downgrade <更早修订>` 都会先跑它——但一个在本修订
存在之前就停在 `c41f7b2e9d08` 的数据库**不**在它覆盖范围内，因为这个修订的函数根本不会运行。如果
`alembic current` 报的是 `c41f7b2e9d08`，先跑 `alembic upgrade head`（免费——升级是纯追加的），
然后才能降级。

## 4. 建出的 schema

| 表 | 用途 |
|---|---|
| `knowledge_document` | 外部可标识的文档。身份不可变；ACTIVE → RETIRED 生命周期。 |
| `knowledge_document_version` | 不可变内容。变更会追加一个版本。 |
| `knowledge_content_chunk` | **不可变内容身份**——引用目标。写一次、永不重写，并带生成的 FTS 列。 |
| `knowledge_chunk_embedding` | 内容块**可重建的**向量投影。不带内容，也不带哈希。 |
| `embedding_profile` | 内容块被索引进去的向量空间。至多一个 ACTIVE。 |
| `attack_release` | 钉住的 ATT&CK 发布及其权威。每个 framework 至多一个 ACTIVE。 |
| `attack_technique` | 归属于某个发布的钉住技术快照。 |
| `attack_release_projection` | 某个发布的技术到该发布**所暂存**的那个不可变文档版本的绑定。来源，不是权威。 |
| `knowledge_chunk` | **已被取代，予以保留。** `ed6af82d9b13` 的表。知识子系统从不读写它；留着它只是为了让 `downgrade` 能逐字节还原它。`doctor` 报告它，而不是 drop 它。 |

迁移之后值得核对的属性：

- **没有 HNSW，没有 IVFFlat。** 本仓精确排序。
- `embedding` 列是**无类型**的 `vector` 类型——schema 里没有烧进任何维度。
- `lexical_document` 是一个 `GENERATED` `tsvector` 列，带 **GIN** 索引，所以它永远不会和它所描述的
  内容漂移。
- `uq_embedding_profile_single_active` 是 `status` 上的**部分唯一索引**
  （`WHERE status = 'ACTIVE'`）——「只有一个 ACTIVE」是数据库事实，不是应用代码。
- `uq_knowledge_document_global_key` 与 `uq_knowledge_document_tenant_key` 是**部分**唯一索引。
  两个，不是一个：`NULL` 在朴素唯一索引里永不冲突，所以单个索引会静默允许重复的全局文档。
- `uq_knowledge_content_chunk_generation_ordinal` 在
  `(document_version_id, generation, ordinal)` 上唯一。正是它让稳定排序键成为**全序**，也意味着
  一次重新分块写的是新的一代，而不是重写旧的一代。
- `uq_knowledge_chunk_embedding_content_profile` 在
  `(content_chunk_id, embedding_profile_id)` 上唯一——一个空间里每个内容块一个向量，所以重建是
  upsert 而不是重复插入。
- `uq_attack_release_single_active` 是一条**按 framework 的部分唯一索引**
  （`WHERE status = 'ACTIVE'`）。因此「每个 framework 至多一个权威 ATT&CK 发布」是数据库事实，不是
  约定：两次并发激活不可能都提交。`uq_attack_release_framework_source_release` 让一个发布名只能
  注册一次。
- `uq_attack_release_projection_release_technique` 在
  `(framework, source_release, technique_id)` 上唯一——那个让被重试的暂存收敛而不是重复的冲突目标
  ——而 `uq_attack_release_projection_release_document` 在
  `(framework, source_release, document_id)` 上唯一，所以一个发布不能声称同一份文档的两个不同投影。
  因此「暂存与切换之间发生崩溃」会重新落进同样的行，而不是落进一次重复或一次假冲突。

## 5. embedding 配置

embedding provider 的配置**独立于对话 LLM**。它们是两个不同的服务、有不同的契约，而「假定对话
endpoint 也能做 embedding」正是这种分离要防的那个错误。

| 变量 | 含义 |
|---|---|
| `EMBEDDING_PROVIDER` | `unconfigured`（默认）或 `openai_compatible` |
| `EMBEDDING_BASE_URL` | 例如 `https://api.example.com/v1` |
| `EMBEDDING_MODEL` | embedding 模型 id |
| `EMBEDDING_DIMENSION` | 该模型产出的向量维度 |
| `EMBEDDING_API_KEY` | 密钥。**名字可通过 `EMBEDDING_API_KEY_ENV` 配置** |
| `EMBEDDING_NORMALIZATION` | `NONE`（默认）或 `L2` |
| `EMBEDDING_DISTANCE_METRIC` | `COSINE`（当前唯一取值） |
| `EMBEDDING_TIMEOUT_SECONDS`、`EMBEDDING_MAX_RETRIES` | 只对瞬时故障做有界重试 |

**默认 `unconfigured`。** 这是一个诚实的默认值：没有真 provider 就没有向量检索，系统如实说明这一
点，而不是拿假向量顶替。`LEXICAL_ONLY` 检索照常工作。

API key 永不是配置默认值，永不被记录，永不出现在异常消息里，也永不被 `doctor` 回显——它只报告配置
是否*存在*。

### 切换 ACTIVE profile 不是一次摄取

当存在一个 `ACTIVE` profile、而所配置 provider 的描述符身份与它不同时，**每一次**普通文档摄取都
fail closed：

```
error: EMBEDDING_PROFILE_SWITCH_REQUIRES_CORPUS_REINDEX: ...
```

这**包括**传了 `--allow-embedding-profile-switch` 的那次摄取。该 flag 是**遗留的、且一律拒绝**：
保留它只是为了让既有调用方收到那条诊断，而不是一个「未知参数」错误，它不做别的事。

这是刻意的，而替代方案的后果比看上去更糟。让一份文档的摄取把旧 profile 退役并创建一个新的
`ACTIVE` profile，会让语料一半嵌在一个空间、一半嵌在另一个不可比的空间，而检索还在跨它们比较余弦
距离——算出一些看似合理的数字，却不存在于任何一个单一空间里。切换 embedding 空间是**全语料重建
索引**，不是一次文档摄取。

一次拒绝之后，下列全部依然成立：

- 先前的 profile **仍然是** `ACTIVE` profile；
- 既有语料仍然向量可检索；
- 单 ACTIVE profile 索引完好，因为没有创建第二个 profile；
- 没有任何 embedding 投影行被重写。

那条正确的全语料流程——暂存一个新 profile、重建整个语料的索引、校验完整性、原子激活、退役旧
profile——已为未来阶段记录在案，并在此刻意**不实现**。不存在部分切换，也不存在一个「生产检索可能
误用」的 `STAGING` 状态。

### 确定性测试夹具

`--embedding-provider deterministic-test-only` 选择一个开发夹具，它的向量**不携带任何语义含义**。
它存在的意义是演练接线。它永不是生产默认值，它的模块只在那条分支上被导入，它产出的任何产物都会在
文件名和 JSON 里被标成 `PLUMBING_ONLY`。

### 知识上限

| 变量 | 默认值 | 含义 |
|---|---|---|
| `KNOWLEDGE_MAX_DOCUMENT_BYTES` | 2,000,000 | 拒绝阈值 |
| `KNOWLEDGE_MAX_NORMALIZED_CHARS` | 2,000,000 | 拒绝阈值 |
| `KNOWLEDGE_MAX_CHUNKS_PER_DOCUMENT` *（或 `KNOWLEDGE_MAX_CHUNKS`）* | 512 | 每文档最大内容块数 |
| `KNOWLEDGE_MAX_CHUNK_CHARS` | 8,000 | 每内容块最大字符数 |
| `KNOWLEDGE_CHUNK_TARGET_TOKENS` *（或 `KNOWLEDGE_CHUNK_TARGET`）* | 600 | 分块器目标 token 数 |
| `KNOWLEDGE_CHUNK_MAX_TOKENS` *（或 `KNOWLEDGE_CHUNK_MAX`）* | 800 | 分块器最大 token 数（上限 800） |
| `KNOWLEDGE_CHUNK_OVERLAP_TOKENS` *（或 `KNOWLEDGE_CHUNK_OVERLAP`）* | 80 | 分块器重叠，必须 < 目标 |
| `KNOWLEDGE_LEXICAL_CANDIDATE_LIMIT` | 20 | 检索 profile |
| `KNOWLEDGE_VECTOR_CANDIDATE_LIMIT` | 20 | 检索 profile |
| `KNOWLEDGE_RRF_K` | 60 | 检索 profile |
| `KNOWLEDGE_MAX_HITS_PER_DOCUMENT` | 2 | 多样化上限 |
| `KNOWLEDGE_EVALUATION_OUTPUT_DIR` | `.eval-runs/knowledge` | 产物目录 |

分块器上限两种拼写都接受；各自都是完整名字，所以任一个都能绑定。

所有上限都是**拒绝**，永远不是截断。

## 6. `doctor`

任何地方看着不对时，第一件要跑的东西。

```bash
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli doctor
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli doctor --json
```

```
knowledge doctor: DEGRADED
  database: postgresql+psycopg://copilot:***@127.0.0.1:5434/copilot
  [OK] database: connected
  [OK] vector_extension: installed and visible
  [OK] knowledge_schema: 7 knowledge tables present
  [WARN] active_embedding_profile: no ACTIVE embedding profile: lexical retrieval works, vector and hybrid retrieval are unavailable until a document is ingested
  [WARN] attack_release_authority: no ACTIVE ATT&CK release: every canonical technique row is non-authoritative. ...
  [WARN] attack_release_projection: no ACTIVE ATT&CK release to check; there is no authoritative projection to compare retrieval against
  [OK] legacy_chunk_table: knowledge_chunk present with 0 superseded row(s): kept only so a downgrade can restore them byte for byte, never read or written by P3-A
  [WARN] embedding_provider: no embedding provider configured (EMBEDDING_PROVIDER=unconfigured): lexical retrieval only
```

八项检查，全部只读：

| 检查 | 何时 FAIL | 何时 WARN |
|---|---|---|
| `database` | 不可达 | — |
| `vector_extension` | 未安装，或已安装但在本连接的 `search_path` 上不可见 | — |
| `knowledge_schema` | `KNOWLEDGE_TABLES` 里那**七**张有任何一张缺失（消息会告诉你跑 `alembic upgrade head`） | — |
| `active_embedding_profile` | — | 没有 ACTIVE profile |
| `attack_release_authority` | 某个 framework 有多于一个 ACTIVE 发布——`ATTACK_RELEASE_AUTHORITY_AMBIGUOUS` | 完全没有 ACTIVE 发布 |
| `attack_release_projection` | 某个权威发布对某项技术没有已暂存投影，或它绑定的文档服务的是别的版本——`ATTACK_RELEASE_PROJECTION_DIVERGED` | 没有 ACTIVE 发布可与检索对照 |
| `legacy_chunk_table` | 永不 | —（报告 `absent`，或保留的行数） |
| `embedding_provider` | — | 未配置 |

`attack_release_authority` 只读 `attack_release`，别的都不读，因为权威住在发布粒度上。schema 本身
已经用部分唯一索引强制了单 ACTIVE 规则；这项检查存在的理由是：一个还原了 dump、或乱序应用了迁移集
的运维，可能最终拿到一个索引缺失的库——那时行是唯一剩下的证人。它也正是一处让「升级刻意拒绝解决的
那种歧义」变得可见的地方；见 §8。

`attack_release_projection` 是它的搭档，也是唯一一项把「本次闭合让它们一起移动」的两个事实放在一起
比较的检查：该发布的权威，以及它每份已绑定文档实际服务的版本。它只读，且对内容保持沉默——只涉及
技术 id 与 external key——而且它是 `ATTACK_RELEASE_PROJECTION_DIVERGED` 唯一会被报告的地方，因为
切换在能造出那种状态之前就先拒绝了；一个 dump 还原、或乱序应用的修订，才是数据库抵达那种状态的
途径。重新导入该发布会把它切回来（§8）。

判定：任何失败即 `NOT_READY`，任何警告即 `DEGRADED`，否则 `READY`。退出码在 `NOT_READY` 时为
`1`，其他为 `0`。

`DEGRADED` 是对「语料可达但向量通道不可用」的诚实回答：词法检索是能用的。把它压成 `READY` 或
`NOT_READY` 中的任何一个，要么夸大了能用的部分，要么藏起了一条可用的路径。

当数据库不可达时，那六项依赖它的检查会报成 `FAIL — not checked: the database is unreachable`，
而不是再堆上六个同一原因的复读。`embedding_provider` 仍然会跑：它读的是配置而不是数据库，所以即便
别的都答不上来，它也有答案。

当 schema 缺失时，只有 `active_embedding_profile` 会被追加成
`not checked: the knowledge schema is missing (run: alembic upgrade head)`，而
`attack_release_authority`、`attack_release_projection`、`legacy_chunk_table` 以及 profile 检查
干脆不出现。这是检查数量不足八的唯一情形，而且它是刻意的：再多四行「没有这张表」会埋掉那唯一一行
告诉运维该做什么的话。

## 7. 摄取一份文档

只支持本地 `.txt` 与 `.md` 文件。PDF、DOCX 与 HTML 在**任何字节被解码之前**就被拒绝——一个我们没有
的解析器会静默摄取任何碰巧被解出来的东西。

```bash
# GLOBAL curated guidance
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli ingest-file \
  --path docs/guidance/ssh-hardening.md \
  --source-kind CURATED_GUIDANCE \
  --external-key ssh-hardening \
  --visibility GLOBAL \
  --title "SSH hardening guidance"

# A tenant's own runbook
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli ingest-file \
  --path runbooks/triage.md \
  --source-kind TENANT_RUNBOOK \
  --external-key triage-runbook \
  --visibility TENANT --tenant tenant-a \
  --title "Triage runbook" \
  --source-version 2026.09 \
  --metadata owner=soc-engineering --metadata review=quarterly
```

输出：

```
document <uuid> version 1 (a1b2c3d4e5f6) chunks=7 created_document=True
created_version=True rebuilt_projection=True
```

- `GLOBAL` **禁止** `--tenant`；`TENANT` **要求**它。两者都在 CLI 边界处被拒绝，早于任何归一化、
  哈希或 embedding。
- 用**完全相同的内容**重跑同一条命令，会报 `created_version=False`，并且不会再调 embedding
  provider。这是幂等，不是缓存：`UNIQUE(document_id, content_hash)` 把「相同字节不能创建第二个
  版本」变成数据库事实。
- 改内容会追加版本 2 并移动 `active_version_id`。旧版本行永不被修改。没有任何东西被静默覆盖。
- `ingest-file --source-kind` 只接受 `CURATED_GUIDANCE` 与 `TENANT_RUNBOOK`。`MITRE_ATTACK`
  内容只能通过 `import-attack` 进入（§8）：普通 handler 以 `SYSTEM_MANAGED_KNOWLEDGE_SOURCE`
  拒绝它，而解析器在读任何东西之前就拒绝这个取值。
- 同一个文件从 Windows 检出和从 Linux 检出摄取，哈希相同。

### 退役一份文档

```bash
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli retire \
  --tenant tenant-a --document-id <uuid> --reason "superseded by v2 guidance"
```

退役是终态的，且不需要 embedding provider——撤回一份文档是生命周期变迁，不是 embedding 操作，它
必须在 embedding 故障期间照样能用。已退役文档会离开普通检索。退役之前捕获的引用**仍然能解析**。

通过这条命令退役一份 MITRE 文档会被 `SYSTEM_MANAGED_KNOWLEDGE_SOURCE` 拒绝：权威投影只能由一个
ATT&CK 专属工作流撤回，在某个 ACTIVE 发布底下把它撤掉会让那个发布的权威变成孤儿。

## 8. 导入 MITRE ATT&CK

只支持本地的、由运维提供的 STIX 2.1 JSON。**没有 URL，没有运行时 GitHub 访问。** 用 Enterprise
bundle；测试用的是一份小的钉住夹具，而不是上游那份 100 MB+ 的数据集。

```bash
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli import-attack \
  --file attack-enterprise.json --release v15.1 --activate
```

输出：

```
release v15.1: parsed=214 techniques_created=214 documents_created=214
versions_ingested=214 unchanged=0 skipped=0
```

- 只支持 Enterprise。不支持的 framework 会被拒绝。
- `--activate` 是**显式的权威开关**，而且现在是唯一能改变检索所服务内容的东西。注册与暂存无论如何
  都会发生：该发布的规范行被写入，每项技术都以不可变版本连同它的内容块与 embedding 被摄取。
  `--activate` 在此之上加上**切换**——一个事务，校验暂存投影、翻转本发布的权威、把**同一
  framework** 的其他每个发布置为非 active，并把每份已绑定文档的指针移到**本发布所暂存**的那个版本。
  不带 `--activate` 时，发布被注册并**完整暂存但 INACTIVE**：它的版本、内容块与 embedding 全都
  存在，而普通检索继续服务它此前服务的那些内容。**新发布只是加新行**；旧发布永不被删除，并且能被
  自己的 `source_release` 继续读到。
- 重新导入一个**已经**权威的发布，即便不带 `--activate` 也会再次切换，因为只要存储的发布是 ACTIVE，
  这次导入就具有权威意图。这是一条针对已漂移投影的修复路径：它重新校验并重新指向，否则就在什么都
  不写的情况下拒绝。
- 发布以**内容指纹**注册进 `attack_release`——对它的规范技术集合求 SHA-256，按
  `(technique_id, source_stix_id)` 排序，`tactics`/`platforms` 排序并去重。它与输入 JSON 的对象
  顺序、以及 STIX bundle 顺序无关，并且可以仅凭数据库重新推导。
- 每项技术成为一份 GLOBAL 的 `MITRE_ATTACK` 文档，`external_key = mitre-attack:<technique_id>`、
  `source_version = <release>`，经与运维 runbook **同一条**版本化/分块/embedding 路径摄取。不存在
  第二条摄取管线。
- 被 `revoked` 或 `deprecated` 的技术会被**跳过并报告**，永不静默丢弃。`skipped ids:` 那一行列
  出它们。
- 用同一个 bundle 重新导入同一个发布是幂等的：`techniques_created=0`、`versions_ingested=0`、
  `unchanged=<技术数>`。
- 畸形的 bundle 在写入任何东西**之前**被拒绝；一次坏导入永远不会是部分的。超出大小上限的 bundle
  不会被解析就被拒绝。

规范 `attack_technique` 行与知识文档是**相关但不同**的权威：行是规范投影，文档是它的**检索投影**，
而规范行哈希与文档版本的内容哈希来自同一个规范函数，所以一个投影文档不可能描述它的规范行不描述的
内容。

这**不**等于说检索自动就在服务权威发布。一份文档只携带一个 `active_version_id`，而「投影是同一内容
的投影」与「这就是检索返回的版本」不是同一个断言。`attack_release_projection` 就是补上这个缺口的
东西：它记录某个发布暂存的确切版本，而切换把指针移到它上面。

### 注册、暂存、切换

一次导入是三个动作，只有第三个能改变任何人能读到的东西。

1. **注册。** bundle 被解析、取指纹，并按钉住的发布校验；随后 `attack_release` 行及其
   `attack_technique` 行在**一个短事务**里写成 **INACTIVE**。与钉住指纹冲突的 bundle 会在这里被
   拒绝，早于写入任何东西。
2. **暂存。** 每项技术都经普通版本/内容块/embedding 路径摄取，带 `activate_version=False`：不可变
   版本、它的内容块与 embedding 都被创建，而**没有任何文档指针移动**。每项技术记录一条
   `attack_release_projection` 绑定，全部在一个属于它们自己的短事务里完成（按发布一个，不是按技术
   一个），所以崩溃后的重试会收敛而不是重复。
3. **切换**，仅当这次导入具有权威意图。一个事务取该 framework 的 advisory lock、在**变更任何东西
   之前**校验投影、翻转该发布的权威与 `attack_technique.active` 镜像，然后把每份已绑定文档指到本
   发布所暂存的那个版本。因为它是一个事务，一次拒绝——无论是在第一次写之前被抓到，还是在解析某条
   绑定时被抓到——都会让该发布的权威和每一个文档指针原封不动。

要记住的后果是：一个已注册、已暂存但从未切换的发布**已完整投影，且对检索完全不可见**。在这次闭合
之前，暂存走的是通用摄取路径，而那条路径会无条件激活每个新版本——所以为了回填而导入一个发布，会
静默改变普通检索返回的东西。

### 钉住的发布不可变

「v15.1」是一句不可变声明，而指纹是让它可核查的东西：

| 情形 | 行为 |
|---|---|
| 某发布的首次导入 | 指纹持久化到 `attack_release` |
| 同一发布、同一指纹 | 幂等——导入收敛，不重写任何东西 |
| 同一发布、**不同**指纹 | 以 `ATTACK_RELEASE_CONTENT_CONFLICT` 拒绝，且在**任何变更之前**检出 |
| 从早于指纹机制的既有行收养的发布 | `content_fingerprint IS NULL`；第一次把它钉住的导入会与数据库里实际存的行比较 |

```
error: ATTACK_RELEASE_CONTENT_CONFLICT: release v15.1 is already pinned to a
different technique collection; re-import the pinned bundle or use a new release name
```

一次被拒的导入不留下**任何**规范 `attack_technique` 行、不留 `KnowledgeDocument`、不留
`KnowledgeDocumentVersion`、不改变权威发布、也不留下 embedding 投影行。因为检查跑在最前面，「什么
都没被改」是一个事实，而不是事后重建出来的说法。

STIX 对象顺序被打乱、或一份语义相同但序列化方式不同的 bundle，产出**同一个**指纹，因而收敛。只有
技术内容变了才会冲突——这正是要点：这项检查不得在重新序列化时误报，也不得在真正的内容变更上保持
沉默。

### 崩溃与重试

一次在规范行已提交、但文档尚未完成时被中断的运行，会让指纹留在库里。用**同一**发布、**同一**指纹
重跑会继续并完成文档：不会有重复的规范行，不会有假冲突。用**不同**指纹重跑依然冲突，即便文档投影
还不完整——不存在「半新版本的回填」。

### `ATTACK_RELEASE_AUTHORITY_AMBIGUOUS`

如果既有行已经让某个 framework 有多于一个发布在声称权威，升级会**拒绝去猜**并报告：

```
ATTACK_RELEASE_AUTHORITY_AMBIGUOUS - ...
```

`doctor` 的 `attack_release_authority` 检查在运行时报告同样的事情。运维通过用 `--activate` 导入
那个意图中的发布来显式解决它；选择哪份规范知识算权威是运维的决定，不是迁移的。

## 9. 检索

```bash
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli search \
  --tenant tenant-a --topic T1110 --context-term ssh --mode hybrid --limit 5

.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli search \
  --tenant tenant-a --topic "brute force" --context-term authentication --json
```

```
profile=hybrid-v1 hits=2 truncated=False
1. [MITRE_ATTACK] Brute Force (3f2a…) citation=kcit:9c1d…:a1b2c3d4e5f6
   Adversaries may use brute force techniques to gain access…
```

`--mode` 取 `hybrid`（默认）、`lexical` 或 `vector`。向量或混合检索需要一个 ACTIVE embedding
profile 和一个已配置的 provider；两者缺任一，命令会报告原因并以 `3` 退出，而不是崩掉。

`--json` 输出一组有文档的键：`profile_id`、`truncated`、`retrieval_profile`，以及每条命中的
`citation_id`、`document_id`、`document_version_id`、`chunk_id`、`source_kind`、`title`、
`language`、`source_version`、`excerpt`。**永远没有向量，永远没有原始行，永远没有凭据。**

已退役文档不出现。GLOBAL 文档对每个租户可见；某租户自己的文档只对该租户可见。

## 10. 解析引用

```bash
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli resolve-citation \
  --tenant tenant-a --citation kcit:<chunk-uuid>:<hash-prefix>
```

```
kcit:9c1d…:a1b2c3d4e5f6: resolved
```

解析成功退出 `0`，不成功退出 `1`。`--json` 额外给出 `reason`：

| 原因 | 含义 |
|---|---|
| `MALFORMED_CITATION` | 字符串解析不了。永不修补，永不信任。 |
| `CHUNK_NOT_FOUND` | 没有这个内容块，或对本租户不可读。 |
| `SCOPE_MISMATCH` | 该内容块属于另一个租户。 |
| `CONTENT_INTEGRITY_MISMATCH` | 存储的哈希不是实际读到内容的哈希，**或**存储的哈希不携带串里的那个前缀。 |

两项完整性检查都需要，它们抓的是不同的篡改。存储的哈希不是证据；它是*写在内容旁边的一句声明*。
解析器对**实际读到的**内容重算 `SHA-256`，要求重算值等于存储的完整哈希，**并且**存储哈希以该引用的
前缀开头。带外改文本会破坏前者；把存储哈希改成与伪造前缀匹配会破坏后者。无论哪一种，答案都是带该
原因的 `unresolved`，且没有异常逃出去。

解析**刻意对退役文档、历史版本以及已被取代的分块代仍然有效**。一次过去调查里捕获的引用，必须在它
指向的文档被取代、撤回或重新分块之后仍然可解释。引用命名的是**不可变内容块**，永远不是 embedding
行，所以它还能在重新嵌入、embedding profile 重建、检索投影重建以及进程重启之后存活。

普通 `search` 排除已退役文档与更早的代；`resolve` 两者都不排除。本仓**不提供破坏性删除**，所以让
一条历史引用无法解析的唯一途径是运维手工删行——这正是上面那两项完整性检查是重算、而不是读取的
原因。

解析证明**来源**。它不证明正确性。

## 11. 跑基线

```bash
# Full KB-GOLDEN-V1 suite over the deployment's embedding provider
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli evaluate

# Explicit modes
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli evaluate --mode LEXICAL_ONLY --mode HYBRID

# Re-score a corpus already in the database
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli evaluate --skip-ingest

# Plumbing only -- the deterministic fixture, explicitly requested
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli --embedding-provider deterministic-test-only evaluate

# NOT a sealed baseline -- measure whatever the database currently holds
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli evaluate --allow-ambient-corpus
```

针对已配置 provider 的一次运行会打印 provider 行；一次 plumbing 运行会改为打印警告行，所以两者在
屏幕上永远不可能被混淆：

```
KB-GOLDEN-V1 (corpus 1) cases=22 k=5
corpus: SEALED fingerprint=<16 hex> documents=17 versions=17 chunks=67
embedding: openai_compatible/text-embedding-3-small dim=1536 COSINE
  LEXICAL_ONLY  recall@5=0.xxx mrr=0.xxx ndcg=0.xxx citation_resolution=1.000 leaks=0 forbidden=0 scored=21
  VECTOR_ONLY   recall@5=0.xxx mrr=0.xxx ndcg=0.xxx citation_resolution=1.000 leaks=0 forbidden=0 scored=21
  HYBRID        recall@5=0.xxx mrr=0.xxx ndcg=0.xxx citation_resolution=1.000 leaks=0 forbidden=0 scored=21
hybrid gate: PASS -- ...
artifact: .eval-runs/knowledge/kb-golden-v1-k5.json
```

`cases=22` 数的是夹具；`scored=21` 数的是某个模式实际排过序的用例。差值是夹具里的 `unanswerable`
用例，它们按设计被排除在均值指标之外，并作为 `cases_excluded` 报告。

**一次完整的 plumbing 运行**——当未配置 embedding provider、且显式传入
`--embedding-provider deterministic-test-only` 时，输出就是这个形状。它是本仓至今产出过的唯一一次
运行，而它是一次管线检查，不是质量测量：

```
KB-GOLDEN-V1 (corpus 1) cases=22 k=5
corpus: SEALED fingerprint=6eee7f56c8a01f50 documents=17 versions=17 chunks=67
embedding: PLUMBING ONLY (deterministic-test-only) -- these vectors carry no semantic meaning
  LEXICAL_ONLY  recall@5=0.952 mrr=0.952 ndcg=0.924 citation_resolution=1.000 leaks=0 forbidden=0 scored=21
  VECTOR_ONLY   recall@5=0.381 mrr=0.248 ndcg=0.265 citation_resolution=1.000 leaks=0 forbidden=5 scored=21
  HYBRID        recall@5=0.976 mrr=0.750 ndcg=0.797 citation_resolution=1.000 leaks=0 forbidden=1 scored=21
hybrid gate: PASS -- hybrid mean recall@5 0.976 against the better single-channel baseline 0.952 (tolerance 0.02); 6 mixed case(s) retrieved
artifact: .eval-runs/knowledge/kb-golden-v1-k5-plumbing-only.json
```

`documents=17` 而不是 18：夹具自己退役了一份文档，而「合格」意味着租户可见**且** ACTIVE。

这些就是当前代码在测试数据库上产出的数字。`HYBRID` 的 `mrr`/`ndcg` 略低于闭合前的数字
（`0.782`/`0.828`），因为并列现在按语义身份打破，而不是按一个随机 UUID——正是同一个改动让这条基线
能跨数据库复现，而不只是在一个库上可重复。`LEXICAL_ONLY` 与 `VECTOR_ONLY` 未变。

### 语料前置条件

在第一次检索跑起来之前，这次运行会从数据库推导出租户可见、ACTIVE、合格的语料，并与封存的夹具比对。
一个意外的文档、一个缺失的预期文档、或一个变过的内容哈希/版本/内容块投影，都会让这次运行以
`CORPUS_PRECONDITION_FAILED` 失败，而不是产出一个分数：

```
error: CORPUS_PRECONDITION_FAILED: expected corpus fingerprint ... does not match ...
```

在错误的语料上量出来的排序不只是没用；它是一个会被人引用的数字。默认是**封存**的，而处在竞争作用域
里的环境文档属于前置条件失败，不是一条宽容的警告。

`--allow-ambient-corpus` 是一次**不同的测量**，不是一次降低了标准的封存运行。它完全跳过前置条件，
把产物标成 `corpus_mode: "OPEN_CORPUS"` / `sealed: false`，把文件命名为 `...-open-corpus.json`，
打印 `NOT A SEALED BASELINE`，并**不产出任何 hybrid 闸门判定**——所以一次开放语料运行永远不可能报
出 `KB-GOLDEN-V1 baseline PASS`。它可以说数据库当前返回了什么；它永远不能说那就是夹具。

### 语料指纹

每份封存产物都记录一个 `corpus_fingerprint`：对一组去重、排序后的
`(visibility, tenant_id, source_kind, external_key, document_version_number,
document_content_hash, chunk_generation, ordinal, chunk_content_hash)` 事实求 SHA-256，规范序列化。
它**不**含任何行 UUID、时间戳、数据库主机或 embedding 向量——其中任何一个都会让两份相同的语料看起来
不同——也永不含凭据。

它的用途是可核查：两个独立数据库摄入同一份夹具，产出同一个指纹，这才让「同一条基线」成为一句可
证伪的主张，而不是一句保证。

按它们实际是什么来读这些数字。`VECTOR_ONLY` 检索不到，是随机向量的**预期**结果——它是「向量通道
确实接在 embedding provider 上、而不是意外回退到了词法匹配」的证据。因此一次 plumbing 的 `HYBRID`
闸门 PASS 只说明融合、排序、引用与评分这套机械端到端能用；它对面语义检索质量什么都没说。**不要把
一次 plumbing 运行当成语义基线来呈现。**

驱动通过普通摄取用例摄入封存语料、退役夹具里那份按设计要退役的文档、运行每个被请求的模式，并写出
产物。退出码只在 hybrid 闸门 `FAIL` 时为 `1`；`NOT_RUN` 退出 `0`。

### 第二次运行是安全的

`evaluate` 可以对一个已经在库里的语料重复运行。夹具按设计退役的那一份语料文档会以它既有的 id 向前
携带，而不是被重新摄取，因为 `RETIRED` 在领域里按设计是终态的，重新摄取它会被正确地拒绝。这个跳过
很窄：它只适用于封存夹具自己退役的那些键，所以一份处于 `RETIRED` 状态的不相干文档依然会让这次运行
响亮地失败。重复运行时，预期在语料进度行里看到这一行：

```
  [9/18] guidance-legacy-ssh-hardening: skipped (already retired by a previous run)
```

`--skip-ingest` 是更强的形式：它复用库里已有的语料，完全不摄入任何东西。

指标、闸门规则与产物 schema 见
[knowledge-evaluation-contract.md](../evaluation/knowledge-evaluation-contract.md)。

## 12. 验证部署

```bash
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe -m pytest tests/unit tests/architecture -q
.venv/Scripts/python.exe -m pytest tests/integration -q
.venv/Scripts/python.exe -m alembic check
.venv/Scripts/python.exe -m alembic heads
```

真实 PostgreSQL 的知识套件针对支撑 pgvector 的测试数据库（默认 `127.0.0.1:5434`），不可达时会
**跳过**：

```bash
.venv/Scripts/python.exe -m pytest tests/integration/persistence/test_knowledge_persistence.py -q
```

它核验租户隔离、作用域 CHECK 约束、部分唯一索引、单 ACTIVE profile 规则、单一权威发布规则、跨两个
独立摄入数据库的稳定排序键，以及针对真实数据库的引用解析。

必须保持绿的回归检查：GP-01 闭合测试、P1 工作区测试、P2 响应/持久性测试，以及 ToolRegistry 的可选
名字集（`hisiem.search_events`、`hisiem.get_detection_rule`——未变）。

## 13. 故障排查

| 症状 | 原因 | 处置 |
|---|---|---|
| `type "vector" does not exist` | pgvector 装在一个不在钉住 `search_path` 上的 schema 里 | `ALTER EXTENSION vector SET SCHEMA copilot;` |
| 迁移以 pgvector 消息拒绝 | 扩展缺失且该角色无权创建它 | 以管理员执行 `CREATE EXTENSION IF NOT EXISTS vector SCHEMA copilot;` |
| `doctor` → `NOT_READY`，`knowledge_schema` FAIL | 知识子系统表缺失 | `alembic upgrade head` |
| 第一支迁移上 `InvalidSchemaName: no schema has been selected to create in` | 既有数据卷：`docker-entrypoint-initdb.d` 从未运行 | `CREATE SCHEMA IF NOT EXISTS copilot;`（§2.3） |
| `doctor` → `NOT_READY`、`attack_release_authority` FAIL、`ATTACK_RELEASE_AUTHORITY_AMBIGUOUS` | 某个 framework 有多于一个 ACTIVE 发布 | 用 `--activate` 导入意图中的发布；让其他的保持非 active（§8） |
| `import-attack` → `ATTACK_RELEASE_CONTENT_CONFLICT` | 同名发布被用不同的技术集合导入过 | 重新导入钉住的 bundle，或用一个新发布名（§8） |
| `import-attack` → `ATTACK_RELEASE_PROJECTION_INCOMPLETE` | 暂存投影不完整：暂存中途崩溃，或某份已绑定文档在该发布底下被退役 | 重跑同一次导入以完成暂存；消息会点名缺失的技术。什么都没被改（§8） |
| `import-attack` → `ATTACK_RELEASE_PROJECTION_MISSING_VERSION` | 某条绑定解析不了，或它的内容哈希不再匹配其规范行 | 暂存投影不是这个发布的内容。重跑导入；若持续存在，说明数据库被带外还原过（§8） |
| `import-attack` → `ATTACK_RELEASE_PROJECTION_INVALID_BINDING` | 绑定存在但关系无效：版本属于另一份文档、目标非 MITRE/非 GLOBAL/已退役、external key 不对，或 规范行 == 绑定 == 版本 哈希链断裂 | 暂存绑定不是这个发布的投影。重新暂存该发布；若持续存在，说明数据库被带外编辑过（§8） |
| `ingest-file --source-kind MITRE_ATTACK` | 被解析器拒绝 | MITRE 内容经 `import-attack` 进入（§8）；普通路径不是它的写者 |
| `ingest-file`/`retire` → `SYSTEM_MANAGED_KNOWLEDGE_SOURCE` | 一次普通写入或退役指向了 MITRE 文档 | MITRE 内容用 `import-attack`；MITRE 生命周期属于一个 ATT&CK 专属工作流 |
| `doctor` → `NOT_READY`、`attack_release_projection` FAIL、`ATTACK_RELEASE_PROJECTION_DIVERGED` | 权威发布与普通检索服务的内容不一致 | 重新导入那个发布。它已经是 ACTIVE，所以即便不带 `--activate` 这次导入也会切换（§8） |
| `alembic downgrade` → `P3A_DOWNGRADE_UNSAFE` | 库里存有闭合前 schema 无法表示的行 | 什么都没被改。做一次物理备份并有意删除列出的行，或留在 head。若 `alembic current` 是 `c41f7b2e9d08`，先跑 `alembic upgrade head`（§3，**降级安全**） |
| `ingest-file` → `EMBEDDING_PROFILE_SWITCH_REQUIRES_CORPUS_REINDEX` | 所配置 provider 与 ACTIVE profile 不同 | 切换 embedding 空间是全语料重建索引，不是一次摄取；恢复原来的 provider 配置，或重建语料索引（§5） |
| `evaluate` → `CORPUS_PRECONDITION_FAILED` | 库里没有在预期作用域上恰好持有那份封存夹具 | 用专用数据库，或带 `--allow-ambient-corpus` 运行并把结果读作 `OPEN_CORPUS`，不要当作基线（§11） |
| `doctor` → `DEGRADED`，`active_embedding_profile` WARN | 还没有摄取任何文档 | 摄取一份；或配置 embedding provider 后重新摄取 |
| `doctor` → `attack_release_authority` WARN | 没有 ACTIVE 的 ATT&CK 发布 | 用 `--activate` 导入意图中的发布（§8） |
| `search --mode vector` 退出 `3` | 没有 ACTIVE profile，或未配置 provider | 配置 `EMBEDDING_*`，重启，摄取 |
| `ingest-file` 拒绝该文件 | 不是 `.txt`/`.md` | 转成 Markdown；PDF/DOCX/HTML 不在范围内 |
| `import-attack` 报告 `skipped` | bundle 里有已撤销/已弃用的技术 | 预期行为。id 会被列出；没有任何东西被静默丢弃 |
| 产物写入被拒绝 | 该路径上已存在一份基线 | 传 `--overwrite`，或把 `--out` 指向另一个**目录**（`--out` 命名的是目录；文件名由套件、`k` 与证据质量派生） |
