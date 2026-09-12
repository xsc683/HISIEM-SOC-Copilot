# P3-A Operations

How to provision, verify, ingest, search, and evaluate the knowledge subsystem.

**Nothing in this document requires dropping a volume, resetting a database, or
re-sealing GP-01.** The P3-A migration only ever creates its own five tables; it
removes nothing that existed before it.

> **P3-B is NOT YET ACTIVE.** The knowledge Agent tools
> (`knowledge.retrieve_security_guidance`, `knowledge.resolve_attack_technique`)
> are catalogued but not registered. No model can reach this subsystem. What
> follows is an operator procedure, not an Agent capability.

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| PostgreSQL | The Copilot database (local dev: `127.0.0.1:5433`) |
| **pgvector** | **One-time install — see §2** |
| Python env | `.venv/Scripts/python.exe` on Windows |
| `COPILOT_DATABASE_URL` | Defaults to `postgresql+psycopg://copilot:copilot@127.0.0.1:5433/copilot` |

The connection is pinned to the `copilot` schema via `search_path`. That detail
matters for pgvector and is the source of the one confusing failure mode below.

## 2. The one-time pgvector prerequisite

pgvector is an **infrastructure prerequisite**, not a Python dependency the app
can install for you. The migration does **not** assume superuser rights: it
verifies the extension is present, attempts to create it if the role is permitted
to, and otherwise fails explicitly with an actionable message *before any table
exists*.

### On a NEW database

`alembic upgrade head` handles it, provided the role may create extensions. The
extension is installed into the connection's own schema (`copilot`) so the
`vector` type is reachable under the pinned `search_path`.

### On an EXISTING database (the usual case)

Run this **once**, as a database administrator:

```sql
CREATE EXTENSION IF NOT EXISTS vector SCHEMA copilot;
```

Then `alembic upgrade head` proceeds normally. No data is touched.

### If pgvector is already installed in `public`

This is the failure that produces a bare, unhelpful
`type "vector" does not exist`, because a `search_path=copilot` connection cannot
see it. `doctor` detects this case specifically and reports:

```
[FAIL] vector_extension: the vector extension is installed but not visible on
       this connection's search_path; run: ALTER EXTENSION vector SET SCHEMA copilot;
```

Fix it once with:

```sql
ALTER EXTENSION vector SET SCHEMA copilot;
```

Then re-run the migration — or point the connection at a `search_path` that
includes `public`, if your deployment prefers that. Both work; the first is
recommended because it keeps the type resolved by the pinned search path.

### What the migration says when it cannot proceed

```
The PostgreSQL 'vector' extension (pgvector) is required by this migration and is
not installed, and this role may not create it. Ask a database administrator to run
`CREATE EXTENSION IF NOT EXISTS vector SCHEMA copilot;` in this database once, then
re-run `alembic upgrade head`. Nothing was changed by this failed run.
```

A failed run is atomic: the check runs before any `CREATE TABLE`.

### Observed state of this workstation's existing databases

Two databases are relevant here, and they are **not** interchangeable.

| Database | State | Use |
|---|---|---|
| `127.0.0.1:5433` | The operator's Copilot database. PostgreSQL 16.15. Revision `979070495d4f` (P2) — one revision behind P3-A head. `pg_available_extensions` lists neither `vector` nor any pgvector package, so `CREATE EXTENSION vector` **cannot** succeed on this server as packaged. | **READ-ONLY** for P3-A. Safe for `doctor`, which only reads. Never run `alembic upgrade`/`downgrade` or any DDL against it until its server image carries pgvector. |
| `127.0.0.1:5434` | The pgvector-capable test database used by the P3-A integration suite. | Migrated, exercised, and cycled by tests. |

That is why the P3-A integration tests hardcode `127.0.0.1:5434` rather than
honouring `COPILOT_DATABASE_URL`: a test that wrote to the operator's database
would be a defect, not a convenience.

Bringing `5433` up to P3-A is therefore a **two-part** operator action, not one:
install pgvector into that server's image first (a packaging change, outside this
repository), then run the one-time `CREATE EXTENSION` statement above, then
`alembic upgrade head`. Until all three happen, `doctor` against `5433` reports
`NOT_READY` with `vector_extension` and `knowledge_schema` FAIL — which is the
correct, non-destructive answer, and the one it gives without touching anything:

```
knowledge doctor: NOT_READY
  database: postgresql+psycopg://copilot:***@127.0.0.1:5433/copilot
  [OK] database: connected
  [FAIL] vector_extension: the vector extension is not installed in this database; see docs/p3/p3-a-operations.md for the one-time prerequisite
  [FAIL] knowledge_schema: missing tables: attack_technique, embedding_profile, knowledge_chunk, knowledge_document, knowledge_document_version (run: alembic upgrade head)
  [FAIL] active_embedding_profile: not checked: the knowledge schema is missing (run: alembic upgrade head)
  [WARN] embedding_provider: no embedding provider configured (EMBEDDING_PROVIDER=unconfigured): lexical retrieval only
```

## 3. Migrating

```bash
.venv/Scripts/python.exe -m alembic heads
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m alembic check
```

The P3-A revision is `ed6af82d9b13`, on top of `979070495d4f` (the P2 response
lifecycle migration).

### Verifying a migration cycle

```bash
.venv/Scripts/python.exe -m alembic downgrade -1
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m alembic check
```

`downgrade -1` drops **only** the five P3-A tables and their indexes. Every
pre-P3-A object — `investigation`, `domain_event`, `outbox_message`,
`command_receipt`, `orchestration_binding`, `tool_invocation`, `response_proposal`,
and the LangGraph checkpoint schema — is untouched. The `vector` extension is
deliberately **not** dropped: it may predate P3-A and other schemas may depend on
it, so removing it would be a destructive change far outside the migration's
scope.

`alembic check` must report no drift both after the upgrade and after the
downgrade/upgrade cycle.

## 4. Schema created

| Table | Purpose |
|---|---|
| `knowledge_document` | Externally-identified document. Immutable identity; ACTIVE → RETIRED lifecycle. |
| `knowledge_document_version` | Immutable content. A change appends a version. |
| `embedding_profile` | The vector space the chunks were indexed in. At most one ACTIVE. |
| `knowledge_chunk` | Rebuildable retrieval projection, with the vector and the generated FTS column. |
| `attack_technique` | Canonical technique rows for a pinned ATT&CK release. |

Properties worth checking after a migration:

- **No HNSW, no IVFFlat.** P3-A ranks exactly.
- The `embedding` column is the **untyped** `vector` type — no dimension is baked
  into the schema.
- `lexical_document` is a `GENERATED` `tsvector` column with a **GIN** index, so
  it can never drift from the content it describes.
- `uq_embedding_profile_single_active` is a **partial unique index** on `status`
  `WHERE status = 'ACTIVE'` — the one-ACTIVE rule is a database fact, not
  application code.
- `uq_knowledge_document_global_key` and `uq_knowledge_document_tenant_key` are
  **partial** unique indexes. Two of them, not one: `NULL` never conflicts in a
  plain unique index, so a single index would silently allow duplicate global
  documents.

## 5. Embedding configuration

The embedding provider is configured **independently of the chat LLM**. They are
different services with different contracts, and assuming the chat endpoint can
embed is exactly the mistake the separation prevents.

| Variable | Meaning |
|---|---|
| `EMBEDDING_PROVIDER` | `unconfigured` (default) or `openai_compatible` |
| `EMBEDDING_BASE_URL` | e.g. `https://api.example.com/v1` |
| `EMBEDDING_MODEL` | The embedding model id |
| `EMBEDDING_DIMENSION` | The vector dimension the model produces |
| `EMBEDDING_API_KEY` | The secret. **Name is configurable via `EMBEDDING_API_KEY_ENV`** |
| `EMBEDDING_NORMALIZATION` | `NONE` (default) or `L2` |
| `EMBEDDING_DISTANCE_METRIC` | `COSINE` (the only value in P3-A) |
| `EMBEDDING_TIMEOUT_SECONDS`, `EMBEDDING_MAX_RETRIES` | Bounded retry over transient faults only |

**Defaults to `unconfigured`.** That is the honest default: without a real
provider there is no vector retrieval, and the system says so instead of
substituting fake vectors. `LEXICAL_ONLY` retrieval keeps working.

The API key is never a config default, never logged, never placed in an exception
message, and never echoed by `doctor` — which reports only whether a
configuration is *present*.

### The deterministic test fixture

`--embedding-provider deterministic-test-only` selects a dev fixture whose vectors
carry **no semantic meaning**. It exists to exercise the plumbing. It is never a
production default, its module is imported only on that branch, and any artifact
it produces is marked `PLUMBING_ONLY` in the filename and in the JSON.

### Knowledge bounds

