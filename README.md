# HISIEM SOC Copilot

**AI SOC · Security Investigation · Evidence Grounding · Tool-Using Agent · Human-in-the-loop**

HISIEM SOC Copilot is an AI investigation & response collaboration system for SOC analysts. It runs agent-driven investigations over HISIEM platform data, grounds every judgement in traceable Evidence, and requires human approval before any side-effecting response.

This repository currently contains the **V1 initial engineering skeleton**: the domain model, application layer, persistence, migrations, agent orchestration boundary, and a minimal runnable API. It is the translation of the committed product/domain/persistence/module-boundary specs (`docs/*.md`) into runnable, testable Python.

> Architecture, domain, persistence and boundary authority: see `docs/` — **do not redesign them**. This implementation follows them conservatively.

## Repository layout

```
src/hisiem_soc_copilot/
├── config.py            # single typed-settings entry point
├── main.py              # process entrypoint (config → container → uvicorn)
├── domain/              # pure stdlib domain (aggregates, entities, VOs, events, invariants)
│   ├── investigation/   #   Investigation aggregate + state machine
│   ├── response/        #   ResponseProposal aggregate + policy + approval
│   └── shared/
├── application/         # commands, queries, handlers, ports, services (domain + ports only)
├── contracts/           # boundary schemas (API/LLM/Tools) — Pydantic lives here, not domain
├── agent/               # LangGraph orchestration: bounded Graph State, checkpoint seam
├── api/                 # FastAPI transport only (depends on application, never infra)
├── infrastructure/      # PostgreSQL (copilot schema), HISIEM HTTP adapter, LangGraph checkpointer
└── bootstrap/           # Composition Root (container, lifespan)
alembic/                 # Alembic migrations for the `copilot` schema only
tests/
├── unit/                # domain + application (no DB)
├── architecture/        # import-boundary enforcement
├── integration/         # persistence/hisiem/api against real PostgreSQL when reachable
└── e2e/
infra/docker-compose.yml # local PostgreSQL (hosts `copilot` + `langgraph_checkpoint` schemas)
docs/                    # authoritative product/domain/persistence/boundary specs
```

## Boundaries this skeleton enforces

- **Domain is pure** — no FastAPI/SQLAlchemy/LangGraph/httpx/pydantic in `domain/`.
- **Application uses ports/UoW** — handlers never see a SQL session; infrastructure adapters are injected.
- **Investigation ≠ LangGraph thread** — the graph holds only bounded working state; domain is the source of truth.
- **Graph state ≠ domain state** — `agent/graph/state.py` TypedDict holds cross-step working state only.
- **Tenant scope** — client/model never declares tenant/actor; they come from the authenticated HISIEM context.
- **One Active Investigation per Tenant + Alert** — enforced by a partial unique index, converged at the DB.
- **Optimistic lock** on aggregate updates (no last-write-wins).
- **`copilot` schema is Alembic-owned; `langgraph_checkpoint` is LangGraph-owned** — separate schemas, separate connections, separate migration owners.
- Approval/Evidence/Result constraints are expressed both in domain invariants and DB constraints.

## Prerequisites

- Python **3.12+** (tested on 3.13)
- PostgreSQL 16 (local, or `docker compose`)

## Quick start

```bash
# 1. environment + editable install (from repo root)
python -m venv .venv
.\.venv\Scripts\activate            # Windows PowerShell
pip install -e ".[dev]"             # editable; package resolves via normal imports

# 2. local PostgreSQL (schema containers)
docker compose -f infra/docker-compose.yml up -d

# 3. create the two schemas and apply the copilot migrations (Alembic owns `copilot`)
docker exec copilot-postgres psql -U copilot -d copilot \
  -c "CREATE SCHEMA IF NOT EXISTS copilot;" \
  -c "CREATE SCHEMA IF NOT EXISTS langgraph_checkpoint;"
alembic upgrade head

# 4. run the API
python -m hisiem_soc_copilot.main
# health:  GET /healthz
```

