# P3-A Operations

How to provision, verify, ingest, search, and evaluate the knowledge subsystem.

**Nothing in this document requires dropping a volume, resetting a database, or
re-sealing GP-01.** The P3-A migrations only ever add their own tables; they remove
nothing that existed before them. The closure revision copies every pre-existing
chunk forward **preserving its `id`**, so neither an upgrade nor a rollback
discards knowledge that was already stored.

> **P3-B is NOT YET ACTIVE.** The knowledge Agent tools
> (`knowledge.retrieve_security_guidance`, `knowledge.resolve_attack_technique`)
> are catalogued but not registered. No model can reach this subsystem. What
> follows is an operator procedure, not an Agent capability.

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| PostgreSQL 16 **with pgvector** | Shipped by `infra/docker-compose.yml` as **`pgvector/pgvector:pg16`** — a pinned tag, not `postgres:16` and not `latest`. See §2.1. |
| The `copilot` schema | Created for you on a fresh volume by `infra/postgres-init/01-copilot-schema.sql`. On an existing volume it may need one statement by hand — see §2.3. |
| Python env | `.venv/Scripts/python.exe` on Windows |
| `COPILOT_DATABASE_URL` | Defaults to `postgresql+psycopg://copilot:copilot@127.0.0.1:5433/copilot` |

The connection is pinned to the `copilot` schema via `search_path`, which is why
the schema has to **exist** before Alembic can record anything in it. That detail
is the source of both confusing failure modes in §2.

**Never `docker compose down -v`.** The volume is `copilot_pgdata`; it holds every
investigation, evidence row, and knowledge document in the deployment. Every
procedure in this document works against the volume that already exists.

## 2. pgvector and the `copilot` schema

pgvector is an **infrastructure prerequisite**. It is a PostgreSQL extension, not
a Python dependency the application can install for you, and it cannot be added to
a server image that does not ship it.

### 2.1 The shipped image

`infra/docker-compose.yml` runs **`pgvector/pgvector:pg16`** — upstream PostgreSQL
16 with the pgvector extension added, on a **pinned** tag rather than `latest`.
The plain `postgres:16` image does **not** ship pgvector, so a fresh clone on that
image dies at the first migration with `type "vector" does not exist`, and no
amount of configuration recovers it. That is why the default compose file is the
pgvector image. Nothing else about the service changed: same `container_name`, same
`5433:5432` port mapping, same `POSTGRES_USER`/`POSTGRES_DB`, same `copilot_pgdata`
volume. HISIEM's own PostgreSQL on `5432` is untouched.

Shipping the binary is **not** the same as having the extension. An extension is
created **per database**, not per image, so the first `alembic upgrade head` still
issues `CREATE EXTENSION IF NOT EXISTS vector`. Nothing here assumes a superuser:
the migration verifies the extension is present, attempts to create it if the role
is permitted to, and otherwise fails explicitly with an actionable message *before
any table exists*.

### 2.2 A fresh clone

```bash
docker compose -f infra/docker-compose.yml up -d
# wait for the healthcheck (pg_isready -U copilot -d copilot)
.venv/Scripts/python.exe -m alembic upgrade head
```

Two things make that work end to end, and neither is automatic on a deployment
that already exists:

- `infra/postgres-init/01-copilot-schema.sql` creates the `copilot` **schema**
  during cluster initialisation — but only for an empty data directory (§2.3).
- the first `alembic upgrade head` creates the `vector` extension **in that
  schema**, so the type resolves under the pinned `search_path` (§2.4).

### 2.3 The `copilot` schema on an existing volume

`docker-entrypoint-initdb.d` runs **only** when the data directory is empty —
exactly once, for a fresh `copilot_pgdata`. An existing volume never re-runs it, so
an existing deployment can have the `copilot` database without the `copilot`
schema. The symptom appears before any migration runs:

```
psycopg.errors.InvalidSchemaName: no schema has been selected to create in
[SQL: CREATE TABLE alembic_version (...)]
```

Fix it once, as a database administrator:

```sql
CREATE SCHEMA IF NOT EXISTS copilot;
```

Alembic still owns every object inside it. This creates the empty namespace, which
is the one thing Alembic cannot do for itself — it needs the namespace to exist
before it can record its own version table.

### 2.4 The extension on an EXISTING database

Run this **once**, as a database administrator:

