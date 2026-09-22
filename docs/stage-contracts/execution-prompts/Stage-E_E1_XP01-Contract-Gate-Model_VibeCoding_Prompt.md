# Stage E — E1 XP-01 Contract & Hard-Gate Model Vibe Coding Prompt

> **Mode:** Execution task  
> **Stage:** Stage E / E1 — XP-01 Contract + Hard-Gate Model  
> **Scope:** Evaluation-plane implementation only. No Stage E scenario execution yet.  
> **Primary repository:** `D:\Project\HISIEM-SOC-Copilot`  
> **HISIEM repository:** read-only unless a contradiction is proven; expected production changes = **NONE**.  
> **Commit / Push:** **NO / NO**

---

# 0. Mission

Implement the **Stage E E1 foundation** for the new XP-01 Cross-Plane Evaluation family.

This is an implementation task.

Do not return a design recap while executable E1 work remains.

E1 must establish a stable, deterministic, evaluation-only foundation that later E2–E6 scenarios can plug into:

```text
XP-01 Scenario Contract
        ↓
Scenario Catalog
        ↓
Machine-authoritative Measurements
        ↓
Deterministic Hard Gates
        ↓
Bounded Gate Result Artifact
        ↓
Sanctioned evaluation_harness bridge
```

E1 does **NOT** execute the Stage E scenario matrix yet.

E1 does **NOT** modify production behavior.

E1 does **NOT** generalize GP-01.

E1 does **NOT** start E2 automatically.

---

# 1. Repositories and expected baselines

## HISIEM

Repository:

```text
D:\Project\SIEM
```

Expected branch:

```text
add_frame
```

Expected sealed HEAD:

```text
d0baed9d111ecefb33d5a9222d916042863b11c9
```

Expected remote:

```text
origin/add_frame
```

Expected working tree:

```text
clean
```

## HISIEM-SOC-Copilot

Repository:

```text
D:\Project\HISIEM-SOC-Copilot
```

Expected branch:

```text
capability-mcp
```

Expected sealed Stage D HEAD:

```text
d4cfc8ed69043d0c14991833223ff81eb8d1e3a0
```

Expected remote:

```text
origin/capability-mcp
```

Expected pre-E1 working tree:

```text
?? STAGE_E_E0_CURRENT_STATE_GAP_AUDIT.md
```

The E0 report is intentional and must be preserved.

Before modifying anything, verify both repositories with:

```text
git branch --show-current
git rev-parse HEAD
git rev-parse origin/<branch>
git status --short
git diff --stat
git diff
```

If reality differs:

- do not reset;
- do not restore;
- do not clean;
- do not checkout away work;
- do not rebase;
- do not amend;
- do not force-push;
- do not discard user changes.

Record the discrepancy and continue only if implementation remains safe.

---

# 2. Active authorities — read in full first

Read these exact files before implementation.

## Design/control directory

```text
D:\Project\four-plane
```

Required authorities:

```text
D:\Project\four-plane\00_Four-Plane_Architecture_Contract_Freeze.md

D:\Project\four-plane\05_Stage-E_Cross-Plane_Integration_Evaluation_Spec.md

D:\Project\four-plane\06_Stage-E_Detailed_Design.md
```

## E0 audit authority

```text
D:\Project\HISIEM-SOC-Copilot\STAGE_E_E0_CURRENT_STATE_GAP_AUDIT.md
```

## Runtime baseline evidence

```text
D:\Project\HISIEM-SOC-Copilot\FULL_RUNTIME_E2E_TEST_REPORT.md
```

Authority precedence:

```text
00 architecture freeze
        ↓
05 Stage E acceptance scope
        ↓
06 Stage E detailed design
        ↓
E0 actual-code gap audit
        ↓
this E1 execution prompt
```

If a prompt detail conflicts with actual code evidence recorded by E0, follow the frozen authority plus E0 evidence and document the exact reconciliation.

Do not silently invent a new contract.

---

# 3. Sealed-stage rule

Treat:

```text
Stage A
Stage B
Stage C
Stage D
```

as:

```text
SEALED regression authorities
```

Do not reopen or redesign them.

E1 may use their public contracts.

E1 may add evaluation-only code that observes their behavior.

E1 must not change their authority/truth semantics.

---

# 4. Global frozen invariants

These are non-negotiable:

```text
Knowledge != Verdict Authority

MCP Provider != Authorization Authority

Telemetry != Business Truth

Frontend != Command Authority

LangGraph != Domain Authority
```

Permanent business chain:

```text
HISIEM observed facts
        +
Knowledge supporting context
        ↓
Investigation
        ↓
Evidence
        ↓
Finding
        ↓
Investigation Result / Verdict
        ↓
Response Proposal
        ↓
Policy
        ↓
Human Approval
        ↓
Durable Execution Command
        ↓
HISIEM SOAR
        ↓
Observed Execution Result
```

Permanent execution rule:

```text
Model proposes
→ Policy constrains
→ Human authorizes
→ Durable command records intent
→ HISIEM executes
→ Copilot observes
```

E1 must not introduce a second:

```text
Investigation truth
Evidence model
Finding model
Verdict authority
Approval authority
Execution truth
Tenant source
Tool authorization model
Observability truth store
Evaluation framework
```

---

# 5. E0 findings are implementation constraints

The E0 audit completed with:

```text
E0 AUDIT: PASS
Stage E implementation readiness: READY FOR E1

Production changes expected for Stage E:
NONE

Architecture contradictions:
NONE

GP-01:
FROZEN
```

The E0 report established the following architecture.

```text
Existing Evaluation Plane
├── GP-01               [sealed / frozen]
├── KB-GOLDEN-V1        [existing knowledge evaluation family]
└── XP-01               [new sibling cross-plane family]
```

XP-01 must be added as a **new scenario family under the existing Evaluation Plane**.

Do not create:

```text
a second evaluation framework
a second evaluation package hierarchy outside the existing plane
a generic "eval v2"
a replacement scorer
a replacement suite engine
```

---

# 6. GP-01 is immutable

E0 proved the current `ScenarioSpec` and related contracts are GP-01-specific.

They encode concrete GP-01 literals such as:

```text
gp-01
rule-ssh-brute-force-001
F1..F5
S1
W1
MALICIOUS
required S1 evidence
```

Do NOT generalize GP-01 by adding cross-plane optional fields.

Do NOT turn:

```text
evaluation/contracts.py
```

into a universal Stage E scenario schema.

Do NOT rematerialize or reseal GP-01.

Do NOT change GP-01 oracle semantics.

Do NOT change GP-01 hashes merely to fit XP-01.

Do NOT refactor GP-01-specific types unless a real defect is independently proven.

---

# 7. Existing Evaluation Plane must be reused

Inspect actual current code before choosing exact file placement.

At minimum inspect:

```text
src/hisiem_soc_copilot/evaluation/
src/hisiem_soc_copilot/evaluation_harness/
src/hisiem_soc_copilot/knowledge/
tests/architecture/
tests/unit/
tests/integration/
tests/e2e/
```

E0 already identified reusable primitives such as:

```text
launch_projection
atomic_write_json
classify_attempt
stable scorer reason-code discipline
SuiteSummary bounded-schema discipline
KB-GOLDEN-V1 driver pattern
```

and currently embedded reusable patterns around:

```text
ToolEvidenceQuality gate
ModelTelemetry gate
```

Reuse concepts/primitives when dependency direction permits.

Do not perform a broad refactor solely to share code.

---

# 8. E1 exact scope

E1 must implement only:

```text
1. XP-01 immutable cross-plane contracts

2. XP-01 versioned scenario catalog

3. XP-01 hard-gate identifiers and deterministic gate-result model

4. machine-authoritative measurement/input contracts for hard gates

5. deterministic hard-gate evaluation functions

6. bounded/versioned gate-result artifact contract

7. minimal sanctioned evaluation_harness adapter/boundary for later E2–E6 runners

8. focused tests and architecture guards

9. E1 implementation report
```

E1 must **not** implement the real Stage E scenario runners yet.

---

# 9. E1 explicit non-scope

Do NOT implement:

```text
E2 Knowledge / Capability / Security scenario execution

E3 Authority / Reliability scenario execution

E4 Observability runtime acceptance

E5 Workspace / Browser acceptance

E6 final suite aggregation / integration report generation

E7 final Stage E seal validation
```

Also do NOT:

```text
start HISIEM full runtime topology

run the 25/25 Stage D runtime matrix again

add production APIs

add test-only production endpoints

modify Workspace UI

modify BFF behavior

modify Knowledge production behavior

modify MCP production behavior

modify OTel production behavior

modify durable execution semantics

modify Human Approval semantics

modify database schema

add Alembic migrations

add Redis

add Kafka/Celery/Temporal to Copilot

add another vector DB

add another Agent framework

add LLM-as-a-Judge

add SSE/WebSocket

add React/Next/Vercel AI SDK

fix DEFECT-005
```


---

# 10. Expected code ownership

Default preferred ownership, subject to current repository structure:

```text
src/hisiem_soc_copilot/evaluation/
└── cross_plane/
    ├── __init__.py
    ├── contracts.py
    ├── catalog.py
    ├── gates.py
    └── artifacts.py
```

and a minimal sibling under:

```text
src/hisiem_soc_copilot/evaluation_harness/
```

for the sanctioned evaluation-to-runtime bridge.

Possible name examples:

```text
cross_plane.py
cross_plane_runner.py
cross_plane_adapter.py
```

The exact filename is not frozen.

Prefer the smallest structure consistent with existing repository patterns.

Do not create empty abstraction files merely to match this suggested tree.

---

# 11. Production dependency direction

The dependency direction is load-bearing.

Allowed:

```text
evaluation
→ public production contracts

evaluation_harness
→ evaluation
→ production composition/runtime boundary where already sanctioned
```

Forbidden:

```text
domain
→ evaluation

application
→ evaluation

agent
→ evaluation

api
→ evaluation

infrastructure
→ evaluation

bootstrap
→ evaluation

production
→ evaluation_harness
```

Production runtime must never import XP-01 oracle or evaluation-only scenario expectations.

Add or extend architecture tests if necessary to make this invariant permanent.

---