> Note: if the configured PyPI mirror cannot serve build dependencies, install
> hatchling into the venv from an alternate index first (e.g.
> `pip install --index-url https://pypi.org/simple hatchling`) or add
> `--no-build-isolation` — the packaging itself is standard `src/` layout + hatchling.

## Trusted request context (boundary)

The API/application depend on a `TrustedContextProvider` abstraction
(`application/ports/trust.py`); tenant/actor/authorization are never taken from an
untrusted client body or the model. The provider is selected by configuration:

| `COPILOT_AUTH_TRUSTED_CONTEXT_PROVIDER` | Behaviour |
|---|---|
| `none` (default) | Fail closed — no request can be trusted; every protected route returns `403 UNTRUSTED_REQUEST`. |
| `header` | **Dev/test only** adapter reading `X-Tenant-ID` / `X-Actor-Subject`. Must not be the production default. |

Production must wire a real authenticator (authenticated principal at the edge,
e.g. HISIEM-injected identity) behind the same abstraction. Full production auth
is intentionally out of scope this round.

## Configuration

Typed settings live in `config.py` (pydantic-settings). Key env vars (see `.env.example`):

| Variable | Purpose |
|---|---|
| `COPILOT_DATABASE_URL` | SQLAlchemy async URL for the `copilot` schema |
| `LANGGRAPH_DATABASE_URL` | psycopg URL for the `langgraph_checkpoint` schema (`options=-csearch_path=...`) |
| `HISIEM_BASE_URL` | HISIEM control API base URL |
| `LLM_PROVIDER` | `scripted` (default, offline) or `openai_compatible` (Command Code API) |
| `LLM_MODEL` | model name sent to the provider (`deepseek/deepseek-v4-flash`) |
| `CMD_API_KEY` | API key env var read by the real provider (never a config default) |
| `AGENT_MAX_STEPS` etc. | Agent autonomy budget bounds |

## API surface (V1 skeleton)

```
POST /api/v1/investigations                  # start (or reuse active) investigation for a HISIEM alert
GET  /api/v1/investigations/{id}             # tenant-scoped overview
POST /api/v1/investigations/{id}/cancel      # cancel while still cancellable
GET  /healthz
```

Tenant and actor are resolved through the configured `TrustedContextProvider` (see above) — never from a body field. In dev/test the `header` provider reads `X-Tenant-ID` / `X-Actor-Subject`; production must use an authenticated provider.

## Migrations (Alembic)

- Alembic manages **only** the `copilot` schema (`alembic/`).
- `langgraph_checkpoint` is created/migrated by the LangGraph `AsyncPostgresSaver.setup()` at runtime.
- On Windows, `alembic` forces a `SelectorEventLoop` (psycopg async cannot run on Proactor).
- The initial migration is `alembic/versions/<rev>_initial_copilot_schema_*.py`.

## Tests

```bash
ruff check .
mypy src
pytest                         # unit + architecture always; DB tests run when PostgreSQL is reachable
alembic check                  # drift-free when run after `alembic upgrade head`
```

Persistence/API integration tests skip automatically when PostgreSQL is not reachable, so the suite stays green on machines without Docker.

## V1 scope status

Implemented in this skeleton:

- Pure domain: `Investigation` state machine (CREATED → RUNNING → {COMPLETED, WAITING_APPROVAL, CANCELLED, FAILED}), `ResponseProposal` + policy (DENY / REQUIRE_APPROVAL only), Evidence/Finding/Result/Approval invariants.
- Application: `StartAlertInvestigation` / `CancelInvestigation` commands + handler, read service, tenant-scoped repository ports, UoW port.
- Persistence: all `copilot` tables + the documented constraints, explicit ORM↔domain mappers, SqlAlchemyUnitOfWork with optimistic locking.
- HISIEM adapter (read-only alert hydration), LangGraph checkpointer wiring, minimal compiled graph seam.
- FastAPI transport with health + investigation start/read/cancel.

Deliberately **not** in this round (V1 follow-ups): the full agent investigation graph, real LLM provider calls, MCP, RAG, long-term memory, Celery/Kafka, SSE, full observability, multi-agent, and a frontend. None of those are stubbed with fake `pass` bodies.
