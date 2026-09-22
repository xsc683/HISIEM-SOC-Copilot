# HISIEM SOC Copilot — Four-Plane Architecture & Contract Freeze

**Status:** FROZEN BASELINE  
**Version:** 1.0  
**Date:** 2026-09-14  
**Purpose:** Architecture / Contract authority for subsequent design, Vibe Coding, code review, and acceptance.

## 0. Scope

This baseline freezes four platform planes that still need closure:

1. Knowledge Plane
2. Capability Plane
3. Observability Plane
4. Analyst Experience Plane

Stable foundations to reuse rather than redesign:

- Domain Plane
- Agent Runtime Plane
- Persistence Plane
- Durable Execution Plane
- Human Authority Plane
- HISIEM Integration Plane
- Evaluation Plane

## 1. Global architecture principles

### 1.1 No new undefined authority

- Knowledge != Verdict Authority
- MCP Provider != Authorization Authority
- Telemetry != Business Authority
- Frontend != Command Authority
- LangGraph != Domain Authority

### 1.2 No second truth source

Do not create parallel Investigation, Execution, Approval, Evidence, Tenant, Tool authorization, or Agent business-state systems.

Examples:

- OTel Span != Investigation State
- LangGraph Checkpoint != Audit Truth
- Frontend Local State != Business Truth
- MCP Invocation State != Durable Execution State

### 1.3 Stable foundation means responsibility/authority stability

Stable does not mean code can never change. Local extensions are allowed when they preserve responsibility, authority, truth source, and security boundaries. No parallel architecture may be introduced merely to integrate a new technology.

## 2. Global business authority chain

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

Permanent rule:

```text
Model proposes
Policy constrains
Human authorizes
Durable command records intent
HISIEM executes
Copilot observes
```

# 3. Knowledge Plane

## 3.1 Goal

Provide trustworthy, traceable, tenant-safe supporting context to Investigation and normalize retrieved knowledge into the same Evidence model used by the rest of the system.

```text
Knowledge Source
      ↓
Ingestion
      ↓
Document / Version / Chunk
      ↓
Retrieval
      ↓
Citation
      ↓
Citation Validation
      ↓
Knowledge ToolResult
      ↓
EvidenceNormalizer
      ↓
Immutable Knowledge Evidence
      ↓
Finding Citation
```

Knowledge does not directly produce Verdict.

## 3.2 Stable foundation

Reuse without rebuilding:

- Knowledge Ingestion
- Structure-aware Chunking
- PostgreSQL
- FTS
- pgvector
- RRF
- Embedding Profile
- Document / DocumentVersion / Chunk
- Citation / CitationResolver
- ATT&CK bundle / release / projection
- Tenant scope
- Knowledge Evaluation

Do not add a second RAG framework, second Knowledge model, second Citation model, second embedding-space manager, or independent vector database.

## 3.3 Model-selectable tool surface

Frozen selectable tools:

- `hisiem.search_events`
- `hisiem.get_detection_rule`
- `knowledge.retrieve_security_guidance`
- `knowledge.resolve_attack_technique`

System-controlled:

- `hisiem.get_alert_context`

Deferred:

- `hisiem.get_entity_activity`
- `threat_intel.lookup_ip`

No expansion without a new contract freeze.

## 3.4 `knowledge.retrieve_security_guidance`

Allowed model arguments:

- topic
- context_terms
- limit

Forbidden model-controlled fields:

- tenant_id
- visibility
- embedding_profile
- retrieval_profile_id
- raw SQL / raw tsquery / raw vector
- internal document/chunk IDs

Tenant comes only from Trusted Investigation Runtime Context.

## 3.5 Retrieval modes

Frozen modes:

- `HYBRID` — production default: FTS + Vector + RRF
- `LEXICAL_ONLY` — supported diagnostic/evaluation/attribution mode
- `VECTOR_ONLY` — supported diagnostic/evaluation/attribution mode

The ToolResult/Evidence provenance must report the mode actually executed. Silent downgrade is forbidden.

