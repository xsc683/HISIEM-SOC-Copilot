# GP-01 Manifest Sealer Contract

## 1. Scope

This document defines E1-B.4, the GP-01 Manifest Sealer contract.

The Sealer converts a verified materialized dataset into an immutable evaluation manifest that can be consumed by the Golden Path evaluation harness and scorer.

```text
VerifiedDataset
      ↓
ManifestBuilder
      ↓
Canonical Manifest Payload
      ↓
ManifestSealer
      ↓
Immutable SealedManifest
```

The Sealer does not ingest logs, resolve HISIEM resources, run the Copilot investigation, invoke an LLM, or determine the investigation result.

E1-B.3 is the only stage responsible for proving that provider resources exist and satisfy the GP-01 materialization invariants.

## 2. Input authority

The Sealer MUST accept only a `VerifiedDataset` produced by the Dataset Materializer verification boundary.

It MUST reject:

- an unverified `MaterializationDraft`;
- partially resolved provider resources;
- an unstable source alert;
- an ambiguous source alert;
- locally inferred provider identifiers;
- a dataset whose required scenario invariants failed.

The API SHOULD make invalid construction difficult by separating types:

```text
MaterializationDraft
        ↓ verify
VerifiedDataset
        ↓ seal
SealedManifest
```

`seal(unverified_dataset)` is not a supported operation.

## 3. Sealer purity boundary

`ManifestSealer` MUST NOT perform provider or model I/O.

It MUST NOT:

- call HISIEM;
- call the ModelProvider;
- run LangGraph;
- create or update Copilot domain state;
- query Elasticsearch directly;
- execute detection logic;
- mutate the materialized dataset.

The expected split is:

```text
MaterializationVerifier
        ↓
VerifiedDataset
        ↓
ManifestBuilder
        ↓
CanonicalManifest
        ↓
ManifestSealer
        ↓
filesystem persistence
```

`ManifestBuilder` and `ManifestSealer` SHOULD be deterministic for the same explicit input values.

## 4. Manifest purpose

The sealed manifest has three responsibilities:

1. identify the exact real HISIEM resources used by one GP-01 evaluation run;
2. carry the private evaluation oracle required by the scorer;
3. cryptographically detect mutation of the evaluation record after sealing.

It is not an operational Copilot payload and MUST NOT be treated as investigation context.

## 5. Manifest schema

The manifest MUST be versioned.

Recommended top-level schema:

```json
{
  "schema_version": "gp-eval-manifest/v1",
  "scenario": {},
  "run": {},
  "scope": {},
  "entities": {},
  "events": [],
  "control_events": [],
  "source_alert": {},
  "oracle": {},
  "code": {},
  "integrity": {}
}
```

A schema version change is required for a non-backward-compatible change to canonical meaning.

## 6. Scenario identity

The manifest MUST identify the exact scenario definition used to materialize the run.

Required fields:

```text
scenario.id
scenario.version
scenario.source_file_sha256
scenario.semantic_sha256
```

`source_file_sha256` is the SHA-256 of the exact committed scenario source bytes.

`semantic_sha256` is the SHA-256 of a canonical parsed `ScenarioSpec` representation.

These hashes have different meanings and MUST NOT be conflated:

- source hash detects byte-level edits;
- semantic hash detects scenario meaning changes independent of irrelevant source formatting.

## 7. Run identity

The manifest MUST record:

```text
run.run_id
run.materialized_at
run.sealed_at
```

Timestamps MUST use one canonical RFC 3339 UTC representation before hashing.

The manifest MAY contain additional bounded runtime metadata necessary to reproduce or diagnose an evaluation, but MUST NOT contain credentials or process environment dumps.

## 8. Scope and entities

The manifest MUST preserve the evaluation scope used to resolve provider resources.

At minimum:

```text
scope.provider = hisiem
scope.tenant_id
```

The GP-01 entity block SHOULD include the normalized attack entity used for semantic scoring:

```text
entities.source_ip
entities.user_name
entities.host_name
```

These values are evaluation facts, not authorization claims. They MUST NOT be used to establish tenant or actor authority inside the Copilot runtime.

## 9. Event references

Every semantic event stored in the manifest MUST originate from the verified provider dataset.

A semantic event entry SHOULD contain:

```text
role
classification = GROUND_TRUTH
provider_ref.provider
provider_ref.index
provider_ref.document_id
timestamp
event_action
event_outcome?
source_ip
user_name
host_name
payload_sha256
```

Only bounded normalized facts required for evaluation and correlation SHOULD be stored.

The manifest MUST NOT copy complete raw Elasticsearch documents as its normal representation.

Provider references MUST be the exact `_index` and `_id` values resolved from HISIEM. They MUST NOT be regenerated from logical roles or hashes.

## 10. Control event isolation

The watermark/control event MUST be stored separately from semantic ground-truth events.

Example representation:

```json
{
  "role": "W1",
  "classification": "WATERMARK_CONTROL",
  "excluded_from_ground_truth": true,
  "provider_ref": {
    "provider": "hisiem",
    "index": "...",
    "document_id": "..."
  }
}
```

