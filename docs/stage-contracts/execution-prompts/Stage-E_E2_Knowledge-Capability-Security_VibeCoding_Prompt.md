# Stage E — E2 Knowledge / Capability / MCP / Tenant / Security Vibe Coding Prompt

> **Mode:** Execution task  
> **Stage:** Stage E / E2  
> **Primary repo:** `D:\Project\HISIEM-SOC-Copilot`  
> **HISIEM repo:** `D:\Project\SIEM` (read-only unless a real contradiction is proven)  
> **Commit / Push:** NO / NO

---

## 0. Mission

Implement the first real XP-01 scenario-execution slice on top of the sealed E1 foundation.

E2 owns only these XP-01 families:

```text
KNOWLEDGE
CAPABILITY
MCP
TENANT
SECURITY
```

Required flow:

```text
existing production behavior / deterministic fixture
→ E2 measurement adapter
→ E1 typed measurement contract
→ E1 deterministic hard gate
→ cross-plane-gate-results/v1
```

This is an implementation task. Do not return a plan first. Do not stop at a locally resolvable implementation/test failure. Do not redesign XP-01. Do not start E3 automatically.

---

## 1. Baseline

### HISIEM

```text
D:\Project\SIEM
branch: add_frame
HEAD: d0baed9d111ecefb33d5a9222d916042863b11c9
origin/add_frame: same
expected working tree: clean
```

### Copilot

```text
D:\Project\HISIEM-SOC-Copilot
branch: capability-mcp
sealed base HEAD: d4cfc8ed69043d0c14991833223ff81eb8d1e3a0
origin/capability-mcp: same
```

The Copilot working tree already contains intentional uncommitted E0/E1 changes. Preserve them.

Known E1 baseline:

```text
E1 IMPLEMENTATION: PASS
E2 READINESS: READY

XP-01 v1
29 scenarios
13 hard gates
3 execution profiles
9 gate families
37 expected fact tokens
21 forbidden fact tokens
artifact schema: cross-plane-gate-results/v1

GP-01: UNCHANGED
KB-GOLDEN-V1: UNCHANGED
Production changes: NONE

Full pytest after E1:
1941 passed / 9 skipped / 0 failed / 0 errors
```

Before editing, verify both repos:

```text
git branch --show-current
git rev-parse HEAD
git rev-parse origin/<branch>
git status --short
git diff --stat
git diff
```

Never reset/restore/clean/rebase/amend/force-push/discard existing work.

---

## 2. Read active authorities in full

Read:

```text
D:\Project\four-plane\00_Four-Plane_Architecture_Contract_Freeze.md
D:\Project\four-plane\05_Stage-E_Cross-Plane_Integration_Evaluation_Spec.md
D:\Project\four-plane\06_Stage-E_Detailed_Design.md
D:\Project\HISIEM-SOC-Copilot\STAGE_E_E0_CURRENT_STATE_GAP_AUDIT.md
D:\Project\HISIEM-SOC-Copilot\STAGE_E_E1_XP01_CONTRACT_GATE_MODEL_REPORT.md
```

Use when needed as runtime evidence:

```text
D:\Project\HISIEM-SOC-Copilot\FULL_RUNTIME_E2E_TEST_REPORT.md
```

Authority order:

```text
00 → 05 → 06 → E0 audit → E1 implemented contract/report → this E2 prompt
```

Also inspect the actual uncommitted E1 source before writing code.

---

## 3. E1 is frozen for E2

Do not casually change:

```text
XP-01 pack identity/version
29-scenario catalog
scenario IDs/semantic identities
13 hard-gate IDs
9 gate families
3 execution profiles
expected/forbidden fact vocabulary
typed measurement contracts
cross-plane-gate-results/v1
non-compensating hard-gate semantics
```

If E2 exposes a real E1 correctness defect: reproduce it, prove it, make the smallest correction, add a regression test, and record it in the E2 report.

Do not reopen CATALOG-001:

```text
XP-01 v1 = 29 scenarios
```

The old 27-row statement was a prose miscount.

---

## 4. Exact E2 scenario ownership

Derive the exact E2 scenario IDs from the implemented E1 catalog.

E2 owns every XP-01 scenario whose family is:

```text
KNOWLEDGE
CAPABILITY
MCP
TENANT
SECURITY
```

At the start of execution, enumerate the exact IDs and counts by family. That list becomes the E2 checklist.

Do NOT implement:

```text
AUTHORITY
RELIABILITY
OBSERVABILITY
WORKSPACE
```

Those belong to E3/E4/E5.

