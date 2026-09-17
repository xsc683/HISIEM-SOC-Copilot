"""E2 measurement-adapter tests (Stage E / E2 §23, §26).

The adapters turn real production objects into E1 measurements. These tests prove
they report the facts the gates need — and that the safety-relevant ones are
produced by the REAL production rule (`validate_candidate`, the knowledge authority
guard, the MCP fingerprint contract) rather than by a second implementation of it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from hisiem_soc_copilot.agent.tools.providers import (
    AdmissionEntry,
    ProviderFailure,
    ProviderIdentity,
    ProviderInvocationResult,
    ResultBounds,
)
from hisiem_soc_copilot.domain.investigation.entities import (
    Evidence,
    EvidenceSource,
    Finding,
)
from hisiem_soc_copilot.domain.investigation.enums import (
    EvidenceSourceType,
    VerdictDisposition,
)
from hisiem_soc_copilot.evaluation.cross_plane import GateStatus, scenario
from hisiem_soc_copilot.evaluation_harness.cross_plane_measure import (
    capability_selection_from_registry,
    citation_integrity,
    cross_tenant_leak,
    fact_set,
    is_typed_failure,
    knowledge_authority_from_findings,
    mcp_result_facts,
    oracle_firewall_of,
    registry_for,
    secret_scan_of,
    write_risk_admission_names,
)

_NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
_INV = uuid4()


def _evidence(source: EvidenceSourceType, *, investigation_id: UUID | None = None) -> Evidence:
    return Evidence(
        id=uuid4(),
        investigation_id=investigation_id or _INV,
        source=EvidenceSource(type=source, provider="p", operation="op"),
        collected_at=_NOW,
        observation={},
    )


def _finding(cites: list[UUID], *, investigation_id: UUID | None = None) -> Finding:
    return Finding(
        id=uuid4(),
        investigation_id=investigation_id or _INV,
        statement="s",
        evidence_citations=cites,
        created_at=_NOW,
    )


def _read_admission(name: str) -> AdmissionEntry:
    return AdmissionEntry(
        internal_name=name,
        description="d",
        server_id="native",
        external_name=name,
        tenant_scope="TENANT_SCOPED",
    )


def _result(
    status: str, *, code: str | None = None, fingerprint: str | None = None
) -> ProviderInvocationResult:
    failure = (
        ProviderFailure(code=code)  # type: ignore[arg-type]
        if code is not None
        else None
    )
    return ProviderInvocationResult(
        tool_name="mcp.x",
        status=status,  # type: ignore[arg-type]
        failure=failure,
        provider=ProviderIdentity(provider_type="mcp"),
        schema_fingerprint=fingerprint,
    )


# --- fact sets ----------------------------------------------------------------


def test_fact_set_dedupes_and_preserves_order() -> None:
    measurement = fact_set("A", "B", "A")
    assert measurement.observed_facts == ("A", "B")


def test_fact_set_is_bounded() -> None:
    with pytest.raises(Exception, match="exceeds"):
        fact_set(*[f"F{i}" for i in range(200)])


# --- capability governance (real registry + real policy) ----------------------


def test_native_read_tools_are_model_selectable() -> None:
    """The registry seeds the frozen native read tools itself; no admission needed."""
    registry = registry_for([])
    measurement = capability_selection_from_registry(
        registry, attempts=["hisiem.search_events"]
    )
    assert measurement.selected_tool_names == ("hisiem.search_events",)
    # model_selectable_tool_names is the whole selectable surface, not one entry.
    assert "hisiem.search_events" in measurement.model_selectable_tool_names
    assert "hisiem.get_alert_context" not in measurement.model_selectable_tool_names


def test_unknown_tool_is_not_selected() -> None:
    """A dynamic/discovered-but-unadmitted tool is refused by the REAL policy."""
    registry = registry_for([])
    measurement = capability_selection_from_registry(
        registry, attempts=["mcp.dynamic.only"]
    )
    assert measurement.selected_tool_names == ()


def test_system_controlled_tool_is_not_selected() -> None:
    registry = registry_for([])
    measurement = capability_selection_from_registry(
        registry, attempts=["hisiem.get_alert_context"]
    )
    assert measurement.selected_tool_names == ()
    assert "hisiem.get_alert_context" not in measurement.model_selectable_tool_names


def test_exhausted_budget_blocks_selection() -> None:
    registry = registry_for([])
    measurement = capability_selection_from_registry(
        registry, attempts=["hisiem.search_events"], budget_remaining=0
    )
    assert measurement.selected_tool_names == ()


def test_native_admissions_cannot_be_re_registered() -> None:
    """A native tool name is already seeded, so re-admitting it collides loudly."""
    with pytest.raises(ValueError, match="collides with registered tool"):
        registry_for([_read_admission("hisiem.search_events")])


def test_write_risk_admission_is_never_model_selectable() -> None:
    """`is_model_selectable` requires READ_ONLY — a write admission cannot be selected."""
    write = AdmissionEntry(
        internal_name="mcp.block_ip",
        description="d",
        server_id="e2-local",
        external_name="block_ip",
        risk="WRITE",
    )
    assert write.is_model_selectable is False
    registry = registry_for([write])
    measurement = capability_selection_from_registry(registry, attempts=["mcp.block_ip"])
    assert measurement.selected_tool_names == ()
    assert "mcp.block_ip" not in measurement.model_selectable_tool_names


def test_write_risk_names_are_read_from_the_admission_model() -> None:
    admissions = [
        _read_admission("hisiem.search_events"),
        AdmissionEntry(
            internal_name="mcp.block_ip",
            description="d",
            server_id="s",
            external_name="block_ip",
            risk="WRITE",
        ),
    ]
    assert write_risk_admission_names(admissions) == ("mcp.block_ip",)


def test_registry_refuses_a_forbidden_tool_name() -> None:
    """The production registry itself rejects explicitly forbidden capability names."""
    forbidden = AdmissionEntry(
        internal_name="isolate_host",
        description="d",
        server_id="s",
        external_name="isolate_host",
        risk="WRITE",
    )
    with pytest.raises(ValueError, match="forbidden tool cannot be admitted"):
        registry_for([forbidden])


# --- knowledge authority (real production guard) ------------------------------


def test_platform_grounded_definitive_verdict_is_grounded() -> None:
    platform = _evidence(EvidenceSourceType.HISIEM_LOG_SEARCH)
    measurement = knowledge_authority_from_findings(
        findings=[_finding([platform.id])],
        evidence=[platform],
        disposition=VerdictDisposition.MALICIOUS.value,
    )
    assert measurement.definitive_disposition_observed is True
    assert measurement.platform_grounded_finding_present is True
    from hisiem_soc_copilot.evaluation.cross_plane import (
        GATE_KNOWLEDGE_ONLY_DEFINITIVE_VERDICT,
        evaluate_gate,
    )

    assert (
        evaluate_gate(
            GATE_KNOWLEDGE_ONLY_DEFINITIVE_VERDICT,
            measurement,
            scenario=scenario("XP-KNOW-002"),
        ).status
        is GateStatus.PASS
    )


def test_knowledge_only_definitive_verdict_is_not_grounded() -> None:
    """The production guard refuses to ground a verdict on KNOWLEDGE evidence."""
    knowledge = _evidence(EvidenceSourceType.KNOWLEDGE)
    measurement = knowledge_authority_from_findings(
        findings=[_finding([knowledge.id])],
        evidence=[knowledge],
        disposition=VerdictDisposition.MALICIOUS.value,
    )
    assert measurement.platform_grounded_finding_present is False
    from hisiem_soc_copilot.evaluation.cross_plane import (
        GATE_KNOWLEDGE_ONLY_DEFINITIVE_VERDICT,
        evaluate_gate,
    )

    result = evaluate_gate(
        GATE_KNOWLEDGE_ONLY_DEFINITIVE_VERDICT,
        measurement,
        scenario=scenario("XP-KNOW-002"),
    )
    assert result.status is GateStatus.FAIL
    assert result.reason_codes == ("KNOWLEDGE_AUTHORITY_VIOLATION",)


def test_system_evidence_also_fails_to_ground_a_definitive_verdict() -> None:
    system = _evidence(EvidenceSourceType.SYSTEM)
    measurement = knowledge_authority_from_findings(
        findings=[_finding([system.id])],
        evidence=[system],
        disposition=VerdictDisposition.MALICIOUS.value,
    )
    assert measurement.platform_grounded_finding_present is False


def test_inconclusive_disposition_is_not_definitive() -> None:
    knowledge = _evidence(EvidenceSourceType.KNOWLEDGE)
    measurement = knowledge_authority_from_findings(
        findings=[_finding([knowledge.id])],
        evidence=[knowledge],
        disposition=VerdictDisposition.INCONCLUSIVE.value,
    )
    assert measurement.definitive_disposition_observed is False


def test_adapter_agrees_with_the_production_guard_by_construction() -> None:
    """The adapter's grounding fact IS the production guard's answer."""
    from hisiem_soc_copilot.agent.graph.nodes import _findings_with_platform_evidence

    platform = _evidence(EvidenceSourceType.HISIEM_ALERT)
    findings = [_finding([platform.id])]
    measurement = knowledge_authority_from_findings(
        findings=findings, evidence=[platform], disposition="MALICIOUS"
    )
    assert measurement.platform_grounded_finding_present == (
        _findings_with_platform_evidence(findings, [platform])
    )


# --- citation integrity / tenant ---------------------------------------------


def test_citation_integrity_separates_dangling_from_foreign() -> None:
    m = citation_integrity(
        cited_evidence_ids=("a", "b", "c"),
        resolved_cited_evidence_ids=("a",),
        unresolved_cited_evidence_ids=("b",),
        foreign_owner_cited_evidence_ids=("c",),
    )
    assert m.unresolved_cited_evidence_ids == ("b",)
    assert m.foreign_owner_cited_evidence_ids == ("c",)


def test_cross_tenant_leak_reports_foreign_ids() -> None:
    m = cross_tenant_leak(
        scope_tenant_id="tenant-a",
        foreign_evidence_ids=("ev-x",),
        cross_tenant_retrieval_hit_count=2,
    )
    assert m.foreign_evidence_ids == ("ev-x",)
    assert m.cross_tenant_retrieval_hit_count == 2


def test_cross_tenant_hit_count_never_goes_negative() -> None:
    assert (
        cross_tenant_leak(scope_tenant_id="t", cross_tenant_retrieval_hit_count=-5)
        .cross_tenant_retrieval_hit_count
        == 0
    )


# --- scans --------------------------------------------------------------------


def test_secret_scan_reports_only_marker_spellings() -> None:
    m = secret_scan_of({"artifact": "Authorization: Bearer super-secret-value"})
    assert m.marker_hits
    markers = {marker for _surface, marker in m.marker_hits}
    assert "Bearer" in markers
    assert all("super-secret-value" not in marker for _s, marker in m.marker_hits)


def test_secret_scan_clean_surface_has_no_hits() -> None:
    assert secret_scan_of({"artifact": "nothing sensitive here"}).marker_hits == ()


def test_oracle_firewall_flags_evaluation_tokens() -> None:
    m = oracle_firewall_of(
        {"telemetry": "span ok", "workspace": "XP-KNOW-001 leaked"},
        tokens=("XP-KNOW-001", "XP-MCP-003"),
    )
    assert m.oracle_marker_hits == (("workspace", "XP-KNOW-001"),)


def test_oracle_firewall_clean_surfaces_pass() -> None:
    m = oracle_firewall_of({"telemetry": "span ok"}, tokens=("XP-KNOW-001",))
    assert m.oracle_marker_hits == ()


# --- MCP result facts ---------------------------------------------------------


def test_success_result_yields_invocation_and_fingerprint_facts() -> None:
    facts = mcp_result_facts(_result("SUCCESS", fingerprint="abc"))
    assert "MCP_CAPABILITY_INVOKED" in facts
    assert "MCP_SCHEMA_FINGERPRINT_MATCHED" in facts


def test_schema_mismatch_yields_rejection_and_typed_failure() -> None:
    facts = mcp_result_facts(_result("REJECTED", code="SCHEMA_MISMATCH"))
    assert "SCHEMA_MISMATCH_REJECTED" in facts
    assert "TOOL_INVOCATION_FAILED_TYPED" in facts


def test_oversized_result_yields_bounds_rejection() -> None:
    facts = mcp_result_facts(_result("REJECTED", code="RESULT_TOO_LARGE"))
    assert "RESULT_TOO_LARGE_REJECTED" in facts
    assert "TOOL_INVOCATION_FAILED_TYPED" in facts


def test_typed_failure_detection() -> None:
    assert is_typed_failure(_result("REJECTED", code="PROVIDER_ERROR")) is True
    assert is_typed_failure(_result("SUCCESS", fingerprint="f")) is False


def test_result_bounds_defaults_are_rejection_thresholds() -> None:
    bounds = ResultBounds()
    assert bounds.max_text_chars > 0
    assert bounds.max_serialized_bytes > 0
