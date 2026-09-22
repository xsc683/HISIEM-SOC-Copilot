# Stage E — E4 Observability Acceptance Vibe Coding Prompt

> **Mode:** Execution task  
> **Stage:** Stage E / E4  
> **Primary repo:** `D:\Project\HISIEM-SOC-Copilot`  
> **HISIEM repo:** `D:\Project\SIEM` (read-only unless a real contradiction is proven)  
> **Commit / Push:** NO / NO

---

## 0. Mission

Implement the XP-01 scenario-execution slice for:

```text
OBSERVABILITY
```

E4 must prove that the existing OpenTelemetry foundation can explain a representative cross-plane lifecycle safely while remaining strictly non-authoritative.

Required path:

```text
existing runtime / telemetry facts
→ E4 observability measurement adapter
→ E1 typed measurement contract
→ E1 deterministic hard gate
→ cross-plane-gate-results/v1
```

This is an execution task. Do not return a plan first. Do not stop at a locally resolvable blocker. Do not redesign XP-01. Do not start E5.

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

The Copilot tree intentionally contains uncommitted E0/E1/E2/E3 work.

Known baseline:

```text
E0 PASS
E1 PASS
E2 PASS — 15/15
E3 PASS — 10/10

XP-01 v1:
29 scenarios
13 hard gates
3 execution profiles
9 gate families
artifact: cross-plane-gate-results/v1

Completed:
25 / 29

GP-01: unchanged
KB-GOLDEN-V1: unchanged
Production changes: NONE

Full pytest after E3:
2105 passed / 9 skipped / 0 failed / 0 errors
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

Preserve all E0/E1/E2/E3 work. Never reset/restore/clean/rebase/amend/discard it.

---

## 2. Read active authorities in full

Read:

```text
D:\Project\four-plane\00_Four-Plane_Architecture_Contract_Freeze.md
D:\Project\four-plane\05_Stage-E_Cross-Plane_Integration_Evaluation_Spec.md
D:\Project\four-plane\06_Stage-E_Detailed_Design.md

