# Stage E — E3 Authority / Reliability Vibe Coding Prompt

> **Mode:** Execution task  
> **Stage:** Stage E / E3  
> **Primary repo:** `D:\Project\HISIEM-SOC-Copilot`  
> **HISIEM repo:** `D:\Project\SIEM` (read-only unless a real contradiction is proven)  
> **Commit / Push:** NO / NO

---

## 0. Mission

Implement the XP-01 scenario-execution slice for:

```text
AUTHORITY
RELIABILITY
```

E3 must prove that the already-sealed authority and durable-execution boundaries remain distinct under normal flow, retries, uncertainty, stale approval, restart/recovery, and observed execution reconciliation.

Required evaluation flow:

```text
persisted/runtime production facts
→ E3 measurement adapter
→ E1 typed measurement contract
→ E1 deterministic hard gate
→ cross-plane-gate-results/v1
```

This is an implementation task.

Do not return a design recap first.

Do not stop at a locally resolvable implementation/test failure.

Do not redesign XP-01.

Do not start E4 automatically.

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

The Copilot working tree intentionally contains uncommitted E0/E1/E2 work.

Known Stage E baseline:

```text
E0 AUDIT: PASS

E1 IMPLEMENTATION: PASS
XP-01 v1
29 scenarios
13 hard gates
3 execution profiles
9 gate families
artifact schema: cross-plane-gate-results/v1

E2 IMPLEMENTATION: PASS
15 / 15 E2 scenarios PASS

KNOWLEDGE: 4
CAPABILITY: 1
MCP: 5
TENANT: 2
SECURITY: 3

GP-01: UNCHANGED
KB-GOLDEN-V1: UNCHANGED
Production changes: NONE

Full pytest after E2:
2003 passed / 9 skipped / 0 failed / 0 errors
```

Before editing, verify both repositories:

```text
git branch --show-current
git rev-parse HEAD
git rev-parse origin/<branch>
git status --short
git diff --stat
git diff
```

Do not reset/restore/clean/rebase/amend/discard any existing E0/E1/E2 work.

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
```

Use when needed as runtime baseline:

```text
D:\Project\HISIEM-SOC-Copilot\FULL_RUNTIME_E2E_TEST_REPORT.md
```

Authority order:

```text
00
→ 05
→ 06
→ E0 audit
→ E1 implemented contract
→ E2 implemented scenario slice
→ this E3 prompt
```

Also inspect the actual current uncommitted E1/E2 source before writing code.

---

## 3. E1/E2 contracts are frozen for E3

Do not casually modify:

```text
XP-01 pack identity/version
29-scenario catalog
scenario IDs
scenario semantic identities
13 hard-gate IDs
9 gate families
3 execution profiles
expected/forbidden fact vocabulary
typed measurement contracts
cross-plane-gate-results/v1
E2 scenario semantics
```

If E3 reveals a real E1/E2 contract defect:

1. reproduce it;
2. prove the contradiction;
3. apply the smallest correction;
4. add regression coverage;
5. record it explicitly in the E3 report.

Do not redesign Stage E foundations for convenience.

---

## 4. Exact E3 scenario ownership

Derive the exact scenario IDs from the implemented E1 XP-01 catalog.

E3 owns every scenario whose family is:

```text
AUTHORITY
RELIABILITY
```

At the start of execution:

```text
extract exact IDs
group by family
record exact count
use that set as the E3 implementation checklist
```

Do not hard-code guessed scenario counts from this prompt.

Do NOT implement:

```text
OBSERVABILITY
WORKSPACE
```

Those belong to E4/E5.

---

## 5. E3 core architecture

E3 validates this real authority/execution chain:

```text
Investigation Result / Agent Verdict
→ Response Proposal
→ Policy Evaluation
→ Human Decision
→ Durable Execution Command
→ Dispatch Attempt
→ HISIEM SOAR Submission
→ HISIEM Observed Execution State
→ Copilot Projection
```

Frozen responsibility chain:

```text
Model proposes
Policy constrains
Human authorizes
Durable command records intent
HISIEM executes
Copilot observes
```

E3 must prove these states do not collapse into one another.

---

## 6. Authority invariants

E3 must measure and gate the catalog equivalents of:

```text
Agent Verdict != Analyst Disposition

Policy Decision != Human Approval

Human Approval != Execution

Submission != Execution Success

