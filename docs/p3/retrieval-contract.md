# Retrieval Contract

The typed surface of `KnowledgeRetrievalService` and `KnowledgeCitationResolver`,
in `src/hisiem_soc_copilot/application/services/knowledge_retrieval.py`.

Everything in that module is pure and deterministic except the two repository
calls and the embedding call it orchestrates. Ranking is plain functions over
plain data, which is what makes a ranking reproducible and testable without a
database.

> **Not yet reachable by the model.** These services are internal. The catalog
> tools that will eventually call them — `knowledge.retrieve_security_guidance`
> and `knowledge.resolve_attack_technique` — are in `FUTURE_CATALOG_TOOLS` and are
> **NOT YET ACTIVE** (see [security-boundary.md](security-boundary.md) §7).

## 1. Types

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

`KnowledgeHit` carries **no** field that could be read as a control signal. It
has no `instructions`, no `action`, no `severity`, no `authority`. That absence is
asserted against the exact frozen field set, so adding one in a future change
fails a test rather than quietly widening what a retrieval can express.

`chunk_id` names an **immutable content chunk** (`knowledge_content_chunk`) — the
citation target — and never an embedding-projection row. That is what makes the
citation in `citation_id` outlive every rebuild described in §7.

`MAX_RESULT_LIMIT = 5` — a request for more is **rejected**, not clamped.

The ATT&CK authoritative projection has single-writer cutover semantics: an
`MITRE_ATTACK` document's `active_version_id` moves only inside the ATT&CK
cutover transaction, never by ordinary ingestion. Retrieval itself needs no
release awareness -- it reads the pointer, and the cutover guarantees the
pointer -- but that guarantee is what makes serving a MITRE hit the same claim
as serving the authoritative release.

## 2. The scope is mandatory

```python
async def retrieve(
    self, *, tenant_id: str, query: KnowledgeQuery, mode: RetrievalMode = RetrievalMode.HYBRID
) -> KnowledgeSearchResult
```

`tenant_id` is a required keyword with no default. There is **no scope-less
variant**, so no future caller — tool, CLI, or evaluation — can accidentally
search the whole corpus. An empty or whitespace tenant raises
`InvalidKnowledgeQueryError`; it never defaults.

`KnowledgeCitationResolver.resolve(tenant_id=..., citation_id=...)` has the same
shape for the same reason.

## 3. Query normalization (rejection, never repair)

| Bound | Limit |
|---|---|
| `topic` length | 256 chars |
| context terms | 12 |
| one context term | 64 chars |
| derived search terms (backstop) | 96 |

Normalization applies Unicode NFC and collapses whitespace. It does **not**
lowercase, stem, or strip punctuation: security identifiers must survive
byte-identical. Context terms are deduped while preserving first-seen order —
stable ordering matters because both the lexical and the vector query are derived
from the same tuple, so an unstable order would make one question produce two
different searches.

A query that does not fit a bound is refused with `InvalidKnowledgeQueryError`.
It is never quietly reshaped into a different question.

## 4. Retrieval modes

| Mode | Channels | `profile_id` |
|---|---|---|
| `LEXICAL_ONLY` | PostgreSQL FTS | `lexical-v1` |
| `VECTOR_ONLY` | exact pgvector NN | `vector-v1` |
| `HYBRID` | both, fused | `hybrid-v1` |

Production uses `HYBRID`. The single-channel modes exist so quality can be
**attributed** — an evaluation can tell whether hybrid actually added value over
the better baseline instead of asserting it.

### Lexical channel

- PostgreSQL full-text search with the `simple` configuration (no stemming, no
  stopword list — security tokens must not be transformed).
- The query is expanded to a **list of terms** and combined with **OR**, because
  `plainto_tsquery` ANDs its input: requiring one chunk to contain every word of a
  question would suppress exactly the semantic matches hybrid retrieval exists to
  add.
- Each term is an independent bind parameter. No caller-supplied text is ever
  concatenated into tsquery syntax, and there is no path by which a raw tsquery,
  SQL fragment, or operator DSL reaches the database.
- `lexical_document` is a `GENERATED` column
  (`to_tsvector('simple', heading_path || ' ' || content)`), so the index can
  never drift from the content it describes.

### Vector channel

- **Exact** nearest-neighbour over pgvector. There is deliberately **no HNSW and
  no IVFFlat index**: P3-A ranks exactly, and an ANN index would silently change
  which neighbours are found.
- The `embedding` column is the **untyped** `vector` type — no dimension is baked
  into the schema, so a second embedding model of a different size needs no
  migration. The dimension contract is enforced by the active embedding profile.
