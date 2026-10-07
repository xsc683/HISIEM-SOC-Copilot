# 本地一体化运行时

> **来源。** 这份基线是作为工作步骤 **E1-C0** 交付的。代号是工程史；本文档以它所描述的运行时命名。

在 Windows 开发机上本地运行 **HISIEM SOC Copilot** 智能体评估运行时的规范参考，连同它所读取的
**HISIEM** 平台一并说明。本文档定义部署 profile、固定的端口契约、HISIEM 认证自动化、Copilot 数据库
守卫 + 迁移、启动/状态/停止脚本、健康契约、模型就绪、密钥处理，以及冒烟验证序列。它刻意不含任何
叙述或学习史。

> **仓库路径（按实际）：** HISIEM 平台在 `D:\Project\SIEM`（分支 `add_frame`，此处**默认只读**），
> **不**是 `D:\Project\HISIEM`。主项目是 `D:\Project\HISIEM-SOC-Copilot`（分支 `main`）。下面所有
> 编辑与脚本都落在 Copilot 仓。

---

## 1. 仓库边界与 git 纪律

- **SIEM（`D:\Project\SIEM`）** —— 参考平台。可以读、可以执行、可以做健康检查，但作为这项工作的一部分
  **绝不修改**（源码、配置或文档都不改）。它的 `infra/docker-compose.yml`（compose 项目 `infra`）
  拥有 HISIEM 的那些容器。如果哪天真需要改 HISIEM，停下并报告——不要自行修改。
- **HISIEM-SOC-Copilot（`D:\Project\HISIEM-SOC-Copilot`）** —— 所有改动落地的地方：源码、
  `infra/docker-compose.yml`、脚本、文档、测试。
- 动手之前：确认 `git status` 干净到足以开工；绝不覆盖别人未提交的文件。
- 绝不提交：`.env.local`、真实密钥/token、`.eval-runs/`、封存清单，或任何生成产物。

---

## 2. 部署清单（按实际）

### HISIEM —— infra compose（`D:\Project\SIEM\infra\docker-compose.yml`，项目 `infra`）

| 容器 | 宿主端口 | 角色 |
|---|---|---|
| `siem-postgres` | 5432 | 平台控制面 PostgreSQL |
| `siem-elasticsearch` | 9200 | 事件/告警存储（数据面） |
| `siem-kibana` | 5601 | Web 控制台（仅 full profile） |
| `siem-logstash` | 5000–5007、9600 | 摄取管线（仅 full profile） |
| `siem-kafka` | 9092（内部 9094） | 事件总线（仅 full profile） |
| `siem-flink-jobmanager` | 8081 | 检测作业驱动（仅 full profile） |
| `siem-flink-taskmanager` | — | 检测作业 worker（仅 full profile） |

### HISIEM —— 宿主进程（不在 compose 里）

| 进程 | 怎么跑 | 健康 |
|---|---|---|
| **control-api**（Spring Boot，宿主 8080） | 在 `D:\Project\SIEM` 下 `java -jar applications/control-api/target/hsiem-platform.jar`（可传 `--app.rules-dir=...`） | `GET /actuator/health` → `{"status":"UP"}` |
| web（Vue/Vite，宿主 5173） | `npm --prefix web run dev` | （运维管理；不属于 Agent profile） |
| detection-controller / soar-worker | 可选的独立 JVM | （不属于 Agent profile） |

### Copilot —— HISIEM-SOC-Copilot 仓

| 组件 | 怎么跑 | 健康 |
|---|---|---|
| `copilot-postgres`（postgres:16，docker） | `docker compose -p copilot -f infra/docker-compose.yml up -d` | `pg_isready`，宿主 **5433** |
| **Copilot API**（FastAPI/uvicorn，宿主 8000） | `python -m hisiem_soc_copilot.main` | `GET /healthz` → 200 |

schema 归属：`copilot` 归 **Alembic**；`langgraph_checkpoint` 归 **LangGraph**（此处永不迁移）。

---

## 3. 部署 profile（冻结）

- **A —— 完整 HISIEM（数据集生成、GP-01 物化）：** infra compose 里的全部内容，包括数据管线
  （Logstash、Kafka、Flink、Kibana）外加 control-api。由 `scripts/dev/up-full.ps1` 启动。
