# Stage E — Cross-Plane Integration & Evaluation Spec

**Goal:** Prove that Knowledge, Capability, Observability, and Analyst Experience operate as one governed platform while the stable Domain/Durable/Human/HISIEM/Evaluation foundations remain authoritative.

## 1. End-to-end reference flow

```text
Alert
→ Investigation
→ Native/MCP Tool
→ Knowledge Retrieval / ATT&CK
→ Evidence
→ Finding
→ Investigation Result / Verdict
→ Response Proposal
→ Policy
→ Human Approval
→ Durable Execution Command
→ HISIEM SOAR
→ Observed Execution Result
→ Workspace reconstruction
```

Observability traces the runtime path but never substitutes for any business state above.

## 2. Integration contracts to prove

### 2.1 Knowledge + Capability

Knowledge tools remain normal governed capabilities through Registry/Policy/Budget/Executor. No Knowledge-specific bypass.

### 2.2 Capability + Observability

Native and MCP tool paths emit the same semantic tool telemetry with provider-type distinction.

### 2.3 Knowledge + Observability

Retrieval records safe mode/duration/hit count/errors without full content or embeddings.

### 2.4 Knowledge + Workspace

Workspace shows persisted/validated Knowledge Evidence and its citations, not raw unvalidated retrieval hits as formal Evidence.

### 2.5 Human Authority + Workspace

UI presents Proposal, Policy, Approval, Submission, and HISIEM execution as distinct states and only invokes formal commands.

### 2.6 Durable Execution + Observability

Async submit/observe paths maintain business idempotency independently of trace availability; OTel context is correlation only.

## 3. Security scenarios

Must cover:

- Tenant A cannot retrieve Knowledge or MCP data belonging to B
- model cannot specify tenant/MCP server credential
- prompt injection in Knowledge remains DATA
- prompt injection in MCP result remains DATA
- dynamic MCP write capability is not model-selectable
- unknown MCP server fails closed
- schema drift fails closed
- secrets absent from Evidence/logs/traces/metrics
- high-cardinality IDs absent from metrics labels

## 4. Authority scenarios

Must prove:

- Knowledge-only cannot bypass current definitive-verdict rule
- Platform Evidence + Knowledge follows normal Investigation rules
- Agent Verdict != Analyst Disposition
- Policy Decision != Human Approval
- Human Approval != Execution
- Submission != Execution Success
- HISIEM observed result remains execution truth
- telemetry loss does not alter business outcome

## 5. Reliability scenarios

Cover:

- Tool timeout
- MCP unavailable
- Knowledge unavailable
- retrieval failure vs successful empty result
- provider oversized result
- durable retry/dead-letter behavior where applicable
- response submit uncertainty -> ATTENTION_REQUIRED semantics
- frontend refresh/stale behavior
- Collector/observability backend outage

## 6. Evaluation additions

Extend the existing Evaluation Plane rather than creating a new framework. Add focused datasets/scenarios for:

- Knowledge grounding/citation validity
- Knowledge authority guard
- MCP tool-selection/admission behavior
- tenant isolation across MCP/Knowledge
- prompt-injection resistance
- typed failure behavior
- UI authority semantics where E2E automation is appropriate

Keep GP-01 and existing sealed datasets immutable unless the evaluation contract explicitly requires a new version/new gate.

## 7. Observability acceptance

For at least one representative Investigation, demonstrate trace/metric/log correlation across:

- API
- durable dispatcher where used
- Agent runtime
- LLM
- Tool execution
- MCP if involved
- Knowledge retrieval
- HISIEM HTTP
- PostgreSQL
- response submit/observe if involved

No raw prompt/full ToolResult/secrets.

## 8. Workspace acceptance

Representative flows:

- RUNNING Investigation
- COMPLETED Investigation
- Knowledge Evidence selected
- Finding -> Evidence
- waiting approval
- rejected approval
- successful execution observation
- ATTENTION_REQUIRED
- refresh reconstruction
- transient fetch/stale state

## 9. Test strategy

Use stage-scoped regression first:

Copilot:

- ruff
- mypy
- focused Agent/Knowledge/Capability/Observability/Response tests
- affected architecture/security tests

HISIEM backend:

- affected BFF/auth/SOAR contracts if changed

Frontend:

- lint
- tests
- build
- targeted Playwright/browser acceptance

Run broader suites only when shared-global contracts changed or focused failures indicate cross-context regression.

## 10. Environment / trust inventory

Final integration report should record non-secret runtime inventory:

- component
- repo/branch/final SHA
- local path
- start command
- host/port
- health endpoint
- dependency
- validation status
- browser login/trust path
- service-to-service auth mode
- actual secrets REDACTED / never printed

## 11. Final acceptance

Stage E passes when the four new planes are integrated without introducing a new authority/truth source, core tenant/HITL/durable boundaries remain intact, telemetry is safe and diagnostic, MCP remains governed, Knowledge remains supporting context, and the Workspace accurately presents persisted business truth.