D:\Project\HISIEM-SOC-Copilot\STAGE_E_E0_CURRENT_STATE_GAP_AUDIT.md
D:\Project\HISIEM-SOC-Copilot\STAGE_E_E1_XP01_CONTRACT_GATE_MODEL_REPORT.md
D:\Project\HISIEM-SOC-Copilot\STAGE_E_E2_KNOWLEDGE_CAPABILITY_SECURITY_REPORT.md
D:\Project\HISIEM-SOC-Copilot\STAGE_E_E3_AUTHORITY_RELIABILITY_REPORT.md
```

Use when needed:

```text
D:\Project\HISIEM-SOC-Copilot\FULL_RUNTIME_E2E_TEST_REPORT.md
```

Authority order:

```text
00 → 05 → 06 → E0 → E1 → E2 → E3 → this E4 prompt
```

Inspect the actual current uncommitted source before coding.

---

## 3. Freeze prior contracts

Do not casually modify:

```text
XP-01 pack identity/version
29-scenario catalog
scenario IDs
13 hard-gate IDs
9 gate families
3 execution profiles
typed measurement contracts
expected/forbidden fact vocabulary
cross-plane-gate-results/v1
E2 semantics
E3 semantics
```

If E4 exposes a real prior-contract defect: reproduce it, prove it, make the smallest correction, add regression coverage, and document it.

---

## 4. Exact E4 ownership

Derive exact E4 scenario IDs from the implemented E1 catalog.

E4 owns every scenario with family:

```text
OBSERVABILITY
```

At the start, record:

```text
exact IDs
exact count
execution profile
required gates
```

Use that as the E4 checklist.

Do NOT implement WORKSPACE; that is E5.

---

## 5. Core frozen rule

E4 validates:

```text
Telemetry explains runtime.
Telemetry does not authorize, decide, persist, or override business truth.
```

Never allow:

```text
span success == business success
trace existence == approval
exporter success == execution success
collector failure == business failure
```

---

## 6. Representative lifecycle

Validate the representative path actually required by the catalog and current Stage B implementation:

```text
API
→ investigation.run
→ graph.invoke
→ llm.call
→ tool.execute
→ knowledge.retrieve
→ mcp.call
→ investigation.persist
→ response.submit
→ durable.dispatch
→ HISIEM HTTP
→ response.observe
```

Only require spans that the actual 05/06 acceptance and E1 catalog require.

Do not fabricate missing spans.

---

## 7. OBS-001

Known non-blocker:

```text
OBS-001
```

00 lists richer theoretical taxonomy examples:

```text
queue.wait
alert.hydrate
native.call
embedding
postgres.fts
pgvector.search
retrieval.merge
```

E0 found the existing implementation sufficient for 05/06 acceptance.

Therefore:

```text
DO NOT add these spans merely for completeness.
```

If the current trace satisfies E4 acceptance, leave:

```text
OBS-001 OPEN / NON-BLOCKING / unchanged
```

Only production telemetry defects that block the actual XP-01 OBS scenario may justify a minimal fix.

---

## 8. Correlation

Prove one representative lifecycle can be correlated across relevant boundaries using bounded references such as:

```text
investigation_id
tool invocation id
evidence id
finding id
response proposal id
execution command id
provider execution ref
trace_id
span_id
```

But:

```text
correlation reference != business truth
```

Do not make trace/span identity authoritative.

---

## 9. Trace context propagation

Validate existing W3C context semantics across synchronous boundaries.

For durable/asynchronous boundaries validate existing continuation/link semantics instead of pretending the entire lifecycle is one uninterrupted synchronous call.

Do not invent a new propagation contract.

---

## 10. Durable async telemetry

Where the scenario covers:

```text
response.submit
durable.dispatch
response.observe
```

prove telemetry can correlate the lifecycle while:

```text
business idempotency
durable command identity
```

remain independent of trace/span identity.

A new trace after durable recovery must not imply a new business command.

---

## 11. Telemetry safety

Hard-gate absence of:

```text
raw prompt
raw completion
full ToolResult
full Event/Alert body
Authorization
Bearer
API key value
password
DB credential
MCP credential
session/service token
.env contents
raw environment dump
embedding vector
chain-of-thought
```

Use synthetic sentinel values for tests. Never expose real secrets.

---

## 12. Knowledge telemetry safety

Safe fields may include:

```text
retrieval mode
duration
hit count
safe provider/profile identifiers
typed error category
```

Do not record by default:

```text
full retrieved content
unbounded/sensitive query
embedding vectors
full citation payload
```

Follow the frozen Stage B/00/05 contract.

---

## 13. Tool/MCP telemetry safety

Native and MCP paths should use the existing common semantic telemetry.

Safe examples:

```text
tool
provider type
duration
status
error category
bounded retry facts
```

Never expose:

```text
MCP token
credential
trusted endpoint secret
full provider payload
full ToolResult
```

Do not create a second E4-only tool telemetry format.

---

## 14. Metric-cardinality hard gate

Metrics must remain low-cardinality.

Forbidden labels/tags include:

```text
investigation_id
tenant_id
trace_id
span_id
invocation_id
command_id
request_id
user_id
alert_id
evidence_id
finding_id
response_proposal_id
```

These may exist as safe trace correlation attributes where appropriate, but not metric labels.

Reuse the canonical production helper such as:

```text
sanitize_metric_attributes
```

Do not copy/reimplement sanitizer logic in evaluation.

---

## 15. Collector outage invariant

Critical E4 rule:

```text
Collector/exporter unavailable
→ telemetry degraded or dropped
```

must NOT cause:

```text
Investigation failure
Tool failure
Knowledge/MCP failure
Approval failure
Durable execution failure
different business result
```

Hard invariant:

```text
telemetry failure does not change business outcome
```

---

## 16. Collector outage test

Use the smallest reliable mechanism required by the scenario profile:

```text
failing in-process exporter
unreachable collector endpoint
stopped local collector
```

Compare healthy-vs-unavailable telemetry runs on business facts.

Required:

```text
business outcome semantically equivalent
```

Do not stop the whole platform just to test exporter failure.

---

## 17. ENV-001 baseline

Known environment observation from E3:

```text
hsiem-platform.jar
hsiem-soar-worker.jar
siem-postgres
siem-kafka
```

may still be running because termination was denied.

Before any runtime launch, inspect:

```text
jps -l
docker ps
docker ps -a
relevant health endpoints
ports/processes
collector state
```

Rule:

```text
reuse healthy existing components
or
start only missing required components
```

Do not blindly start duplicate services or bind duplicate ports.

Record what was reused/started/stopped.

---

## 18. Collector handling

If a real collector is required, use the existing Stage B/repository-supported collector configuration.

Do not introduce:

```text
second collector stack
new tracing vendor
new metrics backend
new telemetry DB
```

If a test collector is started, clean it up when permitted.

If cleanup is denied by permission but repos/runtime correctness are unaffected, record the residue; do not fail E4 solely for cleanup denial.

---

## 19. No business dependency on OTel

Statically or behaviorally prove production does not read trace/span/export status to decide:

```text
verdict
approval
command creation
execution success
```

Telemetry must never become an authority source.

---

## 20. E4 adapters

Add only the smallest evaluation-only adapters needed for:

```text
required semantic spans observed
trace/link correlation facts
forbidden telemetry fields absent
metric-label safety
collector-outage business-equivalence facts
```

Pattern:

```text
existing telemetry/runtime facts
→ evaluation_harness adapter
→ E1 typed measurement
```

Avoid generic raw telemetry dump objects.

---

## 21. Span projection

Do not persist full raw spans into XP-01 artifacts.

Project bounded facts only:

```text
semantic operation name
provider type
status category
parent/link presence
safe correlation refs
safe attribute keys
forbidden-field booleans
```

---

## 22. Gate falsifiability

Add direct invalid-measurement tests for normally prevented violations:

```text
secret in span attrs
high-cardinality metric label
required semantic span absent
collector outage changes business outcome
raw ToolResult projected into telemetry
```

This proves gates can fail and are not vacuously green.

---

## 23. Positive + negative pairs

Mandatory:

```text
Representative trace:
required operations present → PASS
required operation absent → FAIL

