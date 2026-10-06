# Knowledge Domain — versioned security knowledge

P3-A adds a **supporting bounded context** at `src/hisiem_soc_copilot/domain/knowledge/`.
It answers one question and refuses every other:

> What did a trusted source say, in which immutable version, and what does that
> version hash to?

It is not part of the Investigation aggregate, it is not a permission model, and
it cannot authorize anything. The reasoning behind that split is in
[product-positioning.md](../product-positioning.md) and
[domain-model.md](../domain-model.md); this document is the concrete contract.

## 1. Why a separate bounded context

An investigation is a *decision procedure over evidence*. Knowledge is
*reference material*. Merging them would give a document the ability to change an
investigation's state, and the whole point of the P2 invariant — the Agent
recommends and cannot authorize — is that no artifact may do that.

So knowledge is a peer context, not an entity inside Investigation:

| Concern | Owner |
|---|---|
| Lifecycle of an investigation, its evidence, its verdict | `domain/investigation` |
| Lifecycle of a knowledge document and its versions | `domain/knowledge` |
| Which tenant may *read* a document | `domain/knowledge` (`Visibility`) |
| Whether a tenant may *act* on anything | `domain/response`, Human Approval |

`KnowledgeDocument` is deliberately **not** a field on the Investigation
aggregate. A retrieval result reaches an investigation only as a citation
attached to something else (in a future phase), never as an owned child.

## 2. The entities

### `KnowledgeDocument` — the aggregate root

One externally-identified piece of knowledge. Identity is
`(source_kind, external_key)` resolved inside a scope.

| Field | Notes |
|---|---|
| `id` | UUID |
| `source_kind` | `MITRE_ATTACK` \| `CURATED_GUIDANCE` \| `TENANT_RUNBOOK` — **immutable** |
| `external_key` | The source's own identifier. **immutable** |
| `visibility` | `GLOBAL` \| `TENANT` — **immutable** |
| `tenant_id` | `None` for GLOBAL, the owner for TENANT |
| `title` | ≤ 512 chars; tracks the active version's title |
| `status` | `ACTIVE` \| `RETIRED` |
| `active_version_id` | Pointer to the current version, or `None` before first ingest |
| `revision` | Domain change counter, emitted with events |
| `lock_version` | Persistence CAS token, owned by the repository |
| `created_at`, `retired_at` | `retired_at` is set exactly when status is RETIRED |

Three rules are enforced in the domain *and* as database CHECK constraints:

1. `GLOBAL` ⟺ `tenant_id IS NULL`; `TENANT` ⟺ `tenant_id IS NOT NULL`.
2. `status = 'ACTIVE'` ⟺ `retired_at IS NULL`.
3. `revision >= 0`, `lock_version >= 0`.

`source_kind`, `external_key` and `visibility` never change after creation.
There is no method that mutates them. A document that needs different identity is
a different document.

### `KnowledgeDocumentVersion` — immutable

A version is written once and never updated. A content change **appends** a
version; it never rewrites one. That is what makes provenance checkable: a
citation naming `(document_version_id, content_hash)` stays verifiable, because
neither the bytes nor the row can change afterwards.

| Field | Notes |
|---|---|
| `id`, `document_id` | UUIDs |
| `version` | ≥ 1, dense per document |
| `content_hash` | lowercase SHA-256 hex |
| `title` | The title as of this version |
| `normalized_content` | Non-empty, and **already normalized** |
| `language` | ≤ 16 chars, default `en` |
| `source_version` | The upstream version, e.g. an ATT&CK release |
| `metadata` | Free-form JSONB, stored and never interpreted |
| `ingested_at`, `effective_at` | Timestamps |

`__post_init__` enforces the invariant that makes `content_hash` mean anything,
and it enforces it on **every** construction path — `create()`, a mapper load, and
a test literal alike:

```python
compute_content_hash(normalized_content) == content_hash   # else InvalidKnowledgeVersionError
```

Two failure modes are rejected rather than repaired, and repairing either would
be a *second* definition of the hash:

1. `normalized_content` is not a fixed point of `normalize_knowledge_content`.
   A version whose "normalized" content re-normalizes to something else carries a
   hash that a later re-normalization of the same bytes could not reproduce.
2. `content_hash` is not the hash of the content stored beside it. A row whose
   hash and body disagree describes neither, so provenance is already broken
   before anyone reads it.

Both checks go through the one domain hash function. There is no second SHA-256
in the mapper, the repository, or the migration.

### `KnowledgeContentChunk` — immutable content identity

The citation **target**. Written once and never rewritten.