Copilot response state != HISIEM execution truth
```

Valid intermediate states must remain valid.

Example:

```text
policy = ALLOW
approval = PENDING
execution command = absent
```

This is not an error.

Likewise:

```text
approval = APPROVED
execution not yet submitted
```

is valid.

Do not force terminal-state simplification.

---

## 7. Final execution truth

Frozen rule:

```text
HISIEM observed execution state
=
final execution truth
```

Not:

```text
HTTP 2xx
dispatcher returned
Copilot recorded submitted
OTel span success
frontend projection
```

E3 must evaluate production facts such that:

```text
SUBMITTED
!=
SUCCESS
```

and any later HISIEM observed terminal result overrides prior Copilot expectation.

---

## 8. Human rejection semantics

Human rejection must mean:

```text
not authorized for execution
```

It must not be conflated with:

```text
execution failure
```

Required invariant:

```text
Human Decision = REJECTED
→ no dispatchable durable execution command for that authorization
```

E3 should measure this with persisted/business facts, not UI text.

---

## 9. Approval revision/hash binding

Approval is not just:

```text
approved = true
```

Formal authorization must remain bound to the approved intent.

Where current production contracts expose them, measure:

```text
proposal revision
approved revision
approval hash
command authorization hash / intent hash
```

Required invariant:

```text
approved revision/hash
must match
the command being authorized
```

Stage D already proved stale revision/hash rejection. E3 turns this into XP-01 measurable invariants.

---

## 10. TOCTOU protection

E3 must cover the catalog equivalent of:

```text
proposal revision N approved

proposal changes to N+1

attempt to execute N+1 using approval for N