No E2-owned scenario may be silently left unimplemented.

---

## 5. Production change budget

Expected:

```text
HISIEM production changes: NONE

Copilot production-layer changes: NONE
```

Production layers:

```text
domain/
application/
agent/
api/
infrastructure/
bootstrap/
```

Expected E2 ownership:

```text
evaluation/
evaluation_harness/
tests/
E2 report
```

Do not add production APIs, debug endpoints, migrations, new tables, new auth paths, or test-only production bypasses to make evaluation easier.

---

## 6. Frozen architecture

Preserve:

```text
Knowledge != Verdict Authority
MCP Provider != Authorization Authority
Telemetry != Business Truth
Frontend != Command Authority
LangGraph != Domain Authority
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

Do not introduce a second Evidence, tenant, policy, approval, execution, or evaluation system.

---

## 7. Reuse existing systems

Reuse E1:

```text
XP-01 catalog
typed measurements
hard-gate evaluators
gate-results artifact
evaluation_harness bridge
```

Reuse existing Knowledge assets:

```text
KB-GOLDEN-V1
CitationResolver
ATT&CK evaluation fixtures
tenant fixtures
prompt-injection cases
retrieval mode support
```

Reuse existing Capability/MCP assets:

```text
ToolRegistry
ToolPolicy
ToolBudget
ToolExecutor
provider router
MCPToolProvider
admission model
schema fingerprint
protected-argument logic
result bounds
typed failures
EvidenceNormalizer
local/deterministic MCP test support
```

Do not create second versions of any of these.

---

## 8. Execution profile rule

Prefer the minimum valid XP-01 execution profile.

Use `deterministic` for security/tenant/admission/schema/secret/citation/prompt-injection invariants whenever possible.

Use `runtime-integrated` only when the catalog scenario genuinely requires real process/service behavior.

Do not require a live external model for security or tenant hard gates.

No LLM-as-a-Judge.

---

## 9. Knowledge scenarios

For every E2 Knowledge scenario, evaluate the real chain where applicable:

```text
ToolRegistry
→ ToolPolicy
→ ToolBudget
→ ToolExecutor
→ knowledge tool
→ Citation validation
→ ToolResult
→ EvidenceNormalizer
→ Knowledge Evidence
→ Finding / result guard
```

Cover the catalog equivalents of:

```text
Knowledge grounding/citation validity
Knowledge-only definitive-verdict guard
successful empty retrieval != unavailable retrieval
citation invalidation / retired / superseded / hash mismatch fail-closed
prompt injection remains DATA
tenant isolation
ATT&CK exact resolution where required
```

Do not duplicate Stage A internals merely to increase test count.

---

## 10. Knowledge authority guard

The actual guard is in:

```text
agent/graph/nodes.py
```

including the current path around:

```text
_findings_with_platform_evidence
finalize_result
```

Do not use the obsolete/nonexistent `agent/graph/workflow.py` reference.

XP-01 must measure production facts such as:

```text
final disposition
supporting finding IDs
cited Evidence authority/source classes
whether platform Evidence participates
```

Do not reimplement the verdict policy in evaluation code.

---

## 11. Empty success vs Knowledge failure

Mandatory distinction:

```text
successful retrieval with 0 hits
!=
KnowledgeUnavailable / RetrievalUnavailable
```

The adapter must preserve typed failure state.

Forbidden:

```text
catch Exception → [] → treat as successful empty
```

---

## 12. Citation integrity

Reuse exact citation/revalidation semantics.

Measure facts such as:

```text
citation resolves
tenant/scope matches
document/version/chunk identity valid
content identity/hash valid
retired/superseded semantics respected
Finding cites persisted Evidence
same-Investigation relationship when required
```

No fuzzy text/title matching.

---

## 13. ATT&CK

When the E1 catalog requires ATT&CK:

```text
T1059.001 → exact canonical T1059.001
```

Authority remains:

```text
ACTIVE release + projection + content/hash chain
```

Do not replace exact resolution with fuzzy RAG.

Do not duplicate KB-GOLDEN-V1 golden cases unless XP-01 needs a distinct cross-plane assertion.

---

## 14. Capability governance

Prove Knowledge and MCP remain under:

```text
ToolRegistry
→ ToolPolicy
→ ToolBudget
→ ToolExecutor
→ Provider
```

Cover catalog equivalents of:

```text
governed Knowledge tool
admitted read-only MCP tool
unadmitted dynamic MCP tool not selectable
write-capable MCP tool not model-selectable
unknown server fail closed
schema drift fail closed
provider cannot bypass Policy/Budget
```

Use actual production facts/pure validators. Do not reproduce governance logic inside XP-01.

---

## 15. MCP V1 read-only

Frozen rule:

```text
MCP V1 = read-only
```

Write/high-risk actions remain forbidden as model-selectable tools.

Future writes still require:

```text
Response Proposal → Policy → Human Approval → Durable Execution → HISIEM
```

E2 tests admission/selection. Do not add write MCP execution just to test it.

---

## 16. MCP discovery/admission

Prove:

```text
provider discovers capability
!=
capability is model-selectable
```

An unknown/unadmitted dynamic tool must remain unavailable to the model.

Use existing `AdmissionEntry.is_model_selectable` / current canonical production behavior where appropriate.

---

## 17. MCP schema drift and protected arguments

Use the existing external schema fingerprint/identity contract.

Incompatible schema drift must fail closed and produce no false success/Evidence.

The model must not control trusted fields such as:

```text
tenant_id
trusted server_id
endpoint
credential/token
authorization material
```

Measure actual model-visible schemas and trusted binding behavior.

---

## 18. MCP result bounds and failures

Exercise catalog-required bounds:

```text
timeout
max result size
max item count
max text size
nested payload bounds
```

Preserve current typed failures such as:

```text
TIMEOUT
UNAVAILABLE
AUTH_FAILURE
RATE_LIMITED
REMOTE_TOOL_ERROR
PROTOCOL_ERROR
UNSUPPORTED_INTERACTION
SCHEMA_MISMATCH
INVALID_RESULT
RESULT_TOO_LARGE
PROVIDER_ERROR
```

Do not expose raw transport exception prose as the primary contract.

---

## 19. DEFECT-005 stays open

Known:

```text
unreachable MCP server → PROVIDER_ERROR
```

instead of ideal `UNAVAILABLE`.

Status:

```text
OPEN / LOW / NON-BLOCKING
```

Required invariant:

```text
fail closed
zero Evidence
no fabricated success
bounded safe failure
```

Do not add message-string heuristics. Do not modify MCP production code. Do not report DEFECT-005 closed.

---

## 20. Tenant scenarios

Tenant source remains:

```text
Trusted Runtime Context
```

Never model args, prompt content, provider result, or browser body.

Cover catalog equivalents of:

```text
Tenant A cannot retrieve Tenant B Knowledge
Tenant A cannot invoke MCP under Tenant B scope
model cannot spoof tenant
provider result cannot override tenant
tenant/protected args absent from model-visible schema
cross-tenant Evidence/citation leakage == 0
```

Use multi-tenant fixtures where required.

---

## 21. Prompt injection

Cover injection from:

```text
Knowledge content
MCP/provider result
```

Frozen semantic:

```text
injected text remains DATA
```

It must not become:

```text
system instruction
authorization
tenant source
tool admission
approval
command authority
```

Do not inspect hidden chain-of-thought. Measure observable tool selection, trusted args, authority classes, Evidence/provenance, and forbidden-command absence.

---

## 22. Secret safety

Use synthetic sentinel secrets only.

Verify E2 artifacts/projections do not contain real or synthetic forbidden material when the hard gate says it must be excluded.

Forbidden classes include:

```text
Authorization
Bearer
API-key value
password
credential-bearing DSN
MCP credential
session/service token
.env contents
raw environment dump
raw prompt
raw completion
full ToolResult
full sensitive HTTP response
embedding vector
```

Never print real `.env.local` contents.

---

## 23. E2 adapters

Add only the adapters needed by E2 families.

Preferred pattern:

```text
existing production/test object
→ small evaluation_harness adapter
→ E1 typed measurement
```

Avoid a giant adapter with many optional fields.

Keep `production → evaluation/evaluation_harness = 0`.

---

## 24. E2 artifacts

Use E1:

```text
cross-plane-gate-results/v1
```

Do not invent a competing schema.

Each E2 scenario must be able to produce a bounded result containing:

```text
scenario identity
execution profile
measurement refs
required gate results
overall hard gate
stable reason codes
```

Generated test/runtime results belong in temp/ignored evaluation directories, not tracked source directories.

---

## 25. Fact vocabulary

E1 froze:

```text
37 expected fact tokens
21 forbidden fact tokens
```

Use that vocabulary.

Do not introduce ad-hoc strings where a token exists.

If a truly necessary token is missing for an already-frozen scenario, treat that as a narrow E1 contract defect: prove it, minimally correct it, test it, and record it.

---

## 26. Positive and negative coverage

Mandatory negative coverage for catalog equivalents of:

```text
cross-tenant Knowledge denied
cross-tenant MCP spoof denied
model tenant override denied
unadmitted MCP tool not selectable
write MCP tool not selectable
schema drift fails closed
unknown MCP server fails closed
oversized MCP result bounded/fails safely
Knowledge unavailable != empty success
invalid citation fails closed
Knowledge-only definitive verdict blocked
Knowledge prompt injection remains DATA
MCP prompt injection remains DATA
synthetic secret sentinel rejected
forbidden fact causes scenario failure
```

Also prove valid allow paths:

```text
admitted read-only MCP can execute
valid Knowledge retrieval becomes validated Evidence
valid citation revalidates
correct tenant Knowledge accessible
valid MCP result follows EvidenceNormalizer path
governed Knowledge tool passes Registry/Policy/Budget/Executor
expected facts can produce scenario PASS
```

---

## 27. Test/runtime environment

Do not start the whole HISIEM topology by default.

Most E2 work should be:

```text
deterministic + focused integration
```

If persisted Knowledge/Evidence tests need the disposable Copilot integration PostgreSQL, use the existing test convention only.

Protected DBs:

```text
HISIEM: 5432
Copilot operator/primary: 5433
```

Do not mutate them for E2 tests.

Existing disposable integration DB may use port 5434 if that is what current tooling expects.

---

## 28. TEST-INFRA-001

Known non-blocker:

```text
some evaluation integration modules call session_scratch_database_url()
directly and error with ConnectionTimeout when the disposable DB is down,
instead of cleanly skipping through requires_database
```

Status:

```text
TEST-INFRA-001 OPEN / NON-PRODUCTION / NON-BLOCKING
```

Do not absorb this into E2 unless it directly blocks new E2 tests and the smallest fix is test-infrastructure-only.

If the test DB is required, use only the disposable test server. Record this in the report.

---

## 29. Explicit non-scope

Do NOT implement:

```text
AUTHORITY / RELIABILITY scenarios (E3)
Observability runtime scenarios (E4)
Workspace/browser scenarios (E5)
final suite aggregation/reporting (E6)
final Stage E seal (E7)
```

Also do not:

```text
modify frontend
modify BFF
add spans for OBS-001
fix DEFECT-005
generalize GP-01
change KB-GOLDEN-V1 semantics
add DB migrations
add new production endpoint
add second evaluation framework
add LLM-as-a-Judge
```

---

## 30. Architecture guards

Preserve and run:

```text
tests/architecture/test_knowledge_boundary.py
tests/architecture/test_evaluation_boundary.py
tests/architecture/test_import_boundaries.py
the E1 production→evaluation_harness boundary tests
```

Required:

```text
production → evaluation = 0
production → evaluation_harness = 0
ALLOWED_EVALUATION_IMPORTERS = {"evaluation_harness","knowledge"}
```

Do not broaden the allowlist for convenience.

---

## 31. Regression gates

Run:

### E2 focused tests
All new E2 measurement/driver/integration tests.

### E1 foundation
Baseline:

```text
172 passed
```

All E1 XP-01 contract/catalog/gate/artifact/adapter/boundary tests must still pass.

### GP-01
Baseline:

```text
346 focused tests passed
```

No GP-01 semantic change.

### KB-GOLDEN-V1
Baseline:

```text
645 passed / 34 skipped
```

No expected-output rewrite.

### Stage C / MCP
Run focused provider/admission/security/result-bound/failure/Evidence-normalization tests relevant to E2.

### Static
Run project-standard:

```text
ruff
mypy
git diff --check
```

### Full pytest
E1 baseline:

```text
1941 passed / 9 skipped / 0 failed
```

E2 may increase pass count. Required:

```text
0 failures
0 errors
no new skip hiding an E2 defect
```

---

## 32. Implementation order

Execute in this order:

```text
E2-A baseline verification
E2-B extract exact E2 scenario IDs from E1 catalog
E2-C map existing fixtures / pure validators / production facts
E2-D implement missing measurement adapters
E2-E implement E2 scenario drivers
E2-F add positive + negative tests
E2-G verify every E2-owned scenario has an XP-01 execution path
E2-H run focused regressions / architecture / static / full pytest
E2-I inspect complete diff
E2-J write E2 report
STOP
```

Do not start E3.

---

## 33. E2 completion semantics

Every E2-owned catalog scenario must end as:

```text
PASS
```

or, only for a true external dependency:

```text
BLOCKED
```

Do not leave “not implemented”.

A scenario PASS requires:

```text
declared/minimum execution profile used
required expected facts observed
forbidden facts absent
all required E1 hard gates PASS
artifact validation PASS
secret safety PASS where applicable
```

Hard gates remain non-compensating.

---

## 34. E2 report

Create:

```text
D:\Project\HISIEM-SOC-Copilot\STAGE_E_E2_KNOWLEDGE_CAPABILITY_SECURITY_REPORT.md
```

Required sections:

```text
1. Header / baseline / authorities
2. E2 final status
3. Exact E2 scenario inventory
4. Files changed
5. Knowledge coverage
6. Capability/MCP coverage
7. Tenant/Security coverage
8. Measurement adapters
9. Artifact evidence
10. Architecture-boundary evidence
11. Test/regression evidence
12. Production diff
13. Known observations
14. E3 handoff boundary
```

Scenario table:

```text
| Scenario ID | Family | Profile | Measurement Adapter | Gate IDs | Artifact | Result |
```

Known observations must include:

```text
CATALOG-001: RESOLVED — XP-01 remains 29
OBS-001: OPEN / unchanged / not E2
DEFECT-005: OPEN / LOW / NON-BLOCKING / unchanged
TEST-INFRA-001: OPEN / current status
```

---

## 35. Acceptance criteria

Return:

```text
E2 IMPLEMENTATION: PASS
E3 READINESS: READY
```

only if all are true:

```text
all E2-owned scenarios have executable XP-01 paths
all executed required hard gates PASS
Knowledge uses existing production contracts
Capability/MCP use existing governance/provider contracts
Tenant trusted-context boundary holds
prompt injection remains DATA
secret scan passes
write/unadmitted MCP remain non-selectable
schema drift fails closed
Knowledge unavailable != empty success
Knowledge-only verdict guard is measured, not reimplemented
cross-plane-gate-results/v1 retained
XP-01 remains 29 scenarios / 13 hard gates unless a proven E1 defect was minimally fixed
GP-01 unchanged
KB-GOLDEN-V1 unchanged
HISIEM diff = 0
Copilot production-layer diff = 0
architecture tests pass
ruff passes
mypy passes
git diff --check passes
full pytest has 0 failures/errors
E2 report exists
no commit
no push
E3 not started
```

Use FAIL for unresolved correctness/security/architecture defects. Use BLOCKED only for a true external dependency, not ordinary test failures.

---

## 36. Git rules

Do NOT:

```text
commit
push
stage for convenience
amend
rebase
reset
restore
clean
force-push
discard E0/E1 work
```

Before finishing:

```text
git status --short
git diff --stat
git diff
git diff --check
```

Review every changed path and confirm:

```text
no HISIEM change
no Copilot production-layer change
no frontend change
no migration
no GP-01 semantic change
no KB-GOLDEN-V1 semantic change
no E3+ implementation
no DEFECT-005 heuristic
no OBS-001 span work
no real secret
no generated runtime artifact accidentally tracked
```

---

## 37. Final response format

Return only:

```text
E2 IMPLEMENTATION: PASS | FAIL | BLOCKED
E3 READINESS: READY | NOT READY

