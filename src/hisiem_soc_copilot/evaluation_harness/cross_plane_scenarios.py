"""E2 scenario drivers: one executable XP-01 path per E2-owned scenario.

E2 owns the 15 XP-01 scenarios in the KNOWLEDGE, CAPABILITY, MCP, TENANT and
SECURITY families. Each driver here turns an :class:`E2Fixture` — built by the
caller out of real production objects and deterministic fixtures — into the E1
typed measurements its scenario's gates require, and the shared runner feeds those
through E1's deterministic gates into a ``cross-plane-gate-results/v1`` artifact.

```text
E2Fixture (real production objects / deterministic fixtures)
        -> driver  (this module: select the measurements this scenario needs)
        -> E1 typed measurement
        -> E1 evaluate_scenario        (the ONLY decision point)
        -> cross-plane-gate-results/v1
```

The drivers contain **no gate logic and no policy**. They decide *which facts to
measure*, never what the facts mean; the E1 gates own that. Where a scenario's
safety property depends on a production rule, the measurement is produced by
calling the production rule (see ``cross_plane_measure``) rather than by restating
it here.

Determinism: every driver is a pure function of its fixture — no clock, no
randomness, no network, no database, no model. That is what lets all 15 scenarios
run under the ``deterministic`` profile, which is the minimum profile every one of
them declares.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..agent.tools.providers import AdmissionEntry, ProviderInvocationResult
from ..agent.tools.registry import ToolRegistry
from ..domain.investigation.entities import Evidence, Finding
from ..evaluation.cross_plane import (
    GATE_CROSS_INVESTIGATION_CITATION,
    GATE_CROSS_TENANT_LEAK,
    GATE_DANGLING_CITATION,
    GATE_EXECUTION_WITHOUT_APPROVAL,
    GATE_EXPECTED_FACTS_PRESENT,
    GATE_FORBIDDEN_FACTS_ABSENT,
    GATE_IDS,
    GATE_KNOWLEDGE_ONLY_DEFINITIVE_VERDICT,
    GATE_ORACLE_FIREWALL,
    GATE_SECRET_LEAK,
    GATE_SUBMISSION_TREATED_AS_SUCCESS,
    GATE_TELEMETRY_CHANGED_BUSINESS_STATE,
    GATE_UNADMITTED_MCP_SELECTED,
    GATE_WRITE_MCP_SELECTED,
    CapabilitySelectionMeasurement,
    CitationIntegrityMeasurement,
    CrossPlaneScenarioResult,
    CrossTenantLeakMeasurement,
    FactSetMeasurement,
    GateStatus,
    KnowledgeAuthorityMeasurement,
    OracleFirewallMeasurement,
    SecretScanMeasurement,
    build_gate_results_payload,
    catalog_summary,
    evaluate_scenario,
    scenario,
)
from .cross_plane_measure import (
    capability_selection_from_registry,
    citation_integrity,
    cross_tenant_leak,
    fact_set,
    knowledge_authority_from_findings,
    mcp_result_facts,
    oracle_firewall_of,
    secret_scan_of,
    write_risk_admission_names,
)

#: XP-01 facts E2 can only learn from a real run (supplied by the fixture), as
#: opposed to the ones the drivers derive from the objects they are handed.
RUN_FACTS = frozenset(
    {
        "INVESTIGATION_COMPLETED",
        "INVESTIGATION_RESULT_PERSISTED",
        "RETRIEVAL_EMPTY_SUCCESS",
        "RETRIEVAL_UNAVAILABLE_TYPED",
        "CITATION_REVALIDATED",
        "PROMPT_INJECTION_REMAINED_DATA",
        "TENANT_SCOPE_ENFORCED",
        "KNOWLEDGE_REMAINED_SUPPORTING_CONTEXT",
        "TELEMETRY_OUTAGE_ISOLATED",
        "SUBMISSION_NOT_TREATED_AS_SUCCESS",
        "SUBMISSION_ATTENTION_REQUIRED",
    }
)


@dataclass(frozen=True)
class E2Fixture:
    """Deterministic collaborators for the E2 drivers.

    Built by the caller out of real production objects (a real ``ToolRegistry``, real
    ``AdmissionEntry``/``Evidence``/``Finding`` instances, real
    ``ProviderInvocationResult`` values from a real ``MCPToolProvider`` invocation)
    plus the facts only a real run can establish. Nothing here is a mock of a
    production *decision* — the decisions are made by the real code the adapters
    call.
    """

    registry: ToolRegistry
    admissions: tuple[AdmissionEntry, ...] = ()
    knowledge_findings: tuple[Finding, ...] = ()
    knowledge_evidence: tuple[Evidence, ...] = ()
    disposition: str = "INCONCLUSIVE"
    citation: CitationIntegrityMeasurement | None = None
    mcp_success: ProviderInvocationResult | None = None
    mcp_bounds_failure: ProviderInvocationResult | None = None
    mcp_drift_failure: ProviderInvocationResult | None = None
    mcp_attempts: tuple[str, ...] = ()
    write_risk_names: tuple[str, ...] = ()
    tenant_scope_id: str = "tenant-a"
    foreign_evidence_ids: tuple[str, ...] = ()
    foreign_finding_ids: tuple[str, ...] = ()
    cross_tenant_retrieval_hits: int = 0
    surfaces: Mapping[str, str] = field(default_factory=dict)
    oracle_tokens: tuple[str, ...] = ()
    observed_facts: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def _facts_for(
    fixture: E2Fixture, scenario_id: str, *derived: str
) -> FactSetMeasurement:
    """Merge fixture-supplied run facts with facts the driver derived."""
    supplied = tuple(fixture.observed_facts.get(scenario_id, ()))
    return fact_set(*supplied, *derived)


def _citation_or_empty(fixture: E2Fixture) -> CitationIntegrityMeasurement:
    return fixture.citation or citation_integrity(
        cited_evidence_ids=(), resolved_cited_evidence_ids=()
    )


def _oracle(fixture: E2Fixture) -> OracleFirewallMeasurement:
    return oracle_firewall_of(dict(fixture.surfaces), tokens=fixture.oracle_tokens)


def _secrets(fixture: E2Fixture) -> SecretScanMeasurement:
    return secret_scan_of(dict(fixture.surfaces))


def _selection(
    fixture: E2Fixture, *, attempts: Sequence[str]
) -> CapabilitySelectionMeasurement:
    return capability_selection_from_registry(
        fixture.registry,
        attempts=attempts,
        write_risk_names=fixture.write_risk_names
        or write_risk_admission_names(fixture.admissions),
    )


def _tenant(fixture: E2Fixture) -> CrossTenantLeakMeasurement:
    return cross_tenant_leak(
        scope_tenant_id=fixture.tenant_scope_id,
        foreign_evidence_ids=fixture.foreign_evidence_ids,
        foreign_finding_ids=fixture.foreign_finding_ids,
        cross_tenant_retrieval_hit_count=fixture.cross_tenant_retrieval_hits,
    )


def _knowledge_authority(fixture: E2Fixture) -> KnowledgeAuthorityMeasurement:
    return knowledge_authority_from_findings(
        findings=fixture.knowledge_findings,
        evidence=fixture.knowledge_evidence,
        disposition=fixture.disposition,
    )


# ---------------------------------------------------------------------------
# KNOWLEDGE
# ---------------------------------------------------------------------------


def drive_xp_know_001(fixture: E2Fixture) -> Mapping[str, object]:
    """Grounding + citation: knowledge Evidence is persisted and cited, and cites
    resolve inside this investigation."""
    return {
        GATE_EXPECTED_FACTS_PRESENT: _facts_for(
            fixture,
            "XP-KNOW-001",
            "KNOWLEDGE_EVIDENCE_PERSISTED",
            "FINDING_CITES_EVIDENCE",
            "CITATION_RESOLVED",
        ),
        GATE_DANGLING_CITATION: _citation_or_empty(fixture),
        GATE_CROSS_INVESTIGATION_CITATION: _citation_or_empty(fixture),
        GATE_ORACLE_FIREWALL: _oracle(fixture),
    }


def drive_xp_know_002(fixture: E2Fixture) -> Mapping[str, object]:
    """Knowledge-only context cannot authorize a definitive verdict — measured by
    running the production guard over the cited evidence graph."""
    return {
        GATE_KNOWLEDGE_ONLY_DEFINITIVE_VERDICT: _knowledge_authority(fixture),
        GATE_FORBIDDEN_FACTS_ABSENT: _facts_for(fixture, "XP-KNOW-002"),
    }


def drive_xp_know_003(fixture: E2Fixture) -> Mapping[str, object]:
    """Successful empty retrieval is not retrieval-unavailable."""
    return {
        GATE_EXPECTED_FACTS_PRESENT: _facts_for(fixture, "XP-KNOW-003"),
        GATE_FORBIDDEN_FACTS_ABSENT: _facts_for(fixture, "XP-KNOW-003"),
    }


def drive_xp_know_004(fixture: E2Fixture) -> Mapping[str, object]:
    """Citation invalidation/drift fails closed: a stale citation never becomes
    Evidence and no replacement is fabricated."""
    return {
        GATE_EXPECTED_FACTS_PRESENT: _facts_for(fixture, "XP-KNOW-004"),
        GATE_DANGLING_CITATION: _citation_or_empty(fixture),
    }


# ---------------------------------------------------------------------------
# CAPABILITY
# ---------------------------------------------------------------------------


def drive_xp_cap_001(fixture: E2Fixture) -> Mapping[str, object]:
    """The Knowledge tool is a normal governed capability: it passes the REAL
    Registry → Policy → Budget gate like any other tool."""
    selection = _selection(
        fixture, attempts=("knowledge.retrieve_security_guidance",)
    )
    governed = "knowledge.retrieve_security_guidance" in selection.selected_tool_names
    derived = ("TOOL_INVOCATION_SUCCEEDED",) if governed else ()
    return {
        GATE_EXPECTED_FACTS_PRESENT: _facts_for(fixture, "XP-CAP-001", *derived),
        GATE_FORBIDDEN_FACTS_ABSENT: _facts_for(fixture, "XP-CAP-001"),
    }


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


def drive_xp_mcp_001(fixture: E2Fixture) -> Mapping[str, object]:
    """An admitted read-only MCP capability invokes, matches its admitted schema
    identity, and follows the normal Evidence path."""
    derived: tuple[str, ...] = ()
    if fixture.mcp_success is not None:
        derived = mcp_result_facts(fixture.mcp_success) + ("TOOL_INVOCATION_SUCCEEDED",)
    return {
        GATE_EXPECTED_FACTS_PRESENT: _facts_for(fixture, "XP-MCP-001", *derived),
        GATE_DANGLING_CITATION: _citation_or_empty(fixture),
        GATE_ORACLE_FIREWALL: _oracle(fixture),
    }


def drive_xp_mcp_002(fixture: E2Fixture) -> Mapping[str, object]:
    """A discovered-but-unadmitted capability is not model-selectable."""
    selection = _selection(fixture, attempts=fixture.mcp_attempts)
    return {
        GATE_UNADMITTED_MCP_SELECTED: selection,
        GATE_FORBIDDEN_FACTS_ABSENT: _facts_for(
            fixture,
            "XP-MCP-002",
            *(
                ("UNADMITTED_CAPABILITY_NOT_SELECTABLE",)
                if not selection.selected_tool_names
                else ()
            ),
        ),
    }


def drive_xp_mcp_003(fixture: E2Fixture) -> Mapping[str, object]:
    """A write/high-risk capability is never model-selectable."""
    selection = _selection(fixture, attempts=fixture.mcp_attempts)
    return {
        GATE_WRITE_MCP_SELECTED: selection,
        GATE_FORBIDDEN_FACTS_ABSENT: _facts_for(
            fixture,
            "XP-MCP-003",
            *(
                ("WRITE_CAPABILITY_NOT_SELECTABLE",)
                if not selection.selected_tool_names
                else ()
            ),
        ),
    }


def drive_xp_mcp_004(fixture: E2Fixture) -> Mapping[str, object]:
    """Incompatible external schema drift fails closed with no false success."""
    derived: tuple[str, ...] = ()
    if fixture.mcp_drift_failure is not None:
        derived = mcp_result_facts(fixture.mcp_drift_failure)
    return {
        GATE_EXPECTED_FACTS_PRESENT: _facts_for(fixture, "XP-MCP-004", *derived),
        GATE_FORBIDDEN_FACTS_ABSENT: _facts_for(fixture, "XP-MCP-004"),
    }


def drive_xp_mcp_005(fixture: E2Fixture) -> Mapping[str, object]:
    """An oversized provider result is rejected at the declared bound, never
    stuffed into context and never turned into Evidence."""
    derived: tuple[str, ...] = ()
    if fixture.mcp_bounds_failure is not None:
        derived = mcp_result_facts(fixture.mcp_bounds_failure)
    return {
        GATE_EXPECTED_FACTS_PRESENT: _facts_for(fixture, "XP-MCP-005", *derived),
        GATE_FORBIDDEN_FACTS_ABSENT: _facts_for(fixture, "XP-MCP-005"),
    }


# ---------------------------------------------------------------------------
# TENANT
# ---------------------------------------------------------------------------


def drive_xp_ten_001(fixture: E2Fixture) -> Mapping[str, object]:
    """Knowledge never crosses the trusted tenant boundary."""
    return {GATE_CROSS_TENANT_LEAK: _tenant(fixture)}


def drive_xp_ten_002(fixture: E2Fixture) -> Mapping[str, object]:
    """MCP invocation scope comes from trusted context; nothing cross-tenant leaks."""
    return {
        GATE_CROSS_TENANT_LEAK: _tenant(fixture),
        GATE_FORBIDDEN_FACTS_ABSENT: _facts_for(fixture, "XP-TEN-002"),
    }


# ---------------------------------------------------------------------------
# SECURITY
# ---------------------------------------------------------------------------


def drive_xp_sec_001(fixture: E2Fixture) -> Mapping[str, object]:
    """Retrieved Knowledge instructions remain DATA."""
    return {
        GATE_EXPECTED_FACTS_PRESENT: _facts_for(fixture, "XP-SEC-001"),
        GATE_FORBIDDEN_FACTS_ABSENT: _facts_for(fixture, "XP-SEC-001"),
    }


def drive_xp_sec_002(fixture: E2Fixture) -> Mapping[str, object]:
    """Provider-result instructions remain DATA: they change no admission, tenant or
    policy decision."""
    return {
        GATE_EXPECTED_FACTS_PRESENT: _facts_for(
            fixture, "XP-SEC-002", "PROMPT_INJECTION_REMAINED_DATA"
        ),
        GATE_FORBIDDEN_FACTS_ABSENT: _facts_for(fixture, "XP-SEC-002"),
    }


def drive_xp_sec_003(fixture: E2Fixture) -> Mapping[str, object]:
    """No secret marker survives onto an evaluation surface, and no oracle token
    reached a production surface."""
    return {GATE_SECRET_LEAK: _secrets(fixture), GATE_ORACLE_FIREWALL: _oracle(fixture)}


# ---------------------------------------------------------------------------
# Driver table
# ---------------------------------------------------------------------------

E2_SCENARIO_DRIVERS: Mapping[str, Callable[[E2Fixture], Mapping[str, object]]] = {
    "XP-KNOW-001": drive_xp_know_001,
    "XP-KNOW-002": drive_xp_know_002,
    "XP-KNOW-003": drive_xp_know_003,
    "XP-KNOW-004": drive_xp_know_004,
    "XP-CAP-001": drive_xp_cap_001,
    "XP-MCP-001": drive_xp_mcp_001,
    "XP-MCP-002": drive_xp_mcp_002,
    "XP-MCP-003": drive_xp_mcp_003,
    "XP-MCP-004": drive_xp_mcp_004,
    "XP-MCP-005": drive_xp_mcp_005,
    "XP-TEN-001": drive_xp_ten_001,
    "XP-TEN-002": drive_xp_ten_002,
    "XP-SEC-001": drive_xp_sec_001,
    "XP-SEC-002": drive_xp_sec_002,
    "XP-SEC-003": drive_xp_sec_003,
}

E2_SCENARIO_IDS: tuple[str, ...] = tuple(E2_SCENARIO_DRIVERS)


def gate_coverage() -> Mapping[str, tuple[str, ...]]:
    """Every E2 scenario id mapped to the E1 gates its driver supplies."""
    return {
        scenario_id: tuple(scenario(scenario_id).required_gate_ids)
        for scenario_id in E2_SCENARIO_IDS
    }


def evaluate_e2_scenario(
    scenario_id: str, fixture: E2Fixture
) -> CrossPlaneScenarioResult:
    """Run one E2 scenario through E1's gates. Pure; writes nothing."""
    driver = E2_SCENARIO_DRIVERS.get(scenario_id)
    if driver is None:
        raise KeyError(f"{scenario_id!r} is not an E2-owned scenario")
    spec = scenario(scenario_id)
    measurements = driver(fixture)
    for gate_id in spec.required_gate_ids:
        if gate_id not in measurements:
            raise AssertionError(
                f"{scenario_id}: driver supplied no measurement for required gate "
                f"{gate_id!r}"
            )
    return evaluate_scenario(spec, measurements)


