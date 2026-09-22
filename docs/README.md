# Documentation Map

This directory holds the authoritative technical documentation for HISIEM SOC Copilot.
**You should never have to guess which document to open first.** Find your goal below.

> **New here?** Read the [README](../README.md) first, then
> [`architecture-overview.md`](architecture-overview.md) (eight figures). This page is for
> when you want the detail behind one specific area.

---

## Start here

| Document | Read it for | Size |
|---|---|---|
| [`architecture-overview.md`](architecture-overview.md) | The visual model — investigation/authority chain, tool & evidence path, MCP admission, knowledge path, durable execution, truth boundaries, evaluation, cross-repository boundary | 8 figures |
| [`architecture-diagrams.md`](architecture-diagrams.md) | The two canonical full-size diagrams — plane-oriented architecture graph, and the end-to-end investigation data flow | 2 diagrams |
| [`product-positioning.md`](product-positioning.md) | What the product is and is not; target users and boundaries *(Chinese)* | 11 KB |
| [`v1-user-flow-and-scope.md`](v1-user-flow-and-scope.md) | The user journey and what was in scope | 18 KB |
| [`interview/INTERVIEW_GUIDE.md`](interview/INTERVIEW_GUIDE.md) | Review material: from a 30-second introduction down to deep follow-ups | large |

---

## Architecture

| Document | Read it for | Size |
|---|---|---|
| [`python-package-boundary.md`](python-package-boundary.md) | The layer model — domain purity, application ports/UoW, agent orchestration, transport-only API — and the dependency direction rules | 22 KB |
| [`application-commands-domain-events-langgraph-state.md`](application-commands-domain-events-langgraph-state.md) | Commands, domain events, and how LangGraph state relates to (and is *not*) domain state | 35 KB |
| [`model-provider-contract.md`](model-provider-contract.md) | The provider contract: bounded invocation, structured output, typed failures, fail-closed behaviour | 14 KB |
| [`investigation-workspace.md`](investigation-workspace.md) | The analyst workspace projection contract | 9 KB |

---

## Domain & Persistence

| Document | Read it for | Size |
|---|---|---|
| [`domain-model.md`](domain-model.md) | Aggregates, entities, value objects, invariants — the pure core | 28 KB |
| [`persistence-schema.md`](persistence-schema.md) | Tables, constraints, indexes, and the ORM↔domain mapping; optimistic locking | 38 KB |

---

## Knowledge

| Document | Read it for |
|---|---|
| [`p3/knowledge-domain.md`](p3/knowledge-domain.md) | The knowledge domain: documents, versions, immutable content chunks |
| [`p3/retrieval-contract.md`](p3/retrieval-contract.md) | The typed retrieval surface: queries, hits, ranking, the mandatory tenant scope |
| [`p3/security-boundary.md`](p3/security-boundary.md) | The knowledge security boundary and the model-reachable surface |
| [`p3/evaluation-contract.md`](p3/evaluation-contract.md) | The `KB-GOLDEN-V1` baseline: corpus, modes, scoring, preconditions |
| [`p3/p3-a-operations.md`](p3/p3-a-operations.md) | Operator procedure: provisioning, pgvector, ingest, search, evaluation |

> **Read the honest gap first:** no embedding provider is configured in this repository, so
> the semantic half of the hybrid gate has never been exercised. What the hybrid evaluation
> proves is *wiring* — fusion, ranking, tie-breaking, citation resolution and scoring — not
> retrieval quality. Artifacts are labelled `PLUMBING_ONLY`.
> See [`p3/evaluation-contract.md`](p3/evaluation-contract.md).

---

## Capability & MCP

| Document | Read it for |
|---|---|
| [`investigation-tool-contract.md`](investigation-tool-contract.md) | The tool contract: the model-selectable surface, argument and result contracts, typed failures |
| [`hisiem-integration-contract.md`](hisiem-integration-contract.md) | The boundary to HISIEM: what is read, how, and under what tenant/authority rules |

**The model-selectable tool surface is exactly four read-only tools**, plus one
system-controlled tool the model can never call. Unknown servers, unadmitted dynamic tools,
write capabilities and schema drift all fail closed. Exact set and rules: `README §7` and
[`investigation-tool-contract.md`](investigation-tool-contract.md).

---

## Observability

| Document | Read it for |
|---|---|
| [`observability.md`](observability.md) | Spans, context propagation across the durable boundary, metric label safety, and the telemetry data policy |

**The truth boundary:** no business path reads spans, trace ids or collector state. Stopping
the collector mid-run produces an identical persisted business outcome.

---

## Evaluation

| Document | Read it for |
|---|---|
| [`evaluation/gp-01-dataset-materializer.md`](evaluation/gp-01-dataset-materializer.md) | How the GP-01 scenario becomes real HISIEM resources |
| [`evaluation/gp-01-manifest-sealer.md`](evaluation/gp-01-manifest-sealer.md) | Manifest sealing — the pinned, reproducible evaluation input |
| [`evaluation/evaluation-closure-contract.md`](evaluation/evaluation-closure-contract.md) | The closure: deterministic scorer, bounded repeatability, suite aggregation |
| [`p3/evaluation-contract.md`](p3/evaluation-contract.md) | `KB-GOLDEN-V1` — the knowledge baseline |

**Cross-plane acceptance (XP-01)** — 29 scenarios, 13 non-compensating hard gates, one
deterministic aggregate — is described in the [README §12](../README.md#12-evaluation) and
evidenced in [`stage-reports/`](stage-reports/).

---

## Runtime Verification

| Document | Read it for |
|---|---|
| [`local-integrated-runtime.md`](local-integrated-runtime.md) | Bringing up the full local topology: ports, profiles, launchers, the two-process worker setup |

---

## Stage & Engineering History

| Document | Read it for |
|---|---|
| [`stage-contracts/README.md`](stage-contracts/README.md) | **The inputs** — the frozen Four-Plane v1.0 contract, the Stage A–E implementation specs, and the execution prompts, now in-repo |
| [`stage-reports/index.md`](stage-reports/index.md) | **Start here for process context** — what the stages were, the reading order, and the consolidated known-items register |
| [`stage-reports/`](stage-reports/) | The full acceptance evidence: E0 gap audit → E7 final seal, plus the full runtime E2E report |

> Stage terminology is internal engineering history, not the product model. Read it for the
> evidence behind a claim, not as an introduction. The same applies to `stage-contracts/`:
> where its frozen 2026-09-14 baseline disagrees with the code, **the code wins**.

---

## Conventions in this documentation

- Documents describe **current** behaviour and state **implemented / verified / not
  implemented / out of scope** explicitly rather than implying it.
- Where a boundary exists, the boundary is written down — including the ones that cost
  something.
- Architecture and persistence documents are authority: implementation follows them, and
  `tests/architecture/` enforces the layer rules mechanically.
- Acceptance artifacts are machine-readable and produced by real runs; nothing is
  transcribed from a narrative.
