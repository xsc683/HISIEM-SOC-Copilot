# HISIEM SOC Copilot — Interview Guide

**Purpose:** private review material. Every technical claim here is grounded in the code and
documents at `capability-mcp`. Where the implementation has a boundary or a gap, it is stated.

**How to use it.** §1–§3 are the spoken introductions and the boundary. §4 is the code map.
§5–§42 are the topic-by-topic review, in the format *concept → actual implementation → why →
trade-off → follow-up*. §43–§44 are code paths and failure walkthroughs. §45–§49 are design
decisions, non-goals and limits. §50–§51 are question banks.

---

## 1. 30-second introduction

> HISIEM SOC Copilot is an AI investigation layer for a SIEM. A security alert triggers an
> agent investigation that calls a fixed allowlist of four read-only tools, normalizes every
> grounded result into traceable Evidence, and produces a verdict with a response proposal.
> High-risk responses can't be executed by the model: policy constrains them, a human
> approves them, and the approval is durably recorded as an idempotent command that HISIEM's
> SOAR engine executes. Copilot then observes the real execution result and projects it back
> into an analyst workspace.
>
> The engineering thesis is that you can build a genuinely useful security agent **without**
> giving it authority — every place a typical agent demo lets the model act, this system has
> a boundary instead, and each boundary is covered by a falsifiable acceptance gate.

---

## 2. 3-minute introduction

**The problem.** SOC analysts triage alerts by hand: pull context, search logs, check
detection rules, look up runbooks, form a conclusion, write it up. Most of that is
information gathering and context switching. An LLM could help a lot — but an agent that
can *act* on a security platform is a liability, because a prompt injection or a
hallucination becomes a disabled account or an isolated host.

**The design response.** Split the problem: let the model do the reasoning and the
information gathering, and remove it from every decision that carries authority.

**The lifecycle.** An alert starts an investigation. A LangGraph graph runs bounded steps:
hydrate the alert (system-controlled, not model-chosen), then let the model pick from four
read-only tools. Tool results pass through a normalizer into immutable Evidence — and a
typed failure produces **no** Evidence, so a broken call can't be laundered into a fact.
Findings and a verdict come out, and the verdict can produce a response proposal. Policy
then decides `DENY` or `REQUIRE_APPROVAL`. If approval is required, a human decides; the
decision binds to a revision and hash, so a stale approval can't authorize changed intent.
An approval writes a durable outbox event, and a dispatcher submits it idempotently to
HISIEM SOAR. Submission and execution are **separate state machines** — `SUBMITTED` is not
`SUCCEEDED` — and an uncertain outcome becomes `ATTENTION_REQUIRED`, never a fabricated
terminal result. The final truth is what HISIEM observed.

**The parts I'd want to be asked about:**

1. **Authority separation.** Nine distinctions (ToolResult≠Evidence, Policy≠Human Approval,
   Submission≠Execution Success, …) each enforced in code and gated by acceptance scenarios.
2. **Durable execution.** Outbox, idempotency key, bounded retry, explicit uncertainty state,
   TOCTOU-protected approvals.
3. **MCP governance.** Discovery, admission and selection are three different things. The
   model-selectable surface is exactly four read-only tools; writes are designed away, not
   sandboxed.
4. **Evaluation.** A 29-scenario, 13-gate, non-compensating cross-plane acceptance pack with
   machine-readable artifacts — including its own falsifiability.

---

## 3. Project boundary: what it is / is not

| It is | It is not |
|---|---|
| AI-assisted investigation | A SIEM — HISIEM owns security data and detection |
| Evidence-driven (every finding traces to Evidence) | A SOAR — HISIEM SOAR executes |
| Governed (deterministic read-only tool allowlist) | An autonomous SOC — no path lets the model authorize |
| Human-authority gated | A chatbot or generic security Q&A |
| Durable (outbox, idempotent, retryable) | A multi-agent system — one agent, deliberately |

**The permanent rule:**

```text
Model proposes  →  Policy constrains  →  Human authorizes
Durable command records intent  →  HISIEM executes  →  Copilot observes
```

**Likely follow-up:** *"Why single-agent?"* → Every additional agent multiplies the
authority surface and makes tool selection harder to reason about. The constraints here are
about *what the model may do*, and one bounded agent with a 4-tool surface is far easier to
defend than a team of agents negotiating. Multi-agent is a deliberate non-goal (§47).

---

## 4. Repository / package map

```text
src/hisiem_soc_copilot/
├── config.py                     single typed-settings entry point
├── main.py                       process entrypoint (config → container → uvicorn)
├── domain/                       PURE (no FastAPI/SQLAlchemy/LangGraph/httpx/pydantic)
│   ├── investigation/            Investigation aggregate + state machine
│   ├── response/                 ResponseProposal + policy + approval
│   ├── knowledge/                knowledge documents/versions/chunks
│   └── shared/
├── application/                  commands, queries, handlers, ports, services
│   ├── commands/                 investigation | knowledge | response
│   ├── handlers/                 investigation | response | workflow | durable_support | knowledge | attack_import
│   ├── ports/                    16 ports (repositories, uow, hisiem, model_provider, durable, soar, trust, …)
│   ├── queries/                  investigation | workspace
│   └── services/                 investigation_service | knowledge_retrieval | workspace_service | attack_projection | …
├── contracts/                    boundary schemas (API/LLM/Tools) — Pydantic lives here
├── agent/                        LangGraph orchestration (NOT business authority)
│   ├── graph/                    state | nodes | builder | runtime | budget | tool_audit
│   ├── tools/                    registry | policy | args | executor | providers | native_provider | provider_router
│   ├── evidence/normalizer.py    ToolResult → immutable Evidence
│   └── knowledge/catalog.py      model-facing knowledge tool adapter
├── api/                          FastAPI transport only (+ routers/, schemas/)
├── infrastructure/               adapters
│   ├── persistence/  auth/  checkpoint/  embedding/  hisiem/  knowledge/
│   ├── durable/                  dispatcher | investigation_runner | response_runner
│   ├── llm/  mcp/provider.py  messaging/  observability/  soar/  threat_intel/
└── bootstrap/container.py        Composition Root
```

**The densest files to know:**

| File | Why |
|---|---|
| `agent/tools/registry.py` | The exact model-selectable allowlist and the forbidden list |
| `agent/tools/executor.py` | The provider routing seam and result shaping |
| `agent/evidence/normalizer.py` | Where a tool result becomes (or fails to become) Evidence |
| `agent/graph/nodes.py` | The investigation steps |
| `infrastructure/durable/dispatcher.py` | Outbox lease/reclaim/retry semantics |
| `infrastructure/durable/response_runner.py` | Submit and observe runners |
| `application/services/knowledge_retrieval.py` | Hybrid retrieval and ranking |
| `domain/response/` | Policy and approval invariants |

---

## 5. Investigation lifecycle

**Concept.** An investigation is an aggregate with a state machine, not a chat session.

**Implementation.** `CREATED → RUNNING → {COMPLETED, FAILED, CANCELLED}`. The **response
workflow is an independent post-completion aggregate lifecycle** — a response proposal is
not a state of the investigation. One Active Investigation per Tenant + Alert is enforced by
a **partial unique index**, so a duplicate start converges at the database rather than
depending on a check-then-act race in application code.

**Why.** Two design points: (1) separating response from investigation means an investigation
that finished can still produce, and be judged on, a response later; (2) pushing the
one-active-investigation rule into a partial unique index makes it race-proof.

**Alternative.** A single aggregate holding both would couple investigation completion to
response progress and make "reopen the proposal" mutate a closed investigation.

**Trade-off.** Two lifecycles means two things to reason about, and the workspace has to
compose them. That composition is exactly what the workspace projection does.

**Reference.** `domain/investigation/`, `domain/response/`, `docs/domain-model.md`,
`docs/persistence-schema.md`.

**Follow-up:** *"How do you prevent two concurrent starts for the same alert?"* → The partial
unique index; the second insert fails and the handler returns the existing investigation.

---

## 6. Domain model

**Concept.** Business rules live in a pure domain layer with no framework dependencies.

**Implementation.** `domain/` contains aggregates, entities, value objects, domain events and
invariants. **No FastAPI, SQLAlchemy, LangGraph, httpx or Pydantic** — enforced by
`tests/architecture/`, not by convention. Persistence uses explicit ORM↔domain mappers rather
than mapping domain classes directly.

**Why.** Two payoffs: domain invariants can be unit-tested with no database and run fast; and
the domain cannot accidentally acquire a dependency that makes it untestable or
transport-coupled.

**Trade-off.** Mappers are boilerplate — the code you write by hand instead of getting from an
ORM. The return is that the persistence model and the domain model can evolve independently
(e.g. a `jsonb` column shape is not the aggregate shape).

**Reference.** `docs/domain-model.md`, `docs/python-package-boundary.md`,
`tests/architecture/test_import_boundaries.py`.