→ rejected
→ no unauthorized executable command
→ no HISIEM execution
```

Do not add a new scenario ID unless the existing catalog is proven incomplete.

If the scenario is represented by expected/forbidden facts inside an existing AUTHORITY/RELIABILITY scenario, use that.

---

## 11. E3 measurement philosophy

E3 must observe real production state.

Preferred sources:

```text
Response Proposal persistence
Policy decision persistence/result
Human approval persistence
Durable command persistence
Dispatcher attempt persistence
Observed execution persistence/projection
HISIEM response observation
```

Avoid using as truth:

```text
logs
trace spans
frontend local state
timestamps alone
```

Telemetry may correlate; it may not authorize or define business truth.

---

## 12. Authority measurement contract

Use existing E1 typed measurement types where available.

If adapters need to populate authority facts, include bounded facts such as:

```text
investigation_id
response_proposal_id
policy_decision
approval_request_id
approval_state
approval_revision
approval_hash
execution_command_id
command_state
submitted_at
provider_execution_ref
observed_execution_state
observed_at
```

Do not dump full entities.

Do not invent a parallel state machine in evaluation code.

---

## 13. Reliability is semantic reliability, not performance testing

E3 RELIABILITY validates that:

```text
retry
timeout
restart
uncertain submit
exhaustion
reconciliation
```

do not corrupt authority or execution truth.

This is not a load/performance benchmark.

---

## 14. Idempotency

Required invariant:

```text
same authorized intent
+
retry
→ one logical durable execution intent
```

Multiple transport attempts are allowed.

For example:

```text
command-123
attempt 1 → timeout
attempt 2 → timeout
attempt 3 → accepted
```

is still:

```text
1 logical command
3 attempts
```

The gate must not require attempt_count == 1.

It must detect:

```text
duplicate logical execution intent
```

---

## 15. Duplicate execution prevention

E3 should measure facts such as:

```text
logical_command_count
execution_command_id
attempt_count
idempotency identity/reference
approval identity
provider execution refs
```

The required semantic is:

```text
duplicate logical execution == 0
```

Do not confuse:

```text
retry
```

with:

```text
duplicate business intent
```

---

## 16. Submit uncertainty

Important reliability case:

```text
Copilot sends submission
HISIEM may receive/process it
network response becomes uncertain
```

Copilot must not fabricate:

```text
SUCCESS
```

or:

```text
FAILED
```

solely from an uncertain submit.

Correct behavior remains:

```text
observe / reconcile
```

Required invariant:

```text
uncertain submission
→ no fabricated terminal execution state
```

---

## 17. ATTENTION_REQUIRED

Stage D runtime evidence already proved real exhaustion behavior.

E3 must model and gate the semantics:

```text
ATTENTION_REQUIRED != SUCCESS
ATTENTION_REQUIRED != REJECTED
```

It means:

```text
the automated lifecycle cannot safely establish/complete the execution outcome
and human attention is required
```

Preserve explicit uncertainty.

Where existing fields support it, measure:

```text
command_state = ATTENTION_REQUIRED
submission_confirmed = false/unknown
execution_observed = false/unknown
attempt_count
submitted_at
execution result presence
```

Do not reduce ATTENTION_REQUIRED to a generic boolean `failed`.

---

## 18. Retry/exhaustion

Where catalog scenarios require it, evaluate:

```text
retry count
attempt persistence
exhaustion state
dead-letter / attention-required state where applicable
```

Do not implement a second retry engine in evaluation code.

Use real existing dispatcher/service semantics.

---

## 19. Restart/recovery

Prefer existing durable/restart test infrastructure.

E3 should only run process-level restart/recovery when the XP-01 scenario's execution profile genuinely requires it.

Do not restart the entire platform for every reliability case.

The invariant is:

```text
durable authorized intent survives process/runtime interruption
without creating a second business intent
```

---

## 20. Observed truth reconciliation

E3 must ensure:

```text
HISIEM observed result
```

remains authoritative even if Copilot previously projected:

```text
SUBMITTED
PENDING
EXPECTED_SUCCESS
```

Example:

```text
Copilot: SUBMITTED
HISIEM observed: FAILED
→ final business execution state = FAILED
```

Do not create an evaluation-side override.

---

## 21. Deterministic vs integration vs runtime profiles

Use minimum required profile.

### deterministic

Use for:

```text
Policy != Approval
Approval != Execution
Submission != Success
execution_without_approval gate
submission_treated_as_success gate
stale approval measurement
ATTENTION_REQUIRED semantic classification
duplicate logical command falsifiability
```

### focused integration

Use for:

```text
durable command persistence
idempotency
dispatcher attempt persistence
retry state
observe reconciliation
```

### runtime-integrated

Use only where required for:

```text
real uncertain submit
real exhaustion
process restart/recovery
real HISIEM observed execution truth
```

Do not rerun Stage D 25/25 automatically.

---

## 22. Stage D evidence reuse

Treat:

```text
FULL_RUNTIME_E2E_TEST_REPORT.md
```

as sealed runtime baseline evidence.

E3's purpose is not:

```text
rerun every Stage D scenario
```

E3's purpose is:

```text
turn key Authority/Reliability invariants into repeatable XP-01 measurements + gates
```

Only rerun runtime slices whose E3 catalog scenarios genuinely require fresh runtime evidence.

---

## 23. Adapter design

Add only the smallest measurement adapters needed.

Possible logical boundaries:

```text
response lifecycle adapter
approval/authorization adapter
durable command adapter
execution observation adapter
```

Exact filenames are not frozen.

Avoid one giant adapter with dozens of optional fields.

Rule:

```text
adapter extracts facts
gate evaluates invariant
production state machine remains production-owned
```

---

## 24. Production change budget

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

Do not add:

```text
debug approval endpoint
force retry endpoint
force attention endpoint
test-only bypass
evaluation DB column
evaluation status
new migration/table
```

Use existing repositories/services/application boundaries.

---

## 25. HISIEM involvement

Most AUTHORITY gates should be measurable on the Copilot side.

HISIEM participation is appropriate only for scenarios requiring:

```text
observed execution truth
uncertain submit reconciliation
real execution result
```

Prefer deterministic adapter/fake boundary where the scenario does not require real runtime integration.

HISIEM code changes remain expected at zero.

---

## 26. E1 hard gates must be reused

Prioritize existing E1 hard gates such as:

```text
execution_without_approval == 0
submission_treated_as_success == 0
```

Use existing E1 fact/gate vocabulary for stale authorization, duplicate command, observed truth, or related invariants if already present.

Do not introduce:

```text
AuthorityScore
ReliabilityScore
weighted pass percentages
```

Hard gates remain deterministic and non-compensating.

---

## 27. Positive + negative pairs

Mandatory.

Examples:

### Approval

```text
Positive:
approved revision/hash matches command
→ authorization valid

Negative:
stale revision/hash
→ execution not authorized
```

### Idempotency

```text
Positive:
same command retried
→ one logical intent

Negative:
same approval/intent produces two logical commands
→ gate FAIL
```

### Submission

```text
Positive:
submitted then HISIEM observed success
→ success

