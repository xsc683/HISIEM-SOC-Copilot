# HISIEM SOC Copilot — Architecture Diagram & Data Flow Diagram

Two canonical diagrams for the system:

1. **[Architecture diagram](#1-architecture-diagram)** — *what the system is made of*
2. **[Data flow diagram](#2-data-flow-diagram)** — *what happens across one investigation lifecycle*

Companion documents: [`architecture-overview.md`](architecture-overview.md) (eight-figure visual
model), [`domain-model.md`](domain-model.md), [`python-package-boundary.md`](python-package-boundary.md),
[`application-commands-domain-events-langgraph-state.md`](application-commands-domain-events-langgraph-state.md).

> **Scope.** These diagrams describe **this repository**. The security platform it runs on —
> ingestion, streaming detection, alerts, cases and deterministic SOAR execution — is the sibling
> repository **[HISIEM](https://github.com/xsc683/HISIEM)**, shown here as an external participant.

---

## 1. Architecture Diagram

Plane-oriented: every component grouped by the responsibility plane that owns it, with the
governance and authority chain made explicit as a flow.

> **On the plane model — why "four" and "eleven" both appear.**
> The frozen architecture contract names **eleven** responsibility planes, and this diagram
> groups components by exactly those eleven. The machine-readable form is the `Plane` enum in
> `evaluation/cross_plane/contracts.py`, whose own docstring says it is *"the planes named by
> the architecture freeze — **not a new taxonomy**"*.
>
> "**Four-plane**" is a different thing: it is the framing of which planes this repository
> **closes** (Knowledge, Capability, Observability, Analyst Experience), with the remaining
> seven treated as **reused rather than re-architected**.
>
> So the two are **not a 1:1 mapping**, and they are not competing taxonomies either: four is a
> **subset** of eleven, presented for a different purpose — *what this project closes* versus
> *which plane a gate belongs to*. **Do not change either side to make the counts agree.**

```mermaid
graph TB

    %% =========================
    %% HISIEM
    %% =========================
    subgraph HISIEM["HISIEM Security Platform — sibling repository"]
        ALERT["Alert"]
        HISIEM_API["HISIEM API / Adapter"]
        SOAR["SOAR Execution"]
        EXEC_RESULT["Observed Execution Result"]
    end

    %% =========================
    %% Agent Runtime
    %% =========================
    subgraph Runtime["Agent Runtime Plane"]
        INV["Investigation"]
        GRAPH["LangGraph Investigation Graph"]
        STATE["InvestigationGraphState"]
        RUNTIME_BUDGET["Runtime Budget"]
    end

    %% =========================
    %% Capability
    %% =========================
    subgraph Capability["Capability / Tool Provider Plane"]
        REGISTRY["ToolRegistry"]
        POLICY["ToolPolicy"]
        TOOL_BUDGET["ToolBudget"]
        EXECUTOR["ToolExecutor"]
        ROUTER["ProviderRouter"]
        NATIVE["NativeToolProvider"]
        MCP["MCPToolProvider"]
        EXT["MCP External Server"]
        TOOL_RESULT["ToolResult"]
    end

    %% =========================
    %% Knowledge
    %% =========================
    subgraph Knowledge["Knowledge Plane"]
        INGEST["Knowledge Ingestion"]
        RETRIEVAL["Hybrid Retrieval"]
        FTS["PostgreSQL FTS"]
        VECTOR["pgvector"]
        RRF["RRF"]
        CITATION["Citation Validation"]
    end

    %% =========================
    %% Evidence / Domain
    %% =========================
    subgraph Domain["Domain Plane"]
        NORMALIZER["EvidenceNormalizer"]
        EVIDENCE["Evidence"]
        FINDING["Finding"]
        VERDICT["Investigation Result / Verdict"]
        PROPOSAL["Response Proposal"]
    end

    %% =========================
    %% Human Authority
    %% =========================
    subgraph Authority["Human Authority Plane"]
        POLICY_DECISION["Policy Decision"]
        HUMAN["Human Decision"]
        COMMAND["Durable Command"]
    end

    %% =========================
    %% Durable Execution
    %% =========================
    subgraph Durable["Durable Execution Plane"]
        OUTBOX["Outbox"]
        DISPATCHER["Outbox Dispatcher"]
        OBSERVE["Execution Observation / Reconciliation"]
    end

    %% =========================
    %% Persistence
    %% =========================
    subgraph Persistence["Persistence Plane"]
        PG[("PostgreSQL")]
        CHECKPOINT[("LangGraph Checkpoint")]
    end

    %% =========================
    %% Workspace
    %% =========================
    subgraph Workspace["Analyst Workspace Plane"]
        WS_SERVICE["InvestigationWorkspaceService"]
        PROJECTION["Workspace Projection"]
        UI["Analyst Workspace"]
    end

    %% =========================
    %% Observability
    %% =========================
    subgraph Observability["Observability Plane"]
        OTEL["OpenTelemetry"]
        TRACE["Traces"]
        METRIC["Metrics"]
    end

    %% =========================
    %% Evaluation
    %% =========================
    subgraph Evaluation["Evaluation Plane"]
        GP["GP-01"]
        KB["KB-GOLDEN-V1"]
        XP["XP-01"]
    end

    %% ---- Main investigation flow ----
    ALERT --> HISIEM_API
    HISIEM_API --> INV
    INV --> GRAPH
    GRAPH --> STATE
    GRAPH --> REGISTRY

    %% ---- Capability governance ----
    REGISTRY --> POLICY
    POLICY --> TOOL_BUDGET
    TOOL_BUDGET --> EXECUTOR
    EXECUTOR --> ROUTER
    ROUTER --> NATIVE
    ROUTER --> MCP
    MCP --> EXT

    %% ---- Tool results ----
    NATIVE --> TOOL_RESULT
    MCP --> TOOL_RESULT

    %% ---- Knowledge capability ----
    EXECUTOR --> RETRIEVAL
    RETRIEVAL --> FTS
    RETRIEVAL --> VECTOR
    FTS --> RRF
    VECTOR --> RRF
    RRF --> CITATION
    CITATION --> TOOL_RESULT
    INGEST --> RETRIEVAL

    %% ---- Evidence grounding ----
    TOOL_RESULT --> NORMALIZER
    NORMALIZER --> EVIDENCE
    EVIDENCE --> FINDING
    FINDING --> VERDICT
    VERDICT --> PROPOSAL

    %% ---- Authority lifecycle ----
    PROPOSAL --> POLICY_DECISION
    POLICY_DECISION --> HUMAN
    HUMAN --> COMMAND

    %% ---- Durable execution ----
    COMMAND --> OUTBOX
    OUTBOX --> DISPATCHER
    DISPATCHER --> SOAR
    SOAR --> EXEC_RESULT
    EXEC_RESULT --> OBSERVE

    %% ---- Persistence ----
    INV --> PG
    EVIDENCE --> PG
    FINDING --> PG
    VERDICT --> PG
    PROPOSAL --> PG
    POLICY_DECISION --> PG
    HUMAN --> PG
    COMMAND --> PG
    OBSERVE --> PG
    STATE --> CHECKPOINT

    %% ---- Workspace reconstructs persisted truth ----
    PG --> WS_SERVICE
    WS_SERVICE --> PROJECTION
    PROJECTION --> UI

    %% ---- Observability ----
    OTEL --> TRACE
    OTEL --> METRIC
    GRAPH -. observed by .-> OTEL
    EXECUTOR -. observed by .-> OTEL
    RETRIEVAL -. observed by .-> OTEL
    DISPATCHER -. observed by .-> OTEL
    HISIEM_API -. observed by .-> OTEL

    %% ---- Evaluation ----
    GP -. evaluates .-> GRAPH
    KB -. evaluates .-> RETRIEVAL
    XP -. evaluates .-> Domain
    XP -. evaluates .-> Authority
    XP -. evaluates .-> Durable
    XP -. evaluates .-> Workspace
```

### What this diagram solves

It answers **"which plane owns which responsibility, and where does authority actually change
hands?"** The vertical chain `ToolResult → Evidence → Finding → Verdict → Proposal → Policy →
Human → Command → SOAR → Observed Result` is the whole system in one line, and each arrow is a
boundary that a plausible implementation would collapse.

### Component responsibilities

| Plane | Owns | Notable non-responsibility |
|---|---|---|
| **Agent Runtime** | Graph orchestration, bounded working state, runtime budget | **Not** business authority |
| **Capability / Tool Provider** | Discovery, admission, selection, invocation, bounded results | **Not** a policy or authorization authority |
| **Knowledge** | Retrieval, fusion, citation validation | **Not** verdict authority |
| **Domain** | Aggregates, invariants, findings, verdict, proposal | Not transport, not orchestration |
| **Human Authority** | Policy evaluation, human consent, durable command | Policy ≠ consent; consent ≠ execution |
| **Durable Execution** | Outbox, dispatch, reconciliation | Not execution truth |
| **Persistence** | PostgreSQL as business truth; LangGraph checkpoint as working memory | Checkpoint is never business state |
| **Analyst Workspace** | Presentation projection of persisted truth | **Not** an authority; invents nothing |
| **Observability** | Traces, metrics, log correlation | **Not** business truth |
| **Evaluation** | GP-01, KB-GOLDEN-V1, XP-01 | Does not write back into production |
| **HISIEM** (sibling) | Security data, detection, alerts, cases, **execution truth** | — |

### Where truth and authority live

```text
Business truth        = domain aggregates persisted in PostgreSQL
Execution truth       = the state HISIEM observes (never Copilot's belief about what it sent)
Orchestration state   = LangGraph checkpoint — working memory only, never business state
Telemetry             = fully side-channel; no business path reads spans or collector state
Model authority       = none. The model proposes; it has no code path to authorize or execute.
```

### The authority boundary

```text
Model-influenced:  ToolResult → Evidence → Finding → Verdict → Response Proposal
─────────────────────────── Authority Boundary ───────────────────────────
Deterministic / human / observed:
                   Policy → Human Decision → Durable Command → HISIEM Execution → Observation
```

Everything above the line is probabilistic. Everything below is a deterministic function, a
person, or an observed fact.

### Reliability boundaries

| # | Boundary | Where | What can go wrong |
|---|---|---|---|
| 1 | Tool boundary | `ToolExecutor` → provider | A typed failure must produce **zero Evidence**, never an empty result |
| 2 | Admission boundary | MCP discovery → registry | An unadmitted capability must be visible to operators but unselectable by the model |
| 3 | Knowledge boundary | retrieval → citation validation | A citation that does not resolve must be rejected, not rendered |
| 4 | Authority boundary | proposal → policy → human | No path may let model output authorize a side effect |
| 5 | Durability boundary | approval → outbox → dispatch | A restart must not lose the command nor create a second business intent |
| 6 | Truth boundary | HISIEM observed state vs. Copilot record | HISIEM wins, always |
| 7 | Workspace boundary | persisted state → projection | A stale client must never win over newer server truth |

---

## 2. Data Flow Diagram

One investigation, from the alert that starts it to the workspace that reconstructs it.

```mermaid
sequenceDiagram
    participant HISIEM as HISIEM Platform
    participant API as Copilot API
    participant Domain as Domain / Persistence
    participant Graph as LangGraph Agent
    participant Tools as Tool Executor
    participant Native as Native Provider
    participant Know as Knowledge Provider
    participant MCP as MCP Provider
    participant Evidence as Evidence Normalizer
    participant Policy as Policy Engine
    participant Human as Analyst
    participant Outbox as Durable Outbox / Dispatcher
    participant SOAR as HISIEM SOAR
    participant Workspace as Workspace Service

    %% =========================
    %% Investigation bootstrap
    %% =========================

    HISIEM->>API: Create investigation(alert_ref)
    API->>Domain: Create Investigation
    Domain-->>API: Investigation(id, CREATED)

    API->>Graph: Start investigation

    Note over API,HISIEM: System-controlled hydration, not model-selected capability

    API->>HISIEM: Resolve alert context
    HISIEM-->>API: AlertContext snapshot
    API->>Domain: Persist hydrated context

    %% =========================
    %% Agent investigation
    %% =========================

    rect rgb(240,248,255)
        Note over Graph,Evidence: Investigation Loop
    end

    Graph->>Graph: Plan / decide_next

    loop Until FINALIZE or runtime budget exhausted

        Graph->>Tools: Execute candidate tool

        Tools->>Tools: Registry → Policy → Budget

        alt Native platform capability
            Tools->>Native: invoke(tool, trusted context)
            Native->>HISIEM: Read platform data
            HISIEM-->>Native: Platform result
            Native-->>Tools: ToolResult

        else Knowledge capability
            Tools->>Know: retrieve(query, tenant context)
            Know->>Know: FTS + pgvector + RRF
            Know->>Know: Citation validation
            Know-->>Tools: ToolResult

        else Admitted read-only MCP capability
            Tools->>MCP: invoke admitted capability
            MCP-->>Tools: ToolResult
        end

        Tools-->>Graph: ToolResult

        Graph->>Evidence: normalize(ToolResult)
        Evidence->>Evidence: Validate / bound / hash / deduplicate
        Evidence->>Domain: Persist Evidence

        Note over Evidence,Domain: ToolResult != Evidence
        Note over Evidence,Domain: Knowledge Evidence != Platform Evidence

        Graph->>Graph: Update hypotheses / decide_next
    end

    %% =========================
    %% Findings and verdict
    %% =========================

    Graph->>Domain: Record Findings grounded in Evidence
    Note over Graph,Domain: Evidence != Finding

    Graph->>Domain: Finalize InvestigationResult / Verdict
    Note over Graph,Domain: Finding != Verdict

    %% =========================
    %% Response proposal
    %% =========================

    Graph->>Domain: Create ResponseProposal
    Domain->>Policy: Evaluate proposal

    alt Policy DENY
        Policy-->>Domain: PolicyDecision(DENY)
        Domain->>Domain: Persist decision
        Note over Policy,Human: Policy DENY blocks execution path

    else Policy REQUIRE_APPROVAL
        Policy-->>Domain: PolicyDecision(REQUIRE_APPROVAL)
        Domain->>Domain: Persist proposal + policy decision

        Note over Policy,Human: Policy REQUIRE_APPROVAL != Human Approval

        %% =========================
        %% Workspace projection
        %% =========================

        Human->>Workspace: Open investigation workspace
        Workspace->>Domain: Load persisted investigation truth
        Domain-->>Workspace: Evidence + Findings + Verdict + Proposal + Policy state
        Workspace-->>Human: Read-model projection

        %% =========================
        %% Human authority
        %% =========================

        Human->>API: Approve proposal
        API->>Domain: Validate proposal revision/hash

        alt Proposal stale or changed
            Domain-->>API: Reject stale authorization
            API-->>Human: Approval rejected / refresh required

        else Proposal still valid
            API->>Domain: Persist ApprovalDecision(APPROVED)
            API->>Domain: Create DurableCommand
            Domain->>Outbox: Persist execution intent

            Note over Human,Outbox: Human Approval != Execution
        end
    end

    %% =========================
    %% Durable execution
    %% =========================

    rect rgb(240,255,240)
        Note over Outbox,SOAR: Durable Execution
    end

    Outbox->>SOAR: Submit execution command

    alt Submission acknowledged
        SOAR-->>Outbox: execution_id
        Outbox->>Domain: Persist submission reference

    else Submission outcome uncertain
        SOAR--xOutbox: Timeout / response lost
        Outbox->>Domain: Persist uncertain submission state
        Note over Outbox,Domain: Do not fabricate SUCCESS or FAILED
    end

    %% =========================
    %% Execution observation
    %% =========================

    loop Observe / reconcile until terminal or attention required
        Outbox->>SOAR: Query execution state
        SOAR-->>Outbox: Current execution state

        alt Terminal state observed
            Outbox->>Domain: Persist observed execution result
            Note over Outbox,Domain: HISIEM observed state = final execution truth

        else Still running
            Outbox->>Outbox: Schedule next observation

        else Cannot safely establish terminal truth
            Outbox->>Domain: Persist ATTENTION_REQUIRED
            Note over Outbox,Domain: ATTENTION_REQUIRED != SUCCESS / REJECTED
        end
    end

    %% =========================
    %% Workspace reconstruction
    %% =========================

    Human->>Workspace: Refresh / reopen workspace
    Workspace->>Domain: Reload persisted business truth
    Domain-->>Workspace: Current authoritative state
    Workspace-->>Human: Reconstructed workspace

    Note over Domain,Workspace: Server truth wins over stale client state
```

### What this diagram solves

It answers **"in what order does authority change hands, and where can the flow go wrong?"**
Three `alt` blocks carry most of the design:

| Branch | What it protects |
|---|---|
| `Proposal stale or changed` | An approval authorizes a **specific intent**, not a general permission |
| `Submission outcome uncertain` | The system records uncertainty instead of guessing a terminal state |
| `Cannot safely establish terminal truth` | `ATTENTION_REQUIRED` stays a distinct state from `SUCCESS` / `REJECTED` |

### The distinctions the diagram is built to preserve

```text
ToolResult              !=  Evidence
Knowledge Evidence      !=  Platform Evidence
Evidence                !=  Finding
Finding                 !=  Verdict
Policy                  !=  Human Approval
Human Approval          !=  Execution
Submission outcome      !=  Execution success
ATTENTION_REQUIRED      !=  SUCCESS / REJECTED
HISIEM observed state   ==  final execution truth
```

### Load-bearing ordering

```text
1. Hydration is SYSTEM-CONTROLLED, not a model-selected capability.
   The alert context is resolved before the model makes any choice.

2. Registry → Policy → Budget happen BEFORE the provider is invoked.
   A denied or over-budget call must cost nothing on the remote side.

3. Evidence is persisted by the DOMAIN, not held in graph state.
   The checkpoint is working memory; the persisted Evidence is the fact.

4. The approval validates the proposal revision/hash BEFORE the decision is recorded.
   A stale approval is rejected rather than silently applied.

5. Execution intent is persisted to the outbox BEFORE any submission is attempted.
   A restart resumes from the record, not from an in-flight call.

6. Observation persists the state HISIEM reports — not the state Copilot hoped for.
```

---

## Diagram source note

The two diagrams above were supplied as standalone Mermaid sources and are reproduced here as the
canonical pair. **One correction was applied against the code**, consistent with this
repository's rule that the current implementation is the authority:

| Was | Now | Evidence |
|---|---|---|
| `PolicyDecision(ALLOW)` / "Policy ALLOW != Human Approval" | `PolicyDecision(REQUIRE_APPROVAL)` / "Policy REQUIRE_APPROVAL != Human Approval" | `domain/response/enums.py` defines `PolicyDecision = DENY \| REQUIRE_APPROVAL`. There is **no `ALLOW`** value anywhere in the implementation. |

The `else` branch of the policy evaluation is therefore "not denied", which in this system always
means "approval required" — a distinction worth keeping exact, because it is precisely the branch
where human authority enters the flow.