**Follow-up:** *"How is purity actually enforced?"* → An architecture test walks imports and
fails the build if a forbidden package appears in `domain/`.

---

## 7. LangGraph and the checkpoint boundary

**Concept.** The graph is orchestration and bounded working state. It is **not** the source of
business truth.

**Implementation.** The graph's state is a bounded cross-step working structure
(`agent/graph/state.py`). Checkpoints are persisted to the `langgraph_checkpoint` schema —
**owned and migrated by LangGraph**, separate from the `copilot` schema which Alembic owns,
with separate connections and migration owners.

**Why.** A checkpoint is a *resumability mechanism*. If it were read as domain state, then a
graph replay, a checkpoint restore, or a graph refactor could silently redefine an
investigation's business state. Keeping them separate means the investigation's truth is
whatever the domain aggregates persisted.

**Trade-off.** Resuming a graph requires re-deriving domain context rather than reading it out
of the checkpoint — slightly more work at resume, in exchange for a single truth source.

**Reference.** `agent/graph/`, `docs/application-commands-domain-events-langgraph-state.md`.

**Follow-up:** *"What if the checkpoint and the domain disagree?"* → The domain wins. The
checkpoint is working memory; it influences what the graph does next, not what the
investigation *is*.

---

## 8. Tool Registry

**Concept.** A deterministic allowlist — the exact set of tools the model may choose.

**Implementation.**

```python
AGENT_SELECTABLE_TOOLS = {
    "hisiem.search_events",
    "hisiem.get_detection_rule",
    "knowledge.retrieve_security_guidance",
    "knowledge.resolve_attack_technique",
}
SYSTEM_CONTROLLED_TOOL = "hisiem.get_alert_context"   # never offered to the model
FUTURE_CATALOG_TOOLS = {"hisiem.get_entity_activity", "threat_intel.lookup_ip"}  # not registered
```

Plus an explicit `FORBIDDEN_TOOLS` list naming the kinds of capabilities that must never
appear — `execute_shell`, `raw_http_request`, `write_alert`, `set_alert_verdict`,
`block_ip`, `isolate_host`, `start_soar_execution`, `approve_response`, and so on.

**Why.** The registry is the model's entire action space. Keeping it explicit, small and
tested means the question "what can the agent do?" has a one-line answer. Catalogued-but-
unimplemented tools are kept in a *separate* set so they are never registered — **the model
can never select a tool with no executor**, which is how agents end up hallucinating
capability.

**Trade-off.** Adding a tool requires an executor, a schema and a policy entry — deliberately
more friction than letting the model try.

**Reference.** `agent/tools/registry.py`, `tests/unit/agent/test_tool_surface.py`,
`tests/architecture/test_knowledge_boundary.py`.

**Follow-up:** *"Why is `get_alert_context` system-controlled?"* → The graph's hydrate step
needs alert context before the model has decided anything, so it is called directly. It is
removed from the model-visible surface so the model cannot spend a turn re-fetching it or
steer it.

---

## 9. Tool Policy

**Concept.** A deterministic gate that can deny a tool call before anything executes.

**Implementation.** `agent/tools/policy.py` evaluates a candidate against policy before the
executor does anything. **Policy DENY and exhausted budget return before any provider
invocation.**

**Why.** Ordering is the whole point. If policy ran *after* the provider call, a denied
capability would still have caused a side effect on a remote system. Short-circuiting means
denial is free of consequences.

**Trade-off.** Policy must be reachable without the side effect, which constrains what policy
can depend on.

**Reference.** `agent/tools/policy.py`, `agent/graph/nodes.py`.

**Follow-up:** *"Is policy the same as approval?"* → No. Policy is deterministic and
system-owned; approval is a human decision. `DENY` is not a rejection — they are different
facts from different authorities (§24, §25).

---

## 10. Tool Budget

**Concept.** A bound on how much a single investigation may spend in tool calls.

**Implementation.** `agent/graph/budget.py` tracks the budget; the executor checks it before
invoking a provider, so a runaway loop cannot produce unbounded external calls.

**Why.** An agent loop is unbounded by construction — the model decides when to stop. A budget
is what makes the worst case finite. It is enforced *before* the call, so an exhausted budget
pays no external cost.

**Trade-off.** A budget can cut off a legitimate investigation. The mitigation is that
exhaustion is an explicit, observable outcome rather than a silence.

**Follow-up:** *"What happens when the budget is exhausted?"* → The tool call does not happen
and the graph proceeds with what it has. It never fabricates a result.

---

## 11. Tool Executor

**Concept.** The single seam where a tool candidate becomes a provider invocation.

**Implementation.** `agent/tools/executor.py`:
- Looks up the tool and dispatches to the right path (native provider, MCP provider, or a
  knowledge catalog adapter).
- Injects the trusted invocation context — **tenant and actor come from the executor, not
  from the model's arguments**.
- Builds provider-side arguments, rejecting model-supplied tenant fields.
- Shapes the provider result into a bounded `ToolResult`.
- Handles a knowledge tool with no wired adapter as an **unavailable** result with an explicit
  reason, rather than an exception or a silent empty success.

**Why.** Concentrating the boundary here means the tenant-injection and result-shaping rules
exist once. `provider_router.py` selects the provider; the executor does not know whether a
capability came from native code or MCP.

**Trade-off.** One more layer between the model and the provider — worth it because that layer
is exactly where the trust boundary lives.

**Reference.** `agent/tools/executor.py`, `agent/tools/provider_router.py`,
`agent/tools/native_provider.py`.

**Follow-up:** *"How does an MCP tool look different to the model?"* → It does not. The model
sees a trusted internal spec; server, endpoint, transport and credential are invisible to it.

---

## 12. ToolResult vs Evidence

**Concept.** A tool response is data. Evidence is a grounded, provenance-bearing fact.

**Implementation.** Evidence is produced only through the normalizer, and only from a
**grounded success**. A typed failure produces **no Evidence**. Failure facts are recorded
distinctly: false-success evidence fires only when a typed failure *also* produced Evidence,
and "failure normalized as empty" fires only when the backend was unavailable and the status
was SUCCESS/NO_DATA.

**Why.** This is the single most important anti-hallucination boundary in an evidence-driven
agent. Without it, a broken call returning `{}` becomes "no findings", which becomes a
benign-looking verdict. With it, a broken call is visibly a broken call.

**Alternative.** Let the model interpret raw tool output. Cheaper to build; then the verdict's
grounding depends on the model's reading of an error string.

**Trade-off.** Every new result shape needs a normalizer path. The acceptance scenario
`XP-REL-001/002` exists to prove the failure half — a typed failure with zero Evidence.

**Reference.** `agent/evidence/normalizer.py`, gates `FORBIDDEN_FACTS_ABSENT`.

**Follow-up:** *"Give me a concrete failure mode this prevents."* → An MCP server is stopped.
The capability is admitted, the call fails. Without this boundary, the agent reports "no
matching events" and concludes the alert is benign. With it, the tool failure is a typed
failure with zero Evidence and the verdict cannot rest on it.

---

## 13. EvidenceNormalizer and provenance

**Concept.** One normalization path for every evidence source.

**Implementation.** `EvidenceNormalizer.normalize_provider_result(...)` takes the provider
result plus keyword-only `tool_call_id`, `provider` and `operation`, and produces immutable
Evidence rows with provenance. A `ProviderInvocationResult` carries a typed failure
classification; `tool_result_is_typed_failure` distinguishes a typed failure from a success.

**Why.** A single path means provenance cannot be forgotten for one source type, and Evidence
from an MCP tool and a native tool have the same shape and the same guarantees.

**Trade-off.** Sources with genuinely different shapes must be adapted into the common model —
which is the point, but it means a new source is a deliberate integration rather than a
passthrough.

**Reference.** `agent/evidence/normalizer.py`, `docs/investigation-tool-contract.md`.

---

## 14. Knowledge Plane

**Concept.** Retrieved security documentation supplies *supporting context* — never verdict
authority.

**Implementation.** Knowledge documents are versioned; content chunks are **immutable**. The
citation target is an immutable chunk, so a citation survives an embedding rebuild. An
ATT&CK authoritative projection has single-writer cutover: an `MITRE_ATTACK` document's
`active_version_id` moves only inside the cutover transaction, never via ordinary ingestion.

**Why.** Versioning plus immutability is what makes a citation *mean something over time*. If
chunks were mutable, a citation would silently point at different text after a re-ingest.

**Trade-off.** Immutability means storage grows with rebuilds; the benefit is that a citation
is a stable reference.

**Reference.** `docs/p3/knowledge-domain.md`, `domain/knowledge/`.

**Follow-up:** *"Why does the ATT&CK projection need single-writer cutover?"* → Because
serving an ATT&CK hit is a claim about an authoritative release. If ordinary ingestion could
move the pointer, a retrieval could mix two releases and the claim would be false.

---

## 15. FTS + pgvector + Hybrid Retrieval + RRF