## 3.6 Embedding contract

HYBRID and VECTOR_ONLY production paths use the ACTIVE embedding profile. Coverage asks whether ACTIVE fully covers the current document/chunk generation. It must not infer coverage by UUID ordering or historical profile ordering.

Required semantics:

- ACTIVE A complete + retired B complete -> VALID
- ACTIVE A complete + retired B partial -> VALID
- ACTIVE A partial + retired B complete -> INVALID
- ACTIVE A absent + retired B complete -> INVALID

## 3.7 Citation contract

Citation identifies stable Knowledge identity and must resolve to document, version, chunk, source kind/version, tenant scope, and content identity. Citation is revalidated before Knowledge Evidence creation.

## 3.8 Knowledge Evidence provenance

Stable Evidence provenance answers what the knowledge is:

- citation identity
- document identity/version
- chunk identity
- source kind/version
- tenant scope
- content identity/hash
- ATT&CK release identity when applicable

Retrieval execution provenance answers how it was found:

- retrieval mode
- retrieval profile
- channel
- rank
- retrieved_at

Rank/vector score/RRF position are retrieval metadata, not authority.

Evidence dedup identity must not depend on rank, score, retrieved_at, retrieval mode, or result order.

## 3.9 ATT&CK resolver

`knowledge.resolve_attack_technique` is exact canonical resolution, not fuzzy RAG.

Example:

```text
T1059.001 -> exact T1059.001
```

Case/canonical normalization is allowed. Semantic guessing from natural-language descriptions is not.

Authority is ACTIVE ATT&CK release + projection + content/hash chain; `KnowledgeDocument.source_version` is not a substitute for ATT&CK release authority.

## 3.10 Knowledge authority

Architecture invariant:

- Retrieved Knowledge != authoritative observed fact
- Retrieved Knowledge != direct Verdict authority

Knowledge flows through Evidence -> Investigation assessment -> Finding.

Current product policy: definitive MALICIOUS/BENIGN requires at least one observed platform Evidence. Observed platform evidence includes HISIEM_ALERT, HISIEM_EVENT, HISIEM_LOG_SEARCH, HISIEM_ENTITY. KNOWLEDGE and SYSTEM detection-rule context do not satisfy this condition.

## 3.11 Failure semantics

Knowledge unavailability must produce typed Tool failure, not crash the Investigation and not masquerade as an empty successful result. The Agent may continue with platform evidence, reduce certainty, or return INCONCLUSIVE. Fabricated citations are forbidden.

# 4. Capability Plane

## 4.1 Goal

Bring Native and External tools under the same Discovery, Admission, Policy, Budget, Execution, Audit, and Evidence path. MCP is the current external-tool protocol; the platform abstraction is Tool Provider Architecture.

## 4.2 Capability discovery / registration

```text
MCP Server
    ↓
MCPToolProvider
    ↓
Capability Discovery
    ↓
Capability Normalization
    ↓
Registry Admission / Allowlist
    ↓
ToolRegistry
```

Provider describes capability. Registry admission decides whether it enters the Copilot catalog. Dynamic server tools are not automatically model-selectable.

## 4.3 Runtime invocation

```text
Agent
  ↓
ToolRegistry
  ↓
ToolPolicy
  ↓
ToolBudget
  ↓
ToolExecutor
  ↓
ToolProvider
  ├── NativeToolProvider → HISIEM
  └── MCPToolProvider    → MCP Server
  ↓
ToolResult
  ↓
EvidenceNormalizer
  ↓
Evidence
```

Provider owns capability description/invocation. Registry/Policy/Budget own governance.

## 4.4 ToolProvider responsibilities

Provider is responsible for:

- describe capabilities
- invoke capability
- normalize transport errors
- report provider identity
- report schema identity/version

Provider is not responsible for tenant/model authorization, tool budget, human approval, verdict, or response authorization.

## 4.5 Native provider

Existing HISIEM adapter evolves incrementally to satisfy ToolProvider boundaries. Do not rewrite the native tool stack merely to introduce the abstraction.