- The channel only runs when an `ACTIVE` embedding profile exists **and** the
  configured provider's descriptor identity matches it exactly. A mismatch raises
  `KnowledgeRetrievalUnavailableError` rather than embedding the query in a
  different space and producing plausible-looking nonsense.

### Fusion

Reciprocal Rank Fusion, `score(d) = Σ 1 / (60 + rank_i(d))` with 1-based ranks.

RRF is used instead of a weighted sum of raw scores because the two channels
produce numbers on incomparable scales: a `ts_rank` and a cosine distance have no
shared unit, so any mixing coefficient would be a magic number dressed up as a
parameter.

With a single list RRF degenerates to "keep that list's order" (it is strictly
monotone in rank), which is why the single-channel modes need no separate code
path.

## 5. Ranking, tie-breaks, diversification

Candidate budgets per channel: **20 lexical, 20 vector**, fused down to at most 5.

**Tie-break order**: the ONE stable semantic key, shared by the SQL candidate
queries and the Python fusion so the two can never disagree.

```python
stable_ranking_key(view) -> (
    source_kind, visibility, tenant_id or "", external_key,
    document_version_number, chunk_generation, ordinal,
)
```

Ranking ties are broken by **semantic business identity** — what the knowledge
*is* — and never by a database-generated surrogate. A `uuid4` tie-break makes two
databases that ingested the same corpus order two equally-scored chunks
differently, which is precisely what a reproducible baseline cannot tolerate;
`document_id`/`document_version_id` are both random UUIDs, so neither may appear
in the key.

Every component is stable under re-ingestion into a fresh database: the same
document at the same version, chunked by the same chunker, always produces the
same key.

- `visibility`/`tenant_id` are in the key because a `GLOBAL` document and a
  `TENANT` document may share an `external_key` (the two partial unique indexes
  allow exactly that), so without the scope the order would not be **total**.
  `tenant_id` folds to `""` for ordering, which is safe because the fold is only
  applied *within* one visibility value, where the tenant is uniform anyway.
- `ordinal` is unique within `(document_version_id, generation)`, which is what
  makes the key total rather than merely nearly total.

The SQL twin is `_stable_order_columns()`, which orders by the channel's own
score first and then by exactly these columns, using `COLLATE "C"`. The `C`
collation is not decoration: the database's default collation is locale-dependent
and would order `external_key` differently on two machines, whereas `C` (byte
order) is identical to Python's `str` comparison for UTF-8 text — which is what
makes the SQL order and the Python key *provably* the same order. The vector
channel orders by cosine distance and then by the same columns.

**Diversification:** at most `max_chunks_per_document = 2` chunks per document.
The first pass takes candidates in rank order while each document is under its
cap. If that leaves the result set short, a second pass fills the remaining slots
from the deferred candidates in rank order — still deterministic, just less
diversified, so a small corpus returns a full page instead of an artificially
empty one. `truncated` reports whether anything was dropped.

**Excerpts** are collapsed and truncated to `MAX_EXCERPT_CHARS = 480`. Bounded so
a hit is a *pointer* to content, never a way to smuggle a whole document into a
future model context.

### Which generation is retrieved

Normal retrieval selects **only the highest `generation` present for the
version**. A rechunk writes a new generation and leaves the old one intact; the
older generation stops being returned by `retrieve` and keeps resolving through
`resolve`. Nothing in the retrieval path deletes a chunk generation, so a
citation captured before a rechunk is not affected by one.

## 6. The retrieval profile

Every result carries the frozen knobs it was produced under:

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

Recording this is what makes a ranking reproducible: the same corpus snapshot
plus the same profile yields the same order. The defaults **are** the frozen
`hybrid-v1` profile; changing one changes every ranking.

## 7. Citations

Format: `kcit:<content_chunk_uuid>:<content_hash_prefix>`

The UUID is an **immutable content chunk** id. This is the specific change that
makes a citation durable, and it is worth stating as the invariant it is:

> **A historical citation must survive retrieval reindexing.**

The identifier therefore names content identity, never a disposable retrieval
row. Under the previous model the citation named a row that a projection rebuild
dropped and recreated with fresh `uuid4`s, so every citation captured before the
rebuild became permanently unresolvable — silently, because the string still
parsed.

- The chunk UUID is canonical-form (`str(UUID(x)) == x`).
- The hash prefix is 8–16 lowercase hex digits (12 by default).
- Parsing never raises and never repairs. Anything malformed returns `None` and
  fails to resolve.