| Field | Notes |
|---|---|
| `id`, `document_id`, `document_version_id` | UUIDs |
| `generation` | ≥ 1. A rechunk of the same version is a **new** generation; the old one is left intact |
| `ordinal` | ≥ 0, unique within `(document_version_id, generation)` |
| `heading_path` | The section path the chunk came from |
| `content` | The chunk text. **immutable** |
| `content_hash` | Lowercase SHA-256 hex, derived from `content` by `create()` |
| `token_count`, `language` | Bounds and metadata |
| `chunker_version` | Which chunker produced it |
| `created_at` | |

Why it is a separate entity from the embedding row is the whole point:

- Content identity is written once and never rewritten, so a citation into it
  stays resolvable across re-embedding, retrieval-projection rebuilds, process
  restarts, document retirement, the activation of a later document version, and
  a rechunk.
- The embedding row is a **rebuildable projection** of it. Dropping and recreating
  every embedding row breaks no citation, because no citation names one.

The same content/content_hash self-validation the version entity enforces is
enforced here too, at the entity boundary and through the **same** shared helper:
`create()` derives the hash from the content instead of accepting one, and
`__post_init__` re-checks the pair on every load. A chunk whose stored hash is not
the hash of its stored text is rejected, never repaired.

### `KnowledgeChunkEmbedding` — the rebuildable projection

`(content_chunk_id, embedding_profile_id, embedding, indexed_at)`, unique on
`(content_chunk_id, embedding_profile_id)`.

It carries **no content and no hash**. That absence is the design rather than an
omission: nothing can be cited *through* this table, so rebuilding it is a purely
operational act. It is the only knowledge table expected to be dropped and
recreated.

### `AttackRelease` — the pinned release and its authority

| Field | Notes |
|---|---|
| `id`, `framework`, `source_release` | Unique on `(framework, source_release)` |
| `content_fingerprint` | SHA-256 over the release's canonical technique collection. `NULL` only for a release the migration adopted from rows that predate fingerprinting |
| `status` | `ACTIVE` \| `INACTIVE`. A **per-framework partial unique index** makes "at most one ACTIVE release per framework" a database fact |
| `technique_count`, `created_at`, `activated_at` | |

Authority lives **here**, at release granularity — not on the technique rows.
"Which release is authoritative for this framework" is one fact about one release;
modelling it per technique is what previously allowed two releases to be
authoritative at once. `attack_technique` remains the pinned technique *snapshot*
— one row per technique — and each row references its release by
`(framework, source_release)`. See [security-boundary.md](security-boundary.md)
§10 for the immutability rule and its fingerprint.

An import is three acts, and keeping them apart is what makes the authority claim
true rather than merely intended:

1. **Registration.** The bundle is parsed, fingerprinted, and verified against the
   pinned immutability rule; the release and its canonical technique rows are then
   written **INACTIVE**, in one short transaction. `activate=True` does not
   activate anything at this point — it records that the import has authority
   *intent*.
2. **Staging.** Each technique is ingested through a dedicated MITRE-capable
   ingestion handler -- the same ingestion use case, but built by the
   container's `attack_projection_ingestion_handler` factory with a capability
   the ordinary factory cannot mint -- with `activate_version=False`, so its
   immutable version, its chunks and its embeddings are all created — but
   `active_version_id` is **not** moved. One binding row per technique is then
   recorded in `attack_release_projection`. A staged release is therefore fully
   projected and completely invisible to retrieval.

   `MITRE_ATTACK` is system-managed. The ordinary knowledge handler refuses it
   with `SYSTEM_MANAGED_KNOWLEDGE_SOURCE` on both ingest and retire, and
   `ingest-file --source-kind` does not offer it. There is no caller-controlled
   bypass flag: capability comes from wiring at bootstrap, never from a command
   field, metadata, tenant, actor, or CLI flag.
3. **Cutover**, only when the import has authority intent. It is ONE transaction:
   take the framework's advisory lock, **validate before any mutation**, flip the
   release's authority, mirror `attack_technique.active`, then move each bound
   document's pointer through `activate_version()`. Authority and retrieval
   therefore switch together or not at all.

### `AttackReleaseProjection` — the release → version binding

| Field | Notes |
|---|---|
| `id`, `framework`, `source_release`, `technique_id` | Unique on `(framework, source_release, technique_id)` — the natural key, and the conflict target a retried stage converges on |
| `document_id`, `document_version_id` | The exact immutable version this release staged |
| `content_hash` | The staged version's hash, checked against its canonical row's at cutover |
| `created_at` | |