## 4.6 MCP server identity

Each MCP server requires controlled identity including server ID/type, trusted configuration source, endpoint identity, transport, enabled state, and trust policy. Unknown servers fail closed. The model cannot add servers dynamically by URL.

## 4.7 Tool schema identity

Each MCP Tool carries provider identity, tool name, schema identity/version or digest, normalized input schema, and normalized output contract. Incompatible schema changes fail closed. Newly discovered tools are not model-selectable by default.

## 4.8 Capability admission

Admission considers:

- server trust
- tool identity
- schema compatibility
- capability classification
- read/write risk
- tenant applicability
- result type
- allowlist

Final catalog classifications remain model-selectable, system-controlled, future catalog, forbidden.

## 4.9 MCP V1 scope

V1 is read-only. Suitable verbs: lookup/search/get/resolve/query/context retrieval. Write/high-risk capabilities such as block_ip, disable_user, isolate_host, delete, modify, execute are not model-selectable.

Future write capability still flows through Response Proposal -> Policy -> Human Approval -> Durable Execution; Agent -> MCP write -> execute is forbidden.

## 4.10 Tenant/auth

Tenant comes from Trusted Runtime Context, never model args, MCP result, browser request body, or prompt content. MCP credentials come from runtime config/secret provider and are never exposed to model, Evidence, ToolResult, logs, or telemetry.

## 4.11 Result bounds

Every external capability has timeout, max result size, max item count, max text size, and nested payload bounds. Oversized results are explicitly bounded/truncated or rejected with typed semantics; they are never silently stuffed into the model context.

## 4.12 Failure taxonomy

At minimum:

- TIMEOUT
- UNAVAILABLE
- AUTH_FAILURE
- SCHEMA_MISMATCH
- RATE_LIMITED
- INVALID_RESULT
- RESULT_TOO_LARGE
- PROVIDER_ERROR

Raw transport exceptions are not exposed directly to the model.

## 4.13 Audit

Persist/audit safe invocation facts such as tool identity, provider identity, investigation identity, trusted tenant context, attempt, status, safe error category, timestamps. Never persist credentials, authorization headers, secrets, or raw provider internal errors.

# 5. Observability Plane

## 5.1 Goal

Explain runtime behavior: where time is spent, where failures/retries occur, resource usage, provider reliability, and bottlenecks. Observability is not business audit.

## 5.2 Architecture

```text
Application / Runtime
        ↓
OpenTelemetry Instrumentation
        ↓
Context Propagation
        ↓
OTel Collector
     /      |      \
 Traces   Metrics   Logs
     \      |      /
      Observability Backend
              ↓
       Runtime Diagnostics
```

OTel provides tracing/context-propagation standards, metrics instrumentation, and logs-correlation integration. It does not persist business state.

## 5.3 Span taxonomy

High-level stable semantic spans:

```text
investigation.run
├── queue.wait
├── alert.hydrate
├── graph.invoke
│   ├── llm.call
│   ├── tool.execute
│   │   ├── native.call
│   │   └── mcp.call
│   ├── knowledge.retrieve
│   │   ├── embedding
│   │   ├── postgres.fts
│   │   ├── pgvector.search
│   │   └── retrieval.merge
│   └── investigation.persist
├── response.submit
└── response.observe
```

Span names should be semantically stable and not become a public mirror of LangGraph internal node names.

## 5.4 Context propagation

Propagate trace context across HTTP using W3C Trace Context. Durable dispatcher boundaries require explicit continuation/link strategy rather than thread-local assumptions.

Trace/logs may carry controlled high-cardinality correlation attributes such as investigation_id, tool_invocation_id, execution_command_id, provider execution refs.

## 5.5 Metrics cardinality

Metric labels must remain low-cardinality.

Allowed examples:

- tool_name
- tool_provider=native|mcp
- model_provider
- model_family
- retrieval_mode
- result
- error_category
- response_state
- operation

Forbidden as metric labels:

- investigation_id
- trace_id
- span_id
- tenant_id
- tool_invocation_id
- execution_command_id
- request_id
- user_id
- alert_id

## 5.6 Metrics baseline

Investigation:

- investigation.duration
- investigation.total
- investigation.failure

Agent:

- agent.steps
- agent.tool_calls
- agent.retry_count
- agent.loop_count

LLM:

- llm.duration
- llm.calls
- llm.errors
- llm.input_tokens
- llm.output_tokens

Tool:

- tool.duration
- tool.calls
- tool.errors
- tool.retries

MCP:

- mcp.duration
- mcp.calls
- mcp.errors

Retrieval:

- retrieval.duration
- retrieval.hit_count
- retrieval.errors

Durable runtime:

- durable.queue_depth
- durable.retry_count
- durable.dead_letter_count

Response:

- response.submit_duration
- response.observe_duration
- response.attention_required

## 5.7 Structured logging

No mandatory rewrite to OTel Logs API. Python structured logging + trace/span injection + business correlation IDs + Collector/log backend is acceptable. Requirements are machine-readable fields, consistent taxonomy, trace correlation, and redaction.

## 5.8 Telemetry data policy

Do not record by default:

- raw prompt
- raw model response
- full ToolResult
- full Alert/Event payload
- authorization header
- session token
- service bearer
- MCP secret
- DB credential
- `.env.local`
- embedding vectors

Prefer operation/provider/model/status/error-category/duration/counts/token-count/retry-count/retrieval-mode/tool-name/provider-type.

## 5.9 Sampling

Development/test may use 100% sampling. Production must support configurable sampling. Error/exception retention may be higher. No vendor-specific sampling policy belongs in Domain/Application layers.

## 5.10 Collector/backend

Application emits standard OTel signals. Collector receives, batches, filters/redacts as needed, and exports. Business code must not bind directly to Jaeger/Grafana/Phoenix/LangSmith-specific tracing APIs.

Backend product remains implementation-time choice but must support trace search, metrics query, log search, correlation, dashboards, and retention controls. Phoenix/LangSmith remain optional after OTel baseline.

## 5.11 Truth boundary

Telemetry != Business Truth. Telemetry outage must not break Investigation, Evidence, Finding, Verdict, Approval, Execution Command, or HISIEM SOAR Result.

# 6. Analyst Experience Plane

## 6.1 Goal

Turn Investigation, Evidence, Knowledge, Finding, Response, Approval, and Execution into an analyst-operable and auditable product experience. Workspace != ChatGPT Clone.

## 6.2 Core analyst workflow

```text
Alert
  ↓
Open / Start Investigation
  ↓
Understand Current Investigation State
  ↓
Review Evidence
  ↓
Review Findings
  ↓
Understand Supporting Knowledge
  ↓
Review Investigation Result
  ↓
Review Response Proposal
  ↓
Human Decision
  ↓
Observe Execution
```

## 6.3 Information architecture

V1 must expose:

- Investigation State
- Alert Context
- Investigation Activity
- Evidence
- Findings
- Knowledge / Citation
- Investigation Result / Verdict
- Response Proposal
- Approval
- Execution
- Audit / Timeline

No fixed 3-column layout or fixed tab count is frozen here. Layout belongs to implementation design.

## 6.4 Authority UX

Must distinguish with label + icon + semantic wording, not color only:

- Platform Fact
- Knowledge Context
- Model-derived Finding
- Agent Verdict
- Human Decision
- Execution Result

Frozen semantic distinctions:

- Agent Verdict != Analyst Disposition
- Policy Decision != Human Approval
- Human Approval != Execution
- Submission != Execution Success
- Copilot Response Status != HISIEM Execution Truth

Final execution truth is HISIEM SOAR observed state.

## 6.5 Evidence UX

Evidence first answers what it means to the analyst. Primary: summary/observation, source class, authority class, observed/retrieved time, related finding/entity. IDs/hash/dedup/citation/raw reference/provider metadata are secondary technical provenance.

