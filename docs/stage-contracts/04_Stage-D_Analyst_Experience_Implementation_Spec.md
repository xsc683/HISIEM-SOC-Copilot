# Stage D — Analyst Experience Productization Implementation Spec

**Goal:** Productize the Investigation Workspace around the frozen analyst workflow and authority semantics without re-opening a full-SIEM redesign or precommitting to a graph-heavy UI.

## 1. Product scope

Implement the Investigation Workspace experience for:

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

This is not a full HISIEM UI redesign.

## 2. Technology/trust constraints

Keep:

- Vue 3
- Vite
- Ant Design Vue
- Vue Router
- HISIEM browser authentication
- HISIEM BFF -> Copilot trusted context
- existing polling baseline unless a real streaming need is proven

Do not add React/Next.js/Vercel AI SDK or a second Copilot browser login.

## 3. UX design freeze before coding

Before implementation, create a concise implementation-level UX brief covering:

- page information hierarchy
- navigation model
- how current state/activity/evidence/findings/knowledge/response are reached
- Evidence/Finding drill-down behavior
- authority visual semantics
- responsive behavior
- loading/error/empty/stale behavior
- whether any graph is justified

Do not freeze a 3-column layout or tab count unless the UX review supports it.

## 4. Authority semantic contract

UI must visibly distinguish, using wording + icon + restrained semantic treatment:

- Platform Fact
- Knowledge Context
- Model-derived Finding
- Agent Verdict
- Human Decision
- Execution Result

Frozen distinctions:

- Agent Verdict != Analyst Disposition
- Policy Decision != Human Approval
- Human Approval != Execution
- Submission != Execution Success
- HISIEM observed SOAR state = final execution truth

## 5. Investigation State

The primary workspace landing view must immediately answer:

- what Investigation this is
- current status/phase
- whether analyst action is needed
- Evidence/Findings counts or equivalent summary
- current/final Verdict state
- Response/Approval/Execution state
- last refresh / stale state

Do not make the analyst hunt through multiple tabs to understand current state.

## 6. Activity / Timeline

Use chronological durable facts to show what happened and when. Activity is allowed to include Tool/Knowledge/Response lifecycle events when they are durable/safe.

Timeline is chronological reconstruction, not proof of causality.

## 7. Trace / graph policy

Graph is optional.

Only add Vue Flow when a specific causal/lifecycle relationship materially improves comprehension and is supported by explicit durable references.

Forbidden:

- infer edges from timestamp proximity
- expose CoT
- expose raw prompt/model response
- expose LangGraph checkpoint/internal nodes
- create frontend-only trace truth

A simple Activity Feed + Evidence/Finding linkage is acceptable V1 and preferred over decorative graph complexity.

## 8. Evidence UX

Evidence primary view:

- analyst-relevant observation/summary
- source class
- authority class
- observed/retrieved time
- related entity
- related Finding(s)

Secondary provenance:

- IDs
- citation ID
- hashes
- raw reference
- provider metadata
- retrieval technical metadata

Knowledge Evidence is explicitly labeled Supporting Context.

## 9. Finding UX

Every Finding exposes supporting Evidence linkage. Analyst can open cited Evidence directly. Do not present a model-generated Finding without source linkage as an authoritative result.

## 10. Knowledge / Citation UX

Show:

- source title
- citation
- source/document kind
- source version
- relevant excerpt/content
- retrieval time
- ATT&CK release/technique identity where applicable

Do not display vector/RRF scores as authority ratings.

## 11. Verdict UX

Show:

- AI Investigation Verdict label
- disposition
- confidence
- supporting Findings
- uncertainty/limitations

Never conflate with Analyst Disposition.

## 12. Response / Approval / Execution UX

Make the lifecycle explicit:

```text
Agent Recommendation
→ Policy Constraint
→ Human Approval/Rejection
→ Durable Command
→ Submission
→ HISIEM Observed Execution
```

ATTENTION_REQUIRED must be visually and semantically distinct from definitive failure.

Manual SOAR actions and Agent-proposed Response remain clearly distinguishable.

## 13. Command behavior

Approve/Reject/Cancel/Start Investigation/Submit Response all call formal application/BFF boundaries. No UI-local business mutation.

## 14. Refresh/stale behavior

Refresh reconstructs from durable backend state. Polling pauses appropriately when hidden/terminal if existing behavior supports it. Transient fetch failure may retain last known snapshot but must visibly mark stale state when appropriate.

## 15. Responsive behavior

Desktop should use available space efficiently; narrow/mobile may move details into drawers/sheets. Do not force a graph on screens where it becomes unreadable. Prefer a linear activity representation there.

## 16. Accessibility / interaction baseline

- keyboard-reachable key actions
- labels not color-only
- readable status text
- predictable focus for drawers/dialogs
- no detail-below-list anti-pattern inside the Workspace
- loading/error/empty states are first-class

## 17. Backend contract discipline

Frontend consumes existing Workspace/BFF contracts. If a missing field is required, prove the user need and add the smallest additive backend read-model change. Do not create frontend-only business state or a second trace projection merely for presentation.

## 18. Tests

Targeted tests should cover:

1. investigation state rendering
2. Evidence inspection
3. Finding -> Evidence navigation
4. Knowledge Evidence labeling/provenance
5. ATT&CK exact-resolution display if present
6. Agent Verdict vs Analyst Disposition
7. waiting approval
8. approve/reject command path
9. submission != execution-success semantics
10. ATTENTION_REQUIRED
11. HISIEM execution observation
12. refresh reconstruction
13. stale/loading/error/empty states
14. narrow/mobile fallback
15. no CoT/raw prompt/checkpoint leakage

Use frontend lint/test/build plus targeted Playwright/browser review. Do not default to full-SIEM visual redesign acceptance.

## 19. Acceptance

Stage D passes when an analyst can understand the Investigation, inspect Evidence, trace Findings to Evidence, understand Knowledge as supporting context, distinguish AI/Human/Execution authority, approve/reject safely, observe HISIEM execution truth, and refresh without losing state—without relying on Chat-first UX or a mandatory DAG.

