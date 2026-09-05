# GP-01 Dataset Materializer Contract

## 1. Scope

This document defines E1-B.3, the GP-01 Dataset Materializer contract.

The Materializer turns the committed logical GP-01 scenario into real HISIEM resources and resolves their provider identities for later evaluation.

```text
Committed GP-01 Scenario
        ↓
Dataset Materializer
        ↓
Real HISIEM ingestion
        ↓
Real event resolution
        ↓
Real alert resolution
        ↓
Dataset verification
        ↓
VerifiedDataset
```

The Materializer does not run the Copilot investigation, decide the verdict, score the result, or seal the evaluation manifest.

## 2. Authority boundaries

The Materializer MUST use HISIEM as the source of truth for materialized resources.

It MUST NOT:

- write directly to Elasticsearch;
- produce directly to Kafka;
- write directly to `siem-alerts`;
- write Copilot domain state;
- derive provider addressing identifiers locally;
- expose evaluation oracle facts to the Copilot runtime.

GP-01 event injection MUST enter through the real SSH log ingestion path used by HISIEM. The intended path is:

```text
TCP SSH log input
→ HISIEM SSH parser
→ siem-events-*
→ detection pipeline
→ siem-alerts
```

Provider resources are considered materialized only after they are resolvable through HISIEM-supported read interfaces.

## 3. GP-01 logical dataset

GP-01 contains six semantic events and one control event.

Semantic events:

- `F1` — SSH authentication failure
- `F2` — SSH authentication failure
- `F3` — SSH authentication failure
- `F4` — SSH authentication failure
- `F5` — SSH authentication failure
- `S1` — SSH authentication success after the failure sequence

`F1` through `F5` MUST share:

- `source.ip`;
- `user.name`;
- `host.name`;
- `event.category = authentication`;
- `event.action = authentication_failure`;
- `event.outcome = failure`.

`S1` MUST share the same source, account, and host and MUST satisfy:

- `event.category = authentication`;
- `event.action = authentication_success`;
- `event.outcome = success`;
- `S1.timestamp > max(F1..F5.timestamp)`.

The scenario ground truth is that a brute-force sequence is followed by a successful authentication for the same entity. Expected verdict data belongs to the evaluation oracle and MUST NOT be passed to the investigation runtime.

## 4. Watermark control event

The Materializer MUST generate one control event `W1` when the deployed detection runtime requires event-time advancement for the relevant window to close.

`W1` exists only to advance detection processing. It MUST:

- be classified as `WATERMARK_CONTROL`;
- use an entity distinct from the GP-01 attack entity;
- use a distinct `source.ip`;
- not satisfy any GP-01 evidence requirement;
- not be included in semantic ground truth;
- not be injected into Copilot graph state or prompts.

The event remains a real HISIEM event and therefore may be observable if an investigation performs an intentionally broad search. Evaluation scoring MUST classify it as control data and MUST NOT allow it to satisfy GP-01 evidence requirements.

## 5. Runtime identity

Each materialization creates a unique `run_id` and a deterministic short `run_tag` derived from it.

Runtime-specific entities MUST be derived from `run_id` so concurrent or repeated evaluation runs do not share the same detection identity.

At minimum the bound scenario MUST contain:

```text
run_id
run_tag
attack_source_ip
watermark_source_ip
user_name
host_name
event timestamps
rendered SSH log lines
```

Required invariant:

```text
attack_source_ip != watermark_source_ip
```

Different run identities SHOULD derive different attack entities so prior detection suppression state cannot contaminate a new run.

## 6. Time plan

The logical scenario MUST be bound to past timestamps at materialization time. It MUST NOT generate future events.

Recommended plan:

```text
anchor = floor(now - safe_history_offset)

F1 = anchor + 10s
F2 = anchor + 20s
F3 = anchor + 30s
F4 = anchor + 40s
F5 = anchor + 50s
S1 = anchor + 70s
W1 = anchor + 7m
```

The exact offsets may be configuration constants, but the following invariants are mandatory:

- all failure events are inside the configured brute-force detection interval;
- `S1` occurs after all failure events;
- `W1` occurs after the relevant detection-window close boundary;
- all generated timestamps are in the past when injection starts.

SSH syslog rendering MUST use the timezone expected by the deployed HISIEM parser. For the current GP-01 environment this is `Asia/Shanghai`.

Because the SSH syslog form does not carry a year, the Materializer MUST reject a time plan that crosses a natural-year boundary rather than guess parser year-completion behavior.

Failure code:

```text
EVENT_PLAN_CROSSES_YEAR_BOUNDARY
```

## 7. Logical and provider identities

Scenario identities and HISIEM provider identities are different concepts.

A logical event identity such as `F1` MUST NOT be treated as an Elasticsearch document id.

A resolved event reference MUST use the values returned by HISIEM:

```text
provider = hisiem
index = real _index
document_id = real _id
```