Report:
D:\Project\HISIEM-SOC-Copilot\STAGE_E_E2_KNOWLEDGE_CAPABILITY_SECURITY_REPORT.md

E2 scenarios:
<total count>
KNOWLEDGE: <count / exact IDs>
CAPABILITY: <count / exact IDs>
MCP: <count / exact IDs>
TENANT: <count / exact IDs>
SECURITY: <count / exact IDs>

XP-01:
29 scenarios / 13 hard gates / contract unchanged

E2 gate result:
<passed>/<executed> required scenarios

Production changes:
NONE | exact unexpected scope

Architecture boundary:
PASS | FAIL

E2 focused tests:
<exact result>

E1 regression:
<exact result>

Stage C/MCP regression:
<exact result>

GP-01 regression:
<exact result>

KB-GOLDEN-V1 regression:
<exact result>

Ruff:
PASS | FAIL

mypy:
PASS | FAIL

Full pytest:
<exact passed / skipped / failed / errors>

CATALOG-001:
RESOLVED / unchanged

OBS-001:
OPEN / unchanged

DEFECT-005:
OPEN / LOW / NON-BLOCKING / unchanged

TEST-INFRA-001:
<status>

HISIEM Git:
<branch @ HEAD, status>

Copilot Git:
<branch @ HEAD, status>

Commit: NO
Push: NO
E3: NOT STARTED
```

Stop after E2.