**Concept.** Combine lexical precision with semantic recall, fused deterministically.

**Implementation.** Lexical channel: PostgreSQL **full-text search** over versioned chunks.
Semantic channel: **pgvector** similarity over embeddings. Fusion: **Reciprocal Rank Fusion**
over the two channels. `MAX_RESULT_LIMIT = 5`, and a request for more is **rejected, not
clamped**. Ranking is implemented as **plain functions over plain data** — pure and
deterministic except for the two repository calls and the embedding call it orchestrates.

**Why.** FTS and vector search fail differently: FTS misses paraphrase, vectors miss exact
identifiers (a CVE, a rule id). RRF rewards agreement between channels without needing
score calibration between two incomparable scales. And making ranking a pure function is what
makes a ranking reproducible and testable without a database.

**Alternative.** A dedicated vector database. Rejected: pgvector on the existing PostgreSQL
is enough at this scale and avoids a second truth store and a second operational system.

**Trade-off.** **Real semantic quality is unmeasured** — no embedding provider is configured,
so the hybrid evaluation proves wiring, not quality. Artifacts are labelled `PLUMBING_ONLY`.
State this before being asked.

**Reference.** `application/services/knowledge_retrieval.py`, `docs/p3/retrieval-contract.md`,
`docs/p3/evaluation-contract.md`.

**Follow-up:** *"Why RRF rather than weighted score fusion?"* → Weighted fusion needs scores
from different scales to be comparable, which for BM25 vs. cosine is a calibration problem
with no principled answer. RRF uses ranks, so it only needs agreement, and it is deterministic
and easy to test.

---

## 16. Citation validation

**Concept.** A citation must resolve to real, in-scope content.

**Implementation.** Citations are resolved and **revalidated** against persisted chunks. The
retrieval result exposes `citation_id` naming an immutable content chunk. Two gates exist:
`DANGLING_CITATION` (a citation that does not resolve) and `CROSS_INVESTIGATION_CITATION` (a
citation resolving outside the current investigation/tenant scope).

**Why.** Citations are the mechanism that makes an AI answer auditable. A citation that does
not resolve is worse than no citation — it looks like grounding. Cross-investigation
citations are a **tenant-isolation leak** wearing the costume of a reference.

**Trade-off.** Revalidation costs a lookup per citation; the alternative is trusting a
reference the model produced.

**Reference.** gates `DANGLING_CITATION`, `CROSS_INVESTIGATION_CITATION`;
`application/services/knowledge_retrieval.py`.

---

## 17. Knowledge authority boundary

**Concept.** Knowledge can inform; it cannot decide.

**Implementation.** Knowledge Evidence carries its own authority class — *supporting context* —
distinct from a platform fact. `KNOWLEDGE_ONLY_DEFINITIVE_VERDICT` is a hard gate: a
definitive verdict cannot rest on knowledge alone. `KnowledgeHit` carries **no** field that
could be read as a control signal (no `instructions`, `action`, `severity`, `authority`), and
that absence is asserted against a frozen field set.

**Why.** A runbook saying "block the IP" must not be able to authorise blocking the IP. The
frozen-field assertion is the strong version: adding such a field later fails a test rather
than quietly widening what retrieval can express.

**Trade-off.** A knowledge-only investigation cannot reach a definitive verdict — correct, but
it means the system must be able to say "I can't conclude" (`INCONCLUSIVE`).

**Reference.** gate `KNOWLEDGE_ONLY_DEFINITIVE_VERDICT`, `docs/p3/security-boundary.md`.

**Follow-up:** *"How does the workspace show this?"* → The frontend derives an authority class
from the persisted source type and renders knowledge with a different label — and the
acceptance harness executes the **real frontend module** rather than restating the mapping.

---

## 18. MCP discovery

**Concept.** Learning what a server claims to offer.

**Implementation.** The official MCP Python SDK with the Streamable HTTP transport. Discovery
completes all paginated tool listing, normalizes raw metadata, and computes a deterministic
**SHA-256 fingerprint** over the canonical external tool name plus the input schema and the
output schema (or an explicit absent marker).

**Why.** Discovery is untrusted input. Treating it as a *proposal* rather than a fact is what
makes the next step (admission) meaningful.

**Trade-off.** Pagination and normalization add code between the server and the registry; the
return is that the model never sees raw server metadata.

**Reference.** `infrastructure/mcp/provider.py`.

**Follow-up:** *"What happens to a newly discovered tool?"* → It is discovered but
**unadmitted** — visible to operators, not selectable by the model. Startup and configured
refresh rediscover and revalidate; missing admitted tools become unavailable; fingerprint
drift becomes `SCHEMA_MISMATCH`.

---

## 19. MCP admission

**Concept.** A hand-written, server-side declaration of what this system has decided to trust.

**Implementation.** An admission entry declares: internal name, trusted description, configured
server identity, external name, internal argument/result contracts, expected external schema
fingerprint, read-only/risk classification, tenant scope, and per-capability bounds. Unknown
servers, write/high-risk capabilities, and unadmitted dynamic tools **fail closed**.

**Why.** This is the difference between "the model can call whatever the server offers" and
"the model can call what we decided to trust." The trusted description is what the model
sees — not the server's self-description, which is attacker-influenced content.

**Alternative.** Auto-admit everything the server offers. That makes the server's contents
(and anyone who can influence them) part of the trust boundary.

**Trade-off.** Adding a capability requires a hand-written entry and an expected fingerprint —
friction by design.

**Reference.** `agent/tools/providers.py` (`AdmissionEntry`).

**Follow-up:** *"Why is the trusted description separate from the server's?"* → Because the
server's description is remote content, and remote content is untrusted data. A description
is a prompt surface: a malicious tool description can steer tool selection.

---

## 20. MCP schema fingerprint, protocol pinning, read-only authority

**Concept.** Three independent fail-closed controls.

**Implementation.**

| Control | Mechanism | Failure mode |
|---|---|---|
| Schema fingerprint | SHA-256 over canonical name + input schema + output schema | Drift → `SCHEMA_MISMATCH` |
| Protocol pinning | Production protocol version required | Downgrade → rejected, not accepted |
| Read-only authority | `is_model_selectable` requires `READ_ONLY` | Write capability → never selectable |
| Transport | Trusted configured endpoints; HTTPS unless explicitly trusted-internal | Unexpected redirect/host change → rejected |
| Bounds | Global + per-capability result bounds | Oversized → `RESULT_TOO_LARGE`, **never truncated** |

**Why.** Each control blocks a different attack. Fingerprinting means a server cannot silently
change what a tool does under an admitted name. Pinning means a negotiation cannot be
downgraded to a weaker protocol. Read-only classification means a write capability is not
merely discouraged — it is structurally unselectable.

**Trade-off.** A legitimate schema change becomes an explicit operator action (update the
expected fingerprint). That is the intended cost.

**Reference.** `infrastructure/mcp/provider.py`; gates `UNADMITTED_MCP_SELECTED`,
`WRITE_MCP_SELECTED`.

**Follow-up:** *"Why reject rather than truncate an oversized result?"* → A truncated result
is a silently incomplete fact that still looks like a complete one. An explicit
`RESULT_TOO_LARGE` is recoverable; a truncated fact poisons a verdict.

---

## 21. Tenant boundary

**Concept.** Tenant scope is asserted by the server, never by the model or the client.

**Implementation.** Tenant and actor come from the trusted request context via the configured
`TrustedContextProvider`. The model's arguments cannot declare a tenant; a model-supplied
tenant field is **rejected**. Provider-side argument construction injects trusted tenant
context where required. `ProviderInvocationContext` is executor-injected. Repository reads are
tenant-scoped. Cross-tenant leakage is a hard gate (`CROSS_TENANT_LEAK`, 2 scenarios).

**Why.** Tenant is the one field where a model error becomes a security incident, and it is
also a field the model is naturally tempted to fill in. Making it structurally impossible for
the model to supply is stronger than validating what it supplies.

**Trade-off.** Every provider and repository must accept the injected context, which is
slightly more plumbing than reading a field from the arguments.

**Reference.** `application/ports/trust.py`, `agent/tools/executor.py`, gate `CROSS_TENANT_LEAK`.

---

## 22. Prompt-injection handling

**Concept.** All remote and retrieved content is **data**, never instruction.

**Implementation.** Tool results and retrieved knowledge are normalized as data with no
control semantics. `KnowledgeHit` has no instruction-bearing field. The tool surface is a
fixed allowlist, so injected text cannot cause a new tool to exist. Tenant is server-supplied,
so injected text cannot widen scope. Verdict and policy decisions are not model-authoritative.
Injection resistance is measured by **forbidden facts** in security scenarios rather than
asserted.

**Why.** Injection defence here is *architectural* rather than filter-based. Filters are a
losing game; removing the model's authority is not. Even a perfectly injected model cannot
select an unadmitted tool, change tenant scope, authorize a response, or create a citation
that resolves.