The source alert reference MUST use the actual identifier accepted by the HISIEM alert detail API:

```text
provider = hisiem
resource_type = alert
address_id = real HISIEM alert addressing id
business_id = optional business alert id
```

For the current HISIEM implementation, alert addressing uses the Elasticsearch document `_id`. The Materializer MUST resolve it from HISIEM and MUST NOT infer it from `alert.id`, `run_id`, event ids, timestamps, or hashes.

## 8. Materializer model

The evaluation package SHOULD expose provider-neutral types equivalent to:

```text
ScenarioSpec
RunIdentity
BoundScenario
LogicalEvent
RenderedEvent
InjectedEvent
ResolvedEvent
ResolvedAlert
MaterializationDraft
VerifiedDataset
```

`ResolvedEvent` MUST contain only bounded normalized fields needed to prove scenario identity and later scoring, including:

```text
logical_role
provider
index
document_id
timestamp
event_category
event_action
event_outcome
source_ip
user_name
host_name
message_fingerprint
```

It MUST NOT persist complete Elasticsearch documents as the normal evaluation representation.

`ResolvedAlert` SHOULD contain:

```text
provider
address_id
business_id?
rule_id
rule_name?
source/entity
created_at
event_count
status
related_event_refs[]
```

## 9. State machine

Materialization MUST use an explicit state machine:

```text
NEW
 ↓
PREFLIGHTED
 ↓
EVENTS_RENDERED
 ↓
EVENTS_INJECTED
 ↓
EVENTS_RESOLVED
 ↓
ALERT_RESOLVED
 ↓
VERIFIED
 ↓
MATERIALIZED
```

Failure states:

```text
FAILED
INDETERMINATE
```

`INDETERMINATE` is required for non-idempotent injection whose server-side outcome cannot be proven.

## 10. Preflight

No write may occur before all preflight checks pass.

Required checks:

### 10.1 HISIEM reachability

The configured HISIEM control/read surface MUST be reachable.

### 10.2 Tenant validity

The evaluation tenant MUST be readable using the configured trusted test credentials.

### 10.3 Detection-rule contract

The deployed SSH brute-force rule MUST match the scenario assumptions required by GP-01. At minimum verify the effective rule identity, enabled state, key field, failure predicate, threshold, and detection window.

A material semantic mismatch MUST fail with:

```text
RULE_CONTRACT_MISMATCH
```

Evaluation MUST NOT silently adapt GP-01 to a changed detection rule.

### 10.4 Run collision

Before injection, search the bounded GP-01 time/entity scope.

- resources already belonging to the same `run_id` enter reconciliation/resume;
- resources colliding with a different run identity fail with `RUN_IDENTITY_COLLISION`.

### 10.5 Time validity

The time plan MUST pass the past-time, detection-window, and year-boundary invariants.

## 11. Injection protocol

Injection order is fixed:

```text
F1
F2
F3
F4
F5
S1
W1
```

The caller MUST NOT reorder events.

Each attempted injection MUST record bounded audit data:

```text
logical_role
attempted_at
payload_sha256
socket_target
write_status
```

Secrets and authorization material MUST NOT be recorded.

The rendered log line SHOULD carry a materializer-only correlation fingerprint using fields that survive in the HISIEM event representation, such as host, timestamp, source address, process id, and action. This fingerprint is a resolver aid only; it does not become provider identity.

## 12. Non-idempotent TCP rule

TCP injection MUST NOT be blindly retried after an ambiguous outcome.

If the client cannot determine whether a rendered event was accepted after a write attempt:

```text
state = INDETERMINATE
```

A rerun with the same `run_id` MUST default to reconciliation and resolution. It MUST NOT resend an already-attempted event automatically.

If the existing run cannot be reconciled to one unambiguous provider dataset, the run is abandoned and a new `run_id` is required.

This rule prevents an uncertain retry from changing a five-event failure sequence into a six-event sequence.

## 13. Event resolution

After injection, events MUST be resolved through the HISIEM structured log-search API. Direct Elasticsearch queries are prohibited for normal Materializer behavior.

For each of `F1..F5`, `S1`, and `W1`:

```text
0 valid matches  → continue bounded polling
1 valid match    → resolve provider reference
>1 valid matches → AMBIGUOUS_EVENT
```

Resolution MUST validate at least:

- `_index`;
- `_id`;
- `@timestamp`;
- `event.category`;
- `event.action`;
- `event.outcome` when present;
- `source.ip`;
- `user.name`;
- `host.name`;
- `log.source_id` when present;
- correlation fingerprint fields when present.

All seven events MUST resolve before alert sealing can proceed.

## 14. Alert resolution

Alert resolution starts only after event resolution succeeds.

The Materializer MUST use HISIEM alert APIs and MUST validate candidate alerts against the current run. Candidate selection MUST include the expected detection rule and attack entity and SHOULD include current-run time and related-event constraints where available.

Resolution semantics:

```text
0 valid candidates  → continue bounded polling
1 valid candidate   → resolve
>1 valid candidates → AMBIGUOUS_SOURCE_ALERT
```

The Materializer MUST NOT resolve ambiguity by selecting the newest alert, the highest risk score, or an arbitrary first result.

After selecting a candidate, it MUST read the alert detail using the resolved addressing identifier and verify the same invariants again.

## 15. Alert stability barrier

The first visible alert is not necessarily stable when the detection pipeline may continue updating the same logical alert.

Before verification, the Materializer MUST establish a bounded stability barrier. A suitable implementation is repeated reads until a stable fingerprint is observed for a configured number of consecutive observations.

The fingerprint SHOULD contain:

```text
address_id
rule_id
source/entity
event_count
related-event identity set
status
```

If stability cannot be established before the configured deadline:

```text
ALERT_NOT_STABLE
```

The dataset MUST NOT be verified or sealed.

## 16. Dataset verification

The Materializer MUST produce `VerifiedDataset` only when all mandatory invariants hold.

### 16.1 Event invariants

```text
count(F1..F5) = 5

∀F:
  action = authentication_failure
  same source
  same account
  same host

S1:
  action = authentication_success
  same source
  same account
  same host
  timestamp > max(failure timestamps)

W1:
  classification = WATERMARK_CONTROL
  source != attack source
```

### 16.2 Detection invariants

```text
source alert exists
source alert rule matches GP-01 rule
source alert entity matches attack entity
source alert represents the required failure threshold
```

If HISIEM exposes related-event references, the verifier SHOULD cross-check them against the resolved failure event references.

### 16.3 Addressing invariant

```text
source_alert.address_id == actual HISIEM alert API addressing id
```

### 16.4 Isolation invariant

Resolved provider resources MUST belong unambiguously to the current materialization identity and MUST NOT mix events from another run.

## 17. Materialization draft

The current run SHOULD maintain a mutable local run ledger:

```text
.eval-runs/
  gp-01/
    <run_id>/
      materialization.json
```

The draft records state, attempted injections, resolution progress, and failure diagnostics required for resume/reconciliation.

It is not an evaluation manifest and MUST NOT be consumed by the scorer as ground truth.

Generated `.eval-runs/` artifacts SHOULD be excluded from Git.

## 18. Resume semantics

A resume operation may:

- read `materialization.json`;
- query HISIEM for already-attempted resources;
- complete missing event resolution;
- complete alert resolution;
- re-run dataset verification.

A resume operation MUST NOT automatically re-inject previously attempted events.

## 19. Error taxonomy

The implementation SHOULD expose typed failures equivalent to:

```text
PreflightError
RuleContractMismatch
RunIdentityCollision
EventInjectionError
InjectionOutcomeIndeterminate
EventResolutionTimeout
AmbiguousEventError
AlertResolutionTimeout
AmbiguousSourceAlertError
AlertNotStableError
DatasetInvariantViolation
```

Evaluation failures MUST preserve enough bounded structured context for diagnosis without persisting secrets or raw credential material.

## 20. Package boundary

Recommended package shape:

```text
src/hisiem_soc_copilot/evaluation/
├── contracts.py
├── scenario_loader.py
├── identity.py
├── time_plan.py
├── injector.py
├── hisiem_reader.py
├── materializer.py
├── verifier.py
└── cli.py
```

The evaluation package may depend on production public contracts and adapters. Production domain, application, graph, and provider packages MUST NOT depend on evaluation oracle code.

## 21. Test contract

Default unit and integration tests MUST NOT mutate a real HISIEM deployment.

Real materialization tests MUST be explicitly enabled, for example with:

```text
RUN_HISIEM_DATASET_EVAL=1
```

The live GP-01 Materializer test MUST prove:

1. `F1..F5` resolve to real HISIEM event documents;
2. `S1` resolves to a real success event;
3. `W1` resolves as an independent control event;
4. the real SSH brute-force alert appears;
5. the alert reference uses the actual HISIEM addressing `_id`;
6. the alert correlates unambiguously to the current run;
7. `DatasetVerifier` returns `VerifiedDataset`.

Unit tests MUST cover at least:

- deterministic time planning;
- failure-window invariants;
- `S1` ordering;
- `W1` entity separation;
- year-boundary rejection;
- deterministic identity binding per `run_id`;
- fixed injection order;
- no retry after indeterminate TCP outcome;
- resume without injection;
- ambiguous event rejection;
- ambiguous alert rejection;
- prohibition on deriving `address_id` from `alert.id`;
- verification failure when `S1` does not match the attack entity.

## 22. Completion gate

E1-B.3 is complete only when the real environment demonstrates:

```text
Logical GP-01
→ real SSH-log ingestion
→ seven resolved HISIEM events
→ real SSH brute-force alert
→ exact real alert addressing id
→ DatasetVerifier PASS
→ VerifiedDataset
```

A script completing without these proofs is not sufficient to declare the stage complete.
