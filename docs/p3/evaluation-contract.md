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
kb-golden-v1-k5.json
kb-golden-v1-k5-plumbing-only.json     # over the deterministic test fixture
```

Schema: `knowledge-retrieval-eval/v1`.

```jsonc
{
  "schema_version": "knowledge-retrieval-eval/v1",
  "suite_id": "KB-GOLDEN-V1",
  "corpus_version": "1",
  "retrieval_profile": { "profile_id": "hybrid-v1", "rrf_k": 60, ... },
  "embedding_profile": {
    "available": true,
    "provider": "...", "model_id": "...", "dimension": 1536,
    "evidence": "PLUMBING_ONLY" | "DEPLOYMENT_CONFIGURED" | "NONE",
    "detail": "..."
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

### One honest qualification

Ties are broken by `document_id`, then `document_version_id`, then `ordinal` —
the order the brief specifies. That is a **total order within a database**, so
repeated runs against one deployment are identical, and that has been verified:
three consecutive `--skip-ingest` runs produced byte-identical metrics.

It is not a total order *across* two databases built from the same corpus. A
document's id is a `uuid4` assigned at ingest, so two deployments that ingested
the same bytes in a different order can order two equally-scored chunks
differently. Measured effect: two clean ingests of `KB-GOLDEN-V1` into two
freshly-migrated databases differed in 16 of 66 case/mode rankings, all
permutations **among documents that were already retrieved** — `Recall@5`,
citation resolution, leakage and forbidden counts were identical to the digit;
only the rank-sensitive `MRR` and `nDCG@5` moved (HYBRID `0.722` vs `0.782`).

So: rank-sensitive metrics are reproducible for a deployment, and indicative
rather than exact across deployments. `Recall@5` did not move in any observed
run. This is a property of tie-breaking on random identifiers, not of any
randomness in the retrieval or scoring path — and it is why a quoted baseline
must name the corpus state it was measured against.

## 9. Running it

```bash
python -m hisiem_soc_copilot.knowledge.cli evaluate            # all three modes, k=5
python -m hisiem_soc_copilot.knowledge.cli evaluate --mode HYBRID --mode LEXICAL_ONLY
python -m hisiem_soc_copilot.knowledge.cli evaluate --skip-ingest
python -m hisiem_soc_copilot.knowledge.cli evaluate --embedding-provider deterministic-test-only
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
| Corpus | 18 documents (9 `GLOBAL`, 3 each for `tenant-a`/`tenant-b`/`tenant-c`), 18 versions, 70 labelled chunks |
| Cases | 22 sealed; 21 scored, 1 excluded (the `unanswerable` case) |

```
  LEXICAL_ONLY  recall@5=0.952 mrr=0.952 ndcg=0.924 citation_resolution=1.000 leaks=0 forbidden=0 scored=21
  VECTOR_ONLY   recall@5=0.381 mrr=0.248 ndcg=0.265 citation_resolution=1.000 leaks=0 forbidden=5 scored=21
  HYBRID        recall@5=0.976 mrr=0.782 ndcg=0.828 citation_resolution=1.000 leaks=0 forbidden=1 scored=21
hybrid gate: PASS
```

The database at that moment held exactly the 18 corpus documents — no ATT&CK
release had been imported into it and no other document had been ingested. That
matters more than it looks: the suite scores against the **live** database, so any
ambient document in the same scope competes for the top-5 slots and moves the
rank-sensitive metrics. Importing three unrelated ATT&CK technique documents into
the same database was measured to move `LEXICAL_ONLY` `MRR` from `0.952` to
`0.929` and `HYBRID` `nDCG@5` from `0.828` to `0.766`, with `Recall@5` at `0.952`
for both channels unchanged. A baseline is therefore only meaningful together
with the state of the database it was taken from.

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