**Trade-off.** The architecture constrains what the agent can do, which is the intended
trade. Content-based detection is not attempted.

**Reference.** gates `FORBIDDEN_FACTS_ABSENT` in `XP-SEC-001/002`;
`docs/p3/security-boundary.md`.

**Follow-up:** *"What if the injected content changes the verdict?"* → It can influence a
verdict — the model is reasoning over that text. What it cannot do is *authorize* anything:
the verdict still can't execute without policy and a human. That is the deliberate line.

---

## 23. Response proposal lifecycle

**Concept.** A response proposal is the model's recommendation, expressed as a first-class
aggregate with its own lifecycle.

**Implementation.** `ResponseProposal` is created from a completed investigation via
`POST /api/v1/investigations/{id}/response-proposals`. It carries a policy decision and, when
approval is required, an `ApprovalRequest`. Approve/reject are separate endpoints. The response
lifecycle is **independent of the investigation lifecycle**.

**Why.** Making the proposal an aggregate means approval, rejection and execution state are
persisted facts with their own transitions — not fields on an investigation that a later step
could overwrite.

**Trade-off.** More state to compose in the workspace projection.

**Reference.** `domain/response/`, `application/handlers/response.py`.

---

## 24. Policy

**Concept.** A deterministic decision function, separate from human consent.

**Implementation.** `evaluate_response_policy` returns `DENY` or `REQUIRE_APPROVAL`.
`DENY` produces no dispatchable command. Policy is deterministic system code.

**Why.** **Policy ≠ Human Approval** is a distinction worth defending: a policy decision is a
*rule application*, a human decision is *consent*. Collapsing them means either the system can
claim human approval it never got, or a human can override a policy the system is required to
enforce.

**Trade-off.** Two gates to pass instead of one — the intended cost for a side-effecting
action.

**Reference.** `domain/response/`, gates `EXECUTION_WITHOUT_APPROVAL`.

**Follow-up:** *"Is a policy DENY the same as a human rejection?"* → No. `DENY` is the system
declining on policy grounds; a rejection is a human declining. The workspace presents them as
different facts, and the acceptance scenarios keep them separate.

---

## 25. Human approval

**Concept.** Explicit human consent for a side-effecting response.

**Implementation.** An `ApprovalRequest` is answered by an `ApprovalDecision` with
`APPROVE`/`REJECT`. Approve and reject are distinct endpoints with distinct persisted
decisions. A rejection **cannot create a dispatchable command**. Invariant:
*"Agent recommends, cannot authorize."*

**Why.** This is the core safety property of the whole system. An LLM's output can be wrong or
manipulated; a human decision is the checkpoint where a person takes responsibility for a
side-effecting action.

**Trade-off.** Human latency is in the critical path of response. That is the point — but it
means the system must model "waiting for a human" as a real, durable state, which it does.

**Reference.** `domain/response/`, `application/handlers/response.py`, gates
`EXECUTION_WITHOUT_APPROVAL`, `SUBMISSION_TREATED_AS_SUCCESS`.

**Follow-up:** *"What if the analyst approves and then the intent changes?"* → §26.

---

## 26. Revision / hash TOCTOU protection

**Concept.** An approval authorizes a *specific* intent, not a general permission.

**Implementation.** The approval decision binds to a **revision and hash** of the proposal it
approved. A stale approval — one whose bound revision no longer matches the current
proposal — **cannot authorize changed intent**.

**Why.** This closes a real TOCTOU gap. Without it: analyst reviews proposal v1, approves;
between the approval and the dispatch, the proposal (or the evidence behind it) changes;
the dispatch executes something the human never saw. Binding the decision to a revision turns
that into a refusal.

**Trade-off.** Any change to the proposal invalidates the approval and requires a new decision.
Correct, and mildly annoying — which is the right direction for a security control.

**Reference.** `domain/response/`, `XP-AUTH-003` (stale binding + falsifiability).

**Follow-up:** *"How do you prove the gate isn't vacuous?"* → The E3 work added a stale-binding
adapter path so a stale authorization genuinely FAILs `EXECUTION_WITHOUT_APPROVAL` rather than
being skipped by an absent field.

---

## 27. Durable execution

**Concept.** A response command survives process restarts.

**Implementation.** `infrastructure/durable/` — a **dispatcher**, an **investigation runner**,
and **response runners** (a submit runner and an observe runner). Work is driven from
persisted state, not from in-process callbacks.

**Why.** The gap between "a human approved" and "SOAR executed" can be seconds or hours. If
that gap lived in process memory, a restart would silently lose the command. Durability makes
the command a durable fact rather than an in-flight function call.

**Trade-off.** All the machinery of a dispatcher (leases, retries, dead-lettering) instead of a
function call.

**Reference.** `infrastructure/durable/`, `docs/application-commands-domain-events-langgraph-state.md`.

---

## 28. Transactional outbox

**Concept.** Record the intent to publish inside the same database transaction as the state
change.

**Implementation.** An approval writes a `response_execution_queued` domain event into an
**outbox** in the same transaction. A dispatcher publishes from the outbox. Outbox rows carry
**lease ownership, lease expiry, reclaim, fencing tokens and dead-letter** semantics, and an
outbox **trace context** column carries the originating `traceparent`.

**Why.** The dual-write problem: commit the state change but fail to publish → the response
never happens; publish but fail to commit → the response happens for a decision that was never
recorded. The outbox makes publish a consequence of commit.

The lease details are where the real engineering is: a naive outbox leaks messages when a
publisher dies mid-flight. Leases with expiry plus reclaim mean an abandoned message is
retried; a **fencing token** stops a resumed owner from double-publishing after its lease was
reclaimed.

**Trade-off.** At-least-once publication, so consumers must be idempotent — which is why the
idempotency key exists (§29).

**Reference.** `infrastructure/durable/dispatcher.py`, migrations
`*_outbox_lease_*`, `*_outbox_lease_fencing_token`, `*_outbox_trace_context`.

**Follow-up:** *"Why a fencing token and not just a lease?"* → A lease bounds *when* an owner
may act; it does not stop a paused owner from acting after expiry. The fencing token is what
lets the store reject the stale owner's write.

---

## 29. Idempotency

**Concept.** One logical business intent, no matter how many attempts it takes.

**Implementation.** Submission is keyed: `submission_key = response:<tenant>:<proposal>`. A
**CommandReceipt** is scoped, and a request fingerprint is recorded
(migrations `*_command_receipt_scoped_idempotency`, `*_command_receipt_request_fingerprint`).
Concurrent same-key requests converge.

**Why.** At-least-once delivery plus retries means the same command will be attempted more
than once. Idempotency is what makes that safe. The key is derived from *business identity*
(which tenant, which proposal) rather than from an attempt counter — so a retry and a
duplicate dispatch are recognised as the same intent.

**Trade-off.** Requires a receipt store and careful scoping. The harder part is *scoping*: a
receipt scoped too broadly would swallow a genuinely new intent; the scoped version was a
correctness fix.

**Reference.** `infrastructure/durable/`, `XP-REL-004`, E3 focused-durable integration against
real PostgreSQL.

**Follow-up:** *"How do you prove one logical intent, not two?"* → The focused-durable
integration drives real retries against a real database and asserts a single durable intent.

---

## 30. Retry / backoff

**Concept.** Bounded retry with bounded patience, and an explicit terminal outcome.

**Implementation.** Dispatcher constants: `_MAX_ATTEMPTS = 10`, `_MAX_BACKOFF_SECONDS = 120`.
Exponential-ish bounded backoff. A `ResponseSubmitExhaustionHandler` handles the exhausted
case.

**Why.** Retry without a bound is a livelock. Retry with a bound needs an honest answer for
what happens at the bound — and the answer here is not "fail" (§31).

**Trade-off.** Bounded attempts mean a transiently-broken downstream can be given up on. The
mitigation is that giving up is *explicit and visible*, not silent.

**Reference.** `infrastructure/durable/dispatcher.py`.

**Follow-up:** *"What two durability bugs did you find here?"* → A dispatcher resolver failure
could **dead-letter** a command that should have been retried, and the attempt counter had an
**off-by-one**. Both were found by the durability closure and fixed with regression tests —
good examples of "the obvious implementation is subtly wrong".

---

## 31. ATTENTION_REQUIRED

**Concept.** An explicit uncertainty state.

**Implementation.** `ResponseSubmissionStatus` includes `ATTENTION_REQUIRED` (migration
`979070495d4f_add_attention_required_submission_state`). The real
`ResponseSubmitExhaustionHandler` records it at attempt 10 after uncertain failures. The
timeline carries `SUBMISSION_ATTENTION_REQUIRED` and **no** `SUCCEEDED`/`FAILED` status.

**Why.** The most dangerous failure in a response system is a *guess*. If a submit times out,
the system genuinely does not know whether the action happened. Marking it `FAILED` may cause
a retry of something that already happened; marking it `SUCCEEDED` claims an outcome nobody
observed. `ATTENTION_REQUIRED` says the true thing: *we tried, the outcome is unknown, a human
must look.* The workspace must render it that way — presenting it as a terminal success or as
a provider rejection both FAIL their gates.

