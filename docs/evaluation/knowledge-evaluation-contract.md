# Knowledge Retrieval Evaluation Contract

The `KB-GOLDEN-V1` baseline. Package:
`src/hisiem_soc_copilot/evaluation/knowledge/`. Driver:
`src/hisiem_soc_copilot/knowledge/evaluation.py`.

The purpose is narrow and worth stating plainly: **measure whether hybrid
retrieval earns its place**, and produce a file a reviewer can re-score.

## 1. The boundary

The evaluation package is deliberately self-contained. It imports nothing from
the production layers — `domain`, `application`, `infrastructure`, `bootstrap` —
and receives all retrieval behaviour through two injected async callables:

```python
RetrieveFn = Callable[[str, EvalQuery, str], Awaitable[Sequence[RetrievedHit]]]
ResolveFn  = Callable[[str, str], Awaitable[bool]]
```

Consequences:

- The suite can be run and re-scored **without a database, a model, or a
  network**. The 25 tests in `tests/unit/evaluation/knowledge/` do exactly that.
- The production knowledge domain must not import the evaluation module. The
  driver lives outside both, in `hisiem_soc_copilot.knowledge`, so neither
  direction becomes a dependency.
- `tests/architecture/test_evaluation_boundary.py` enforces that no production
  layer imports `hisiem_soc_copilot.evaluation`. The one legitimate importer is
  `hisiem_soc_copilot.knowledge`, which is a dev/eval surface rather than a
  production layer — the test names it explicitly in an allow-list rather than
  letting it pass by accident.

The packages that build the injected callables are adapters **around the same
Application use cases the operator CLI uses**, never around a parallel path. A
baseline run therefore exercises exactly the code a real ingestion and a real
retrieval run: hashing, chunking, embedding-profile selection, the two-phase
transaction, RRF, diversification, citation resolution.

## 2. Fixture: `KB-GOLDEN-V1`, corpus version `1`

| | |
|---|---|
| Suite id | `KB-GOLDEN-V1` |
| Corpus version | `1` |
| Tenants | `tenant-a`, `tenant-b`, `tenant-c` |
| Documents | **18** |
| Cases | **22** |
| Categories | **9** |
| Labelled chunks | **70** |
| Source kinds | 9 `TENANT_RUNBOOK`, 6 `CURATED_GUIDANCE`, 3 `MITRE_ATTACK` |
| Visibility | 9 `GLOBAL`, 9 `TENANT` |
| Retired by fixture | `guidance-legacy-ssh-hardening` |
| Poisoned documents | `guidance-detection-engineering-review-checklist`, `tenant-a-soar-response-runbook` |

The fixture is **sealed**: `CORPUS` and `CASES` are module constants, not
generated at run time. A change to the corpus is a change to the suite, and the
version must be bumped.

### The nine categories

| Category | Cases | What it tests |
|---|---|---|
| `EXACT_IDENTIFIER` | 4 | A literal token (`T1110`, `sshd`, a CVE) must be found **lexically** |
| `SEMANTIC_GUIDANCE` | 2 | A conceptual question whose wording does not appear in the document — only the vector channel can find it |
| `ATTACK_QUERY` | 3 | ATT&CK technique lookup from imported knowledge |
| `TENANT_RUNBOOK` | 3 | A tenant's own runbook is reachable by that tenant |
| `SCOPE_COMPETITION` | 2 | Identical text exists globally **and** per-tenant; the right one must win |
| `NEAR_MISS_WRONG_DOC` | 2 | A plausible but wrong document must not displace the right one |
| `RETIRED_EXCLUSION` | 2 | A retired document must not appear in normal search |
| `CROSS_TENANT_DENIED` | 2 | Another tenant's document must never be returned |
| `PROMPT_INJECTION_POISON` | 2 | Injected instructions are retrieved as inert DATA |

`EXACT_IDENTIFIER` and `SEMANTIC_GUIDANCE` are the **mixed categories**. They
exist specifically to test that mixing two channels finds what neither finds
alone, and the hybrid gate treats a miss on either as disqualifying — a tolerance
cannot excuse missing the case the hybrid design was built for.

## 3. Modes

| `EvalMode` | Channels |
|---|---|
| `LEXICAL_ONLY` | PostgreSQL FTS only |
| `VECTOR_ONLY` | exact pgvector NN only |
| `HYBRID` | both, fused by RRF |