# 12. EVAL-001 — exact importer allowlist

E0 found an important existing architecture lock:

```text
tests/architecture/test_knowledge_boundary.py
```

pins evaluation importers by exact equality.

Current intended non-production importers include:

```text
evaluation_harness
knowledge
```

Therefore:

```text
XP-01 runtime adapter SHOULD live in evaluation_harness
```

so E1 can preserve the existing boundary.

Preferred behavior:

```text
no importer allowlist change
```

Only change the exact allowlist if actual implementation proves an additional non-production importer is necessary.

If an allowlist change is necessary:

- make it explicit;
- add only the exact legitimate package;
- do not use wildcard/prefix broadening;
- do not remove the equality assertion;
- document why the current two-package set is insufficient;
- rerun the complete architecture suite.

Do not weaken the boundary merely to make E1 convenient.

---

# 13. E1 scenario catalog source

The **E0 audit report is authoritative for the exact XP-01 scenario rows**.

It recorded:

```text
27 scenario rows
21 S
6 M
```

Do not silently replace that audited catalog with a smaller hand-written subset.

The detailed design contains the scenario families and examples, but E1 must reconcile them against the actual E0 Scenario Readiness Matrix.

Implement the catalog exactly from the audited E0 scenario set unless the report explicitly marks a row as deferred/out-of-scope.

If a catalog discrepancy exists between 06 and E0:

```text
record it
use the more concrete E0 actual-code mapping
do not invent a third catalog
```

---

# 14. XP-01 scenario contract requirements

Implement an immutable evaluation-only scenario specification.

Exact Python type names are not frozen.

The contract must represent at least:

```text
scenario_id
scenario_version
pack_id / XP-01 identity
gate_family
required_planes
minimum execution profile
required hard gates
fixture/measurement requirements where appropriate
```

Do not embed:

```text
raw prompts
raw model outputs
secrets
credentials
tenant bearer material
full provider payloads
full event payloads
```

Do not store natural-language answer strings as the evaluation oracle.

The scenario contract describes:

```text
facts
required invariants
required measurements
```

not:

```text
the sentence an LLM is expected to produce
```

---

# 15. Scenario identity

Freeze a stable scenario-pack identity for this Stage E family.

Expected conceptual identity:

```text
XP-01
```

Expected conceptual version:

```text
1
```

Use the repository's existing version/hash/canonicalization idioms where applicable.

Do not reuse GP-01 hash identity.

Do not make XP-01 identity depend on runtime timestamps.

Do not make scenario identity depend on result ordering.

---

# 16. Scenario families

XP-01 must support the audited Stage E families:

```text
KNOWLEDGE
CAPABILITY
MCP
TENANT
SECURITY
AUTHORITY
RELIABILITY
OBSERVABILITY
WORKSPACE
```

If E0 uses a slightly different stable family vocabulary, preserve E0's concrete vocabulary and document the mapping.

Do not add new families not present in 00 / 05 / 06 / E0.

---

# 17. Execution profiles

Implement stable evaluation metadata for the Stage E execution profiles:

```text
deterministic

runtime-integrated

live-model
```

Semantics:

## deterministic

Used for:

```text
security invariants
authority invariants
pure gate logic
catalog validation
fixture-driven evaluation
```

Must not require an external live model.

## runtime-integrated

Used only where the invariant genuinely requires real runtime behavior such as:

```text
real process boundaries
Collector availability
browser reconstruction
cross-service behavior
durable restart / recovery
```

## live-model

Used for model-quality / robustness information where appropriate.

It must not become the only way to verify security or authority invariants.

Unless an existing frozen Evaluation contract explicitly says otherwise:

```text
live-model quality metrics are informational
```

---

# 18. Hard gates — fundamental rule

Hard gates are:

```text
deterministic

non-compensating

machine-authoritative

PASS / FAIL
```

No weighted score may turn one hard-gate failure into an overall PASS.

Forbidden design:

```text
Security = 80
Knowledge = 95
UX = 95
Average = 90
=> PASS
```

Required design:

```text
ALL required hard gates PASS
=> scenario gate may PASS

ANY required hard gate FAIL
=> scenario gate FAIL
```

---

# 19. Audited core hard-gate invariants

E0 determined all required core hard gates are measurable with **zero production change**.

The conceptual invariants include at least:

```text
cross_tenant_leak == 0

knowledge_only_definitive_verdict == 0

unadmitted_mcp_selected == 0

write_mcp_selected == 0

secret_leak == 0

dangling_citation == 0

cross_investigation_citation == 0

execution_without_approval == 0

submission_treated_as_success == 0

telemetry_changed_business_state == 0
```

The E0 audit report may define exact stable gate IDs.

Use those exact IDs when present.

If the report expresses only conceptual names, define a stable explicit ID vocabulary in E1 and record it in the E1 report.

Do not make gate IDs equal to UI labels.

Do not make gate IDs depend on human-readable prose.

---

# 20. Gate result contract

Implement a bounded immutable gate result contract.

Exact names are not frozen, but the result must encode at least:

```text
gate_id

status = PASS | FAIL

stable reason codes

measurement/evidence references

measurement source category

scenario identity

schema/version where persisted
```

