# Documentation Map

This directory holds the authoritative technical documentation for HISIEM SOC Copilot.
**You should never have to guess which document to open first.** Find your goal below.

> **New here?** Read the [README](../README.md) first, then [`guide/`](guide/) — a
> project-first walkthrough in four documents. Come back to
> [`architecture-overview.md`](status/architecture-overview.md) (eight figures) for the visual
> model, and to this page when you want the detail behind one specific area.

---

## Directory map

| Directory | What it holds | Read it when |
| --- | --- | --- |
| `README.md` | this map | first visit |
| [`status/`](status/) | **what is true now**: product positioning, the architecture overview and the two canonical diagrams | you want the current picture |
| [`contracts/`](contracts/) | **what must hold**: domain model, persistence schema, package boundaries, the tool/MCP contract, the provider contract, the upstream integration boundary, the workspace projection, observability, and V1 scope | you are changing behaviour or an interface |
| [`operations/`](operations/) | how to bring the local runtime up | you need to run it |
| [`knowledge/`](knowledge/) | the knowledge subsystem: domain, retrieval contract, security boundary, operator procedure | you are working on knowledge |
| [`evaluation/`](evaluation/) | the four evaluation responsibilities: closure, knowledge baseline, dataset materialisation, manifest sealing | you are working on evaluation |
| [`guide/`](guide/) | project-first introduction | you are new to the project |
| [`evidence/`](evidence/) | **code-level forensics**: every claim carries a `file:line` | you want to check "is the code really like this?" |
| [`interview/`](interview/) | interview review material | — |

## Start here

| Document | Read it for | Size |
|---|---|---|
| [`guide/`](guide/) | **New here? Start with this.** Project-first walkthrough: what problem this decision layer solves, one investigation end to end, and why the agent cannot authorize itself. Four documents | 4 docs |
| [`architecture-overview.md`](status/architecture-overview.md) | The visual model — investigation/authority chain, tool & evidence path, MCP admission, knowledge path, durable execution, truth boundaries, evaluation, cross-repository boundary | 8 figures |
| [`architecture-diagrams.md`](status/architecture-diagrams.md) | The two canonical full-size diagrams — plane-oriented architecture graph, and the end-to-end investigation data flow | 2 diagrams |
| [`product-positioning.md`](status/product-positioning.md) | What the product is and is not; target users and boundaries *(Chinese)* | 11 KB |
| [`product-scope.md`](contracts/product-scope.md) | The user journey, scope and non-goals, and the definition of done | 17 KB |
| [`interview/INTERVIEW_GUIDE.md`](interview/INTERVIEW_GUIDE.md) | Review material: from a 30-second introduction down to deep follow-ups | large |

---

## Architecture

| Document | Read it for | Size |
|---|---|---|
| [`python-package-boundary.md`](contracts/python-package-boundary.md) | The layer model — domain purity, application ports/UoW, agent orchestration, transport-only API — and the dependency direction rules | 22 KB |
| [`application-commands-domain-events-langgraph-state.md`](contracts/application-commands-domain-events-langgraph-state.md) | Commands, domain events, and how LangGraph state relates to (and is *not*) domain state | 35 KB |
| [`model-provider-contract.md`](contracts/model-provider-contract.md) | The provider contract: bounded invocation, structured output, typed failures, fail-closed behaviour | 14 KB |
| [`investigation-workspace.md`](contracts/investigation-workspace.md) | The analyst workspace projection contract | 9 KB |
| [`evidence/architecture-analysis/README.md`](evidence/architecture-analysis/README.md) | **Code-level evidence layer** (9 documents): per-subsystem `file:line` forensics, the counter-intuitive shapes, and the boundaries as they actually are | — |

> The evidence layer is **not** authority. It answers "is the code really like this?" —
> the contracts above answer "what must the code be?". When they disagree, the contracts
> govern and the code is the final fact; the evidence document is then the thing that needs
> updating.

---

## Domain & Persistence

| Document | Read it for | Size |
|---|---|---|
| [`domain-model.md`](contracts/domain-model.md) | Aggregates, entities, value objects, invariants — the pure core | 28 KB |
| [`persistence-schema.md`](contracts/persistence-schema.md) | Tables, constraints, indexes, and the ORM↔domain mapping; optimistic locking | 38 KB |

---

## Knowledge