## 6.6 Knowledge UX

Knowledge Evidence is visibly Supporting Context and shows source title, citation, document/source kind, source version, relevant excerpt/content, retrieval time, and ATT&CK release where applicable. It must not masquerade as an observed detection fact.

## 6.7 Finding UX

Finding must link to supporting Evidence IDs and allow Finding -> cited Evidence drill-down. Natural-language Finding without source linkage is insufficient.

## 6.8 Investigation Result UX

Show disposition, confidence, supporting findings, uncertainty, limitations. Label as AI Investigation Verdict rather than Analyst Disposition.

## 6.9 Activity / Trace strategy

V1 does not require a full DAG. Priority:

- Current State
- Activity Feed / Timeline
- Evidence linkage
- Finding citations
- Response lifecycle

Graph is optional where it measurably improves analyst comprehension.

Execution Trace is optional business visualization, not Workspace architecture. Edges require durable explicit relationship. Timestamp proximity may sort a timeline but may not infer causal edges. Never show CoT, raw prompt/model response, LangGraph checkpoints/internal node names, or hidden reasoning.

Timeline = chronological reconstruction. Trace = explicit causal/lifecycle relationships.

## 6.10 Frontend technology/trust

Continue Vue 3 + Vite + Ant Design Vue + Vue Router. Vue Flow is optional visualization only. Do not migrate to React/Next.js/Vercel AI SDK for stack coverage.

Trust remains Browser -> HISIEM Authentication -> HISIEM BFF -> Copilot Trusted Context. No direct browser service credential and no second Copilot login/user/token system.

## 6.11 Commands and refresh

Approve/Reject/Cancel/Start Investigation/Submit Response must call formal application boundaries and cannot mutate state directly in UI.

Refresh reconstructs from Durable Backend State -> Workspace Query -> UI. Do not rely on browser-only workflow/trace state.

Polling remains baseline. SSE/WebSocket requires a real sub-second/high-frequency/streaming need.

# 7. Cross-plane contracts

## 7.1 Knowledge -> Analyst Experience

Workspace consumes validated/persisted Knowledge Evidence, not raw retrieval hits as formal Evidence.

## 7.2 Capability -> Knowledge

Knowledge tools still pass through Registry -> Policy -> Budget -> Executor. Knowledge Plane does not bypass Tool governance.

## 7.3 Capability -> Observability

Native/MCP tools emit common telemetry: tool name, provider type, duration, status, error category, retry.

## 7.4 Knowledge -> Observability

Record retrieval mode, duration, hit count, provider/profile category, error category; do not record full content, embeddings, or full query text by default.

## 7.5 Observability -> Analyst Experience

Operational telemetry is not the default analyst workspace content. Workspace shows business state. Runtime diagnostics belong to admin/operations experience; links may be added without letting telemetry override durable facts.

## 7.6 Shared identifiers

Cross-plane durable correlation IDs may include investigation_id, tool_invocation_id, evidence_id, finding_id, response_proposal_id, execution_command_id. They may support audit/trace correlation/workspace drill-down but are not metrics labels.

# 8. Unified security and failure contracts

All planes obey:

- Tenant from trusted context
- Secrets never model-visible
- Secrets never Evidence
- Secrets never telemetry
- Prompt injection remains DATA
- External provider result remains DATA
- Frontend cannot elevate authority

Prefer typed failures such as KnowledgeUnavailable, RetrievalUnavailable, ToolUnavailable, ProviderUnavailable, SchemaMismatch, PermissionDenied, BudgetExceeded, Timeout, RateLimited, InvalidResult. Avoid `catch Exception -> empty result` because failure and successful-empty are different semantics.

# 9. Stable foundations not being re-architected