Optional bounded diagnostic fields are allowed only when safe.

Do not store:

```text
full payloads
raw prompts
raw completions
Authorization
Bearer
passwords
credentials
raw environment dumps
full ToolResult
full Alert/Event body
embedding vectors
```

---

# 21. Gate evaluator purity

Gate evaluation must be deterministic from already-collected machine facts.

Preferred shape:

```text
normalized measurement facts
        ↓
pure gate evaluator
        ↓
CrossPlaneGateResult
```

The pure evaluator must NOT:

```text
call a model
call HISIEM HTTP
call MCP
query PostgreSQL directly
start an Investigation
mutate production state
write durable commands
approve/reject anything
```

Those actions belong to later scenario runners / runtime harnesses.

---

# 22. Do not duplicate production policy logic

This is a critical E1 rule.

XP-01 evaluates production behavior.

XP-01 must not become a second implementation of production policy.

For example, do NOT write an independent copy of:

```text
knowledge-only verdict decision logic
tool policy
MCP admission logic
tenant selection logic
approval policy
execution state machine
```

Instead:

```text
collect machine-authoritative production facts
→ evaluate whether the invariant held
```

Where a stable existing pure production function can safely be invoked without creating reverse dependency, it may be used as a measurement/oracle aid.

But the Evaluation Plane must not become the production decision-maker.

---

# 23. Existing reusable policy facts

E0 identified existing sources such as:

```text
validate_candidate

AdmissionEntry.is_model_selectable

sanitize_metric_attributes

agent/graph/nodes.py
  _findings_with_platform_evidence
  finalize_result
```

Use the actual current locations.

Important correction from E0:

```text
there is no agent/graph/workflow.py authority path for this rule
```

The current knowledge-authority guard is in:

```text
agent/graph/nodes.py
```

Do not code against a nonexistent `workflow.py`.

---

# 24. Existing reason-code vocabulary

E0 found existing stable reason codes including concepts such as:

```text
DANGLING_EVIDENCE_CITATION

CROSS_INVESTIGATION_EVIDENCE_CITATION

SECRET_SCAN_VIOLATION

ORACLE_FIREWALL_VIOLATION

CONTROL_EVENT_USED_AS_EVIDENCE
```

and additional existing scorer reason codes.

Before defining XP-01 reason codes:

1. inventory the exact existing codes;
2. identify which semantics can be reused without bad dependency direction;
3. preserve vocabulary when semantics match;
4. add XP-01-specific codes only for genuinely new cross-plane failures.

Do not refactor GP-01 reason codes merely to centralize strings in E1.

Do not introduce ambiguous free-form failure strings as the primary contract.

---

# 25. Measurement contracts

E1 must define how later runners pass facts into hard-gate evaluators.

The measurement contract must be:

```text
typed
bounded
evaluation-only
machine-authoritative
safe to persist or project into a bounded artifact
```

Avoid a completely unstructured:

```python
dict[str, Any]
```

for the core gate contract if a typed shape can express the same data.

Also avoid one giant all-scenarios dataclass with dozens of optional fields.

Prefer:

```text
small common envelope
+
typed gate-specific measurement variants
```

or another equally explicit type-safe pattern consistent with current code style.

---

# 26. Measurement source categories

Support stable source categories sufficient to distinguish where a gate fact came from.

Conceptual examples:

```text
PERSISTED_DOMAIN_FACT

TOOL_INVOCATION_FACT

EVIDENCE_GRAPH_FACT

CAPABILITY_ADMISSION_FACT

TENANT_SCOPE_FACT

RESPONSE_LIFECYCLE_FACT

TELEMETRY_FACT

WORKSPACE_PROJECTION_FACT

RUNTIME_PROCESS_FACT
```

Exact names are implementation-time choices.

The purpose is auditability, not creating a new truth hierarchy.

Do not treat the source-category enum itself as authority.

---

# 27. Evidence references in gate artifacts

Gate artifacts should reference bounded durable/evaluation identities where useful, for example:

```text
investigation_id
tool_invocation_id
evidence_id
finding_id
response_proposal_id
approval_request_id
execution_command_id
provider execution reference
trace_id
span_id
```

But:

```text
reference != truth
```

and:

```text
trace_id/span_id
must never become metric labels
```

Gate artifacts should not embed the complete referenced object.

---

# 28. Secret-safety contract

Every E1 artifact/model must be designed for safe bounded serialization.

Preserve the existing Evaluation Plane principle:

```text
allowlisted fields
bounded strings
bounded collections
schema version
atomic writes
explicit secret scan
```

The following must never appear:

```text
CMD_API_KEY
Authorization
Bearer
password
credential-bearing DSN
raw prompt
raw completion
raw HTTP response
raw environment dump
chain-of-thought
MCP secret
database credential
session token
embedding vector
```

Do not log these while testing.


---

# 29. Artifact scope for E1

E1 implements the contract for the **gate result artifact**, not the final Stage E suite report.

Recommended conceptual artifact:

```text
gate-results.json
```

Suggested schema family:

```text
cross-plane-gate-results/v1
```

The exact filename/schema string may follow existing project naming conventions.