```sql
CREATE EXTENSION IF NOT EXISTS vector SCHEMA copilot;
```

Then `alembic upgrade head` proceeds normally. No data is touched.

### 2.5 Upgrading an existing volume to the pgvector image

This is the safe path, and it touches no data:

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

The new container mounts the **same** `copilot_pgdata` volume, so every existing
row is still there — no `down -v`, no reset, no re-seal. `pgvector/pgvector:pg16`
is upstream PostgreSQL 16 with the extension added, so it is the same server the
previous image ran: the on-disk format does not change and no data-directory
upgrade step is involved. `docker-entrypoint-initdb.d` does not re-run on a
non-empty volume, which is exactly why 2.3 and 2.4 are issued by hand.

### 2.6 If pgvector is already installed in `public`

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

### 2.7 What the migration says when it cannot proceed

```
The PostgreSQL 'vector' extension (pgvector) is required by this migration and is
not installed, and this role may not create it. Ask a database administrator to run
`CREATE EXTENSION IF NOT EXISTS vector SCHEMA copilot;` in this database once, then
re-run `alembic upgrade head`. Nothing was changed by this failed run.
```

A failed run is atomic: the check runs before any `CREATE TABLE`.

### 2.8 Observed state of this workstation's existing databases

Two databases are relevant here, and they are **not** interchangeable.

| Database | State | Use |
|---|---|---|
| `127.0.0.1:5433` | The operator's Copilot database. PostgreSQL 16.15. Revision `979070495d4f` (P2) — **two** revisions behind P3-A head (`ed6af82d9b13`, `c41f7b2e9d08`). `pg_available_extensions` lists neither `vector` nor any pgvector package, so `CREATE EXTENSION vector` **cannot** succeed on this server as packaged. | **READ-ONLY.** Safe for `doctor`, which only reads. Never run `alembic upgrade`/`downgrade` or any DDL against it until its server image carries pgvector (§2.5). |
| `127.0.0.1:5434` | The pgvector-capable test database used by the P3-A integration suite. | Migrated, exercised, and cycled by tests. |

That is why the P3-A integration tests hardcode `127.0.0.1:5434` rather than
honouring `COPILOT_DATABASE_URL`: a test that wrote to the operator's database
would be a defect, not a convenience.

Bringing `5433` up to P3-A is therefore a **multi-part** operator action, not one:
switch that server to the pgvector image (§2.5), issue `CREATE SCHEMA` if §2.3
applies, run the one-time `CREATE EXTENSION` above, then `alembic upgrade head`.
Until then, `doctor` against `5433` reports `NOT_READY` with `vector_extension` and
`knowledge_schema` FAIL — the correct, non-destructive answer, and the one it
gives without touching anything. This is the **observed** output at the time of
this closure:

```
knowledge doctor: NOT_READY
  database: postgresql+psycopg://copilot:***@127.0.0.1:5433/copilot
  [OK] database: connected
  [FAIL] vector_extension: the vector extension is not installed in this database; see docs/p3/p3-a-operations.md for the one-time prerequisite
  [FAIL] knowledge_schema: missing tables: attack_release, attack_technique, embedding_profile, knowledge_chunk_embedding, knowledge_content_chunk, knowledge_document, knowledge_document_version (run: alembic upgrade head)
  [FAIL] active_embedding_profile: not checked: the knowledge schema is missing (run: alembic upgrade head)
  [WARN] embedding_provider: no embedding provider configured (EMBEDDING_PROVIDER=unconfigured): lexical retrieval only
```

`attack_release_authority` and `legacy_chunk_table` are absent from that listing
rather than reported as failures: both query tables the missing schema would not
have, and the report says why a check did not run instead of repeating the cause.

## 3. Migrating

```bash
.venv/Scripts/python.exe -m alembic heads
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m alembic check
```

The P3-A chain is `979070495d4f` (the P2 response lifecycle migration) →
`ed6af82d9b13` (the original P3-A schema) → **`c41f7b2e9d08`** (head; the closure
revision: immutable content chunks and the ATT&CK release model).

`ed6af82d9b13` is released and **strictly unmodifiable**, so the closure's schema
changes arrive as new revisions stacked on top of it. The upgrade is additive and
safe on a database that already carries the original P3-A tables: it copies every
existing chunk into the new immutable pair **preserving its `id`**, which is what
lets a `kcit:` handle minted before the upgrade resolve to the row that now holds
its content. The embedding rows get fresh surrogate ids, which is safe precisely
because that table is the rebuildable projection.