Control events MUST NOT:

- appear in `oracle.required_evidence_roles`;
- satisfy semantic evidence requirements;
- be injected into Copilot prompts or graph state merely because they appear in the manifest;
- be counted as compromise evidence by the scorer.

The scorer MUST understand the distinction between `GROUND_TRUTH` and `WATERMARK_CONTROL`.

## 11. Source alert reference

The manifest MUST bind the exact source alert used to start the investigation.

Required representation:

```text
source_alert.provider = hisiem
source_alert.resource_type = alert
source_alert.address_id
source_alert.business_id?
source_alert.rule_id
source_alert.event_count
```

`source_alert.address_id` MUST be the real identifier accepted by the HISIEM alert detail API.

For the current HISIEM alert implementation this is the Elasticsearch alert document `_id`.

The Sealer MUST NOT derive `address_id` from `alert.id`, scenario ids, hashes, or event ids.

The business alert id may be retained as optional display/correlation metadata only.

## 12. Oracle

The manifest may contain the private GP-01 oracle required for deterministic scoring.

Minimum oracle:

```text
oracle.expected_verdict = MALICIOUS
oracle.facts[]
oracle.required_evidence_roles[]
```

Recommended GP-01 semantic facts:

```text
FAILURE_SEQUENCE
  at least five SSH authentication failures
  same attack source
  same target account
  same target host

POST_FAILURE_SUCCESS
  SSH authentication success exists
  same source
  same account
  same host
  success occurs after the failure sequence
```

The oracle SHOULD describe facts and evidence requirements, not prescribe model wording.

Scoring MUST NOT require an exact generated sentence such as a fixed Finding string.

This keeps GP-01 a grounded investigation evaluation rather than a prompt memorization benchmark.

## 13. Oracle isolation

Oracle isolation is a hard architecture invariant.

The evaluation harness may read the sealed manifest, but the Copilot investigation may receive only the production-safe launch information required to identify the real source resource.

Expected flow:

```text
SealedManifest
      ↓
Evaluation Harness
      ↓ extract only launch ref
ExternalResourceRef(
    provider="hisiem",
    resource_type="alert",
    address_id=<real address id>,
    business_id=<optional>
)
      ↓
Copilot Investigation
```

Forbidden flows:

```text
oracle → ModelProvider
oracle → system/user prompt
oracle → LangGraph state
oracle → ToolResult
oracle → Evidence
oracle → Finding candidate
oracle → InvestigationResult
```

The production application MUST NOT import the evaluation oracle package.

A repository architecture test SHOULD enforce a one-way dependency:

```text
evaluation
    ↓
production public contracts
```

and prohibit:

```text
production domain/application/agent/infrastructure/api
    ↓
evaluation oracle
```

## 14. Evaluation launch view

The harness SHOULD expose a dedicated projection of `SealedManifest` for investigation launch so accidental oracle propagation is structurally difficult.

Equivalent type:

```text
EvaluationLaunchRef
  provider
  resource_type
  address_id
  business_id?
```

The launcher MUST NOT pass the full manifest object to production investigation code.

## 15. Canonicalization

Manifest integrity depends on a project-owned versioned canonical representation.

`gp-eval-manifest/v1` canonicalization MUST define at least:

```text
encoding: UTF-8
object keys: lexical sorted order
separators: compact JSON separators
numbers: finite JSON numbers only
NaN/Infinity: prohibited
timestamps: canonical RFC 3339 UTC
list ordering: deterministic by schema meaning
Unicode: no implementation-dependent re-encoding
```

A suitable Python serialization primitive is equivalent to:

```python
json.dumps(
    payload,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
)
```

List ordering MUST be established before serialization. `sort_keys=True` does not make array order deterministic.

Recommended deterministic ordering:

```text
events           → logical role order F1..F5,S1
control_events   → logical role
oracle.facts     → declared ScenarioSpec order
required roles   → declared ScenarioSpec order
related refs     → stable provider-reference order where semantic order is irrelevant
```

## 16. Integrity hash

The manifest MUST carry a SHA-256 integrity digest.

Hash rule:

```text
manifest_sha256 =
SHA256(
  canonical_json(
    manifest with integrity.manifest_sha256 omitted
  )
)
```

The hash MUST NOT include itself.

The canonicalization identifier MUST be included in the hashed payload, for example:

```text
integrity.canonicalization = json-sort-keys-v1
```

A verifier MUST recompute the digest using the schema-version canonicalization rules and reject a mismatch.

## 17. Code revision

A sealed benchmark record SHOULD identify the code revision under which it was generated.

Recommended fields:

```text
code.git_commit
code.dirty
```

A manifest intended to be an authoritative evaluation record SHOULD require:

```text
code.dirty = false
```

A dirty worktree may produce an explicitly non-authoritative development artifact, but it MUST NOT be labeled equivalent to a clean sealed benchmark record.

## 18. Secret exclusion