- Domain: Investigation != LangGraph Thread; Evidence != ToolResult; Verdict != LLM Response; Approval != Agent Decision
- Agent Runtime: Python + FastAPI + LangGraph + OpenAI-compatible provider abstraction
- Persistence: PostgreSQL + Alembic + pgvector + LangGraph checkpoint schema
- Durable Execution: durable command + idempotency + dispatcher + retry + dead-letter + submit/observe
- Human Authority: Model proposes -> Policy constrains -> Human authorizes
- HISIEM Integration: HISIEM Auth/BFF + Trusted Tenant Context + HISIEM SOAR execution truth
- Evaluation: sealed datasets + focused evaluation + regression gates

# 10. Explicitly deferred / not introduced now

- Redis
- Kafka
- Celery
- Temporal
- A2A
- Multi-Agent
- Sandbox
- independent Vector DB
- React migration
- Next.js migration
- Vercel AI SDK
- SSE/WebSocket
- Model Router
- LangSmith/Phoenix as primary observability

# 11. Implementation sequence freeze

Stage A — Knowledge Closure Validation
- formal contract validation
- integration verification
- evaluation extension
- no redevelopment

Stage B — Observability Foundation
- OTel base
- context propagation
- trace taxonomy
- metrics foundation
- structured-log correlation
- Collector baseline
- first instrument FastAPI, HTTP, PostgreSQL, Investigation Run, Tool Execution

Stage C — Capability / MCP
- ToolProvider abstraction
- MCP server identity
- discovery/normalization/admission
- schema identity/version
- runtime invocation
- result bounds
- typed failures
- telemetry from day one
- read-only V1

Stage D — Analyst Experience Productization
- use stable Knowledge/Capability/Authority/Observability boundaries
- implement frozen IA and semantic contracts
- do not precommit to 3-column/DAG/tab count/full-SIEM redesign

Stage E — Cross-Plane Integration
- validate Alert -> Investigation -> Tool -> Knowledge -> Evidence -> Finding -> Verdict -> Response -> Approval -> Durable Command -> HISIEM Execution
- validate telemetry, UI, tenant, authority, retry, security, and evaluation

# 12. Acceptance by plane

## Knowledge

- Agent uses Knowledge tools
- Citation revalidated
- Knowledge becomes immutable Evidence
- Finding cites Evidence
- ATT&CK exact resolution
- Tenant isolation
- Prompt injection remains DATA
- Knowledge-only cannot bypass verdict policy

## Capability

- Unknown MCP server rejected
- Dynamic tool not automatically selectable
- Schema drift fails closed
- Read-only tool admitted correctly
- Forbidden/write tool hidden
- Tenant cannot be spoofed
- Oversized result bounded
- Timeout typed
- MCP result normalized
- Evidence path unchanged
- Provider cannot bypass Policy/Budget

## Observability

- Investigation runtime trace reconstructable
- Agent/LLM/tool/retrieval spans visible
- MCP spans visible
- async dispatcher correlation works
- metrics stay low-cardinality
- logs are trace-correlated
- secrets/raw prompts/results absent
- observability outage does not break business flow

## Analyst Experience

- analyst understands current investigation state
- Evidence inspectable
- Finding -> Evidence works
- Knowledge visibly Supporting Context
- Agent Verdict != Analyst Disposition
- Approval != Execution
- Submission != Execution Success
- HISIEM result is execution truth
- refresh reconstructs Workspace
- no CoT/raw prompt/checkpoint exposed

# 13. Architecture change control

Implementation may not silently modify Knowledge authority, Tool governance authority, MCP admission semantics, Human approval boundary, Execution truth, Tenant source, Telemetry truth boundary, or UI authority semantics.

If a real contradiction appears:

1. identify the concrete contradiction
2. show code/runtime evidence
3. propose the smallest contract change
4. assess impact across all four Planes
5. freeze the revised contract before implementation continues

# 14. Final frozen principles

1. Knowledge provides context, not authority.
2. Provider exposes capability; Registry/Policy grant usability.
3. Telemetry explains runtime; it never becomes business truth.
4. Workspace presents authority; it never invents authority.
5. New components reuse existing tenant, audit, durability, and authority boundaries.
6. No new technology is introduced only for stack coverage.
7. No second truth source.
8. No second parallel architecture.