The row exists because two facts that look like one are genuinely two: *this
release carries this technique's content*, and *the immutable version row this
release projects is V*. Two releases may carry byte-identical technique content
and therefore share a single `KnowledgeDocumentVersion` — reuse is correct, the
content is the same — and then the version row alone cannot record which release
staged it.

`source_version` on the version cannot serve as the binding. It records which
release happened to **create** the row, so on a shared version it names one release
and silently misattributes the other. Re-deriving the binding from content hashes
fails for the same reason — it yields the set of releases whose content matches,
not which projection a release actually staged — and the row ids are random, so no
derivation can reconstruct the one a caller observed. The binding is written once,
at stage time, and never re-derived; that is what makes re-activating an older
release restore the exact version it staged.

The binding is **provenance, not authority**. Which release is authoritative is
`attack_release.status`; this row says only which version a release's projection
*is*, which is what the cutover moves the document pointers to.

### Why the cutover did not weaken `activate_version`

`KnowledgeDocument.activate_version()` keeps its forward-only contract for
ordinary knowledge. Re-ingesting historical content still never rolls a document's
active pointer backwards — that rule lives in `_converge_on_existing`, unchanged —
and a staged ingest never moves the pointer in either direction. The cutover is a
different act with its own explicit semantics, which is why the generic rule was
left alone rather than relaxed to accommodate it.

## 3. Normalization and hashing

```python
content_hash = SHA-256(normalize_knowledge_content(raw).encode("utf-8"))
```

`normalize_knowledge_content` does exactly five things, and no more:

1. drops a leading BOM (an editor artefact, not content);
2. collapses `\r\n` and bare `\r` to `\n`;
3. applies Unicode NFC;
4. trims trailing whitespace on every line;
5. removes meaningless trailing blank lines and ends with exactly one `\n`
   (empty input normalizes to the empty string).

It must **not** lowercase, strip punctuation, or stem. `T1110`, `sshd`,
`authentication_failure` and CVE-shaped tokens are security identifiers; changing
them changes what the document means.

**Consequence:** the same document ingested from a Windows checkout (`CRLF`) and
a Linux checkout (`LF`) produces the same `content_hash` and therefore does not
create a second version. This is asserted directly by
`tests/unit/knowledge/test_domain_knowledge.py`.

## 4. Enums (frozen)

`SourceKind` = `MITRE_ATTACK`, `CURATED_GUIDANCE`, `TENANT_RUNBOOK`
`Visibility` = `GLOBAL`, `TENANT`
`DocumentStatus` = `ACTIVE`, `RETIRED`
`EmbeddingProfileStatus` = `ACTIVE`, `RETIRED`
`DistanceMetric` = `COSINE` (only value in P3-A)

There is deliberately **no** `PUBLIC`/`PRIVATE`/`ORG`/`GROUP`/`USER`/
`CONFIDENTIAL`. Those are an authorization model, and knowledge carries none.
`Visibility` is a **scope** — whose retrieval may read the row — not a permission.

## 5. Lifecycle

```
            create()                     retire()
  (none) ──────────────► ACTIVE ──────────────────► RETIRED
                           │  ▲
       activate_version()  │  │  (no path)
                           ▼  │
                    active_version_id moves forward only
```

- `ACTIVE → RETIRED` is legal, once. `RETIRED → ACTIVE` is **not supported** and
  has no method. `retire()` on an already-retired document raises
  `KnowledgeDocumentStateError`; the ingestion use case catches that state and
  reports `already_retired=True` instead, so a repeated retirement is idempotent
  at the use-case boundary without weakening the aggregate.
- `activate_version()` moves `active_version_id` forward and is rejected on a
  RETIRED document. Re-activating the already-active version is a legal no-op for
  idempotent replay — but it still emits the audit event, so the ledger records
  that the version was re-confirmed.
- "Forward only" is a rule of the ordinary ingestion path, not a property of the
  aggregate method. The one deliberate exception is the ATT&CK **cutover**, which
  points a bound document at the version its release staged even when that version
  is older than the one currently served — restoring an earlier release means
  restoring its projection, and §2 gives that act its own explicit semantics.
- The activation is always written in the **same transaction** that persists the
  version and its chunks, so a pointer can never reference a version whose
  retrieval projection is missing.

## 6. Chunking configuration (frozen)

Chunking is a *retrieval projection*, not domain truth. The configuration is a
domain value object because its identity travels with every chunk:

```python
ChunkerProfile(
    chunker_version="structure-aware-v1",
    target_tokens=600,
    max_tokens=800,      # hard ceiling 800
    overlap_tokens=80,   # must be < target_tokens
)
```