- **B —— Agent 评估（默认）：** siem-postgres（5432）、Elasticsearch（9200）、control-api
  （8080）、copilot-postgres（5433）、Copilot API（8000）。**不含** Kafka、Logstash、Flink、
  Kibana、SOAR 与 web 控制台。由 `scripts/dev/up-agent.ps1` 启动。

---

## 4. 端口契约（冻结）

| 端口 | 归属 |
|---|---|
| 5432 | HISIEM siem-postgres |
| 5433 | Copilot copilot-postgres |
| 8080 | HISIEM control-api |
| 8000 | Copilot API |
| 9200 | Elasticsearch |
| 9092 | Kafka（full profile） |
| 8081 | Flink jobmanager（full profile） |
| 5601 | Kibana（full profile） |

Copilot 的 compose 把宿主 `5433` 映射到容器 `5432`。两个数据库永不共用宿主端口。不要在 5432/5433 上
再起一个 postgres。

---

## 5. HISIEM 认证自动化

**原则：只用 API。不绕数据库。** 禁止：直接 `UPDATE users`、改密码哈希、清
`passwordChangeRequired`、关认证、绕 RBAC、硬编码或提交 token、把密钥写进日志。

由 `scripts/dev/hisiem_auth.py` 驱动的官方流程：

```
GET  /actuator/health            (health gate)
POST /api/auth/login             {username, password}
POST /api/auth/password          {currentPassword, newPassword}   (rotate, first-run)
```

- 登录响应包含 `token`、`role`、`expiresAt`、`passwordChangeRequired`。
- 若 `passwordChangeRequired` 为 true，辅助脚本经 `/api/auth/password` 把
  **bootstrap→dev** 密码轮换掉（策略：≥12 字符、新≠旧），然后用 dev 密码重新登录。
- 只有真正首次运行的 control-api（用户表为空）才需要 `HISIEM_BOOTSTRAP_PASSWORD`；当遗留
  `users.yaml` 里的用户已经被导入时，他们起手就是 `passwordChangeRequired=true`，适用的是轮换
  路径。
- 会话 token 单次返回、在存储里以 SHA-256 哈希保存、TTL 8 小时、连续 5 次失败锁定 15 分钟。

`.env.local`（被 gitignore）存放 `HISIEM_DEV_USERNAME`、`HISIEM_DEV_PASSWORD`、
`HISIEM_BOOTSTRAP_PASSWORD`（仅真正首次运行用）、`HISIEM_TENANT_ID` 与 `CMD_API_KEY`。
`.env.example` 只有变量名 + 安全占位符。

---

## 6. Bearer token 处理（仅运行期）

HISIEM Bearer token 由 `up-agent.ps1` 在启动时取得，并作为 `HISIEM_BEARER_TOKEN` 注入 **Copilot
子进程环境**。它永不被写入 git、日志、`.env.local`、清单、agent 状态或 checkpoint，也永不被打印。
`scripts/dev/hisiem_auth.py` 这个 CLI 只把 token 打到 stdout（由启动器消费）。

---

## 7. Copilot PostgreSQL 与迁移守卫

`scripts/dev/copilot_db.py` 在任何 Alembic 运行之前强制一道 **fail-closed 目标守卫**：

- 主机必须是本地回环（`127.0.0.1`/`localhost`/`::1`）。
- 端口必须是 **5433**（Copilot 保留端口）。指向 HISIEM **5432** 或其他任何端口的 URL 都会被拒绝。
- 数据库必须是 **`copilot`**。

然后它依次运行 `alembic current`、`alembic upgrade head` 与 `alembic check`（漂移）。schema 归属被
尊重：只迁移 `copilot` schema；`langgraph_checkpoint` 在运行时归 LangGraph。

`up-agent.ps1` 在迁移之前保证两个 schema 存在（`CREATE SCHEMA IF NOT EXISTS`）。项目验证永远对
Copilot 5433 跑 Alembic，永不对 HISIEM 5432。

---

## 8. 启动脚本

从 Copilot 仓根目录运行。