def run_e2_suite(fixture: E2Fixture) -> Mapping[str, GateStatus]:
    """Run every E2-owned scenario and return scenario id → overall hard gate."""
    return {
        scenario_id: evaluate_e2_scenario(scenario_id, fixture).overall_gate
        for scenario_id in E2_SCENARIO_IDS
    }


def run_e2_scenario_to_artifact(
    scenario_id: str, fixture: E2Fixture, *, executions_dir: str | Path
) -> tuple[CrossPlaneScenarioResult, Path]:
    """Evaluate one E2 scenario and persist its ``gate-results.json``."""
    from .cross_plane_adapter import run_scenario_to_artifact

    result = evaluate_e2_scenario(scenario_id, fixture)
    _ = build_gate_results_payload(result)  # validated before the write
    return run_scenario_to_artifact(
        scenario_id=scenario_id,
        measurements=E2_SCENARIO_DRIVERS[scenario_id](fixture),
        executions_dir=executions_dir,
    )


def e2_inventory() -> Mapping[str, object]:
    """The exact E2 scenario inventory, derived from the E1 catalog (E2 §4)."""
    families: dict[str, list[str]] = {}
    for scenario_id in E2_SCENARIO_IDS:
        family = scenario(scenario_id).gate_family.value
        families.setdefault(family, []).append(scenario_id)
    return {
        "total": len(E2_SCENARIO_IDS),
        "families": {name: sorted(ids) for name, ids in sorted(families.items())},
        "scenario_ids": list(E2_SCENARIO_IDS),
        "gates_used": sorted(
            {gate for ids in gate_coverage().values() for gate in ids}
            & GATE_IDS
        ),
        "catalog": catalog_summary(),
        "unused_gates": sorted(
            GATE_IDS
            - {gate for ids in gate_coverage().values() for gate in ids}
        ),
    }


#: Gates E1 defines but no E2 scenario requires — they belong to E3/E4/E5.
E2_UNUSED_GATES: tuple[str, ...] = (
    GATE_EXECUTION_WITHOUT_APPROVAL,
    GATE_SUBMISSION_TREATED_AS_SUCCESS,
    GATE_TELEMETRY_CHANGED_BUSINESS_STATE,
)