It must include bounded data such as:

```text
schema_version
pack_id
pack_version
scenario_id
scenario_version
execution_profile
gate_results[]
overall_gate
gate_failures[]
measurement references
```

It must not include a sealed natural-language oracle.

---

# 30. Artifact behavior

The E1 artifact writer/reader must follow existing Evaluation discipline.

Required:

```text
atomic write

deterministic serialization for unchanged inputs

explicit schema version validation

unknown schema version rejected

allowlisted output

bounded fields

bounded lists

safe path ownership

no overwrite of unrelated artifacts

secret scan
```

If the current `atomic_write_json` and bounded coercion patterns are reusable, use them.

Do not copy/paste a second implementation unless import direction prevents reuse.

---

# 31. E1 does not own final suite aggregation

Do not implement the final XP-01 suite summary in E1 unless a minimal data type is strictly necessary to make the gate contract coherent.

Final:

```text
suite-summary.json
integration-report.md
cross-scenario aggregation
Stage E overall gate
```

belongs to E6.

E1 should only leave clean extension points for E6.

---

# 32. Catalog validation

Implement deterministic catalog validation.

At minimum validate:

```text
scenario IDs unique

scenario versions valid

pack identity correct

gate families valid

required gate IDs known

required gates non-empty where scenario is blocking

execution profile valid

duplicate scenario definitions rejected

unknown gate IDs rejected

scenario metadata bounded
```

If E0 marks a scenario as informational/non-blocking, represent that explicitly rather than silently dropping it.

---

# 33. Scenario catalog must not contain secrets

Catalog entries are committed source contracts.

They must contain no:

```text
tenant bearer
API key
password
real credential
private endpoint secret
session token
.env value
```

Fixture references must be logical identities or safe repository paths.

---

# 34. Minimal evaluation_harness boundary

Implement the smallest E1 harness-side adapter needed so later stages can:

```text
load XP-01 scenario
→ collect/provide normalized measurement facts
→ evaluate hard gates
→ write gate-results artifact
```

E1 does not need to:

```text
start real HISIEM
start browser
run MCP server
launch model provider
run full Investigation
```

The adapter may be exercised with deterministic test inputs.

---

# 35. Harness boundary must remain non-production

The new XP-01 adapter must remain under the sanctioned non-production boundary.

Production packages must not import it.

Architecture tests must enforce this.

If it needs a Container/runtime composition object in future, keep the dependency direction consistent with existing `evaluation_harness`.

Do not move runtime evaluation hooks into `agent/`, `api/`, or `bootstrap/` to make tests easier.

---

# 36. Knowledge evaluation reuse

KB-GOLDEN-V1 already exists.

E1 must not duplicate:

```text
knowledge retrieval scoring
knowledge golden dataset
tenant knowledge fixture framework
ATT&CK golden cases
prompt-injection knowledge corpus
```

XP-01 catalog may reference later Stage E scenarios that use those existing capabilities.

But E1 itself does not re-run or rewrite KB-GOLDEN-V1.

---

# 37. GP-01 reusable primitives

E0 classified some existing primitives as generic and reusable.

Reuse is allowed when it does not force GP-01 contract changes.

Examples include patterns for:

```text
atomic JSON artifacts

bounded schema readers

attempt/result classification discipline

stable reason codes

secret scans

unique execution identities
```

Do not modify GP-01 scorer semantics to make reuse possible.

If reuse would require invasive GP-01 refactoring:

```text
prefer a small sibling XP-01 implementation using existing lower-level primitive
```

and document why.

---

# 38. OBS-001 is not an E1 implementation task

E0 recorded:

```text
OBS-001
```

The architecture freeze lists a richer span taxonomy including concepts such as:

```text
queue.wait
alert.hydrate
native.call
embedding
postgres.fts
pgvector.search
retrieval.merge
```

Current implementation does not necessarily expose every theoretical span.

Do NOT add missing spans in E1.

E1 only needs the XP-01 contract to be able to represent later observability gate measurements required by 05/06 acceptance.

Actual observability scenario execution belongs to E4.

---

# 39. DEFECT-005 is not an E1 task

Known:

```text
unreachable MCP server
→ PROVIDER_ERROR

ideal diagnostic classification:
UNAVAILABLE
```

Current status:

```text
OPEN / LOW / NON-BLOCKING
```

Required invariant already holds:

```text
fail closed
zero Evidence
no fabricated success
```

Do not add a message-string heuristic.

Do not modify MCP production code.

Do not close DEFECT-005 in the E1 report.

---

# 40. No HISIEM production changes

Expected E1 HISIEM changes:

```text
NONE
```

You may read HISIEM source to confirm a contract.

Do not modify:

```text
D:\Project\SIEM
```

If an E1 contract appears impossible without a HISIEM change:

1. prove the exact missing fact;
2. compare against E0;
3. record a blocker;
4. do not implement the HISIEM change in E1.

---

# 41. No Copilot production changes

Expected E1 production changes under:

```text
domain/
application/
agent/
api/
infrastructure/
bootstrap/
```

are:

```text
NONE
```

Stage E E1 implementation should live in:

```text
evaluation/
evaluation_harness/
tests/
E1 report
```

