# Local Integrated Runtime — E1-C0 Baseline

Normative reference for running the **HISIEM SOC Copilot** agent-evaluation
runtime locally on a Windows dev machine, together with the **HISIEM** platform
it reads from. This document defines deployment profiles, a fixed port
contract, the HISIEM auth automation, the Copilot database guard + migration,
the launcher/status/down scripts, health contracts, model readiness, secrets
handling, and the smoke-validation sequence. It intentionally contains no
narrative or learning history.

> **Repo paths (as-built):** HISIEM platform lives at `D:\Project\SIEM`
> (branch `add_frame`, **read-only default** here), NOT `D:\Project\HISIEM`.
> The main project is `D:\Project\HISIEM-SOC-Copilot` (branch `main`). All
> edits and scripts below live in the Copilot repo.

---

## 1. Repo boundaries & git discipline

- **SIEM (`D:\Project\SIEM`)** — reference platform. May be read, executed, and
  health-checked, but **never modified** (source, config, or docs) as part of
  this work. Its `infra/docker-compose.yml` (compose project `infra`) owns the
  HISIEM containers. If a real HISIEM change is ever required, stop and report —
  do not self-modify.
- **HISIEM-SOC-Copilot (`D:\Project\HISIEM-SOC-Copilot`)** — where all changes
  land: source, `infra/docker-compose.yml`, scripts, docs, tests.
- Before any work: confirm `git status` is clean enough to proceed; never
  overwrite another actor's uncommitted files.
- Never commit: `.env.local`, real secrets/tokens, `.eval-runs/`, sealed
  manifests, or any generated artifacts.

---

## 2. Deployment inventory (as-built)

### HISIEM — infra compose (`D:\Project\SIEM\infra\docker-compose.yml`, project `infra`)

| Container | Host port | Role |
|---|---|---|
| `siem-postgres` | 5432 | Platform control-plane PostgreSQL |
| `siem-elasticsearch` | 9200 | Event/alert store (data plane) |
| `siem-kibana` | 5601 | Web console (full profile only) |
| `siem-logstash` | 5000–5007, 9600 | Ingest pipeline (full profile only) |
| `siem-kafka` | 9092 (9094 internal) | Event bus (full profile only) |
| `siem-flink-jobmanager` | 8081 | Detection job driver (full profile only) |
| `siem-flink-taskmanager` | — | Detection job worker (full profile only) |

### HISIEM — host processes (NOT in compose)

| Process | How it runs | Health |
|---|---|---|
| **control-api** (Spring Boot, host 8080) | `java -jar applications/control-api/target/hsiem-platform.jar` from `D:\Project\SIEM` (may pass `--app.rules-dir=...`) | `GET /actuator/health` → `{"status":"UP"}` |
| web (Vue/Vite, host 5173) | `npm --prefix web run dev` | (operator-managed; not part of Agent profile) |
| detection-controller / soar-worker | optional separate JVMs | (not part of Agent profile) |

### Copilot — HISIEM-SOC-Copilot repo

| Component | How it runs | Health |
|---|---|---|
| `copilot-postgres` (postgres:16, docker) | `docker compose -p copilot -f infra/docker-compose.yml up -d` | `pg_isready`, host **5433** |
| **Copilot API** (FastAPI/uvicorn, host 8000) | `python -m hisiem_soc_copilot.main` | `GET /healthz` → 200 |

Schema ownership: `copilot` is **Alembic-owned**; `langgraph_checkpoint` is
**LangGraph-owned** (never migrated here).

---

## 3. Deployment profiles (frozen)

- **A — Full HISIEM (dataset generation, GP-01 E1-B):** everything in the infra
  compose including the data pipeline (Logstash, Kafka, Flink, Kibana) plus
  control-api. Launched by `scripts/dev/up-full.ps1`.