**A citation is a handle, not authority.** Nothing inside the string is trusted.
`KnowledgeCitationResolver.resolve` re-reads the chunk, joins the document and
version, and then re-checks:

| Check | Failure reason |
|---|---|
| The string parses | `MALFORMED_CITATION` |
| The chunk exists and is readable by this tenant | `CHUNK_NOT_FOUND` |
| Scope is coherent (`GLOBAL` ⟺ no tenant) | *(raised)* |
| `TENANT` document belongs to the calling tenant | `SCOPE_MISMATCH` |
| The stored hash equals the hash **recomputed from the content actually read** | `CONTENT_INTEGRITY_MISMATCH` |
| That stored hash starts with the prefix in the string | `CONTENT_INTEGRITY_MISMATCH` |

Both integrity checks are needed, and they catch different tampering. A stored
hash is not evidence; it is a *claim written beside the content*. The resolver
recomputes `compute_content_hash(actual chunk content)` and requires the
recomputed value to equal the stored full hash **and** the stored hash to carry
the citation's prefix. Editing the text out-of-band breaks the first; editing the
stored hash to match a forged prefix breaks the second. Either way the answer is
`unresolved` with an explicit reason, and no exception escapes to the caller.

The scope is re-validated **even though the repository is already tenant-scoped**:
a resolver must not depend on a single layer being right about visibility.

Unlike normal retrieval, resolution **deliberately still works for RETIRED
documents and historical versions, and for superseded chunk generations**. A
citation captured in a past investigation must remain explainable after the
document it points at has been superseded or withdrawn. Normal `retrieve`
excludes retired documents and non-current generations; `resolve` excludes
neither.

A citation therefore still resolves after each of:

| Event | Why it still resolves |
|---|---|
| Embedding-profile rebuild (same generation) | Citations never name an embedding row |
| Vector re-embedding | Same reason — content identity is untouched |
| Retrieval-projection rebuild | Content chunks are not part of the projection |
| Process restart | Nothing about resolution is in-process state |
| Document retirement | Retirement is a lifecycle transition, not a content change |
| New document version activated | The old version's chunks are untouched |
| Rechunk (new generation) | The old generation is retained and still resolvable |
| Crossing tenants | It does **not** resolve — `SCOPE_MISMATCH` |

The only thing that makes a historical citation unresolvable is genuine deletion
of immutable historical knowledge, and **P3-A offers no destructive delete**. The
one way to arrive there is an operator deleting rows by hand, which is why the
integrity checks above are recomputed rather than read from a stored claim.

Resolution proves **provenance** — this chunk, of this immutable version, with
this hash, exists and is readable by this tenant. It proves nothing about
correctness and grants nothing.

## 8. Failure modes

| Condition | Error |
|---|---|
| Query violates a bound, or tenant is empty | `InvalidKnowledgeQueryError` |
| No ACTIVE embedding profile | `KnowledgeRetrievalUnavailableError` |
| No embedding provider configured | `KnowledgeRetrievalUnavailableError` |
| Provider identity ≠ ACTIVE profile identity | `KnowledgeRetrievalUnavailableError` |
| Provider returns a different profile than it declared | `KnowledgeRetrievalUnavailableError` |
| Embedding dimension ≠ profile dimension | `KnowledgeRetrievalUnavailableError` |
| A document ingest would switch the ACTIVE embedding profile | `EmbeddingProfileSwitchRequiresReindexError` (`EMBEDDING_PROFILE_SWITCH_REQUIRES_CORPUS_REINDEX`) |

The last row is the whole of §4: when an `ACTIVE` profile exists and the
configured provider's identity differs from it, **any** ordinary document ingest
fails explicitly — including one that passes `allow_embedding_profile_switch=True`.
There is no partial switch and no single-document cutover in P3-A. The flag is
retained only so an existing caller receives that diagnosis instead of an
unrecognised-argument error. See
[p3-a-operations.md](p3-a-operations.md) §5.

`LEXICAL_ONLY` never needs an embedding provider or a profile, which is why it
keeps working when the vector channel is unavailable — and why `doctor` reports
`DEGRADED` rather than `NOT_READY` in that state.

## 9. Data-only guarantee

Retrieved content is `DATA_ONLY`. A chunk containing `rm -rf`, a `curl | sh`
pipeline, a PowerShell one-liner, or the sentence *"ignore all previous
instructions"* produces exactly the same kind of string as any other text.
Nothing in the retrieval path interprets, executes, or dispatches on content.
The corpus used to prove this is the sealed `KB-GOLDEN-V1` fixture's
`PROMPT_INJECTION_POISON` category.