Secret safety:
safe attrs → PASS
synthetic secret present → FAIL

Metric cardinality:
low-cardinality labels → PASS
investigation_id metric label → FAIL

Collector outage:
telemetry unavailable + business unchanged → PASS
telemetry unavailable + business changed → FAIL
```

---

## 24. Execution profiles

Use minimum valid profile.

### deterministic

For:

```text
sanitizer
cardinality rules
secret scan
bounded span projection
gate falsifiability
```

### focused integration

For:

```text
in-process exporter
span parent/link behavior
semantic emission
```

### runtime-integrated

Only where catalog requires:

```text
representative cross-plane trace
collector outage through real runtime
durable async correlation
```

Do not rerun Stage D 25/25.

---

## 25. Stage D reuse

Treat `FULL_RUNTIME_E2E_TEST_REPORT.md` as baseline runtime proof.

E4 only needs the smallest representative runtime slice needed for:

```text
cross-plane telemetry correlation
collector outage independence
```

---

## 26. Production change budget

Expected:

```text
HISIEM production diff = 0
Copilot production-layer diff = 0
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

Expected E4 ownership:

```text
evaluation/
evaluation_harness/
tests/
E4 report
```

A production telemetry change is allowed only if E4 proves a real Stage B acceptance defect.

If required:

1. document the exact defect;
2. make the smallest Stage-B-consistent fix;
3. add focused regression;
4. record exact production files changed;
5. do not broaden scope.

---

## 27. No E5 work

Do NOT implement:

```text
WORKSPACE scenario execution
frontend changes
AuthorityTag changes
Playwright Workspace acceptance
refresh/stale reconstruction
```

Those belong to E5.

---

## 28. Carry-forward observations

Keep:

```text
CATALOG-001
RESOLVED

OBS-001
OPEN / NON-BLOCKING unless E4 proves otherwise

DEFECT-005
OPEN / LOW / NON-BLOCKING / not E4

TEST-INFRA-001
OPEN / test-infrastructure-only

EVAL-SEAM-001
ACCEPTED / NON-BLOCKING

ENV-001
verify active runtime before starting components
```

Do not absorb unrelated issues.

---

## 29. Regression gates

Run:

### E4 focused tests
All new observability adapter/gate/integration/runtime-slice tests.

### Stage B observability regression
Existing tests for:

```text
semantic spans
trace propagation
links/continuation
metric sanitization
exporter failure
safe attributes
```

### E1 foundation

Baseline:

```text
172 passed
```

### E2 scenarios

Baseline:

```text
15/15 PASS
```

### E3 scenarios

Baseline:

```text
10/10 PASS
```

### GP-01

Baseline:

```text
346 passed
```

### KB-GOLDEN-V1

Latest baseline when disposable DB is available:

```text
679 passed / 0 skipped
```

### Architecture

Run current:

```text
tests/architecture/test_knowledge_boundary.py
tests/architecture/test_evaluation_boundary.py
tests/architecture/test_import_boundaries.py
E1 production→evaluation_harness boundary tests
```

### Static

Run project-standard:

```text
ruff
mypy
git diff --check
```

### Full pytest

E3 baseline:

```text
2105 passed / 9 skipped / 0 failed / 0 errors
```

Required:

```text
0 failures
0 errors
no new skip used to hide E4 defects
```

---

## 30. Secret safety

Never print/write actual:

```text
.env
.env.local
Authorization
Bearer
API keys
DB passwords
MCP credentials
session/service tokens
```

Use synthetic sentinels only.

---

## 31. Implementation order

Execute:

```text
E4-A verify Git baseline + actual runtime/environment state
E4-B read authorities/reports/current E1/E2/E3 source
E4-C extract exact OBSERVABILITY scenario IDs
E4-D map each scenario to existing telemetry/runtime facts
E4-E implement observability measurement adapters
E4-F implement deterministic safety/cardinality tests
E4-G implement focused trace/exporter integration
E4-H implement only required runtime representative trace
E4-I implement collector outage scenario
E4-J add positive/negative/falsifiability tests
E4-K verify every E4-owned scenario has an XP-01 path
E4-L run all focused/regression/architecture/static/full gates
E4-M inspect complete diff + runtime residue
E4-N write E4 report
STOP
```