The chunker *algorithm* lives in `infrastructure/knowledge/chunker.py` and is
reached through `application/ports/chunking.py`. The domain owns only the
configuration and its invariants. Changing the algorithm without changing
`chunker_version` would silently rewrite the retrieval projection of content that
did not change, so `chunker_version` is persisted with every chunk and echoed by
the retrieval profile.

Chunk ordinal and content hash are deterministic: the same normalized content
plus the same profile yields the same count, the same ordinals, and the same
per-chunk hashes, independent of file path and line endings.

A chunker change produces a **new generation** for the same
`document_version_id` rather than a rewrite. `CHUNK_GENERATION_INITIAL = 1`;
normal retrieval selects only the highest generation present for a version, and
the older generation is left on disk untouched — which is what keeps a citation
captured before the rechunk resolvable. **No P3-A path deletes a historical
generation.** There is deliberately no "rechunk destructively" operation to
provide one.

## 7. Events

Three append-only audit facts, recorded inside the same transaction as the rows:

| `event_type` | Emitted by | Payload |
|---|---|---|
| `knowledge_document_created` | `KnowledgeDocument.create()` | `source_kind`, `external_key`, `visibility` |
| `knowledge_document_version_ingested` | `activate_version()` | `document_version_id`, `version`, `content_hash` |
| `knowledge_document_retired` | `retire()` | *(empty)* |

These are audit facts, **not** asynchronous side-effect triggers. **No outbox
destination is wired for them.** They exist so the ledger can never disagree with
the state, not so something downstream can react.

## 8. Errors

All derive from `KnowledgeError` (itself a `DomainError`) and carry a stable
machine `code`. Messages are operator-facing and never contain secrets or raw
document bodies.

| Class | `code` |
|---|---|
| `InvalidKnowledgeDocumentError` | `INVALID_KNOWLEDGE_DOCUMENT` |
| `InvalidKnowledgeScopeError` | `INVALID_KNOWLEDGE_SCOPE` |
| `InvalidKnowledgeVersionError` | `INVALID_KNOWLEDGE_VERSION` |
| `InvalidKnowledgeChunkError` | `INVALID_CONTENT_CHUNK` |
| `KnowledgeDocumentStateError` | *(from `StateTransitionError`)* |
| `InvalidMitreBundleError` | `INVALID_MITRE_BUNDLE` |
| `InvalidContentHashError` | `INVALID_CONTENT_HASH` |
| `InvalidEmbeddingVectorError` | `INVALID_EMBEDDING_VECTOR` |
| `InvalidCitationError` | `INVALID_CITATION` |
| `KnowledgeBoundsExceededError` | `KNOWLEDGE_BOUNDS_EXCEEDED` |

## 9. Import boundary

`domain/knowledge` imports **only** the standard library — `hashlib`, `re`,
`unicodedata`, `dataclasses`, `datetime`, `enum`, `uuid`, `typing` — plus
`domain/shared`. No SQLAlchemy, pgvector, FastAPI, Pydantic, OpenAI, HTTPX, or
LangGraph. This is asserted by `tests/architecture/test_knowledge_boundary.py`,
which parses every file with `ast` rather than grepping.

The same file asserts the reverse direction: no file under `application/` or
`domain/knowledge/` names `AsyncSession`, `text(`, `select(`, `session.execute`,
`cosine_distance`, or the `<=>` operator. Raw SQL and vector queries exist only
in `infrastructure/persistence/repositories/knowledge.py`.

## 10. What knowledge is not

Restating the invariant because it is the thing most likely to erode:

- `KnowledgeDocumentVersion` is **versioned knowledge truth**.
- `KnowledgeContentChunk` is **immutable content identity** — the citation target.
- `KnowledgeChunkEmbedding` is a **rebuildable retrieval projection**.
- An embedding is a **rebuildable index**.
- An `AttackRelease` is the **authoritative pinned snapshot** of an external
  corpus, and its authority extends no further than "this is the release the
  canonical rows came from".
- An `AttackReleaseProjection` is a **release → version binding** — provenance,
  not authority. It records which immutable version a release staged and confers
  nothing: it cannot activate, approve, or be read as a claim about which release
  is authoritative.
- A retrieval score is a **ranking signal**.
- A citation is a **validated reference**.

None of these is business authority. Knowledge cannot authorize, approve,
execute, change a tenant, change a policy, create a Verdict, or call SOAR.
`is_visible_to()` decides whose *retrieval* may read a row; it grants nothing
else.