**Trade-off.** A state that requires human follow-up, by design.

**Reference.** `ResponseSubmitExhaustionHandler`, `XP-REL-004`, gate
`SUBMISSION_TREATED_AS_SUCCESS`.

**Follow-up:** *"Why not just retry forever?"* → Because an unbounded retry of an uncertain
side-effecting action is how you perform it twice. Uncertainty has to surface to a human
eventually.

---

## 32. Submission ≠ Execution Success

**Concept.** Two independent state machines.

**Implementation.**

| `ResponseSubmissionStatus` | Meaning |
|---|---|
| `PENDING` / `RETRYING` | Not yet handed over / retrying |
| `SUBMITTED` | Handed over. **No outcome claim.** |
| `FAILED_DEFINITIVE` | Definitively failed |
| `ATTENTION_REQUIRED` | Uncertain — human needed |

| `ResponseExecutionStatus` | Meaning |
|---|---|
| `QUEUED` / `RUNNING` | Accepted and in progress |
| `SUCCEEDED` / `FAILED` | **Observed** terminal result |

Plus an `ResponseExecutionRef` linking to the provider execution, and a projection of the
provider execution state (`*_response_execution_lifecycle` migration).

**Why.** The most common bug in an integration like this is treating "I sent it" as "it
worked." Keeping the machines separate makes that mistake *unrepresentable*: there is no
value of submission status that means success.

**Trade-off.** The workspace must present two statuses and their relationship, which is more
UI work than one boolean — and it is precisely the distinction the analyst needs.

**Reference.** `domain/response/`, migrations `b7c2d41a90ef_response_execution_lifecycle`,
`c8f1a93d5b27_separate_investigation_response_lifecycles`.

**Follow-up:** *"What does the workspace show after submission but before an observed result?"*
→ "Submitted — awaiting result", with **no** fabricated external execution id.
`XP-UX-001` requires the execution-plane fact to be present but forbids presenting the local
submission state as an observed outcome.

---

## 33. HISIEM execution truth

**Concept.** The execution system owns the execution record.

**Implementation.** The observe runner polls and reconciles the provider execution state into
an observed status. **HISIEM's observed execution state is the final execution truth.**
`XP-AUTH-005` is runtime-integrated: real HISIEM control API, real SOAR worker, real Kafka, a
real provider execution id reaching a terminal state.

**Why.** Copilot has a *belief* about what it submitted; HISIEM has the *record* of what
happened. If Copilot's belief could win, then a lost response or a divergent retry would
produce a system that confidently reports the wrong outcome.

**Trade-off.** Copilot's view can lag the truth while the observe loop runs — which is the
correct behaviour and is why polling continues until a terminal state.

**Reference.** `infrastructure/durable/response_runner.py`, `XP-AUTH-005`.

**Follow-up:** *"What if Copilot and HISIEM disagree?"* → HISIEM wins. That is the definition
of the boundary, and it is what "no second execution truth" means.

---

## 34. OpenTelemetry

**Concept.** Distributed tracing across an asynchronous, durable boundary.

**Implementation.** `setup_telemetry` (write-once SDK globals), `start_span`,
`linked_worker_span` (SpanKind.CONSUMER, a **new root span with a Link** to the originating
trace), `bind_log_context` (5 log-only fields), `capture_traceparent` /
`validate_traceparent` (W3C v00), and auto-instrumentation for FastAPI, httpx, SQLAlchemy and
psycopg.

**Why the async detail matters.** Tracing an HTTP request is routine. Tracing work that
*resumes in a different process 30 seconds later* is not. A naive continuation would create a
child span in a new process with no parent — a broken trace. The implementation persists the
`traceparent` at enqueue time and, on resume, creates a **new root span linked to** the
original trace. The work is correctly attributed to the request that queued it, without
pretending to be the same span — which would be a lie about causality.

**Trade-off.** Two linked traces instead of one continuous trace, which is the honest shape of
an asynchronous boundary.

**Reference.** `infrastructure/observability/`, `docs/observability.md`, migration
`b6c2a4d19f30_outbox_trace_context`.

**Follow-up:** *"Why not propagate the parent context directly?"* → Because the worker is not a
continuation of the HTTP request's call stack; it's a separate root that was *caused by* it.
A Link models that; a parent-child edge claims something stronger and would produce traces
that misrepresent the system.

---

## 35. Telemetry data safety

**Concept.** Telemetry must never become a data-exfiltration path.

**Implementation.** `sanitize_metric_attributes` with an allowlist of label keys, and
**fail-closed whole-observation rejection** — an observation carrying a disallowed key is
rejected entirely, not partially recorded. `FORBIDDEN_TELEMETRY_ATTRIBUTE_KEYS` and
`FORBIDDEN_METRIC_LABEL_KEYS` enumerate what may not appear. Raw prompts, completions, full
tool results, secrets, embeddings and chain-of-thought are never emitted. Evaluation artifacts
are secret-scanned before write.

**Why.** Two reasons, and the second is the interesting one. Cardinality is the usual
justification — a tenant id as a metric label explodes the series count. But the stronger
reason is **privacy**: telemetry is copied, exported, retained and often shipped to a
third-party backend. A prompt or a credential in a span attribute has left your trust boundary.
Fail-closed matters because partial sanitization leaves the forbidden value in place.

**Trade-off.** Debuggability is reduced by withholding payloads. The mitigation is that
correlation is preserved (ids, statuses, durations) without content.

**Reference.** `infrastructure/observability/`, `XP-OBS-002`, `XP-SEC-003` (gate
`SECRET_LEAK`), and the observability foundation stage report under [`../stage-reports/`](../stage-reports/).

**Follow-up:** *"Why fail-closed rather than dropping the bad key?"* → Dropping the key keeps
the observation but silently discards a field the caller believed was recorded. Rejecting the
observation makes the mistake visible. For a privacy control, visible failure beats silent
partial compliance.

---

## 36. Collector outage semantics

**Concept.** Losing telemetry must not change any business outcome.

**Implementation.** No business path reads spans, trace ids or collector state. `XP-REL-005`
is runtime-integrated: the OTel Collector is stopped and restarted across **two real Copilot
worker processes**, and the persisted business outcome is **identical**. The deciding gate is
`TELEMETRY_CHANGED_BUSINESS_STATE`.

**Why.** This is the "Telemetry ≠ Business Truth" boundary made executable. It is easy to
*believe* telemetry is side-channel and accidentally couple to it — for instance by using a
span context to correlate a command, or by failing a request when the exporter is down. The
scenario proves the decoupling rather than asserting it.

**Trade-off.** None worth stating — the whole point is that telemetry is optional by
construction.

**Reference.** `XP-REL-005`, `docs/observability.md`.

**Follow-up:** *"How did you make this testable?"* → By comparing the *persisted business
outcome* across collector-up and collector-down runs of the same work, in real processes.

---

## 37. Analyst Workspace

**Concept.** The analyst's view of an investigation, as a projection of persisted truth.

**Implementation.** `application/services/workspace_service.py` and
`queries/workspace.py` build the projection, served at
`GET /api/v1/investigations/{id}/workspace`. The UI lives in **the HISIEM repository**
(`HISIEM/web/`), because HISIEM owns the platform's web application. The frontend's authority
derivation lives in `web/src/utils/copilot.js` (`evidenceAuthority`, `AUTHORITY_LABELS`,
`proposalSubmissionNeedsAttention`).

**What it must preserve:**

| Requirement | Meaning |
|---|---|
| Authority classes | Platform fact vs. knowledge context vs. model-derived finding vs. agent verdict vs. policy vs. human decision vs. execution result |
| Server truth wins | A stale snapshot is overridden by newer server state on refresh |
| Reconstruction | A fresh load reproduces persisted truth exactly |
| Invent nothing | No approval/execution/submission state the persisted lifecycle does not record |
| Never render internals | No chain-of-thought, prompts or graph checkpoints |

**Why the UI is in the other repository.** HISIEM owns the platform web application and the
analyst's session, routing and auth. Putting Copilot's UI inside HISIEM's console was the
coherent choice — and it is worth stating explicitly, because it looks like an error otherwise.

**Trade-off.** A cross-repository boundary for one feature. Mitigated by executing the real
frontend module from the acceptance harness so evaluation and UI cannot drift.

**Reference.** `docs/investigation-workspace.md`, `XP-UX-001`.

---

## 38. Refresh and stale reconstruction

**Concept.** The workspace is a projection, so a reload must reproduce truth, and a stale
client must lose to the server.