### Verifying a migration cycle

```bash
.venv/Scripts/python.exe -m alembic downgrade -1
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m alembic check
```

`downgrade -1` undoes **only what `c41f7b2e9d08` created**:
`knowledge_content_chunk`, `knowledge_chunk_embedding`, `attack_release`, and the
foreign key it added to `attack_technique`. Every pre-P3-A object —
`investigation`, `domain_event`, `outbox_message`, `command_receipt`,
`orchestration_binding`, `tool_invocation`, `response_proposal`, and the LangGraph
checkpoint schema — is untouched, and so is `knowledge_chunk`, which
`ed6af82d9b13` created and therefore owns. Keeping it standing is what makes
`downgrade -1` followed by `upgrade head` converge instead of losing the rows the
upgrade would have to backfill from. The `vector` extension is deliberately
**not** dropped: it may predate P3-A and other schemas may depend on it, so
removing it would be a destructive change far outside the migration's scope.

One value genuinely cannot be restored: a legacy `attack_technique.active` flag
that contradicted its own release. The upgrade refused to treat such a framework's
rows as authority, so there is no authority to put back — and re-deriving one from
a flag the closure exists to retire would restore the ambiguity, not the
information.

`alembic check` must report no drift both after the upgrade and after the
downgrade/upgrade cycle.

## 4. Schema created

| Table | Purpose |
|---|---|
| `knowledge_document` | Externally-identified document. Immutable identity; ACTIVE → RETIRED lifecycle. |
| `knowledge_document_version` | Immutable content. A change appends a version. |
| `knowledge_content_chunk` | **Immutable content identity** — the citation target. Written once, never rewritten, with the generated FTS column. |
| `knowledge_chunk_embedding` | The **rebuildable** vector projection of a content chunk. Carries no content and no hash. |
| `embedding_profile` | The vector space the chunks were indexed in. At most one ACTIVE. |
| `attack_release` | The pinned ATT&CK release and its authority. At most one ACTIVE per framework. |
| `attack_technique` | The pinned technique snapshot belonging to a release. |
| `knowledge_chunk` | **Superseded, retained.** `ed6af82d9b13`'s table. P3-A never reads or writes it; it is kept only so `downgrade` can restore it byte for byte. `doctor` reports it rather than dropping it. |

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
- `uq_knowledge_content_chunk_generation_ordinal` is unique on
  `(document_version_id, generation, ordinal)`. That is what makes the stable
  ranking key a **total** order, and it means a rechunk writes a new generation
  rather than rewriting the old one.
- `uq_knowledge_chunk_embedding_content_profile` is unique on
  `(content_chunk_id, embedding_profile_id)` — one vector per chunk per space, so
  a rebuild is an upsert rather than a duplicate.
- `uq_attack_release_single_active` is a **per-framework partial unique index**
  (`WHERE status = 'ACTIVE'`). "At most one authoritative ATT&CK release per
  framework" is therefore a database fact, not a convention: two concurrent
  activations cannot both commit. `uq_attack_release_framework_source_release`
  makes a release name registrable once.

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

### Switching the ACTIVE profile is not an ingest

When an `ACTIVE` profile exists and the configured provider's descriptor identity
differs from it, **every** ordinary document ingest fails closed:

```
error: EMBEDDING_PROFILE_SWITCH_REQUIRES_CORPUS_REINDEX: ...
```

That includes an ingest that passes `--allow-embedding-profile-switch`. The flag is
**legacy and always refused**: it is retained only so an existing caller receives
that diagnosis instead of an unrecognised-argument error, and it does nothing else.

This is deliberate, and the alternative is worse than it looks. Letting one
document's ingest retire the old profile and create a new `ACTIVE` one would leave
the corpus half-embedded in two incomparable spaces while retrieval went on
comparing cosine distances across them — plausible numbers computed in no single
space. Switching the embedding space is a **whole-corpus reindex**, not a document
ingest.

After a refusal, all of the following still hold:

- the previous profile is **still** the `ACTIVE` profile;
- the existing corpus is still vector-retrievable;
- the one-ACTIVE-profile index is intact, because no second profile was created;
- no embedding-projection row was rewritten.