| Variable | Default | Meaning |
|---|---|---|
| `KNOWLEDGE_MAX_DOCUMENT_BYTES` | 2,000,000 | Rejection threshold |
| `KNOWLEDGE_MAX_NORMALIZED_CHARS` | 2,000,000 | Rejection threshold |
| `KNOWLEDGE_MAX_CHUNKS_PER_DOCUMENT` *(or `KNOWLEDGE_MAX_CHUNKS`)* | 512 | Max chunks per document |
| `KNOWLEDGE_MAX_CHUNK_CHARS` | 8,000 | Max characters per chunk |
| `KNOWLEDGE_CHUNK_TARGET_TOKENS` *(or `KNOWLEDGE_CHUNK_TARGET`)* | 600 | Chunker target tokens |
| `KNOWLEDGE_CHUNK_MAX_TOKENS` *(or `KNOWLEDGE_CHUNK_MAX`)* | 800 | Chunker max tokens (ceiling 800) |
| `KNOWLEDGE_CHUNK_OVERLAP_TOKENS` *(or `KNOWLEDGE_CHUNK_OVERLAP`)* | 80 | Chunker overlap, must be < target |
| `KNOWLEDGE_LEXICAL_CANDIDATE_LIMIT` | 20 | Retrieval profile |
| `KNOWLEDGE_VECTOR_CANDIDATE_LIMIT` | 20 | Retrieval profile |
| `KNOWLEDGE_RRF_K` | 60 | Retrieval profile |
| `KNOWLEDGE_MAX_HITS_PER_DOCUMENT` | 2 | Diversification cap |
| `KNOWLEDGE_EVALUATION_OUTPUT_DIR` | `.eval-runs/knowledge` | Artifact directory |

Both spellings are accepted for the chunker bounds; each is a full name, so
either binds.

All bounds are **rejections**, never truncations.

## 6. `doctor`

The first thing to run when anything looks wrong.

```bash
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli doctor
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli doctor --json
```

```
knowledge doctor: READY
  database: postgresql+psycopg://copilot:***@127.0.0.1:5433/copilot
  [OK] database: connected
  [OK] vector_extension: installed and visible
  [OK] knowledge_schema: 5 knowledge tables present
  [OK] active_embedding_profile: openai_compatible/text-embedding-3-small dim=1536 COSINE
  [OK] embedding_provider: configured
```

Five checks, all read-only:

| Check | FAIL when | WARN when |
|---|---|---|
| `database` | Unreachable | — |
| `vector_extension` | Not installed, or installed but not visible on this connection's `search_path` | — |
| `knowledge_schema` | Any of the five tables is missing (the message tells you to run `alembic upgrade head`) | — |
| `active_embedding_profile` | — | No ACTIVE profile |
| `embedding_provider` | — | Not configured |

Verdict: `NOT_READY` on any failure, `DEGRADED` on any warning, else `READY`.
Exit code is `1` for `NOT_READY`, `0` otherwise.

`DEGRADED` is the honest answer for "the corpus is reachable but the vector
channel is not": lexical retrieval works. Collapsing that into either `READY` or
`NOT_READY` would either overstate what works or hide a working path.

When the database is unreachable, the three dependent checks are reported as
`FAIL — not checked: the database is unreachable` rather than piling on with
echoes of the same cause.

## 7. Ingesting a document

Local `.txt` and `.md` files only. PDF, DOCX, and HTML are refused **before** any
bytes are decoded — a parser we do not have would silently ingest whatever
happened to decode.

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

Output:

```
document <uuid> version 1 (a1b2c3d4e5f6) chunks=7 created_document=True
created_version=True rebuilt_projection=True
```

- `GLOBAL` **forbids** `--tenant`; `TENANT` **requires** it. Both are refused at
  the CLI boundary, before anything is normalized, hashed, or embedded.
- Re-running the same command with **identical content** reports
  `created_version=False` and does not call the embedding provider again. This is
  idempotency, not caching: `UNIQUE(document_id, content_hash)` makes "identical
  bytes cannot create a second version" a database fact.
- Changing the content appends version 2 and moves `active_version_id`. The old
  version row is never modified. Nothing is overwritten silently.
- The same file ingested from a Windows checkout and a Linux checkout hashes
  identically.

### Retiring a document

```bash
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli retire \
  --tenant tenant-a --document-id <uuid> --reason "superseded by v2 guidance"
```

Retirement is terminal and does not require an embedding provider — withdrawing a
document is a lifecycle transition, not an embedding operation, and it must keep
working during an embedding outage. A retired document leaves normal search.
Citations captured before the retirement **still resolve**.

## 8. Importing MITRE ATT&CK

Local, operator-supplied STIX 2.1 JSON only. **No URLs, no runtime GitHub
access.** Use the Enterprise bundle; tests use a small pinned fixture rather than
the 100 MB+ upstream dataset.

```bash
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli import-attack \
  --file attack-enterprise.json --release v15.1 --activate
```