**Implementation.** `XP-UX-002` measures two things on real projections:
`WORKSPACE_RECONSTRUCTED_FROM_DURABLE_STATE` (a second projection built from persisted state
alone reproduces the persisted truth exactly — same authority classes, findings, verdict,
policy, decision, submission and execution state) and `WORKSPACE_STALE_OVERRIDDEN_BY_REFRESH`
(the stale snapshot differs, and the refresh lands on the server's truth).

**Why.** Reconstruction is measured against **persisted truth**, never against what the client
happened to see — transient browser memory is not an input. This is what makes the workspace a
projection rather than a cache with a sync problem.

**Trade-off.** A reload always costs a server round trip.

**Reference.** `XP-UX-002`, `cross_plane_workspace.py` in the evaluation harness.

**Follow-up:** *"What's the falsifiable half?"* → A refresh that lands on the stale snapshot,
or a reconstruction differing from server truth, FAILs `EXPECTED_FACTS_PRESENT`.

---

## 39. GP-01

**Concept.** End-to-end investigation correctness on a sealed, reproducible dataset.

**Implementation.** A committed logical scenario is materialized into **real HISIEM
resources** (the dataset materializer), their provider identities resolved, and the result
**sealed** into a manifest (the manifest sealer). A real model run executes against the sealed manifest;
tool/evidence quality is assessed; a **deterministic correctness scorer** scores it; a
**bounded repeatability collector** classifies attempts; a suite summary aggregates. GP-01
closure requires **3/3 valid correctness passes**.

**Why.** Seal first, score later. If the inputs could drift, a score would be meaningless —
you'd never know whether a change came from the system or the data. The sealed manifest is
what makes a pass reproducible.

**Trade-off.** Re-sealing is required when the dataset changes — deliberate friction, and
there is a documented "never re-seal GP-01 for an unrelated change" rule.

**Reference.** `docs/evaluation/`, `src/hisiem_soc_copilot/evaluation/`.

**Follow-up:** *"Why 3/3 rather than a pass rate?"* → Because a stochastic system passing 2 of
3 is not obviously better than 0 of 3 — it means variance. Requiring all valid attempts to pass
makes the claim binary and honest.

---

## 40. KB-GOLDEN-V1

**Concept.** The knowledge subsystem's evaluation baseline.

**Implementation.** A versioned corpus with a corpus version. Retrieval modes (`HYBRID`,
`LEXICAL_ONLY`, `VECTOR_ONLY`) with deterministic scoring, a sealed corpus precondition that
derives the eligible corpus from the database before scoring, and a corpus fingerprint that
names the exact database state a baseline was taken from. Result: **679 passed / 0 skipped**
at the relevant gates.

**Why.** Two mechanisms worth knowing: the **precondition** means a run over the wrong database
state refuses to produce a number rather than producing a quietly wrong one; the
**fingerprint** means a baseline is only meaningful together with the state it came from.

**The honest gap.** `REAL EMBEDDING EVALUATION NOT RUN`. No embedding provider is configured,
so the semantic half has never been exercised. The `VECTOR_ONLY` row scoring near-chance
recall is a positive signal for *wiring* (the vector channel really calls a provider rather
than degrading to lexical) and no signal at all for retrieval quality. Artifacts are labelled
`PLUMBING_ONLY` and must not be quoted as a semantic baseline.

**Reference.** `docs/p3/evaluation-contract.md`.

**Follow-up:** *"What would it take to close it?"* → Configure a real embedding provider and
re-run; the harness needs no change. That is the design intent — the fixture is a stand-in,
not a substitute.

---

## 41. XP-01

**Concept.** Cross-plane acceptance: the system's invariants, made executable and falsifiable.

**Implementation.** 29 scenarios across 9 families (Authority 5, Capability 1, Knowledge 4,
MCP 5, Observability 2, Reliability 5, Security 3, Tenant 2, Workspace 2). 13 hard gates.
3 execution profiles: 26 deterministic, 3 runtime-integrated (real HISIEM + Kafka, real OTel
Collector). Every scenario produces a `cross-plane-gate-results/v1` artifact. The aggregate is
`cross-plane-suite-results/v1` and is **non-compensating**.

**Why non-compensating.** Because a score invites optimizing the score. "28 of 29 passing,
weighted 0.96" hides which invariant broke. The aggregate has **no score, weight or pass
percentage anywhere** — one FAIL, one missing scenario, one unknown scenario, or one missing
gate result fails the whole suite. And the negative half is tested against the real artifacts,
so the aggregate can actually fail.

**Three details that show the design was thought through:**

1. **Runtime evidence is never substituted.** Each runtime-integrated scenario *also* keeps its
   own deterministic artifact in the aggregate.
2. **Determinism is enforced.** Byte-identical across reads, independent of input order, no
   timestamps or run ids in the identity.
3. **Artifacts are secret-scanned before the atomic write**, and the frozen scan is a substring
   scan — which is why one invariant key is named `credential_marker_absent` rather than
   anything containing the marker.

**Trade-off.** Maintaining 29 scenarios is real work. The return is that a claim like "the
agent can't execute without approval" is backed by a machine-readable artifact rather than a
paragraph.

**Also worth stating:** it was built **without changing any HISIEM production code** and
without changing this repository's production layers. The invariants were made testable
without touching the systems under test — which is the part I would emphasize.

**Reference.** [README §12](../../README.md#12-evaluation), `docs/stage-reports/`.

---

## 42. Architecture tests

**Concept.** Boundaries enforced mechanically, not by convention.

**Implementation.** `tests/architecture/` — `test_import_boundaries.py`,
`test_knowledge_boundary.py`, `test_evaluation_boundary.py`, `test_cross_plane_boundary.py`,
`test_test_isolation.py`. **285 passed** at the seal. Specifically: production layers never
import the evaluation packages; the domain stays pure; the model-selectable tool surface is
exactly the declared 4-tool set; the XP-01 package has no IO/clock imports.

**Why.** A boundary documented only in prose decays. The tool-surface assertion is the
sharpest example: it means *the model's entire action space* cannot change without a test
failing — which is exactly the property you want on a security-relevant surface.

**Trade-off.** Architecture tests are occasionally inconvenient when a legitimate dependency
is needed — which is the point, since it forces the boundary discussion to happen explicitly.

**Reference.** `tests/architecture/`, `docs/python-package-boundary.md`.

**Follow-up:** *"Give an example of a boundary test catching something."* → The model-selectable
surface test pins it to exactly four tools, so adding a fifth tool without registering it (or
registering one without an executor) fails the build.

---

## 43. Important code paths

**Path A — an alert becomes a verdict.**

```text
POST /api/v1/investigations
  → StartAlertInvestigation command → handler
  → partial unique index: one Active Investigation per (tenant, alert)
  → investigation row (CREATED)
  → durable investigation runner picks it up
  → LangGraph: hydrate (system-controlled get_alert_context)
              → model selects a tool (4-tool allowlist)
              → ToolPolicy → ToolBudget → ToolExecutor → provider
              → ToolResult → (grounded only) EvidenceNormalizer → Evidence
              → findings → verdict
  → investigation COMPLETED (or INCONCLUSIVE / FAILED)
```

**Path B — a verdict becomes an executed response.**

```text
POST .../response-proposals             → ResponseProposal + evaluate_response_policy
                                        → DENY | REQUIRE_APPROVAL
POST .../response-approvals/{id}/approve → ApprovalDecision bound to revision + hash
                                        → (same transaction) outbox event
                                          response_execution_queued
  → dispatcher: lease → submit runner (idempotency key response:<tenant>:<proposal>)
      → HISIEM SOAR
      → PENDING → RETRYING → SUBMITTED | FAILED_DEFINITIVE | ATTENTION_REQUIRED
  → observe runner → ResponseExecutionRef → observed execution status
      → QUEUED → RUNNING → SUCCEEDED | FAILED   (final truth: HISIEM)
  → workspace projection rebuilt from persisted state
```

**Path C — knowledge retrieval.**

```text
KnowledgeQuery(topic, context_terms, limit ≤ 5) + tenant_id (required)
  → FTS channel  +  vector channel (pgvector)
  → RRF fusion → bounded KnowledgeHit set
  → citation → citation revalidation
  → validated Knowledge Evidence (authority class: supporting context)
  → finding
```

---

## 44. Failure-mode walkthroughs

| Failure | Behaviour | Mechanism |
|---|---|---|
| Tool call fails / provider down | Typed failure, **zero Evidence**; verdict cannot rest on it | §12, `XP-REL-001/002` |
| Tool returns an oversized result | `RESULT_TOO_LARGE`, never truncated | §20 |
| Model selects an unadmitted tool | Not selectable — the surface is a fixed allowlist | §8, `XP-MCP-002` |
| Model selects a write capability | Structurally impossible (`READ_ONLY` required) | §20, `XP-MCP-003` |
| MCP server changes a tool's schema | `SCHEMA_MISMATCH`; the tool becomes unavailable | §20 |
| Model supplies a tenant field | Rejected; tenant is server-injected | §21 |
| Model tries to authorize an approval | No such capability exists | §25, `XP-AUTH-001` |
| Prompt injection in a tool result | Cannot change scope, tool surface, policy, or create a citation | §22 |
| A citation doesn't resolve | `DANGLING_CITATION` fails the scenario | §16 |
| A citation crosses investigations | `CROSS_INVESTIGATION_CITATION` fails the scenario | §16 |
| Knowledge-only basis for a definitive verdict | `KNOWLEDGE_ONLY_DEFINITIVE_VERDICT` fails | §17 |
| Human rejects | No dispatchable command is created | §25 |
| Proposal changes after approval | The stale approval cannot authorize the new intent | §26, `XP-AUTH-003` |
| Submit times out | `ATTENTION_REQUIRED` — an explicit uncertainty state | §31 |
| Submit retried after a duplicate delivery | Converges on one logical intent via the idempotency key | §29 |
| Dispatcher resolver fails | The command is **not** dead-lettered; it is retried | §30 |
| Process restarts mid-execution | The outbox is the durable source; work resumes, no duplicate intent | §27–§29 |
| OTel Collector is stopped | Persisted business outcome is **identical** | §36, `XP-REL-005` |
| A secret enters a telemetry attribute | The observation is **rejected whole** | §35 |
| Frontend holds stale state | Server truth wins on refresh | §38 |
| Workspace claims an unrecorded execution | `WORKSPACE_INVENTED_AUTHORITY` fails the scenario | §37 |

---

## 45. Ten hardest design decisions

1. **Separate submission status from execution status.** Two machines, no value of the first
   implying success. Prevents the classic "I sent it, therefore it worked" bug.
2. **`ATTENTION_REQUIRED` instead of guessing a terminal state.** Uncertainty is a real
   outcome and must be representable.
3. **Bind approval to revision + hash.** Closes TOCTOU between approval and dispatch.
4. **Derive the idempotency key from business identity, not attempt identity.** Makes retries
   and duplicate deliveries the same intent.
5. **Idempotency receipts must be *scoped*.** The scoping was a correctness fix — too broad a
   scope swallows a genuinely new intent.
6. **Make a typed tool failure produce no Evidence.** The single strongest
   anti-hallucination boundary.
7. **Treat discovery and admission as different things.** The server's self-description is
   untrusted input.
8. **Design writes away instead of sandboxing them.** A read-only model surface has no
   sandbox-escape class of bug.
9. **Model the async trace boundary with a Link, not a parent.** Honest causality; a
   parent-child edge would misrepresent the system.
10. **Make the acceptance aggregate non-compensating.** It removes the entire category of
    "the suite passes but an invariant is broken."

---

## 46. Alternatives considered

| Decision | Chosen | Alternative | Why not the alternative |
|---|---|---|---|
| Tool surface | Fixed 4-tool read-only allowlist | Dynamic MCP discovery with auto-admission | The server's contents become part of the trust boundary |
| Writes | Not model-selectable at all | Sandboxed write execution | Sandbox escapes are a category of bug; removing the capability removes the category |
| Evidence | Typed failure → no Evidence | Let the model interpret raw results | Grounding would depend on the model's reading of an error string |
| Approval binding | Revision + hash | Time-based approval validity | A time window authorizes changed intent if the intent changes inside it |
| Idempotency key | `response:<tenant>:<proposal>` | Per-attempt id | Per-attempt ids make every retry a new intent |
| Outbox lease | Lease + expiry + reclaim + fencing | Lease only | A paused owner can act after expiry; fencing rejects the stale write |
| Retry terminal state | `ATTENTION_REQUIRED` | `FAILED` after N attempts | `FAILED` is a claim about a side effect nobody observed |
| Execution truth | HISIEM's observed state | Copilot's own submission record | Copilot's record is a belief; HISIEM's is the record |
| Vector store | pgvector on existing PostgreSQL | A dedicated vector database | A second operational system and a second truth store, for no gain at this scale |
| Ranking | RRF over ranks | Weighted score fusion | BM25 vs. cosine score calibration has no principled answer |
| Knowledge authority | Supporting context only | Knowledge can support a definitive verdict | A runbook saying "block the IP" must not authorize blocking |
| Telemetry labels | Fail-closed whole-observation rejection | Drop the disallowed key | Silent partial compliance on a privacy control |
| Agent topology | One agent | Multi-agent / A2A | Multiplies the authority surface to defend |
| Evaluation verdict | Non-compensating | Weighted score | A score invites optimizing the score |
| Test approach for UI invariants | Execute the real frontend module | Restate the mapping in the test | The test would pass while the UI drifted |

---

## 47. Deliberate non-goals

Stated as decisions, not gaps:

- **Multi-agent systems and A2A protocols.** One investigating agent with a bounded tool
  surface. Additional agents multiply the authority surface and make tool selection harder to
  reason about and to gate.
- **A second RAG framework, vector database, observability stack, or evaluation platform.**
  Each concern has exactly one owner. Adding a second platform of any of these creates the
  parallel-architecture problem the four-plane contract explicitly forbids.
- **A model router.** One provider contract, pluggable, with no routing layer.
- **Speculative sandboxing.** The write path is designed away rather than contained.
- **A frontend in this repository.** The workspace UI belongs to the platform repository.
- **Model-based authorization.** Never. Policy is deterministic; approval is human.

---

## 48. Current limitations

**Correct by design but worth stating**

- Submission status and execution status are deliberately separate, so the workspace shows two
  states — more complex to present, and the source of the safety property.
- The workspace's authority class is derived **client-side** from persisted source types
  (`E5-OBS-01`). A server-side field would be simpler; the frontend derivation is measured by
  executing the real module. It is a documented, accepted observation.
- Before any provider execution exists, the workspace's execution-plane fact is the local
  submission state (`E5-OBS-02`) — required to be presented as a submission, forbidden to be
  presented as an observed outcome.

**Unmeasured or partial**

- **Semantic retrieval quality is unmeasured** (no embedding provider configured;
  `PLUMBING_ONLY` artifacts).
- **Seven optional span operations are not emitted** (`queue.wait`, `alert.hydrate`,
  `native.call`, `embedding`, `postgres.fts`, `pgvector.search`, `retrieval.merge`).
  Acceptance runs on the operations the runtime really traverses (`OBS-001`).
- **MCP provider failure classification granularity** is coarse (`DEFECT-005`, low,
  non-blocking; a bounded typed failure with no false-success Evidence).
- **DB-backed test skip-guard hardening** (`TEST-INFRA-001`, test-infrastructure only).
- **Real-world model variability.** Determinism of *scoring* is enforced; determinism of a real
  model's output is not, and cannot be. GP-01's response is the 3/3 requirement and a bounded
  repeatability collector rather than an assumption.

**Operational**

- The evaluation acceptance run is a driven command, not a CI job wired into a pipeline.
- The three runtime-integrated scenarios need the real HISIEM stack and a Collector; they skip
  with an explicit reason when absent, so the deterministic 26 remain runnable anywhere.

---

## 49. How this would be productionized

Ordered by risk, not effort.

1. **Model governance.** Prompt/version pinning with an evaluated change process; a real
   provider matrix rather than one; cost and latency budgets per investigation alongside the
   tool budget.
2. **Semantic retrieval.** Configure a real embedding provider, re-run `KB-GOLDEN-V1`, and
   publish a genuine semantic baseline — closing the largest measurement gap.
3. **Approval operations.** Escalation and expiry policy for pending approvals; notification
   and an SLA on `ATTENTION_REQUIRED` items (today they are explicit but passive).
4. **Multi-tenant hardening.** Per-tenant rate limits, quotas and noisy-neighbour isolation;
   tenant-scoped encryption at rest.
5. **Knowledge lifecycle.** Ingestion pipelines with review/approval for knowledge sources,
   provenance attestation, and a corpus refresh process that respects citation stability.
6. **CI integration.** Make the 29-scenario acceptance pack a per-build gate with the
   aggregate's `suite_identity` recorded, plus the deterministic 26 running on every change.
7. **Operational telemetry.** Wire checkpoint durations, dispatcher backlog, outbox age,
   submission latency and `ATTENTION_REQUIRED` counts into alerting with SLOs.
8. **Disaster recovery.** Tested restore for PostgreSQL including the outbox and receipts, with
   a documented RPO/RTO for in-flight responses.
9. **Audit export.** A tamper-evident audit trail over the full authority chain (who proposed,
   what policy said, who approved, what was submitted, what was observed).
10. **Model cost/perf.** Caching of retrieval results; streaming of the investigation for
    analyst feedback; evaluation of smaller models against the same acceptance pack.

---

## 50. Likely interview questions

**Opening**

1. *What is this system, in one breath?* → §1.
2. *Why does an AI agent need all this machinery?* → Because an agent with write access is a
   liability; the machinery is what makes the agent usable **and** safe.
3. *What's the most interesting part?* → The authority model and the durable execution path.

**Agent engineering**

4. *What can the agent actually do?* → Exactly 4 read-only tools, plus a system-controlled
   context fetch. §8.
5. *How do you stop it hallucinating capability?* → Unimplemented tools are in a separate set
   and are never registered; the surface is asserted by an architecture test. §8, §42.
6. *How do you handle a tool failure?* → Typed failure, zero Evidence. §12.
7. *How is prompt injection handled?* → Architecturally: remove authority, then injection has
   nothing to steer. §22.

**RAG / knowledge**

8. *How does retrieval work?* → FTS + pgvector + RRF, pure deterministic ranking. §15.
9. *Why RRF?* → Rank-based fusion avoids score calibration. §15.
10. *Can knowledge produce a verdict?* → No — a hard gate. §17.
11. *Is retrieval quality measured?* → No. State the gap first. §40.

**Authority / safety**

12. *Walk me through what happens when the agent recommends blocking an IP.* → §43 Path B.
13. *What stops approval from being stale?* → Revision + hash binding. §26.
14. *How do you prove no execution without approval?* → `EXECUTION_WITHOUT_APPROVAL`, plus a
    stale-binding case that genuinely fails it. §26, §41.
15. *Policy vs. approval?* → Rule application vs. consent. §24, §25.

**Reliability**

16. *What if the submit times out?* → `ATTENTION_REQUIRED`. §31.
17. *How do you avoid executing twice?* → Idempotency key + scoped receipts + fencing tokens.
    §28, §29.
18. *What's `SUBMITTED` mean?* → Only that it was handed over. §32.
19. *Whose word is final?* → HISIEM's observed state. §33.
20. *Tell me about the durability bugs you found.* → Resolver failure dead-lettering; attempt
    off-by-one. §30.

**Evaluation**

21. *How do you know any of this works?* → GP-01, KB-GOLDEN-V1, XP-01. §39–§41.
22. *Why non-compensating?* → A score invites optimizing the score. §41.
23. *Can the aggregate fail?* → Yes, and the negative half is tested against real artifacts.

**Design judgment**

24. *What would you do differently?* → §46, §49.
25. *What's deliberately not built?* → §47 — and be ready to defend each.

---

## 51. Deep follow-up questions

### D1. "Why is `ATTENTION_REQUIRED` a state rather than an error?"

Because the true state of the world is *unknown*, and every available error state is a
**claim**. `FAILED` claims the action did not happen — but a timeout could mean it happened
and the response was lost. `SUCCEEDED` claims it did. Both are guesses about a side effect on
a real system, and a wrong guess here is how you block an IP twice or leave a host unisolated.

`ATTENTION_REQUIRED` is the only state that says what is actually true: *we attempted,
the outcome is uncertain, a human needs to look.* The workspace is forbidden from rendering
it as either a terminal success or a provider rejection, and both presentations FAIL their
gates. §31, `XP-REL-004`.

**Follow-up: "Isn't that just pushing work to a human?"** → Yes — deliberately. The
alternative is pushing *risk* to a human who doesn't know it happened, which is worse.
The productionization answer (§49) is escalation and notification so the human finds out
promptly.

### D2. "How do you guarantee the model can't execute something?"

Layered, and the layers are structural rather than behavioural:

1. **No capability exists.** There is no tool in the registry that executes a response. The
   forbidden list names them explicitly (`block_ip`, `isolate_host`, `start_soar_execution`,
   `approve_response`, …) and they are not in the selectable set.
2. **Writes are unselectable at the provider layer.** `is_model_selectable` requires
   `READ_ONLY`, so even an MCP server offering a write capability cannot get it in front of
   the model (§20).
3. **Policy is deterministic and system-owned.** The model does not evaluate policy (§24).
4. **Approval is a human decision on a distinct endpoint.** There is no code path where model
   output becomes an approval (§25).
5. **The approval is bound to a revision and hash**, so it authorizes one specific intent (§26).
6. **The command is durable and idempotent**, so it cannot be double-issued (§27–§29).
7. **All of the above is gated**, in `XP-AUTH-001/002/003` and `EXECUTION_WITHOUT_APPROVAL`.

**The one-sentence version:** *there is no code path from model output to a side effect. Every
edge in that path goes through a component the model cannot influence — and each edge has an
acceptance scenario.*

### D3. "Why is discovery separate from admission?"

Because discovery is remote, attacker-influenceable content and admission is a local,
human-authored decision. A server can add a tool, change a description, or change a schema
whenever it wants. If discovery implied admission, then **control of the server would imply
control of the agent's action space** — including the tool *descriptions*, which are prompt
surface.

Separating them means: a new tool is discovered (operators see it), but it is not selectable
until someone writes an admission entry with a trusted description, contracts, risk class,
tenant scope and expected fingerprint. A schema change under an admitted name becomes
`SCHEMA_MISMATCH` rather than a silently different behaviour. §18–§20.

**Follow-up: "Isn't that a lot of manual work?"** → Yes, and that is the feature. The manual
step is exactly where a human decides to trust something, and there are few enough
capabilities that the cost is bounded.

### D4. "Why isn't the idempotency key just a UUID per attempt?"

Because idempotency is about **business identity**, not attempt identity. A UUID per attempt
makes every retry a brand-new intent — which is the exact bug idempotency is supposed to
prevent. `response:<tenant>:<proposal>` says "this tenant's response to this proposal", so a
retry, a duplicate delivery and a redispatch all collapse to one intent.

The subtlety is **scoping** the receipt. Too broad and it swallows a legitimately new intent
(a second, genuinely different response to the same proposal); too narrow and it fails to
dedupe. Getting that scope right was a correctness fix, not an initial design — an honest
example of the difference between "uses idempotency" and "has correct idempotency". §29.

### D5. "What's the difference between outbox and exactly-once?"

An outbox gives **at-least-once** publication with a guaranteed *causal link* to a committed
transaction. It does not give exactly-once. It solves the dual-write problem — "did the
publish happen if the commit did?" — by making publication a consequence of the commit rather
than a second independent action.

Making the delivery exactly-once would need a transactional broker or deduplication —
and here deduplication is what the idempotency key provides at the consumer. So the
composition is: outbox for the causal guarantee, idempotent consumer for the duplicate
tolerance. Claiming "exactly-once" for the outbox alone would be overclaiming. §28, §29.

### D6. "Why is the workspace in the other repository?"

Because HISIEM owns the platform's web application — the console, the analyst's session,
routing and auth. The Copilot workspace is a page in that console, not a separate application.
Putting it in the Copilot repository would have meant either duplicating the platform's
frontend infrastructure or building a second console.

The cost is a cross-repository feature boundary, and the mitigation is specific: the
acceptance harness **executes the real frontend module** (`web/src/utils/copilot.js`) rather
than restating its authority mapping in a test. So evaluation and UI cannot drift — a change
to the derivation that broke the invariant would fail the acceptance run, not just a unit test
in the other repo. §37.

**Follow-up: "Does that make the Copilot repo look incomplete?"** → It looks that way until
it's explained, which is exactly why the README states it prominently. §3 of the README's
cross-repository section.

### D7. "You claim no second truth source. Convince me."

The claim is that no component's local state can be promoted to business truth. Check the
candidates:

| Candidate | Why it can't win |
|---|---|
| LangGraph checkpoint | Working memory; never read as domain state |
| OTel span / trace id | No business path reads spans; proven by the collector-outage equivalence |
| Frontend state | Derives from persisted state; server wins on refresh; `WORKSPACE_INVENTED_AUTHORITY` is a hard gate |
| MCP invocation state | Provider-local; not a durable execution record |
| Raw ToolResult | Untrusted data; becomes Evidence only through the normalizer on a grounded success |
| Copilot's submission record | A belief; HISIEM's observed state is final |
| Evaluation artifacts | Produced *from* real runs, consumed by an aggregate; they never write back |

The strongest single piece of evidence is `XP-REL-005`: stopping the Collector across two real
worker processes leaves the persisted business outcome **identical**. That is the telemetry
boundary made executable rather than asserted. §36, D5.

### D8. "What would you do differently if you started over?"

Genuine answers, in order of conviction:

1. **Close the embedding gap first.** Building hybrid retrieval and then not being able to
   measure its semantic quality was a sequencing mistake — the measurement is cheap and the
   gap is the largest honest hole in the project (§40, §49).
2. **Server-side authority class.** I derive the evidence authority class in the frontend from
   persisted source types. It is correct and it is measured, but a server field would have
   removed a cross-repository coupling (§48, `E5-OBS-01`).
3. **Make the acceptance pack a CI gate from the start.** It runs today as a driven command;
   the aggregate's stable `suite_identity` is designed to be recorded per build, and wiring
   that earlier would have caught regressions sooner.
4. **Escalation for `ATTENTION_REQUIRED`.** The state is correct, but it is passive — nobody
   is notified. Explicitness is necessary and not sufficient.

What I would **not** change: the single-agent topology, the 4-tool read-only surface, the
non-compensating aggregate, or keeping the workspace in the platform repository. Each of
those is a decision I would defend rather than revisit.