The correct corpus-wide flow — stage a new profile, reindex the whole corpus,
validate completeness, activate atomically, retire the old profile — is documented
for a future phase and deliberately **not implemented** in P3-A. There is no
partial cutover, and no `STAGING` status exists for production retrieval to
accidentally use.

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
knowledge doctor: DEGRADED
  database: postgresql+psycopg://copilot:***@127.0.0.1:5434/copilot
  [OK] database: connected
  [OK] vector_extension: installed and visible
  [OK] knowledge_schema: 7 knowledge tables present
  [WARN] active_embedding_profile: no ACTIVE embedding profile: lexical retrieval works, vector and hybrid retrieval are unavailable until a document is ingested
  [WARN] attack_release_authority: no ACTIVE ATT&CK release: every canonical technique row is non-authoritative. ...
  [OK] legacy_chunk_table: knowledge_chunk present with 0 superseded row(s): kept only so a downgrade can restore them byte for byte, never read or written by P3-A
  [WARN] embedding_provider: no embedding provider configured (EMBEDDING_PROVIDER=unconfigured): lexical retrieval only
```

Seven checks, all read-only:

| Check | FAIL when | WARN when |
|---|---|---|
| `database` | Unreachable | — |
| `vector_extension` | Not installed, or installed but not visible on this connection's `search_path` | — |
| `knowledge_schema` | Any of the **seven** `KNOWLEDGE_TABLES` is missing (the message tells you to run `alembic upgrade head`) | — |
| `active_embedding_profile` | — | No ACTIVE profile |
| `attack_release_authority` | More than one ACTIVE release for one framework — `ATTACK_RELEASE_AUTHORITY_AMBIGUOUS` | No ACTIVE release at all |
| `legacy_chunk_table` | Never | — (reports `absent`, or the retained row count) |
| `embedding_provider` | — | Not configured |

`attack_release_authority` reads `attack_release` and nothing else, because
authority lives at release granularity. The schema already enforces the
single-ACTIVE rule with a partial unique index; the check exists because an
operator who restores a dump, or applies a migration set out of order, can end up
with the index missing — and then the rows are the only witness left. It is also
where the ambiguity the upgrade deliberately refused to resolve becomes visible;
see §8.

Verdict: `NOT_READY` on any failure, `DEGRADED` on any warning, else `READY`.
Exit code is `1` for `NOT_READY`, `0` otherwise.

`DEGRADED` is the honest answer for "the corpus is reachable but the vector
channel is not": lexical retrieval works. Collapsing that into either `READY` or
`NOT_READY` would either overstate what works or hide a working path.

When the database is unreachable, the five dependent checks are reported as
`FAIL — not checked: the database is unreachable` rather than piling on with five
echoes of the same cause. `embedding_provider` still runs: it reads configuration,
not the database, so it has an answer even when nothing else does.

When the schema is missing, only `active_embedding_profile` is appended as
`not checked: the knowledge schema is missing (run: alembic upgrade head)`, and
`attack_release_authority`, `legacy_chunk_table`, and the profile check are simply
absent. That is the one case where the check count is short of seven, and it is
deliberate: three more lines saying "no such table" would bury the one line that
tells the operator what to do.

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
- `--activate` is the **explicit** release switch. Without it, this release's rows
  are created **inactive** and the framework's current authority is untouched. With
  it, this release's rows become active and every other release *of the same
  framework* becomes inactive, in one transaction. A **new release adds new rows**;
  old releases are never deleted and stay readable by their own `source_release`.
- A release is registered in `attack_release` with a **content fingerprint** — a
  SHA-256 over its canonical technique collection, sorted by
  `(technique_id, source_stix_id)`, with `tactics`/`platforms` sorted and deduped.
  It is independent of input JSON object order and of STIX bundle order, and it is
  re-derivable from the database alone.
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
not the same authority**: the row is the canonical projection, the document is a
**retrieval projection** of it, and both derive from the same canonical function so
they cannot drift.

### A pinned release is immutable

"v15.1" is an immutability claim, and the fingerprint is what makes it checkable:

| Situation | Behaviour |
|---|---|
| First import of a release | The fingerprint is persisted on `attack_release` |
| Same release, same fingerprint | Idempotent — the import converges, nothing is rewritten |
| Same release, **different** fingerprint | Refused with `ATTACK_RELEASE_CONTENT_CONFLICT`, detected **before any mutation** |
| A release adopted from pre-fingerprint rows | `content_fingerprint IS NULL`; the first import that pins it compares against the rows actually stored |

```
error: ATTACK_RELEASE_CONTENT_CONFLICT: release v15.1 is already pinned to a
different technique collection; re-import the pinned bundle or use a new release name
```

A refused import leaves **no** canonical `attack_technique` row, no
`KnowledgeDocument`, no `KnowledgeDocumentVersion`, no change to the authoritative
release, and no embedding-projection row behind. Because the check runs first,
"nothing was changed" is a fact rather than a reconstruction.

Reordered STIX objects, or a semantically identical bundle serialized differently,
produce the **same** fingerprint and therefore converge. Only changed technique
content conflicts — which is the point: the check must not fire on re-serialization
and must not stay silent on a genuine content change.

### Crash and retry

A run interrupted after the canonical rows committed but before the documents
finished leaves the fingerprints persisted. Re-running the **same** release with
the **same** fingerprint continues and completes the documents: no duplicate
canonical rows, no false conflict. Re-running with a **different** fingerprint
still conflicts, even though the document projection is incomplete — there is no
half-new-release backfill.

### `ATTACK_RELEASE_AUTHORITY_AMBIGUOUS`

If the pre-existing rows already claimed more than one release of a framework, the
upgrade **refuses to guess** and reports:

```
ATTACK_RELEASE_AUTHORITY_AMBIGUOUS - ...
```

`doctor`'s `attack_release_authority` check reports the same thing at runtime. The
operator resolves it explicitly by importing the intended release with
`--activate`; choosing which canonical knowledge is authoritative is an operator's
decision, not a migration's.

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
| `CONTENT_INTEGRITY_MISMATCH` | The stored hash is not the hash of the content actually read, **or** the stored hash does not carry the prefix in the string. |

Both integrity checks are needed, and they catch different tampering. A stored hash
is not evidence; it is a *claim written beside the content*. The resolver
recomputes `SHA-256` over the content it actually read and requires the recomputed
value to equal the stored full hash **and** the stored hash to start with the
citation's prefix. Editing the text out-of-band breaks the first; editing the
stored hash to match a forged prefix breaks the second. Either way the answer is
`unresolved` with that reason, and no exception escapes.

Resolution deliberately works for **retired documents, historical versions, and
superseded chunk generations**. A citation captured in a past investigation must
remain explainable after the document it points at has been superseded, withdrawn,
or rechunked. The citation names an **immutable content chunk**, never an embedding
row, so it also survives re-embedding, an embedding-profile rebuild, a
retrieval-projection rebuild, and a process restart.

Normal `search` excludes retired documents and older generations; `resolve`
excludes neither. P3-A offers **no destructive delete**, so the only way to make a
historical citation unresolvable is an operator deleting rows by hand — which is
exactly why the integrity checks above are recomputed rather than read.

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
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli --embedding-provider deterministic-test-only evaluate

# NOT a sealed baseline -- measure whatever the database currently holds
.venv/Scripts/python.exe -m hisiem_soc_copilot.knowledge.cli evaluate --allow-ambient-corpus
```