A mode whose channel cannot run is reported as **unavailable with a reason**, not
as a zero. Only `ModeUnavailableError` — raised by the injected `retrieve` when
the channel itself is unavailable — is read that way. Every other exception
propagates and fails the run loudly: a genuine defect must never be reported as an
unavailable channel.

## 4. Metrics

All computed from the raw rankings by deterministic code. **No LLM judge.**

| Metric | Definition |
|---|---|
| `recall_at_k` | Fraction of relevant documents in the top *k*. `None` when the case declares no relevant documents (a pure exclusion case), which excludes it from the mean rather than scoring it as success or failure. |
| `reciprocal_rank` | `1 / rank` of the first relevant document, `0.0` when none is found. |
| `ndcg_at_k` | Binary-gain nDCG@k over the document-level ranking. `DCG = Σ 1/log2(rank+1)` over relevant documents in the top *k*; `IDCG` is the same for the ideal ranking. `0.0` when `IDCG` is zero. |
| `citation_resolution_rate` | Fraction of returned hits whose citation resolved through the resolver. `0.0` for an empty result set — deliberately not a vacuous `1.0`, because a run that resolved nothing has demonstrated nothing. Reported beside the hit count so a zero from an empty result stays distinguishable from a zero from a broken resolver. |
| `cross_tenant_leakage_count` | Hits owned by neither the querying tenant nor `GLOBAL`. Counted **per hit**, not per document, so a repeated leak is a repeated number. |
| `forbidden_retrieval_count` | Hits on documents the case explicitly forbids (retired, or another tenant's). |
| `claim_mismatches` | Hits the retriever claimed were resolved but the resolver rejected. A claim that stops being true shows up as a number rather than as a silent pass. |

Every metric is aggregated per mode, and per case, in the artifact.

## 5. The hybrid gate

`hybrid_verdict` returns one of three verdicts plus the comparison behind it.

| Verdict | When |
|---|---|
| `NOT_RUN` | Hybrid was not requested, the vector channel was excluded, or the vector/hybrid channel could not run. There is nothing to compare against. |
| `PASS` | HYBRID's mean Recall@k is within `HYBRID_RECALL_TOLERANCE = 0.02` of the **better** single-channel baseline **and** every mixed-category case was retrieved by HYBRID. |
| `FAIL` | Neither of the above, reported with the concrete numbers. |

Two properties matter for review:

- **There is no path that produces `PASS` without a number.** The gate is a
  measurement, not an assertion.
- **`NOT_RUN` is not a failure.** It is the correct, honest verdict for a run
  whose vector channel had nothing real to compare against. The driver exits `1`
  only on `FAIL`, so `NOT_RUN` does not train operators to ignore an exit code.

## 6. The artifact

Written to `.eval-runs/knowledge/` by default (override with `--out`). Filename:

```
kb-golden-v1-k5.json                    # SEALED, deployment provider
kb-golden-v1-k5-plumbing-only.json      # SEALED, deterministic test fixture
kb-golden-v1-k5-open-corpus.json        # NOT SEALED -- see section 8.3
```

Schema: `knowledge-retrieval-eval/v1`.

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

`write_artifact` **refuses to replace an existing file** unless `overwrite=True`.
A baseline is evidence; silently replacing evidence is how two people end up
quoting different numbers for the same suite. The file is written with an explicit
`newline="\n"` so the bytes are identical on Windows and POSIX checkouts.

### What the artifact must never contain

Embedding vectors or any part of one · credentials, API keys, tokens, connection
strings · environment variables or host paths · raw HTTP requests or responses ·
full document bodies (only excerpts ≤ 240 characters).

Anyone holding the file can recompute every number in it. That is the point.

## 7. Evidence quality — `PLUMBING_ONLY` vs `DEPLOYMENT_CONFIGURED`

Section 20 of the brief draws a distinction the artifact enforces structurally.

| `embedding_profile.evidence` | Meaning |
|---|---|
| `DEPLOYMENT_CONFIGURED` | The deployment's own embedding provider produced the vectors. The numbers measure a real retrieval system. The *semantic validity of the model itself* is still outside what this harness can assert. |
| `PLUMBING_ONLY` | The deterministic test fixture produced the vectors. They carry **no semantic meaning**. `VECTOR_ONLY` and `HYBRID` results validate the pipeline — chunking, indexing, ranking, fusion, citation resolution — and say nothing about retrieval quality. |
| `NONE` | No provider was configured; the vector channel did not run. |

A plumbing run is marked in **the filename as well as the JSON**, so the honesty
label survives someone quoting the file without opening it. A plumbing run must
never be presented as a semantic baseline, and this document does not present one.

## 8. Determinism

Given the same corpus snapshot and the same retrieval profile, a run is
reproducible: same rankings, same ordinals, same metrics. There are no sleeps, no
network calls, no randomness, and no clock reads inside the scoring path. The
concrete-knob values that produced a run are recorded on every result via
`RetrievalProfile`.

### 8.1 The stable ranking key

Ties are broken by **semantic business identity**, never by a database surrogate:

```python
stable_ranking_key(view) -> (
    source_kind, visibility, tenant_id or "", external_key,
    document_version_number, chunk_generation, ordinal,
)
```

A random document, version, or chunk UUID is **forbidden** as a ranking
tie-break. Every component of the key is reproducible from the corpus itself, so
two databases that independently ingested the same bytes resolve a tie the same
way — which is what makes the baseline reproducible rather than merely repeatable
on one machine. `visibility`/`tenant_id` are in the key so it stays **total** when
a `GLOBAL` and a `TENANT` document share an `external_key`, and `ordinal` is
unique within `(document_version_id, generation)`.

The SQL candidate queries order by exactly these columns too, via
`_stable_order_columns()` and `COLLATE "C"`, so the database's ordering and the
Python RRF fusion cannot disagree — the vector channel included.

The earlier caveat is therefore retired. It said that rank-sensitive metrics were
reproducible for a deployment but only *indicative* across deployments, because a
tie was broken on a `uuid4`. That is no longer true, and the two-fresh-database
test in `tests/integration/persistence/test_knowledge_persistence.py` asserts the
opposite: identical LEXICAL rankings, identical deterministic-fixture
VECTOR/HYBRID rankings, and identical `Recall`/`MRR`/`nDCG` across two
independently-ingested databases.

### 8.2 The sealed corpus precondition

Before any metric is computed, the run derives the **tenant-visible ACTIVE
eligible corpus** from the database and compares it against the sealed fixture's
expected fact set. A mismatch fails with `CORPUS_PRECONDITION_FAILED`, listing
every difference — unexpected eligible document, missing expected document,
changed content hash, changed version, changed chunk projection — rather than
producing a plausible-looking score.

This matters because a score is the most dangerous possible output here. A
ranking measured over the wrong corpus is not merely useless; it is a number
someone would quote. The list of differences is bounded and the truncation is
stated rather than silent, so a bounded message is never mistaken for a complete
one.

The default is **sealed**. Ambient documents in a competing scope are not
tolerated, they are a precondition failure.

### 8.3 `--allow-ambient-corpus` is a different measurement

An explicit escape hatch exists for the operator who wants "what does this
database return right now". It is not a lesser SEALED run:

- The precondition is skipped entirely.
- The artifact records `corpus_mode: "OPEN_CORPUS"` and `sealed: false`, and
  `expected_corpus_fingerprint` is `null` because the run asserted nothing.
- The artifact filename carries `-open-corpus`.
- **No hybrid gate verdict is produced**, so an open-corpus run can never print
  `KB-GOLDEN-V1 baseline PASS`.

It can say what the database currently returns. It can never say "this is the
KB-GOLDEN-V1 baseline", because nothing about the run establishes that the corpus
was the fixture.

### 8.4 The corpus fingerprint

```python
CORPUS_FINGERPRINT_SCHEMA = "knowledge-corpus-fingerprint/v1"

corpus_fingerprint(facts) -> SHA256(canonical JSON of the SORTED fact set)
```

Each fact is one eligible corpus chunk reduced to reproducible semantic facts:

```
visibility, tenant_id, source_kind, external_key,
document_version_number, document_content_hash,
chunk_generation, ordinal, chunk_content_hash
```

`tenant_id` is `""` for a `GLOBAL` document rather than `None`, so the canonical
form is total and the sort is a real total order. The fact set is **deduped and
sorted** before hashing, so the fingerprint is order-independent by construction —
and a `GLOBAL` document visible to three tenant scopes contributes one fact, not
three.

Deliberately absent, because they would make two identical corpora look
different: **row UUIDs, timestamps, the database host, the embedding vectors** —
and credentials, which must never be written down at all.

Two independent databases that ingested the same fixture produce the same
fingerprint. That is the claim the fingerprint exists to make checkable, and it is
asserted directly.

## 9. Running it

```bash
python -m hisiem_soc_copilot.knowledge.cli evaluate            # all three modes, k=5
python -m hisiem_soc_copilot.knowledge.cli evaluate --mode HYBRID --mode LEXICAL_ONLY
python -m hisiem_soc_copilot.knowledge.cli evaluate --skip-ingest
python -m hisiem_soc_copilot.knowledge.cli evaluate --embedding-provider deterministic-test-only
python -m hisiem_soc_copilot.knowledge.cli evaluate --allow-ambient-corpus   # OPEN_CORPUS
```

`--skip-ingest` re-derives the corpus key map from the database via
`find_by_external_key` rather than trusting an in-memory map, and fails with an
actionable message listing any missing keys. It scores a corpus that is actually
there, or it does not run.

## 10. The recorded baseline

Fifteen sections above describe what the harness *can* do. This one records what
has actually been run, because the two are not the same and the difference is the
point.

| | |
|---|---|
| Runs performed | One, against `127.0.0.1:5434` |
| Embedding evidence | `PLUMBING_ONLY` — the deterministic test fixture, passed explicitly |
| Artifact | `.eval-runs/knowledge/kb-golden-v1-k5-plumbing-only.json` |
| Corpus | 18 documents (9 `GLOBAL`, 3 each for `tenant-a`/`tenant-b`/`tenant-c`), 18 versions, 70 labelled chunks — of which **17 / 17 / 67 are eligible**, because `guidance-legacy-ssh-hardening` is retired |
| Eligible corpus fingerprint | `6eee7f56c8a01f50…` (`SEALED`) |
| Cases | 22 sealed; 21 scored, 1 excluded (the `unanswerable` case) |

```
corpus: SEALED fingerprint=6eee7f56c8a01f50 documents=17 versions=17 chunks=67
  LEXICAL_ONLY  recall@5=0.952 mrr=0.952 ndcg=0.924 citation_resolution=1.000 leaks=0 forbidden=0 scored=21
  VECTOR_ONLY   recall@5=0.381 mrr=0.248 ndcg=0.265 citation_resolution=1.000 leaks=0 forbidden=5 scored=21
  HYBRID        recall@5=0.976 mrr=0.750 ndcg=0.797 citation_resolution=1.000 leaks=0 forbidden=1 scored=21
hybrid gate: PASS
```

`HYBRID` `MRR`/`nDCG@5` are slightly below the pre-closure figures (`0.782` /
`0.828`) because ties are now broken on semantic identity instead of a `uuid4` —
the change that turns "repeatable on this machine" into "reproducible across
databases". `LEXICAL_ONLY` and `VECTOR_ONLY` are unchanged.

Under the closure the run's corpus is no longer a matter of operator discipline:
the sealed precondition (section 8.2) derives the eligible corpus from the
database before scoring and **fails with `CORPUS_PRECONDITION_FAILED`** if an
ambient document has appeared in a competing scope. A baseline is still only
meaningful together with the state of the database it was taken from; the
difference is that a run over the wrong state now refuses to produce a number
instead of producing one that is quietly wrong — and the fingerprint above is how
that state is named.

**REAL EMBEDDING EVALUATION NOT RUN.** No embedding provider is configured in
this repository, so the semantic half of the hybrid gate — section 5's requirement
that HYBRID holds up on genuinely semantic cases — has never been exercised. The
`VECTOR_ONLY` row above is what random vectors *should* score: near-chance recall.
That is a positive signal for the *wiring* (the vector channel is really calling
the provider rather than silently degrading to lexical) and no signal at all for
retrieval quality.

The hybrid gate's `PASS` here therefore means one specific thing: fusion,
ranking, tie-breaking, citation resolution and scoring are wired correctly end to
end, on a corpus where one channel is meaningful and one is noise. Replacing the
fixture with a real provider is what turns this into a quality statement — the
harness itself needs no change to do so.

The plumbing numbers may be quoted as an end-to-end pipeline check. They may not
be quoted as a semantic baseline, and nothing in these documents quotes them as
one.

**Status at closure: `REAL EMBEDDING EVALUATION NOT RUN`.** The semantic-provider
configuration step is deliberately not a mandatory task of this closure, so the
deterministic fixture above remains the only run. It must not be substituted for a
semantic run, and the artifacts it produces stay labelled `PLUMBING_ONLY`.