| 脚本 | 用途 | 说明 |
|---|---|---|
| `scripts/dev/up-agent.ps1` | 拉起 Agent 评估核心 | 校验配置 → 确保容器（siem-postgres、ES、copilot-postgres）→ 健康检查 control-api → 取得 Bearer token → 确保 schema → 有守卫地迁移 → 启动 Copilot API（token 在子进程环境里）→ 就绪 → 脱敏状态。**不会自动拉起 control-api JVM**；如果它是 down，就打印一个 `BLOCKED` 状态和确切的启动命令。幂等。 |
| `scripts/dev/up-full.ps1` | Agent 核心 + 完整 HISIEM 数据栈 | 调 up-agent，然后 `docker compose up -d kafka logstash flink-jobmanager flink-taskmanager kibana`。 |
| `scripts/dev/status.ps1` | 机器可读状态 | 每个服务报 `READY`/`DOWN`/`FAILED`/`HEAD`/`DRIFT`/`MISSING_URL`/`SET`/`MISSING`。退出 **0** = Agent profile 完全就绪；否则非零。永不打印密钥。 |
| `scripts/dev/down.ps1` | 停掉运行时 | STOP 保留数据卷。`-Reset` 是显式的，且要求手输 `RESET`；只有那时才删除数据卷。`-SkipHisiem` 只停 Copilot 容器。 |

`up-agent.ps1` 这个 profile 对 control-api 宿主 JVM 只**探测**（不启动）。如果 8080 不是 UP，它会
带着下列内容报 `BLOCKED`：

```
cd D:\Project\SIEM
java -jar applications/control-api/target/hsiem-platform.jar
```

（等 control-api 报 UP 之后再跑一次 up-agent。）

---

## 9. 健康契约

绝不要只凭「端口在监听」判定就绪。

- **HISIEM control-api：** `GET /actuator/health` → HTTP 200，正文 `{"status":"UP"}`。不是 `/`，也
  不是 `/api/health`。
- **HISIEM 认证存活：** 一次空的 `POST /api/auth/login`（permitAll）在端点活着时返回 400/401——不
  发送任何凭据。
- **Copilot API：** `GET /healthz` → HTTP 200，正文 `{"status":"ok", ...}`。
- **Elasticsearch：** `GET :9200/_cluster/health` → HTTP 200。
- **Postgres 容器：** `pg_isready` / `docker ps` 的运行状态过滤。

---

## 10. 模型就绪

`scripts/dev/status.ps1` 在下列情形把 `Model Configuration` 报成 `READY`：

- provider 是 `scripted`（确定性、离线——不需要 key），或
- provider 是 `openai_compatible` 且由 `LLM_API_KEY_ENV`（默认 `CMD_API_KEY`）命名的环境变量已
  设置。

密钥只从那个具名的环境变量读取——永不从配置默认值、prompt、状态或日志里读。

---

## 11. 开发密钥契约

`.env.local` 被 gitignore，存放：`HISIEM_DEV_USERNAME`、`HISIEM_DEV_PASSWORD`、
`HISIEM_BOOTSTRAP_PASSWORD`（仅首次运行）、`HISIEM_TENANT_ID`、`CMD_API_KEY`。`.env.example`
只带变量名与安全占位符。可信上下文 provider `COPILOT_AUTH_TRUSTED_CONTEXT_PROVIDER` **只**由本地
启动器设为 `header`（X-Tenant-ID / X-Actor-Subject 适配器），永不是生产默认值（`none` fail
closed）。

---

## 12. 冒烟验证序列

```
scripts/dev/status.ps1      # expect exit != 0 (DOWN) before first up
scripts/dev/up-agent.ps1    # containers + control-api + migrate + Copilot API
scripts/dev/down.ps1        # STOP (volumes preserved)
scripts/dev/up-agent.ps1    # idempotent re-up
scripts/dev/status.ps1      # expect exit 0 (READY)
```

冒烟序列里不需要手工 bootstrap、不需要手工 token，也不需要判断 DB-URL 或端口。真正的首次运行可能
需要在 `.env.local` 里提供 `HISIEM_DEV_PASSWORD`（在真正空的存储上还需要
`HISIEM_BOOTSTRAP_PASSWORD`）——即下面那条未结事项。

---

## 13. 未结事项（已记录）

在早先的 GP-01 真实闸门期间，SIEM 管理员密码是通过直接对 PostgreSQL 执行 `UPDATE users` 重置的。
那在当时是有效的（数据库就是活的存储），但在这份基线之下是**禁止**的。当前管理员的明文未知；运行时
必须改为驱动官方 HTTP 认证流程（§5）。运维必须在 `.env.local` 里提供可用的 dev 密码
（`HISIEM_DEV_PASSWORD`），或者走一条全新的 bootstrap 路径。不允许用任何数据库改动来解决这件事。