The manifest, materialization draft, canonical payload, seal logs, and diagnostics MUST NOT contain:

- `HISIEM_BEARER_TOKEN`;
- `CMD_API_KEY`;
- `Authorization` headers;
- connection secrets;
- raw environment dumps;
- provider request secrets.

The generated local manifest may contain evaluation-scoping values such as tenant id and synthetic entities where required for deterministic evaluation.

Generated evaluation run artifacts SHOULD not be committed to Git.

## 19. Persistence

Recommended generated layout:

```text
.eval-runs/
  gp-01/
    <run_id>/
      materialization.json
      manifest.json
```

`.eval-runs/` SHOULD be Git-ignored.

The committed repository contains the scenario and contract, not generated provider datasets or runtime manifests.

## 20. Atomic sealing

Sealing MUST be atomic at the filesystem boundary.

Required sequence:

```text
build canonical manifest bytes
↓
write temporary file
↓
flush and fsync file
↓
atomic rename/replace into manifest.json when target is absent
↓
optionally fsync containing directory where supported
```

The final sealed file MUST never be observed as a partially written JSON document.

## 21. Immutability and idempotency

After a manifest exists for a run:

```text
existing bytes == newly computed bytes
→ idempotent success

existing bytes != newly computed bytes
→ SEAL_CONFLICT
```

The Sealer MUST NOT silently overwrite a different sealed manifest.

Changing the verified dataset, oracle, scenario identity, source alert reference, code revision, canonicalization version, or any other hashed field requires a new valid seal result and, where it represents a different evaluation execution, a new run identity.

## 22. Verification API

The evaluation package SHOULD provide explicit operations equivalent to:

```text
build_manifest(VerifiedDataset, ScenarioOracle, CodeRevision)
canonicalize_manifest(manifest)
compute_manifest_sha256(manifest)
seal_manifest(manifest, path)
verify_sealed_manifest(path)
```

`verify_sealed_manifest` MUST validate both schema invariants and integrity hash before returning a trusted sealed object.

## 23. Error taxonomy

The implementation SHOULD expose typed failures equivalent to:

```text
ManifestNotVerifiedError
ManifestSchemaError
ManifestCanonicalizationError
ManifestIntegrityError
ManifestSealConflict
ManifestPersistenceError
OracleIsolationViolation
```

These errors SHOULD carry bounded diagnostic context and MUST NOT expose secrets.

## 24. Package boundary

Recommended additions:

```text
src/hisiem_soc_copilot/evaluation/
├── manifest.py
├── sealer.py
├── oracle.py
└── launch_projection.py
```

The evaluation package may call production public interfaces for execution. Production packages MUST remain unaware of the oracle and sealed-manifest representation.

## 25. CLI contract

Suggested commands:

```text
python -m hisiem_soc_copilot.evaluation.cli seal <run_id>
python -m hisiem_soc_copilot.evaluation.cli verify-manifest <run_id>
```

A convenience preparation command may compose B.3 and B.4:

```text
python -m hisiem_soc_copilot.evaluation.cli prepare GP-01
```

Its internal semantics remain:

```text
preflight
→ materialize
→ resolve
→ verify dataset
→ seal manifest
```

Materialization and sealing MUST remain separate internal contracts even when exposed through one convenience command.

## 26. Test contract

Unit tests MUST cover at least:

1. an unverified draft cannot be sealed;
2. the same explicit verified input produces byte-identical canonical payload bytes;
3. manifest tampering changes or invalidates `manifest_sha256`;
4. an existing different manifest cannot be overwritten;
5. `W1` is absent from semantic oracle requirements;
6. all event provider references originate from `VerifiedDataset`;
7. source alert `address_id` is copied from the resolved provider reference and never derived from business id;
8. canonical list ordering is deterministic;
9. NaN/Infinity are rejected;
10. secret fields cannot be serialized through the typed manifest model;
11. the launch projection contains no oracle data;
12. production packages do not import evaluation oracle modules.

A live end-to-end evaluation preparation test MUST prove:

```text
real GP-01 materialization
→ VerifiedDataset
→ sealed manifest
→ integrity verification PASS
→ source alert launch projection contains exact real HISIEM address_id
```

The live preparation test does not by itself score model quality; scoring belongs to the subsequent Golden Path Evaluation stage.

## 27. Completion gate

E1-B.4 is complete only when:

```text
VerifiedDataset
→ canonical versioned manifest
→ immutable atomic persistence
→ SHA-256 integrity verification
→ oracle isolated from production investigation context
→ deterministic launch projection
```

The resulting architecture is:

```text
GP-01 Scenario
      ↓
Real HISIEM Materialized Dataset
      ↓
VerifiedDataset
      ↓
SealedManifest
      ├─ launch projection ─→ Real Copilot Investigation
      └─ private oracle ────→ Evaluation Scorer
```

Only after this boundary is proven may the sealed GP-01 dataset be used as an authoritative Real HISIEM + Real ModelProvider Golden Path evaluation input.
