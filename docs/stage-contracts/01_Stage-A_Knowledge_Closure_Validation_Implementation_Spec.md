# Stage A — Knowledge Closure Validation Implementation Spec

**Type:** Validation / contract hardening only  
**Implementation policy:** Do not redevelop the Knowledge subsystem. Validate the completed Knowledge Intelligence Closure against the frozen Architecture Contract.

## 1. Objective

Prove that the current `knowledge-intelligence-closure` implementation conforms to the frozen Knowledge Plane contract and establish regression coverage sufficient for subsequent stages.

## 2. In scope

- Verify final model-selectable tool surface
- Verify `knowledge.retrieve_security_guidance`
- Verify `knowledge.resolve_attack_technique`
- Verify ToolResult -> EvidenceNormalizer -> immutable Knowledge Evidence
- Verify Citation resolver revalidation
- Verify stable Evidence identity vs dynamic retrieval provenance
- Verify ACTIVE embedding profile coverage semantics
- Verify HYBRID / LEXICAL_ONLY / VECTOR_ONLY truthfulness
- Verify ATT&CK exact canonical resolution
- Verify tenant isolation
- Verify prompt-injection/data-only semantics
- Verify Knowledge authority guard
- Extend focused evaluation only where a frozen acceptance case is currently missing

## 3. Explicitly out of scope

- RAG rewrite
- new retrieval framework
- new Vector DB
- MCP
- Observability implementation
- Workspace redesign
- Execution Trace UI
- Model Router

## 4. Contracts to validate

### 4.1 Retrieval modes

- HYBRID = production default
- LEXICAL_ONLY = supported diagnostic/evaluation/attribution
- VECTOR_ONLY = supported diagnostic/evaluation/attribution
- reported mode must equal actual execution mode

### 4.2 Knowledge evidence identity

Stable identity must use citation/content identity and must not depend on rank, score, retrieved_at, retrieval mode, or result position.

### 4.3 ATT&CK

Technique ID is canonicalized and resolved exactly against ACTIVE release/projection/hash chain. No semantic guessing.

### 4.4 Authority

Retrieved Knowledge is supporting context. Definitive MALICIOUS/BENIGN requires observed platform evidence under current product policy.

### 4.5 Tenant

Tenant scope comes from trusted runtime Investigation context and is not model-controlled.

## 5. Required focused tests

At minimum cover:

1. HYBRID happy path
2. LEXICAL_ONLY truthfulness
3. VECTOR_ONLY truthfulness
4. active-profile complete + retired complete -> pass
5. active complete + retired partial -> pass
6. active partial + retired complete -> reject
7. active absent + retired complete -> reject
8. adversarial UUID ordering has no effect
9. Citation resolver rejects stale/mismatched citation
10. Stable Evidence dedup is independent of retrieval run metadata
11. Knowledge tool unavailable returns typed failure
12. Tenant A sees GLOBAL + A, not B
13. Prompt-injection text remains data
14. ATT&CK exact technique lookup
15. invalid/fuzzy technique request does not silently guess
16. Knowledge-only evidence cannot produce definitive MALICIOUS/BENIGN
17. platform evidence + Knowledge follows normal verdict rules

## 6. Validation commands

Use stage-scoped validation:

- `ruff check .`
- `mypy src`
- focused Knowledge tests
- focused Agent tests
- focused architecture/security-boundary tests
- active embedding coverage regression

Do not default to full pytest/Maven/Playwright unless focused failures show broader regression.

## 7. Acceptance

Stage A passes when:

- all frozen Knowledge contracts are demonstrated by code/tests
- no new architecture is introduced
- working tree is clean after validation-only changes
- any new tests are contract tests, not a rewrite
- a concise validation report records exact cases and results

## 8. Deliverable

`Knowledge Closure Validation Report` containing:

- branch / actual HEAD
- frozen tool surface
- retrieval modes verified
- ATT&CK exact-resolution result
- authority/tenant/security results
- tests run and counts
- any residual known gap

