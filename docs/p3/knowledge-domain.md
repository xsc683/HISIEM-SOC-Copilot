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

## 2. The two entities

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

`__post_init__` re-normalizes `normalized_content` and rejects it if the result
differs from the input. This is not paranoia: a version whose "normalized"
content is not a fixed point of normalization carries a hash that a later
re-normalization of the same bytes could not reproduce, which silently breaks
provenance. Repairing the content at construction would be a *second* definition
of the hash, so it is rejected instead.

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

- `KnowledgeDocument` is **versioned knowledge truth**.
- `KnowledgeChunk` is a **rebuildable retrieval projection**.
- An embedding is a **rebuildable index**.
- A retrieval score is a **ranking signal**.
- A citation is a **validated reference**.

None of the five is business authority. Knowledge cannot authorize, approve,
execute, change a tenant, change a policy, create a Verdict, or call SOAR.
`is_visible_to()` decides whose *retrieval* may read a row; it grants nothing
else.