| Document | Read it for |
|---|---|
| [`knowledge/domain.md`](knowledge/domain.md) | The knowledge domain: documents, versions, immutable content chunks |
| [`knowledge/retrieval-contract.md`](knowledge/retrieval-contract.md) | The typed retrieval surface: queries, hits, ranking, the mandatory tenant scope |
| [`knowledge/security-boundary.md`](knowledge/security-boundary.md) | The knowledge security boundary and the model-reachable surface |
| [`evaluation/knowledge-evaluation-contract.md`](evaluation/knowledge-evaluation-contract.md) | The `KB-GOLDEN-V1` baseline: corpus, modes, scoring, preconditions |
| [`knowledge/operations.md`](knowledge/operations.md) | Operator procedure: provisioning, pgvector, ingest, search, evaluation |

> **Read the honest gap first:** no embedding provider is configured in this repository, so
> the semantic half of the hybrid gate has never been exercised. What the hybrid evaluation
> proves is *wiring* — fusion, ranking, tie-breaking, citation resolution and scoring — not
> retrieval quality. Artifacts are labelled `PLUMBING_ONLY`.
> See [`evaluation/knowledge-evaluation-contract.md`](evaluation/knowledge-evaluation-contract.md).

---

## Capability & MCP

| Document | Read it for |
|---|---|
| [`investigation-tool-contract.md`](contracts/investigation-tool-contract.md) | The tool contract: the model-selectable surface, argument and result contracts, typed failures |
| [`hisiem-integration-contract.md`](contracts/hisiem-integration-contract.md) | The boundary to HISIEM: what is read, how, and under what tenant/authority rules |

**The model-selectable tool surface is exactly four read-only tools**, plus one
system-controlled tool the model can never call. Unknown servers, unadmitted dynamic tools,
write capabilities and schema drift all fail closed. Exact set and rules:
[`investigation-tool-contract.md`](contracts/investigation-tool-contract.md) §3–§4 — the contract is the authority; the root README
only summarises it.

---

## Observability

| Document | Read it for |
|---|---|
| [`observability.md`](contracts/observability.md) | Spans, context propagation across the durable boundary, metric label safety, and the telemetry data policy |

**The truth boundary:** no business path reads spans, trace ids or collector state. Stopping
the collector mid-run produces an identical persisted business outcome.

---

## Evaluation

| Document | Read it for |
|---|---|
| [`evaluation/gp-01-dataset-materializer.md`](evaluation/gp-01-dataset-materializer.md) | How the GP-01 scenario becomes real HISIEM resources |
| [`evaluation/gp-01-manifest-sealer.md`](evaluation/gp-01-manifest-sealer.md) | Manifest sealing — the pinned, reproducible evaluation input |
| [`evaluation/evaluation-closure-contract.md`](evaluation/evaluation-closure-contract.md) | The closure: deterministic scorer, bounded repeatability, suite aggregation |
| [`evaluation/knowledge-evaluation-contract.md`](evaluation/knowledge-evaluation-contract.md) | `KB-GOLDEN-V1` — the knowledge baseline |

**Cross-plane acceptance (XP-01)** — 29 scenarios, 13 non-compensating hard gates, one
deterministic aggregate — is described in the [README §12](../README.md#12-evaluation) and
evidenced by the gates themselves in `tests/`.

---

## Runtime Verification

| Document | Read it for |
|---|---|
| [`local-integrated-runtime.md`](operations/local-integrated-runtime.md) | Bringing up the full local topology: ports, profiles, launchers, the two-process worker setup |

---

## Engineering History

The engineering-process record — stage specs, acceptance reports, execution prompts — was
**deleted on 2026-10-06**. It recorded how the work was sequenced (what was tried, in what
order, what passed), not the designs themselves, and had no independent reader value.
Nothing was lost: the current rules are in `contracts/`, the current state and risks are in
`status/`, and the acceptance evidence is the gates themselves in `tests/`. Use git when you
need a historical version.

Stage terminology ("Stage A–E", "E1-C2") therefore no longer appears anywhere in this
documentation; if you meet it in code comments, it is internal engineering history, not the
product model.

---

## Conventions in this documentation

- Documents describe **current** behaviour and state **implemented / verified / not
  implemented / out of scope** explicitly rather than implying it.
- Where a boundary exists, the boundary is written down — including the ones that cost
  something.
- Architecture and persistence documents are authority: implementation follows them, and
  `tests/architecture/` enforces the layer rules mechanically. **A recorded divergence does
  not make the document wrong** — the norm stands, and the gap is filed as an Implementation
  Gap in the affected document instead of being smoothed over. See
  [`persistence-schema.md`](contracts/persistence-schema.md) §4 for the one currently open case
  (the spec requires timezone-aware `TIMESTAMPTZ`; the ORM layer is naive throughout).
- Acceptance artifacts are machine-readable and produced by real runs; nothing is
  transcribed from a narrative.