- **B — Agent Evaluation (default):** siem-postgres (5432), Elasticsearch
  (9200), control-api (8080), copilot-postgres (5433), Copilot API (8000).
  **Excludes** Kafka, Logstash, Flink, Kibana, SOAR, and the web console.
  Launched by `scripts/dev/up-agent.ps1`.

---

## 4. Port contract (frozen)

| Port | Owner |
|---|---|
| 5432 | HISIEM siem-postgres |
| 5433 | Copilot copilot-postgres |
| 8080 | HISIEM control-api |
| 8000 | Copilot API |
| 9200 | Elasticsearch |
| 9092 | Kafka (full profile) |
| 8081 | Flink jobmanager (full profile) |
| 5601 | Kibana (full profile) |

The Copilot compose maps host `5433` → container `5432`. Neither database ever
shares a host port. Do not start another postgres on 5432/5433.

---

## 5. HISIEM authentication automation

**Principle: API-only. No DB bypass.** Forbidden: direct `UPDATE users`,
password-hash editing, clearing `passwordChangeRequired`, disabling auth,
RBAC bypass, hardcoding/committing tokens, logging secrets.

Official flow driven by `scripts/dev/hisiem_auth.py`:

```
GET  /actuator/health            (health gate)
POST /api/auth/login             {username, password}
POST /api/auth/password          {currentPassword, newPassword}   (rotate, first-run)
```

- Login response includes `token`, `role`, `expiresAt`, `passwordChangeRequired`.
- If `passwordChangeRequired` is true the helper rotates **bootstrap→dev**
  password via `/api/auth/password` (policy: ≥12 chars, new≠current), then logs
  in again with the dev password.
- Only a genuinely first-run control-api (empty user store) needs
  `HISIEM_BOOTSTRAP_PASSWORD`; when the legacy `users.yaml` users were already
  imported they start with `passwordChangeRequired=true` and the rotation path
  applies instead.
- Session tokens are single-return, SHA-256-hashed in the store, 8h TTL,
  5-fail lockout for 15m.

`.env.local` (gitignored) holds `HISIEM_DEV_USERNAME`, `HISIEM_DEV_PASSWORD`,
`HISIEM_BOOTSTRAP_PASSWORD` (only for genuine first-run), `HISIEM_TENANT_ID`,
and `CMD_API_KEY`. `.env.example` = variable names + safe placeholders only.

---

## 6. Bearer token handling (runtime-only)

The HISIEM Bearer token is obtained by `up-agent.ps1` at launch and injected
into the **Copilot child process environment** as `HISIEM_BEARER_TOKEN`. It is
never written to git, logs, `.env.local`, manifests, agent state, or
checkpoints; never printed. The `scripts/dev/hisiem_auth.py` CLI prints the
token to stdout only (consumed by the launcher).

---

## 7. Copilot PostgreSQL & migration guard

`scripts/dev/copilot_db.py` enforces a **fail-closed target guard** before any
Alembic run:

- Host must be local loopback (`127.0.0.1`/`localhost`/`::1`).
- Port must be **5433** (Copilot's reserved port). A URL pointing at HISIEM's
  **5432**, or any other port, is refused.
- Database must be **`copilot`**.

Then it runs `alembic current`, `alembic upgrade head`, and `alembic check`
(drift). Schema ownership is respected: only the `copilot` schema is migrated;
`langgraph_checkpoint` belongs to LangGraph at runtime.

`up-agent.ps1` guarantees the two schemas exist (`CREATE SCHEMA IF NOT EXISTS`)
before migrating. Project validation always runs Alembic against Copilot 5433,
never HISIEM 5432.

---

## 8. Launcher scripts

Run from the Copilot repo root.

| Script | Purpose | Notes |
|---|---|---|
| `scripts/dev/up-agent.ps1` | Agent Evaluation core up | Validate config → ensure containers (siem-postgres, ES, copilot-postgres) → health-check control-api → obtain Bearer token → ensure schemas → guarded migrate → start Copilot API (token in child env) → readiness → sanitized status. **Does not auto-spawn control-api JVM**; if it is down it prints a `BLOCKED` status and the exact start command. Idempotent. |
| `scripts/dev/up-full.ps1` | Agent core + full HISIEM data stack | Calls up-agent, then `docker compose up -d kafka logstash flink-jobmanager flink-taskmanager kibana`. |
| `scripts/dev/status.ps1` | Machine-readable status | `READY`/`DOWN`/`FAILED`/`HEAD`/`DRIFT`/`MISSING_URL`/`SET`/`MISSING` per service. Exit **0** = Agent profile fully ready; non-zero otherwise. Never prints secrets. |
| `scripts/dev/down.ps1` | Stop the runtime | STOP preserves volumes. `-Reset` is explicit + requires typing `RESET`; only then are volumes removed. `-SkipHisiem` stops only Copilot containers. |

The `up-agent.ps1` profile **detects** (does not start) the control-api host
JVM. If 8080 is not UP it reports `BLOCKED` with:

```
cd D:\Project\SIEM
java -jar applications/control-api/target/hsiem-platform.jar
```

(re-run up-agent once control-api reports UP).

---

## 9. Health contract

Never judge readiness by "port is listening" alone.

- **HISIEM control-api:** `GET /actuator/health` → HTTP 200, body
  `{"status":"UP"}`. Not `/`, not `/api/health`.
- **HISIEM auth liveness:** an empty `POST /api/auth/login` (permitAll) returns
  400/401 when the endpoint is live — no credentials are sent.
- **Copilot API:** `GET /healthz` → HTTP 200, body `{"status":"ok", ...}`.
- **Elasticsearch:** `GET :9200/_cluster/health` → HTTP 200.
- **Postgres containers:** `pg_isready` / `docker ps` running filter.

---

## 10. Model readiness

`scripts/dev/status.ps1` reports `Model Configuration` as `READY` when:

- provider is `scripted` (deterministic, offline — no key needed), or
- provider is `openai_compatible` and the env var named by `LLM_API_KEY_ENV`
  (default `CMD_API_KEY`) is set.

Secrets are only read from that named environment variable — never from config
defaults, prompts, state, or logs.

---

## 11. Dev secrets contract

`.env.local` is gitignored and holds: `HISIEM_DEV_USERNAME`,
`HISIEM_DEV_PASSWORD`, `HISIEM_BOOTSTRAP_PASSWORD` (first-run only),
`HISIEM_TENANT_ID`, `CMD_API_KEY`. `.env.example` carries only variable names
and safe placeholders. Trusted-context provider `COPILOT_AUTH_TRUSTED_CONTEXT_PROVIDER`
is set to `header` **only** by the local launcher (X-Tenant-ID / X-Actor-Subject
adapter) and is never a production default (`none` fails closed).

---

## 12. Smoke validation sequence

```
scripts/dev/status.ps1      # expect exit != 0 (DOWN) before first up
scripts/dev/up-agent.ps1    # containers + control-api + migrate + Copilot API
scripts/dev/down.ps1        # STOP (volumes preserved)
scripts/dev/up-agent.ps1    # idempotent re-up
scripts/dev/status.ps1      # expect exit 0 (READY)
```

No manual bootstrap, no manual token, no DB-URL or port judgment is required in
the smoke sequence. Real first-run may require supplying `HISIEM_DEV_PASSWORD`
(and, on a genuinely empty store, `HISIEM_BOOTSTRAP_PASSWORD`) in `.env.local`
— the outstanding issue below.

---

## 13. Outstanding issue (recorded)

During the earlier GP-01 real gate, the SIEM admin password was reset via a
direct PostgreSQL `UPDATE users`. That was effective then (the DB is the live
store) but is **forbidden** under this baseline. The current admin plaintext is
unknown; the runtime must instead drive the official HTTP auth flow
(§5). The operator must provide the working dev password in `.env.local`
(`HISIEM_DEV_PASSWORD`), or a fresh bootstrap path must be exercised. No DB
mutation is permitted to resolve this.