A run against a configured provider prints the provider line; a plumbing run
prints the warning line instead, so the two can never be confused on screen:

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

`cases=22` counts the fixture; `scored=21` counts the cases a mode actually
ranked. The difference is the fixture's `unanswerable` cases, which are
excluded from the mean metrics by design and reported as `cases_excluded`.

**A plumbing run, in full** — this is the shape of the output when no embedding
provider is configured and `--embedding-provider deterministic-test-only` is
passed explicitly. It is the only run this repository has ever produced, and it
is a pipeline check, not a quality measurement:

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

`documents=17` and not 18: the fixture itself retires one document, and eligible
means tenant-visible **and** ACTIVE.

Those are the numbers the current code produces on the test database. `HYBRID`
`mrr`/`ndcg` sit slightly below the pre-closure figures (`0.782`/`0.828`) because
ties are now broken on semantic identity instead of a random UUID — the same
change that makes the baseline reproducible across databases rather than merely
repeatable on one. `LEXICAL_ONLY` and `VECTOR_ONLY` are unchanged.

### The corpus precondition

Before a single retrieval runs, the run derives the tenant-visible ACTIVE eligible
corpus from the database and compares it against the sealed fixture. An unexpected
document, a missing expected one, or a changed content hash / version / chunk
projection fails the run with `CORPUS_PRECONDITION_FAILED` instead of producing a
score:

```
error: CORPUS_PRECONDITION_FAILED: expected corpus fingerprint ... does not match ...
```

A ranking measured over the wrong corpus is not merely useless; it is a number
someone would quote. The default is **sealed**, and ambient documents in a
competing scope are a precondition failure rather than a tolerant warning.

`--allow-ambient-corpus` is a **different measurement**, not a lesser sealed run.
It skips the precondition entirely, marks the artifact `corpus_mode: "OPEN_CORPUS"`
/ `sealed: false`, names the file `...-open-corpus.json`, prints
`NOT A SEALED BASELINE`, and produces **no hybrid gate verdict** — so an
open-corpus run can never report a `KB-GOLDEN-V1 baseline PASS`. It can say what the
database currently returns; it can never say that is the fixture.

### The corpus fingerprint

Every sealed artifact records a `corpus_fingerprint`: a SHA-256 over the deduped,
sorted set of `(visibility, tenant_id, source_kind, external_key,
document_version_number, document_content_hash, chunk_generation, ordinal,
chunk_content_hash)` facts, serialized canonically. It contains **no** row UUIDs,
timestamps, database host, or embedding vectors — each of which would make two
identical corpora look different — and never a credential.

Its purpose is checkability: two independent databases that ingested the same
fixture produce the same fingerprint, which is what makes "the same baseline" a
falsifiable claim rather than an assurance.

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
indexes, the one-ACTIVE-profile rule, the single-authoritative-release rule, the
stable ranking key across two independently-ingested databases, and citation
resolution against a real database.

Regression checks that must stay green: GP-01 closure tests, P1 workspace tests,
P2 response/durability tests, and the ToolRegistry selectable-name set
(`hisiem.search_events`, `hisiem.get_detection_rule` — unchanged).

## 13. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `type "vector" does not exist` | pgvector installed in a schema not on the pinned `search_path` | `ALTER EXTENSION vector SET SCHEMA copilot;` |
| Migration refuses with the pgvector message | Extension absent and the role may not create it | `CREATE EXTENSION IF NOT EXISTS vector SCHEMA copilot;` as an admin |
| `doctor` → `NOT_READY`, `knowledge_schema` FAIL | P3-A tables missing | `alembic upgrade head` |
| `InvalidSchemaName: no schema has been selected to create in` on the first migration | Existing volume: `docker-entrypoint-initdb.d` never ran | `CREATE SCHEMA IF NOT EXISTS copilot;` (§2.3) |
| `doctor` → `NOT_READY`, `attack_release_authority` FAIL, `ATTACK_RELEASE_AUTHORITY_AMBIGUOUS` | More than one ACTIVE release for one framework | Import the intended release with `--activate`; leave the others inactive (§8) |
| `import-attack` → `ATTACK_RELEASE_CONTENT_CONFLICT` | The same release name was imported with a different technique collection | Re-import the pinned bundle, or use a new release name (§8) |
| `ingest-file` → `EMBEDDING_PROFILE_SWITCH_REQUIRES_CORPUS_REINDEX` | The configured provider differs from the ACTIVE profile | Switching the embedding space is a whole-corpus reindex, not an ingest; restore the original provider configuration, or reindex the corpus (§5) |
| `evaluate` → `CORPUS_PRECONDITION_FAILED` | The database does not hold exactly the sealed fixture in the expected scopes | Use a dedicated database, or run with `--allow-ambient-corpus` and read the result as `OPEN_CORPUS`, not as the baseline (§11) |
| `doctor` → `DEGRADED`, `active_embedding_profile` WARN | No document ingested yet | Ingest one; or configure the embedding provider and re-ingest |
| `doctor` → `attack_release_authority` WARN | No ACTIVE ATT&CK release | Import the intended release with `--activate` (§8) |
| `search --mode vector` exits `3` | No ACTIVE profile or no provider configured | Configure `EMBEDDING_*`, restart, ingest |
| `ingest-file` refuses the file | Not `.txt`/`.md` | Convert to Markdown; PDF/DOCX/HTML are out of scope |
| `import-attack` reports `skipped` | Revoked/deprecated techniques in the bundle | Expected. The ids are listed; nothing was silently dropped |
| Artifact write refused | A baseline already exists at that path | Pass `--overwrite`, or point `--out` at another **directory** (`--out` names a directory; the filename is derived from the suite, `k`, and the evidence quality) |