Do not start E5.

---

## 32. E4 report

Create:

```text
D:\Project\HISIEM-SOC-Copilot\STAGE_E_E4_OBSERVABILITY_ACCEPTANCE_REPORT.md
```

Required sections:

```text
1. Header / authorities / baseline
2. Final E4 status
3. Exact E4 scenario inventory
4. Files changed
5. Representative trace coverage
6. Trace propagation / durable-link coverage
7. Telemetry safety coverage
8. Metric-cardinality coverage
9. Collector outage / business-independence coverage
10. Measurement adapters
11. Artifact evidence
12. Runtime environment/components used
13. Architecture-boundary evidence
14. Test/regression evidence
15. Production diff
16. Known observations/non-blockers
17. E5 handoff
```

Scenario table:

```text
| Scenario ID | Profile | Measurement Adapter | Required Gates | Runtime Needed | Artifact | Result |
```

---

## 33. Acceptance criteria

Return:

```text
E4 IMPLEMENTATION: PASS
E5 READINESS: READY
```

only if:

```text
all OBSERVABILITY scenarios have executable XP-01 paths
all required E4 scenarios PASS

representative lifecycle is correlatable
required accepted semantic spans are present
durable async correlation follows existing link/continuation semantics
trace/span identity does not replace business identity

no raw prompt/completion/full ToolResult/secrets leak
Knowledge telemetry remains bounded/safe
MCP/tool telemetry remains bounded/safe

metric labels remain low-cardinality
forbidden IDs are absent from metric labels

collector/exporter outage does not change business outcome
telemetry failure does not become authority/business failure

OBS-001 is not opportunistically expanded unless a real defect was proven

cross-plane-gate-results/v1 unchanged
E2 15/15 remains green
E3 10/10 remains green
GP-01 unchanged
KB-GOLDEN-V1 unchanged

HISIEM production diff = 0 unless a proven Stage B defect required a minimal fix
Copilot production-layer diff = 0 unless a proven Stage B defect required a minimal fix

architecture passes
ruff passes
mypy passes
git diff --check passes
full pytest has 0 failures/errors

E4 report exists
no commit
no push
E5 not started
```

Use FAIL for unresolved telemetry-safety/correlation/business-independence defects.

Use BLOCKED only for a true external dependency.

---

## 34. Git rules

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
discard E0/E1/E2/E3 work
```

Before finishing:

```text
git status --short
git diff --stat
git diff
git diff --check
jps -l
docker ps
docker ps -a
```

Review every changed path and runtime residue.

Confirm:

```text
no unrelated HISIEM change
no unrelated Copilot production change
no frontend change
no migration
no GP-01 semantic change
no KB-GOLDEN-V1 semantic change
no E5+ work
no DEFECT-005 heuristic
no TEST-INFRA-001 repair
no real secret
no generated trace/runtime artifact accidentally tracked
```

---

## 35. Final response format

Return only:

```text
E4 IMPLEMENTATION: PASS | FAIL | BLOCKED
E5 READINESS: READY | NOT READY

Report:
D:\Project\HISIEM-SOC-Copilot\STAGE_E_E4_OBSERVABILITY_ACCEPTANCE_REPORT.md

E4 scenarios:
<total count / exact IDs>

XP-01:
29 scenarios / 13 hard gates / contract unchanged

Completed XP-01:
<completed>/29

E4 gate result:
<passed>/<executed> required scenarios

Representative trace:
PASS | FAIL

Trace propagation / durable links:
PASS | FAIL

Telemetry safety:
PASS | FAIL

Metric cardinality:
PASS | FAIL

Collector outage independence:
PASS | FAIL

Production changes:
NONE | exact justified change

Architecture boundary:
PASS | FAIL

E4 focused tests:
<exact result>

Stage B regression:
<exact result>

E3 regression:
<exact result>

E2 regression:
<exact result>

E1 regression:
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
OPEN / NON-BLOCKING / unchanged
or
<exact proven defect and smallest fix>

DEFECT-005:
OPEN / LOW / NON-BLOCKING / unchanged

TEST-INFRA-001:
<status>

EVAL-SEAM-001:
ACCEPTED / NON-BLOCKING / unchanged

ENV-001:
<actual runtime state / components reused or left running>

HISIEM Git:
<branch @ HEAD, status>

Copilot Git:
<branch @ HEAD, status>

Commit: NO
Push: NO
E5: NOT STARTED
```

Stop after E4.