Output:

```
release v15.1: parsed=214 techniques_created=214 documents_created=214
versions_ingested=214 unchanged=0 skipped=0
```

- Enterprise only. An unsupported framework is refused.
- `--activate` is the **explicit** release switch. Without it, existing rows are
  left exactly as they are. A **new release adds new rows**; old releases are
  never deleted and stay readable by their own `source_release`.
- Each technique becomes a GLOBAL `MITRE_ATTACK` document with
  `external_key = mitre-attack:<technique_id>` and `source_version = <release>`,
  ingested through the **same** versioning/chunking/embedding path as an
  operator's runbook. There is no second ingestion pipeline.
- A technique that is `revoked` or `deprecated` is **skipped and reported**, never
  silently dropped. The `skipped ids:` line lists them.
- Re-importing the same release with the same bundle is idempotent:
  `techniques_created=0`, `versions_ingested=0`, `unchanged=<technique count>`.
- A malformed bundle is rejected **before** anything is written; a bad import is
  never partial. A bundle over the size bound is refused without being parsed.

The canonical `attack_technique` row and the knowledge document are **related but
not the same authority**: the row is the canonical projection, the document is
retrieval content.

## 9. Searching

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

`--mode` is `hybrid` (default), `lexical`, or `vector`. A vector or hybrid search
needs an ACTIVE embedding profile and a configured provider; without either, the
command reports the reason and exits `3` rather than crashing.

`--json` emits a documented key set: `profile_id`, `truncated`,
`retrieval_profile`, and per hit `citation_id`, `document_id`,
`document_version_id`, `chunk_id`, `source_kind`, `title`, `language`,
`source_version`, `excerpt`. **Never a vector, never a raw row, never a
credential.**

Retired documents do not appear. A GLOBAL document is visible to every tenant; a
tenant's own documents to that tenant only.

## 10. Resolving a citation

```bash
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli resolve-citation \
  --tenant tenant-a --citation kcit:<chunk-uuid>:<hash-prefix>
```

```
kcit:9c1d…:a1b2c3d4e5f6: resolved
```

Exit `0` when resolved, `1` when not. `--json` adds the `reason`:

| Reason | Meaning |
|---|---|
| `MALFORMED_CITATION` | The string does not parse. Never repaired, never trusted. |
| `CHUNK_NOT_FOUND` | No such chunk, or not readable by this tenant. |
| `SCOPE_MISMATCH` | The chunk belongs to another tenant. |
| `CONTENT_HASH_MISMATCH` | The chunk's hash does not start with the prefix in the string. |

Resolution deliberately works for **retired documents and historical versions**.
A citation captured in a past investigation must remain explainable after the
document it points at has been superseded or withdrawn.

Resolution proves **provenance**. It proves nothing about correctness.

## 11. Running the baseline

```bash
# Full KB-GOLDEN-V1 suite over the deployment's embedding provider
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli evaluate

# Explicit modes
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli evaluate --mode LEXICAL_ONLY --mode HYBRID

# Re-score a corpus already in the database
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli evaluate --skip-ingest

# Plumbing only -- the deterministic fixture, explicitly requested
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli evaluate --embedding-provider deterministic-test-only
```

A run against a configured provider prints the provider line; a plumbing run
prints the warning line instead, so the two can never be confused on screen:

```
KB-GOLDEN-V1 (corpus 1) cases=22 k=5
embedding: openai_compatible/text-embedding-3-small dim=1536 COSINE
  LEXICAL_ONLY  recall@5=0.xxx mrr=0.xxx ndcg=0.xxx citation_resolution=1.000 leaks=0 forbidden=0 scored=21
  VECTOR_ONLY   recall@5=0.xxx mrr=0.xxx ndcg=0.xxx citation_resolution=1.000 leaks=0 forbidden=0 scored=21
  HYBRID        recall@5=0.xxx mrr=0.xxx ndcg=0.xxx citation_resolution=1.000 leaks=0 forbidden=0 scored=21
hybrid gate: PASS -- ...
artifact: .eval-runs/knowledge/kb-golden-v1-k5.json
```

`cases=22` counts the fixture; `scored=21` counts the cases a mode actually
ranked. The difference is the fixture's `unanswerable` cases, which are
excluded from the mean metrics by design and reported as `cases_excluded`.

**A plumbing run, in full** — this is the shape of the output when no embedding
provider is configured and `--embedding-provider deterministic-test-only` is
passed explicitly. It is the only run this repository has ever produced, and it
is a pipeline check, not a quality measurement:

```
KB-GOLDEN-V1 (corpus 1) cases=22 k=5
embedding: PLUMBING ONLY (deterministic-test-only) -- these vectors carry no semantic meaning
  LEXICAL_ONLY  recall@5=0.952 mrr=0.952 ndcg=0.924 citation_resolution=1.000 leaks=0 forbidden=0 scored=21
  VECTOR_ONLY   recall@5=0.381 mrr=0.248 ndcg=0.265 citation_resolution=1.000 leaks=0 forbidden=5 scored=21
  HYBRID        recall@5=0.976 mrr=0.782 ndcg=0.828 citation_resolution=1.000 leaks=0 forbidden=1 scored=21
hybrid gate: PASS -- hybrid mean recall@5 0.976 against the better single-channel baseline 0.952 (tolerance 0.02); 6 mixed case(s) retrieved
artifact: .eval-runs/knowledge/kb-golden-v1-k5-plumbing-only.json
```

These were measured on a database holding exactly the 18 corpus documents.
The suite scores against the live database, so ambient documents in a competing
scope move the rank-sensitive metrics — always record the database state with the
number.

Read those numbers for what they are. `VECTOR_ONLY` failing to retrieve is the
**expected** result of random vectors — it is evidence that the vector channel is
genuinely wired to the embedding provider rather than accidentally falling back
to lexical matching. A plumbing `HYBRID` gate PASS therefore says the fusion,
ranking, citation and scoring machinery works end to end; it says nothing about
semantic retrieval quality. **Do not present a plumbing run as a semantic
baseline.**

The driver ingests the sealed corpus through the ordinary ingestion use case,
retires the fixture's retired document, runs each requested mode, and writes the
artifact. Exit code is `1` only on a `FAIL` hybrid gate; `NOT_RUN` exits `0`.

### A second run is safe

`evaluate` is re-runnable against a corpus that is already in the database. The
one corpus document the fixture retires by design is carried forward under its
existing id rather than re-ingested, because `RETIRED` is terminal in the domain
by design and re-ingesting it is refused — correctly. The skip is narrow: it
applies only to the keys the sealed fixture itself retires, so an unrelated
document found in a `RETIRED` state still fails the run loudly. On a re-run
expect this line among the corpus progress lines:

```
  [9/18] guidance-legacy-ssh-hardening: skipped (already retired by a previous run)
```

`--skip-ingest` is the stronger form: it reuses the corpus already in the
database and ingests nothing at all.

See [evaluation-contract.md](evaluation-contract.md) for the metrics, the gate
rule, and the artifact schema.

## 12. Verifying the deployment

```bash
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe -m pytest tests/unit tests/architecture -q
.venv/Scripts/python.exe -m pytest tests/integration -q
.venv/Scripts/python.exe -m alembic check
.venv/Scripts/python.exe -m alembic heads
```

The real-PostgreSQL knowledge suite targets the pgvector-capable test database
(`127.0.0.1:5434` by default) and **skips** when it is unreachable:

```bash
.venv/Scripts/python.exe -m pytest tests/integration/persistence/test_knowledge_persistence.py -q
```

It verifies tenant isolation, the scope CHECK constraints, the partial unique
indexes, the one-ACTIVE-profile rule, and citation resolution against a real
database.

Regression checks that must stay green: GP-01 closure tests, P1 workspace tests,
P2 response/durability tests, and the ToolRegistry selectable-name set
(`hisiem.search_events`, `hisiem.get_detection_rule` — unchanged).

## 13. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `type "vector" does not exist` | pgvector installed in a schema not on the pinned `search_path` | `ALTER EXTENSION vector SET SCHEMA copilot;` |
| Migration refuses with the pgvector message | Extension absent and the role may not create it | `CREATE EXTENSION IF NOT EXISTS vector SCHEMA copilot;` as an admin |
| `doctor` → `NOT_READY`, `knowledge_schema` FAIL | P3-A tables missing | `alembic upgrade head` |
| `doctor` → `DEGRADED`, `active_embedding_profile` WARN | No document ingested yet | Ingest one; or configure the embedding provider and re-ingest |
| `search --mode vector` exits `3` | No ACTIVE profile or no provider configured | Configure `EMBEDDING_*`, restart, ingest |
| `ingest-file` refuses the file | Not `.txt`/`.md` | Convert to Markdown; PDF/DOCX/HTML are out of scope |
| `import-attack` reports `skipped` | Revoked/deprecated techniques in the bundle | Expected. The ids are listed; nothing was silently dropped |
| Artifact write refused | A baseline already exists at that path | Pass `--overwrite`, or point `--out` at another **directory** (`--out` names a directory; the filename is derived from the suite, `k`, and the evidence quality) |