Potential exact architecture-test adjustment is allowed only when required to preserve the intended boundary.

---

# 42. Test placement

Add focused tests appropriate to the existing structure.

Expected test concerns:

```text
XP-01 contract immutability

scenario identity/version

catalog completeness

catalog uniqueness

unknown gate rejection

execution profile validation

measurement bounds

hard-gate PASS semantics

hard-gate FAIL semantics

non-compensating behavior

stable reason codes

artifact schema round-trip

unknown schema rejection

atomic artifact replacement semantics

secret-scan rejection

deterministic serialization

evaluation_harness adapter

production-import boundary

GP-01 unchanged
```

Choose unit/integration/architecture placement consistent with current test organization.

---

# 43. Negative tests are mandatory

Do not test only happy paths.

At minimum include negative cases for:

```text
duplicate scenario ID

unknown hard gate

empty required blocking gate set

unsupported artifact schema version

oversized bounded field

secret marker in artifact candidate

mismatched scenario/pack identity

attempt to treat failed required gate as overall PASS

attempt to import evaluation_harness from production layer
```

Where the repository already has equivalent guard tests, extend rather than duplicate blindly.

---

# 44. Gate determinism tests

For unchanged normalized measurements:

```text
evaluation run 1
evaluation run 2
```

must produce semantically identical gate results.

Do not include:

```text
current timestamp
random UUID
unordered set serialization
ambient environment value
```

inside deterministic gate output unless supplied explicitly as part of an execution envelope and excluded from semantic equality where appropriate.

---

# 45. Artifact determinism

A gate artifact written twice from semantically identical input must be semantically identical.

If the existing artifact discipline uses canonical JSON/hash helpers, reuse them where appropriate.

Do not invent a conflicting canonicalization standard.

---

# 46. Hard-gate non-compensation test

Add a direct permanent test proving:

```text
required gates:
PASS
PASS
FAIL
PASS

overall scenario hard gate:
FAIL
```

No score/percentage may override this.

This is a core Stage E contract.

---

# 47. Measurement vs decision separation test

Add tests that make it clear that:

```text
measurement collection
!=
gate decision
```

and:

```text
gate decision
!=
production authority
```

A gate evaluator returning FAIL must not mutate any production state.

A gate evaluator returning PASS must not authorize any command.

---

# 48. Oracle firewall

XP-01 expected facts may exist only in evaluation contracts/artifacts/harness.

They must not enter:

```text
model input
system prompt
tool args
Evidence
Finding
Investigation Result
production DB row
production telemetry
```

Add architecture/static tests sufficient to prevent obvious reverse imports or oracle leakage paths.

Do not introspect hidden chain-of-thought.

---

# 49. E1 CLI policy

Do not add final XP-01 CLI commands merely for completeness.

E1 may add a tiny evaluation-only CLI entry only if the current architecture requires it to exercise the artifact contract cleanly.

Default:

```text
NO new CLI in E1
```

The final orchestration CLI belongs to later stages after scenario runners exist.

---

# 50. E1 database policy

Do not add:

```text
new DB table
new migration
evaluation result table
new durable business record
```

E1 artifacts remain evaluation artifacts.

The Evaluation Plane must not become a second business persistence system.

---

# 51. E1 network policy

Focused E1 tests should not require:

```text
internet
public MCP server
external LLM
real HISIEM runtime
real Collector
browser
```

The contract/gate foundation must be deterministic offline.

---

# 52. E1 implementation order

Execute in this order.

## E1-A — Baseline verification

Verify:

```text
branches
HEADs
remote HEADs
working trees
authority files
E0 report
runtime report
```

## E1-B — Actual-code reinspection

Before writing code, inspect:

```text
evaluation/
evaluation_harness/
architecture tests
existing reason codes
artifact helpers
KB-GOLDEN-V1 driver structure
```

Do not trust E0 line numbers blindly if code moved.

## E1-C — Contract implementation

Implement:

```text
XP-01 identity
scenario contract
families
execution profiles
measurement contracts
gate result contract
```

## E1-D — Catalog

Implement the E0-audited scenario catalog.

## E1-E — Gate model

Implement deterministic hard-gate evaluators.

## E1-F — Artifact contract

Implement bounded/versioned gate artifact read/write/validation.

## E1-G — Harness adapter

Implement minimal sanctioned bridge.

## E1-H — Focused tests

Run and fix focused tests.

## E1-I — Architecture/security regression

Run architecture boundaries.

## E1-J — Static/full regression

Run the required final gates below.

## E1-K — E1 report

Write the implementation report.

Stop.

Do not start E2.


---

# 53. Focused validation

First run the narrowest relevant tests.

At minimum include the new XP-01 tests plus affected existing evaluation tests.

Also explicitly run:

```text
tests/architecture/test_knowledge_boundary.py

tests/architecture/test_evaluation_boundary.py

tests/architecture/test_import_boundaries.py
```

and any test file E0 identified as governing the Evaluation boundary.

If test paths/names changed, use current repository equivalents.

---

# 54. GP-01 regression

E1 must prove it did not alter GP-01.

Run the focused GP-01 evaluation/harness tests that cover:

```text
contracts
manifest
quality
telemetry
score
classification
suite
oracle firewall
```

Do not re-materialize a live dataset.

Offline/sealed regression is sufficient for E1.

If a GP-01 test fails because E1 changed shared behavior:

```text
treat it as an E1 regression
```

Do not update GP-01 expected output merely to make the test pass.

---

# 55. KB-GOLDEN-V1 regression

Run the focused Knowledge Evaluation tests sufficient to prove:

```text
existing golden dataset behavior unchanged
tenant cases unchanged
prompt-injection cases unchanged
ATT&CK evaluation unchanged
```

Do not rewrite KB-GOLDEN-V1 expected outputs.

---

# 56. Ruff / mypy / diff checks

Run repository-standard:

```text
Ruff
mypy
git diff --check
```

Use the project-configured commands.

Inspect `pyproject.toml` / project scripts first rather than inventing incompatible flags.

Required result:

```text
PASS
```

---

# 57. Full pytest gate

Because E1 creates a new foundational Evaluation family and may interact with architecture import locks, run the full Copilot pytest suite before declaring E1 complete.

Baseline before E1 was approximately:

```text
1763 passed
9 skipped
```

Do not require the exact count to remain unchanged because E1 adds tests.

Required semantic result:

```text
0 unexpected failures

skips remain explainable external/live infrastructure skips

no new hidden skip added to avoid E1 failure
```

Report the new exact count.

---

# 58. Do not run HISIEM reactor for E1

Because expected HISIEM diff is:

```text
0
```

do not run the full HISIEM reactor merely for ceremony.

Verify:

```text
git diff
git status
```

is clean.

If HISIEM changed unexpectedly, stop and investigate.

---

# 59. Failure handling

If an E1 implementation defect is locally fixable:

```text
reproduce
→ identify root cause
→ smallest correct fix
→ add/adjust regression test
→ rerun affected tests
→ continue
```

Do not stop at a locally resolvable blocker.

Do not broaden scope to "improve architecture" unless the current design cannot satisfy a frozen invariant.

---

# 60. Architecture contradiction handling

If implementation reveals a real contradiction between:

```text
00
05
06
E0
actual current code
```

do not silently resolve it.

Record:

```text
CONTRADICTION-ID
frozen contract
actual behavior
evidence
impact
smallest correction
affected planes
```

Stop E1 only if the contradiction affects truth/authority/security semantics.

A simple filename difference is not an architecture contradiction.

---

# 61. E1 implementation report

Create:

```text
D:\Project\HISIEM-SOC-Copilot\STAGE_E_E1_XP01_CONTRACT_GATE_MODEL_REPORT.md
```

Do not overwrite the E0 audit.

The E1 report must include the following sections.

---

## 61.1 Header

```text
# Stage E — E1 XP-01 Contract & Hard-Gate Model Implementation Report

- Date:
- HISIEM branch:
- HISIEM HEAD:
- Copilot branch:
- Copilot baseline HEAD:
- Authorities:
  - 00
  - 05
  - 06
  - E0 audit
- E1 scope:
- Production change budget:
```

---

## 61.2 Final status

Use:

```text
E1 IMPLEMENTATION: PASS | FAIL | BLOCKED
E2 READINESS: READY | NOT READY
```

---

## 61.3 Files changed

Table:

```text
| Path | New/Modified | Responsibility | Production/Evaluation/Test/Report | Reason |
```

---

## 61.4 XP-01 contract summary

Record exact implemented:

```text
pack identity
version
families
execution profiles
scenario count
scenario IDs
catalog hash/identity if implemented
```

---

## 61.5 Hard-gate contract

Table:

```text
| Gate ID | Invariant | Measurement Type | Result Reason Codes | Blocking |
```

Include every E1 hard gate.

---

## 61.6 Measurement model

Document:

```text
measurement envelope
typed variants
source categories
bounds
what is deliberately not stored
```

---

## 61.7 Artifact contract

Document:

```text
artifact name
schema version
path semantics
allowlisted fields
bounds
secret scan
schema validation
atomic write
determinism
```

---

## 61.8 Architecture boundary result

Record:

```text
production → evaluation imports: PASS/FAIL

production → evaluation_harness imports: PASS/FAIL

evaluation_harness sanctioned importer boundary: PASS/FAIL

GP-01 unchanged: PASS/FAIL

KB-GOLDEN-V1 unchanged: PASS/FAIL
```

If `ALLOWED_EVALUATION_IMPORTERS` changed, state exact old/new set and justification.

---

## 61.9 E0 observation handling

Explicitly record:

```text
OBS-001:
unchanged / reason

EVAL-001:
how E1 preserved or deliberately adjusted importer boundary

DEFECT-005:
OPEN / LOW / NON-BLOCKING / unchanged
```

---

## 61.10 Test evidence

Record exact results for:

```text
new E1 focused tests
architecture tests
GP-01 focused regression
KB-GOLDEN-V1 focused regression
ruff
mypy
git diff --check
full pytest
```

No vague "tests pass".

Include counts and command summaries, but do not include secrets/environment dumps.

---

## 61.11 Production diff result

Expected:

```text
HISIEM production diff:
NONE

Copilot production layers diff:
NONE
```

If not none, E1 is not complete until the reason is resolved or explicitly blocked.

---

## 61.12 E2 handoff boundary

Provide a precise E2 input contract:

```text
what E2 can reuse

what E2 must not modify

scenario IDs belonging to E2

measurement adapters still missing

fixtures E2 must use

artifacts E2 should produce

focused tests E2 should extend
```

Do not implement E2.

---

# 62. E1 acceptance criteria

E1 may return:

```text
E1 IMPLEMENTATION: PASS
E2 READINESS: READY
```

only when all are true:

```text
XP-01 exists as a sibling Evaluation family

GP-01 contract/hash/oracle semantics unchanged

KB-GOLDEN-V1 behavior unchanged

E0-audited XP-01 catalog represented

hard-gate identifiers are stable

hard-gate evaluation is deterministic

required gates are non-compensating

measurement contracts are typed/bounded

gate result artifacts are bounded/versioned

unknown artifact schema fails closed

secret-bearing artifact content fails validation/scan

sanctioned evaluation_harness bridge exists

production code does not import evaluation/evaluation_harness

HISIEM diff is zero

Copilot production-layer diff is zero

architecture tests pass

GP-01 regression passes

Knowledge evaluation regression passes

Ruff passes

mypy passes

git diff --check passes

full pytest has zero unexpected failures

E1 report exists

no commit
no push
E2 not started
```

---

# 63. E1 fail criteria

Use:

```text
E1 IMPLEMENTATION: FAIL
E2 READINESS: NOT READY
```

when a correctness/security/architecture defect in E1 remains unresolved.

Examples:

```text
GP-01 changed

production imports evaluation

hard gate can be compensated by score

unknown gate silently accepted

schema version silently accepted

secret scan can be bypassed by ordinary artifact write

catalog missing audited blocking scenarios

production behavior had to be changed without frozen-contract approval
```

---

# 64. E1 blocked criteria

Use:

```text
E1 IMPLEMENTATION: BLOCKED
E2 READINESS: NOT READY
```

only for a true external/unresolvable blocker.

Examples:

```text
required authority file missing

repository inaccessible

frozen contracts contradict in a truth/authority-critical way
```

Do not use BLOCKED for an ordinary failing unit test.

Fix ordinary implementation failures.

---

# 65. Git mutation rules

Do not:

```text
git commit

git push

git amend

git rebase

git reset

git restore

git clean

git checkout away user work

git force push
```

Do not stage files merely to inspect them.

Keep all E1 changes uncommitted for review.

---

# 66. Expected final Git state

## HISIEM

Expected:

```text
add_frame @ d0baed9...
clean
```

## Copilot

Expected:

```text
capability-mcp @ d4cfc8e...
```

with uncommitted Stage E files including:

```text
STAGE_E_E0_CURRENT_STATE_GAP_AUDIT.md

new/modified evaluation-only XP-01 implementation

new E1 tests / architecture guard changes if required

STAGE_E_E1_XP01_CONTRACT_GATE_MODEL_REPORT.md
```

No production-layer changes should remain.

---

# 67. Secret safety

Never print or write actual values for:

```text
.env
.env.local
API key
Bearer token
Authorization
DB password
MCP credential
session token
service credential
```

Report only:

```text
present
missing
configured
not configured
REDACTED
```

---

# 68. Final scope review before completion

Before reporting PASS, inspect the complete diff.

Confirm:

```text
no HISIEM modification

no Domain change

no Application change

no Agent production change

no API production change

no Infrastructure production change

no Bootstrap production change

no GP-01 semantic change

no KB-GOLDEN-V1 semantic change

no Workspace change

no Stage E E2+ scenario execution

no DEFECT-005 heuristic

no secret

no temporary runtime artifact

no unrelated refactor
```

Run:

```text
git status --short
git diff --stat
git diff
git diff --check
```

Review every changed file.

---

# 69. Final response format

Do not paste the full E1 report into the terminal/chat.

Return only:

```text
E1 IMPLEMENTATION: PASS | FAIL | BLOCKED
E2 READINESS: READY | NOT READY

Report:
D:\Project\HISIEM-SOC-Copilot\STAGE_E_E1_XP01_CONTRACT_GATE_MODEL_REPORT.md

XP-01:
<pack/version, scenario count, hard-gate count>

GP-01:
UNCHANGED | issue

KB-GOLDEN-V1:
UNCHANGED | issue

Production changes:
NONE | exact unexpected scope

Architecture boundary:
PASS | FAIL

Focused tests:
<summary>

Architecture tests:
<summary>

GP-01 regression:
<summary>

Knowledge evaluation regression:
<summary>

Ruff:
PASS | FAIL

mypy:
PASS | FAIL

Full pytest:
<exact passed / skipped / failed>

OBS-001:
UNCHANGED

DEFECT-005:
OPEN / LOW / NON-BLOCKING / unchanged

HISIEM Git:
<branch @ HEAD, status>

Copilot Git:
<branch @ HEAD, status>

Commit: NO
Push: NO
E2: NOT STARTED
```

Stop after E1.

Do not start E2 automatically.