Negative:
submitted but no observed success
→ must not be success
```

### Rejection

```text
Positive:
human rejection
→ no executable command

Negative measurement:
rejected approval + executable command
→ gate FAIL
```

---

## 28. Gate falsifiability

For invariants that production normally prevents, add direct invalid measurement tests proving the XP-01 gate actually fails.

Do this for catalog equivalents of:

```text
execution without approval
submission treated as success
duplicate logical command
stale authorization accepted
rejected approval dispatchable
uncertain submit fabricated terminal state
```

This is testing the gate, not simulating production as broken.

---

## 29. Artifact contract

Continue using:

```text
cross-plane-gate-results/v1
```

Do not introduce:

```text
authority-results/v1
reliability-results/v1
```

Each scenario artifact must remain bounded and include only E1-approved references/facts.

No raw entity dump.

---

## 30. Correlation references

Artifacts may reference bounded IDs such as:

```text
investigation_id
response_proposal_id
approval_request_id
execution_command_id
provider execution ref
```

Do not make these metric labels.

Do not embed the entire related entity.

---

## 31. Existing non-blockers

Carry forward without absorbing them into E3:

```text
CATALOG-001
RESOLVED

OBS-001
OPEN / not E3

DEFECT-005
OPEN / LOW / NON-BLOCKING / not E3

TEST-INFRA-001
OPEN / test-infrastructure-only

EVAL-SEAM-001
ACCEPTED / NON-BLOCKING
```

Do not fix them opportunistically.

---

## 32. Test strategy

### Layer 1 — adapter unit tests

Cover:

```text
proposal facts
policy facts
approval facts
revision/hash facts
durable command facts
attempt facts
observed result facts
ATTENTION_REQUIRED facts
```

### Layer 2 — gate/falsifiability tests

Direct invalid measurements for authority/reliability hard-gate failures.

### Layer 3 — focused integration

Use real:

```text
DB
repositories
services
dispatcher
response observation
```

where needed.

### Layer 4 — runtime-integrated

Only for E3 scenarios whose E1 execution profile requires real runtime.

---

## 33. Regression gates

### E3 focused tests

Run all new adapters/drivers/integration/runtime-slice tests.

### E1 foundation

Expected baseline:

```text
172 passed
```

Must remain green.

### E2 scenario slice

Expected:

```text
15 / 15 scenarios PASS
```

Run the focused E2 scenario suite and preserve semantics.

### GP-01

Expected baseline:

```text
346 passed
```

No semantic changes.

### KB-GOLDEN-V1

Latest stronger baseline:

```text
679 passed / 0 skipped
```

when disposable test DB is available.

No semantic changes.

### Existing Response/Durable tests

Run focused tests covering:

```text
proposal
approval
revision/hash
idempotency
durable command
dispatcher retry
ATTENTION_REQUIRED
submit/observe
```

### Architecture

Run:

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

E2 baseline:

```text
2003 passed / 9 skipped / 0 failed / 0 errors
```

Required final semantics:

```text
0 failures
0 errors
no new skip used to hide E3 defects
```

---

## 34. Database/runtime safety

Do not mutate protected operator databases merely to run E3 evaluation.

Protected:

```text
HISIEM DB 5432
Copilot operator/primary DB 5433
```

Use existing disposable integration database conventions where tests require persistence.

If runtime-integrated E3 scenarios require full official stack state, use the established Stage D runtime process safely and only for the needed slice.

Never expose credentials.

---

## 35. Secret safety

Never print or persist actual:

```text
.env
.env.local
Authorization
Bearer tokens
API keys
DB passwords
MCP credentials
session/service tokens
```

Use synthetic sentinels only if an E3 test needs secret-scan behavior.

---

## 36. E3 implementation order

Execute exactly in this order:

```text
E3-A
baseline verification

E3-B
read authorities/reports/current E1/E2 source

E3-C
extract exact AUTHORITY + RELIABILITY scenario IDs from E1 catalog

E3-D
map each scenario to actual production persisted/runtime facts

E3-E
implement authority measurement adapters

E3-F
implement reliability measurement adapters

E3-G
implement deterministic scenario drivers

E3-H
implement focused durable integration paths

E3-I
implement only catalog-required runtime-integrated slices

E3-J
positive + negative + gate-falsifiability tests

E3-K
verify every E3-owned scenario has an executable XP-01 path

