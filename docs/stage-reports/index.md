# Stage Reports — Engineering Process and Acceptance Evidence

These documents are the **verification record** for the engineering process that built this
system. They are kept in full because they are evidence: each one records what was built,
what was run, what passed, and what was left open.

> **Stage terminology is internal engineering history, not the product model.**
> If you are new to this repository, start with the
> [README](../../README.md) and
> [`docs/architecture-overview.md`](../architecture-overview.md). Come back here when you
> want the evidence behind a specific claim.

---

## What the stages were

The work was organised as a sequence of stages, each closing a well-defined surface before
the next began. The reporting convention is the part worth explaining: **every stage
reports against a frozen contract, with machine-readable acceptance artifacts, and every
acceptance gate must be falsifiable** — a gate that cannot fail is not evidence.

| Stage | Scope | Outcome |
|---|---|---|
| **A** | Knowledge closure validation — validate and harden the retrieval path | Closed |
| **B** | Observability foundation — OTel, context propagation, metrics, log correlation, Collector baseline | Closed |
| **C** | Capability / MCP — tool provider architecture and governed read-only MCP V1 | Closed |
| **D** | Analyst experience — investigation workspace productization | Closed; the full runtime E2E gate passed |
| **E** | Cross-plane integration and evaluation — the acceptance pack | Sealed |

Stage C and Stage D could overlap only once the relevant frozen contracts were stable and
shared boundaries had single ownership.

**Stage E** is the one that produced the cross-plane acceptance pack described in the
[README §12](../../README.md#12-evaluation): 29 scenarios, 13 non-compensating hard gates,
9 families, aggregated into one deterministic `cross-plane-suite-results/v1` artifact. It
was implemented without changing any HISIEM production code and without changing this
repository's production layers — the invariants were made testable *without* touching the
systems under test.

---

## Recommended reading order

Read the reports in order if you want the full process; read them individually if you want
the evidence for one claim.

| # | Report | Read it for |
|---|---|---|
| 1 | [E0 — Current state & gap audit](STAGE_E_E0_CURRENT_STATE_GAP_AUDIT.md) | The honest starting position: what existed, what the gaps were |
| 2 | [E1 — XP-01 contract & gate model](STAGE_E_E1_XP01_CONTRACT_GATE_MODEL_REPORT.md) | How an acceptance pack is designed: the 29-scenario catalog, the 13 gates, the artifact contract |
| 3 | [E2 — Knowledge, capability, security](STAGE_E_E2_KNOWLEDGE_CAPABILITY_SECURITY_REPORT.md) | 15 scenarios across four families |
| 4 | [E3 — Authority & reliability](STAGE_E_E3_AUTHORITY_RELIABILITY_REPORT.md) | 10 scenarios — the authority and durable-execution invariants |
| 5 | [E4 — Observability acceptance](STAGE_E_E4_OBSERVABILITY_ACCEPTANCE_REPORT.md) | 2 scenarios, including the collector-outage equivalence proof |
| 6 | [E5 — Analyst workspace acceptance](STAGE_E_E5_ANALYST_WORKSPACE_ACCEPTANCE_REPORT.md) | 2 scenarios plus real-browser acceptance against the real frontend module |
| 7 | [E6 — Cross-plane aggregation](STAGE_E_E6_CROSS_PLANE_AGGREGATION_REPORT.md) | How 29 results become one non-compensating aggregate |
| 8 | [E7 — Final seal](STAGE_E_E7_FINAL_SEAL_REPORT.md) | The final validation, diff classification, and known-item register |
| — | [Full runtime E2E test report](FULL_RUNTIME_E2E_TEST_REPORT.md) | The Stage D runtime baseline: both projects running for real, cross-project gate |

**If you read only two:** the **E1** report (how the acceptance model is designed) and the
**E7** seal (the complete final validation and the honest list of what remains open).

---

## What these reports are careful about

A few conventions are worth knowing, because they are the reason the reports are long:

- **Machine-readable evidence, not prose.** Every scenario result is a
  `cross-plane-gate-results/v1` artifact produced by a real run. Nothing is transcribed from
  a narrative summary.
- **The negative half is tested.** For each gate, a directly invalid measurement has been
  demonstrated to *fail* it. Where the aggregate is non-compensating, that is proven against
  the real artifacts — dropping a scenario, or degrading one to a valid FAIL, fails the suite.
- **Runtime evidence is never substituted.** Where a scenario needs real processes, it runs
  against them — and it *also* keeps its own deterministic artifact in the aggregate.
- **Known items are recorded, not hidden.** Each stage carries an explicit non-blocker table
  with an ID, a justification, and a follow-up owner.
- **Production diffs are audited.** Each report states the production-layer and frontend
  diffs, and E7 classifies every changed path.

---

## Known non-blocking items

These are recorded across the reports and consolidated here for visibility. None is blocking;
each has a stated justification.

| ID | Status | What it is |
|---|---|---|
| `OBS-001` | Open / non-blocking | Seven optional span operations from the broader taxonomy are not emitted; acceptance runs on the operations the runtime really traverses |
| `DEFECT-005` | Open / low / non-blocking | MCP provider failure classification granularity — a bounded typed failure with no false-success Evidence |
| `TEST-INFRA-001` | Open / test-infrastructure only | Hardening of the DB-backed test skip guard |
| `EVAL-SEAM-001` | Accepted / non-blocking | The single sanctioned evaluation bridge between the evaluation package and the harness; needed no allowlist change |
| `E4-OBS-01` / `E4-OBS-02` | Accepted / non-blocking | The E4 runtime slice scripts part of the upstream read path; `llm.call` is emitted only when a real provider run is in the slice |
| `E5-OBS-01/02/03` | Accepted / non-blocking | Authority class is a frontend derivation from persisted source types; the execution plane is the local submission state before any provider execution |
| `E6-OBS-01` | Accepted / non-blocking | The aggregate is produced from a real acceptance run, never synthesized |
| `P3-A` embedding | Open | No embedding provider is configured; semantic retrieval quality is unmeasured and artifacts are labelled `PLUMBING_ONLY` |

---

## Scope note

These reports describe the **engineering process**. The product architecture is described in
the [README](../../README.md) and
[`docs/architecture-overview.md`](../architecture-overview.md); the domain and persistence
contracts are in [`docs/domain-model.md`](../domain-model.md) and
[`docs/persistence-schema.md`](../persistence-schema.md).
