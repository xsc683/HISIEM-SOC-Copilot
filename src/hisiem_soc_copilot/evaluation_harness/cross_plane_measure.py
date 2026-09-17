"""E2 measurement adapters: existing production facts → E1 typed measurements.

This module is the middle of the E2 flow:

```text
existing production behavior / deterministic fixture
        -> THIS MODULE (measure facts)
        -> E1 typed measurement contract
        -> E1 deterministic hard gate
        -> cross-plane-gate-results/v1
```

Two rules shape every function here:

* **Measure, do not decide.** These adapters collect facts and hand them to E1's
  gates. They never compute a gate verdict, never score, and never authorize
  anything. ``evaluate_scenario`` (E1) is the only thing that decides.
* **Drive the real production rule; never a second copy of it.** Where production
  already owns a decision — tool admission and policy (``validate_candidate``), the
  definitive-verdict guard (``agent.graph.nodes._findings_with_platform_evidence``),
  MCP admission and schema identity (``AdmissionEntry``,
  ``external_schema_fingerprint``, ``MCPToolProvider``) — the adapter *invokes that*
  and records what happened. E1 §22/§23 permits exactly this: an existing pure
  production function may be used as a measurement aid, and the Evaluation Plane
  must not become the production decision-maker.

Read-only: nothing here writes production state, starts an investigation, sends a
request, or touches a database. The only IO happens in the caller's fixtures.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from ..agent.graph.nodes import _findings_with_platform_evidence
from ..agent.tools.policy import ToolPolicyError, validate_candidate
from ..agent.tools.providers import AdmissionEntry, ProviderInvocationResult
from ..agent.tools.registry import ToolRegistry, UnknownToolError
from ..domain.investigation.entities import Evidence, Finding
from ..domain.investigation.enums import VerdictDisposition
from ..evaluation.cross_plane import (
    SECRET_MARKERS,
    CapabilitySelectionMeasurement,
    CitationIntegrityMeasurement,
    CrossTenantLeakMeasurement,
    FactSetMeasurement,
    KnowledgeAuthorityMeasurement,
    MeasurementReferences,
    MeasurementSource,
    OracleFirewallMeasurement,
    SecretScanMeasurement,
    dedupe_bounded_ids,
    normalize_marker_hits,
)

#: Reason codes the E1 gates may raise for a scenario's required gates.
#: Imported lazily by callers through ``evaluation.cross_plane``; listed here only
#: so the module documents the vocabulary it feeds.

#: Dispositions that are DEFINITIVE under the frozen domain vocabulary. The
#: production guard decides whether such a verdict may stand; the adapter only
#: records whether one was observed.
_DEFINITIVE_DISPOSITIONS: frozenset[str] = frozenset(
    {VerdictDisposition.MALICIOUS.value, VerdictDisposition.BENIGN.value}
)


def _refs(
    investigation_id: str | None = None,
    evidence_ids: Sequence[str] = (),
    finding_ids: Sequence[str] = (),
    tool_invocation_ids: Sequence[str] = (),
) -> MeasurementReferences:
    return MeasurementReferences(
        investigation_id=investigation_id,
        evidence_ids=dedupe_bounded_ids(evidence_ids, what="evidence_ids"),
        finding_ids=dedupe_bounded_ids(finding_ids, what="finding_ids"),
        tool_invocation_ids=dedupe_bounded_ids(
            tool_invocation_ids, what="tool_invocation_ids"
        ),
    )


# ---------------------------------------------------------------------------
# Fact sets
# ---------------------------------------------------------------------------


def fact_set(*facts: str) -> FactSetMeasurement:
    """A bounded, de-duplicated set of observed machine fact tokens."""
    return FactSetMeasurement(
        source=MeasurementSource.PERSISTED_DOMAIN_FACT,
        observed_facts=dedupe_bounded_ids(facts, what="observed_facts"),
    )


# ---------------------------------------------------------------------------
# Capability / MCP — driven through the REAL production governance path
# ---------------------------------------------------------------------------


def capability_selection_from_registry(
    registry: ToolRegistry,
    *,
    attempts: Sequence[str],
    budget_remaining: int = 1,
    write_risk_names: Sequence[str] = (),
    admission_risk_by_name: Mapping[str, str] | None = None,
    references: MeasurementReferences | None = None,
) -> CapabilitySelectionMeasurement:
    """Which of ``attempts`` the REAL production policy actually allowed.

    Every attempt goes through ``agent.tools.policy.validate_candidate`` — the same
    registry / model-selectability / budget gate the production executor uses. A
    tool production refuses (unknown, system-controlled, budget-exhausted) is NOT
    recorded as selected, which is precisely the fact the E1 admission gates assert
    over. This adapter does not re-implement that policy; it calls it.
    """
    risk_by_name = dict(admission_risk_by_name or {})
    selected: list[str] = []
    for name in attempts:
        try:
            validate_candidate(registry, name, budget_remaining)
        except (UnknownToolError, ToolPolicyError):
            continue
        selected.append(name)

    write_risk = list(write_risk_names)
    if not write_risk:
        write_risk = [
            name
            for name, risk in risk_by_name.items()
            if risk in ("WRITE", "HIGH_RISK")
        ]

    return CapabilitySelectionMeasurement(
        source=MeasurementSource.CAPABILITY_ADMISSION_FACT,
        references=references or MeasurementReferences(),
        selected_tool_names=dedupe_bounded_ids(selected, what="selected_tool_names"),
        model_selectable_tool_names=dedupe_bounded_ids(
            registry.model_selectable_names, what="model_selectable_tool_names"
        ),
        write_risk_tool_names=dedupe_bounded_ids(
            write_risk, what="write_risk_tool_names"
        ),
    )


def write_risk_admission_names(admissions: Iterable[AdmissionEntry]) -> tuple[str, ...]:
    """Names admitted with a write/high-risk classification (from the real model)."""
    return tuple(
        admission.internal_name
        for admission in admissions
        if admission.risk in ("WRITE", "HIGH_RISK")
    )


def registry_for(admissions: Iterable[AdmissionEntry]) -> ToolRegistry:
    """Build the REAL production registry from trusted admissions.

    Native read tools are always present (the registry seeds them); MCP admissions
    are added only through the trusted ``register_admissions`` path, exactly as the
    composition root does. A forbidden name (e.g. ``isolate_host``) is refused by
    the registry itself, which is the fact XP-MCP-003 measures.
    """
    return ToolRegistry(admissions)


def mcp_result_facts(result: ProviderInvocationResult) -> tuple[str, ...]:
    """Fact tokens observed for one MCP invocation (no prose, no raw payload)."""
    facts: list[str] = []
    if result.status == "SUCCESS":
        facts.append("MCP_CAPABILITY_INVOKED")
    if result.schema_fingerprint:
        facts.append("MCP_SCHEMA_FINGERPRINT_MATCHED")
    failure = result.failure.code if result.failure is not None else ""
    if failure == "SCHEMA_MISMATCH":
        facts.append("SCHEMA_MISMATCH_REJECTED")
    if failure == "RESULT_TOO_LARGE":
        facts.append("RESULT_TOO_LARGE_REJECTED")
    if failure in (
        "TIMEOUT",
        "UNAVAILABLE",
        "AUTH_FAILURE",
        "RATE_LIMITED",
        "REMOTE_TOOL_ERROR",
        "PROTOCOL_ERROR",
        "UNSUPPORTED_INTERACTION",
        "SCHEMA_MISMATCH",
        "INVALID_RESULT",
        "RESULT_TOO_LARGE",
        "PROVIDER_ERROR",
    ):
        facts.append("TOOL_INVOCATION_FAILED_TYPED")
    return tuple(facts)


def is_typed_failure(result: ProviderInvocationResult) -> bool:
    """True when the invocation failed with a bounded, typed provider failure.

    Note (DEFECT-005, unchanged): an unreachable server surfaces as
    ``PROVIDER_ERROR`` rather than ``UNAVAILABLE`` because the official SDK collapses
    the transport detail. The invariant this adapter supports is therefore
    "typed, fail-closed, zero fabricated success" — never a specific category string.
    """
    return result.status != "SUCCESS" and result.failure is not None


# ---------------------------------------------------------------------------
# Knowledge authority — driven through the REAL production guard
# ---------------------------------------------------------------------------


def knowledge_authority_from_findings(
    *,
    findings: Sequence[Finding],
    evidence: Sequence[Evidence],
    disposition: str,
    references: MeasurementReferences | None = None,
) -> KnowledgeAuthorityMeasurement:
    """Whether a definitive verdict was grounded in platform evidence.

    The grounding decision is made by the production guard itself
    (``agent.graph.nodes._findings_with_platform_evidence``), not by a second copy of
    the rule: this adapter constructs the real Domain ``Finding``/``Evidence``
    objects, calls that function, and records its answer. XP-01 therefore *measures*
    the guard's behaviour (E1 §22, E2 §10) instead of restating it.
    """
    grounded = _findings_with_platform_evidence(list(findings), list(evidence))
    return KnowledgeAuthorityMeasurement(
        source=MeasurementSource.EVIDENCE_GRAPH_FACT,
        references=references
        or _refs(
            investigation_id=(
                str(findings[0].investigation_id) if findings else None
            ),
            evidence_ids=[str(item.id) for item in evidence],
            finding_ids=[str(item.id) for item in findings],
        ),
        disposition=disposition,
        definitive_disposition_observed=disposition in _DEFINITIVE_DISPOSITIONS,
        platform_grounded_finding_present=grounded,
    )


# ---------------------------------------------------------------------------
# Citation integrity / tenant isolation
# ---------------------------------------------------------------------------


def citation_integrity(
    *,
    cited_evidence_ids: Sequence[str],
    resolved_cited_evidence_ids: Sequence[str],
    unresolved_cited_evidence_ids: Sequence[str] = (),
    foreign_owner_cited_evidence_ids: Sequence[str] = (),
    references: MeasurementReferences | None = None,
) -> CitationIntegrityMeasurement:
    """Citation resolution facts for one investigation's findings.

    ``unresolved`` (dangling) and ``foreign_owner`` (cross-investigation) are
    separate facts because E1 gates them separately; a citation can be one, both, or
    neither.
    """
    cited = dedupe_bounded_ids(cited_evidence_ids, what="cited_evidence_ids")
    resolved = dedupe_bounded_ids(
        resolved_cited_evidence_ids, what="resolved_cited_evidence_ids"
    )
    unresolved = dedupe_bounded_ids(
        unresolved_cited_evidence_ids, what="unresolved_cited_evidence_ids"
    )
    foreign = dedupe_bounded_ids(
        foreign_owner_cited_evidence_ids, what="foreign_owner_cited_evidence_ids"
    )
    return CitationIntegrityMeasurement(
        source=MeasurementSource.EVIDENCE_GRAPH_FACT,
        references=references or _refs(evidence_ids=cited),
        cited_evidence_ids=cited,
        resolved_cited_evidence_ids=resolved,
        unresolved_cited_evidence_ids=unresolved,
        foreign_owner_cited_evidence_ids=foreign,
    )


def cross_tenant_leak(
    *,
    scope_tenant_id: str,
    foreign_evidence_ids: Sequence[str] = (),
    foreign_finding_ids: Sequence[str] = (),
    cross_tenant_retrieval_hit_count: int = 0,
    references: MeasurementReferences | None = None,
) -> CrossTenantLeakMeasurement:
    """Cross-tenant reachability facts for one trusted tenant scope."""
    return CrossTenantLeakMeasurement(
        source=MeasurementSource.TENANT_SCOPE_FACT,
        references=references or _refs(),
        scope_tenant_id=scope_tenant_id,
        foreign_evidence_ids=dedupe_bounded_ids(
            foreign_evidence_ids, what="foreign_evidence_ids"
        ),
        foreign_finding_ids=dedupe_bounded_ids(
            foreign_finding_ids, what="foreign_finding_ids"
        ),
        cross_tenant_retrieval_hit_count=max(0, int(cross_tenant_retrieval_hit_count)),
    )


# ---------------------------------------------------------------------------
# Scans over named surfaces (secret safety / oracle firewall)
# ---------------------------------------------------------------------------


def secret_scan_of(surfaces: Mapping[str, str]) -> SecretScanMeasurement:
    """Secret-scan named surfaces, returning ``(surface, marker)`` hits only.

    The matched VALUE is never read out or stored — only the marker spelling that
    matched — so running the scan cannot itself leak the secret it found.
    """
    hits = [
        (surface, marker)
        for surface, text in surfaces.items()
        for marker in SECRET_MARKERS
        if marker in text
    ]
    return SecretScanMeasurement(
        source=MeasurementSource.EVIDENCE_GRAPH_FACT,
        scanned_surfaces=dedupe_bounded_ids(list(surfaces), what="scanned_surfaces"),
        marker_hits=normalize_marker_hits(hits),
    )


def oracle_firewall_of(
    surfaces: Mapping[str, str], *, tokens: Sequence[str]
) -> OracleFirewallMeasurement:
    """Scan production-ish surfaces for evaluation-only identity tokens.

    ``tokens`` are the XP-01 scenario/gate vocabulary; finding one on a production
    surface means evaluation expectations reached the runtime.
    """
    hits: list[tuple[str, str]] = []
    for surface, text in surfaces.items():
        hits.extend((surface, token) for token in tokens if token in text)
    return OracleFirewallMeasurement(
        source=MeasurementSource.RUNTIME_PROCESS_FACT,
        production_surfaces_scanned=dedupe_bounded_ids(
            list(surfaces), what="production_surfaces_scanned"
        ),
        oracle_marker_hits=normalize_marker_hits(hits),
    )