E3-L
run focused regressions / architecture / static / full pytest

E3-M
inspect complete diff

E3-N
write E3 report

STOP
```

Do not start E4.

---

## 37. E3 report

Create:

```text
D:\Project\HISIEM-SOC-Copilot\STAGE_E_E3_AUTHORITY_RELIABILITY_REPORT.md
```

Required sections:

```text
1. Header / authorities / baseline
2. Final E3 status
3. Exact E3 scenario inventory
4. Files changed
5. Authority lifecycle coverage
6. Approval revision/hash/TOCTOU coverage
7. Durable command + idempotency coverage
8. Retry/exhaustion coverage
9. ATTENTION_REQUIRED semantics
10. Submit uncertainty / reconciliation
11. HISIEM observed execution truth
12. Measurement adapters
13. Artifact evidence
14. Architecture-boundary evidence
15. Test/regression evidence
16. Production diff
17. Known observations/non-blockers
18. E4 handoff
```

Scenario table:

```text
| Scenario ID | Family | Profile | Measurement Adapter | Gate IDs | Artifact | Result |
```

---

## 38. E3 acceptance criteria

Return:

```text
E3 IMPLEMENTATION: PASS
E4 READINESS: READY
```

only if all are true:

```text
all AUTHORITY scenarios have executable XP-01 paths
all RELIABILITY scenarios have executable XP-01 paths
all required E3 scenarios PASS

Policy != Human Approval
Human Approval != Execution
Submission != Execution Success
HISIEM observed result remains execution truth

no execution without valid human authorization

stale approval revision/hash cannot authorize changed intent

human rejection cannot produce dispatchable execution

retry preserves one logical durable intent

duplicate logical execution is prevented/detected

uncertain submission does not fabricate terminal success/failure

ATTENTION_REQUIRED remains explicit uncertainty semantics

restart/recovery does not create duplicate business intent where catalog requires it

cross-plane-gate-results/v1 unchanged

XP-01 catalog/hard-gate contract unchanged unless a proven minimal correction was required

E2 15/15 remains green

GP-01 unchanged

KB-GOLDEN-V1 unchanged

HISIEM production diff = 0

Copilot production-layer diff = 0

architecture boundaries pass

ruff passes

mypy passes

git diff --check passes

full pytest = 0 failures/errors

E3 report exists

no commit
no push
E4 not started
```

Use FAIL for unresolved correctness/security/authority/reliability defects.

Use BLOCKED only for a true external dependency, not a normal failing test.

---

## 39. Git rules

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
discard E0/E1/E2 work
```

Before finishing:

```text
git status --short
git diff --stat
git diff
git diff --check
```

Review every changed path.

Confirm:

```text
no HISIEM source change
no Copilot production-layer change
no frontend change
no migration
no GP-01 semantic change
no KB-GOLDEN-V1 semantic change
no E4+ implementation
no OBS-001 span work
no DEFECT-005 heuristic
no unrelated TEST-INFRA-001 repair
no secret
no generated runtime artifact accidentally tracked
```

---

## 40. Final response format

Return only:

```text
E3 IMPLEMENTATION: PASS | FAIL | BLOCKED
E4 READINESS: READY | NOT READY

Report:
D:\Project\HISIEM-SOC-Copilot\STAGE_E_E3_AUTHORITY_RELIABILITY_REPORT.md

E3 scenarios:
<total count>
AUTHORITY: <count / exact IDs>
RELIABILITY: <count / exact IDs>

XP-01:
29 scenarios / 13 hard gates / contract unchanged

E3 gate result:
<passed>/<executed> required scenarios

Authority lifecycle:
PASS | FAIL

Revision/hash/TOCTOU:
PASS | FAIL

Idempotency/retry:
PASS | FAIL

ATTENTION_REQUIRED:
PASS | FAIL

Submit uncertainty:
PASS | FAIL

HISIEM execution truth:
PASS | FAIL

Production changes:
NONE | exact unexpected scope

Architecture boundary:
PASS | FAIL

E3 focused tests:
<exact result>

E2 regression:
<exact result>

E1 regression:
<exact result>

Response/Durable regression:
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

EVAL-SEAM-001:
ACCEPTED / NON-BLOCKING / unchanged

HISIEM Git:
<branch @ HEAD, status>

Copilot Git:
<branch @ HEAD, status>

Commit: NO
Push: NO
E4: NOT STARTED
```

Stop after E3.
