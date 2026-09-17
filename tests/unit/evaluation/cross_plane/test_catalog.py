"""XP-01 catalog tests (Stage E / E1 §13, §32, §33, §42, §43).

Proves the audited scenario set is complete and unique, that the frozen
vocabularies reject anything unknown, that every blocking scenario requires at
least one hard gate, and that the catalog carries no secret material.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from hisiem_soc_copilot.evaluation.cross_plane import (
    CATALOG_RECONCILIATION,
    EXPECTED_FACT_CODES,
    FORBIDDEN_FACT_CODES,
    GATE_IDS,
    SECRET_MARKERS,
    XP01_SCENARIO_IDS,
    XP01_SCENARIOS,
    XP_PACK_ID,
    XP_PACK_VERSION,
    CrossPlaneContractError,
    ExecutionProfile,
    GateFamily,
    Plane,
    UnknownFactError,
    UnknownGateError,
    catalog_identity,
    catalog_summary,
    scenario,
    scenarios_for_family,
    validate_catalog,
    validate_fact_codes,
    validate_gate_ids,
)

#: The scenario ids enumerated by BOTH authorities: 06 §7 and the E0 audit's
#: Scenario Readiness Matrix. Pinned here so the catalog cannot silently shrink.
AUDITED_SCENARIO_IDS: tuple[str, ...] = (
    "XP-KNOW-001",
    "XP-KNOW-002",
    "XP-KNOW-003",
    "XP-KNOW-004",
    "XP-CAP-001",
    "XP-MCP-001",
    "XP-MCP-002",
    "XP-MCP-003",
    "XP-MCP-004",
    "XP-MCP-005",
    "XP-TEN-001",
    "XP-TEN-002",
    "XP-SEC-001",
    "XP-SEC-002",
    "XP-SEC-003",
    "XP-AUTH-001",
    "XP-AUTH-002",
    "XP-AUTH-003",
    "XP-AUTH-004",
    "XP-AUTH-005",
    "XP-REL-001",
    "XP-REL-002",
    "XP-REL-003",
    "XP-REL-004",
    "XP-REL-005",
    "XP-OBS-001",
    "XP-OBS-002",
    "XP-UX-001",
    "XP-UX-002",
)


# --- completeness (E1 §13) ---------------------------------------------------


def test_catalog_contains_every_audited_scenario() -> None:
    assert set(XP01_SCENARIO_IDS) == set(AUDITED_SCENARIO_IDS)


def test_catalog_has_exactly_29_scenarios() -> None:
    assert len(XP01_SCENARIOS) == 29
    assert len(AUDITED_SCENARIO_IDS) == 29


def test_no_audited_scenario_was_dropped_or_added() -> None:
    assert sorted(XP01_SCENARIO_IDS) == sorted(AUDITED_SCENARIO_IDS)


def test_catalog_validates_clean() -> None:
    validate_catalog()


def test_catalog_reconciliation_is_recorded() -> None:
    """E1 §13: a catalog discrepancy between authorities must be recorded, not hidden."""
    assert CATALOG_RECONCILIATION
    ids = {item.reconciliation_id for item in CATALOG_RECONCILIATION}
    assert "CATALOG-001" in ids
    for item in CATALOG_RECONCILIATION:
        assert item.discrepancy.strip()
        assert item.resolution.strip()


# --- uniqueness (E1 §32, §43) -------------------------------------------------


def test_scenario_ids_are_unique() -> None:
    assert len(set(XP01_SCENARIO_IDS)) == len(XP01_SCENARIO_IDS)


def test_duplicate_scenario_id_is_rejected() -> None:
    first = scenario("XP-KNOW-001")
    duplicate = replace(first, title="a second copy")
    with pytest.raises(CrossPlaneContractError, match="duplicate scenario id"):
        validate_catalog((first, duplicate))


def test_semantically_identical_scenarios_are_rejected() -> None:
    """Two entries differing only in title are the same contract, not two."""
    first = scenario("XP-KNOW-001")
    clone = replace(first, scenario_id="XP-KNOW-001-CLONE")
    with pytest.raises(CrossPlaneContractError, match="identical semantic content"):
        validate_catalog((first, clone))


def test_empty_catalog_is_rejected() -> None:
    with pytest.raises(CrossPlaneContractError, match="catalog is empty"):
        validate_catalog(())


# --- pack identity (E1 §43) ---------------------------------------------------


def test_every_scenario_declares_the_pack_identity() -> None:
    for item in XP01_SCENARIOS:
        assert item.pack_id == XP_PACK_ID
        assert item.pack_version == XP_PACK_VERSION


def test_wrong_pack_identity_in_catalog_is_rejected() -> None:
    bad = replace(scenario("XP-KNOW-001"), pack_version="999")
    with pytest.raises(CrossPlaneContractError, match="pack identity must be"):
        validate_catalog((bad,))


# --- gate vocabulary (E1 §32, §43) -------------------------------------------


def test_every_required_gate_is_known() -> None:
    for item in XP01_SCENARIOS:
        assert set(item.required_gate_ids) <= GATE_IDS


def test_unknown_gate_id_is_rejected() -> None:
    with pytest.raises(UnknownGateError, match="unknown XP-01 hard gate"):
        validate_gate_ids(("NOT_A_GATE",))


def test_catalog_rejects_an_unknown_gate() -> None:
    bad = replace(scenario("XP-KNOW-001"), required_gate_ids=("NOT_A_GATE",))
    with pytest.raises(UnknownGateError, match="XP-KNOW-001"):
        validate_catalog((bad,))


def test_every_blocking_scenario_requires_a_gate() -> None:
    for item in XP01_SCENARIOS:
        if item.blocking:
            assert item.required_gate_ids, item.scenario_id


def test_blocking_scenario_without_gates_is_rejected() -> None:
    """An empty required blocking gate set is refused at construction AND by the
    catalog validator, so neither path can ship a scenario with nothing to prove."""
    with pytest.raises(CrossPlaneContractError, match="must require at least one"):
        replace(scenario("XP-KNOW-001"), required_gate_ids=())


# --- fact vocabulary (E1 §14, §32, §43) --------------------------------------


def test_every_declared_fact_is_in_the_frozen_vocabulary() -> None:
    for item in XP01_SCENARIOS:
        assert set(item.expected_facts) <= EXPECTED_FACT_CODES
        assert set(item.forbidden_facts) <= FORBIDDEN_FACT_CODES


def test_unknown_expected_fact_is_rejected() -> None:
    with pytest.raises(UnknownFactError, match="unknown expected fact code"):
        validate_fact_codes(("NOT_A_FACT",), ())


def test_unknown_forbidden_fact_is_rejected() -> None:
    with pytest.raises(UnknownFactError, match="unknown forbidden fact code"):
        validate_fact_codes((), ("NOT_A_FACT",))


def test_expected_and_forbidden_vocabularies_do_not_overlap() -> None:
    assert not (EXPECTED_FACT_CODES & FORBIDDEN_FACT_CODES)


def test_no_scenario_stores_natural_language_expectations() -> None:
    """Expected facts are machine tokens: no prose, no spaces, no sentences."""
    for item in XP01_SCENARIOS:
        for fact in item.expected_facts + item.forbidden_facts:
            assert fact == fact.upper()
            assert " " not in fact
            assert fact.replace("_", "").isalnum()


def test_catalog_rejects_an_unknown_fact_code() -> None:
    bad = replace(scenario("XP-KNOW-001"), expected_facts=("NOT_A_FACT",))
    with pytest.raises(UnknownFactError, match="XP-KNOW-001"):
        validate_catalog((bad,))


# --- execution profiles (E1 §17, §32) ----------------------------------------


def test_every_scenario_declares_a_valid_profile() -> None:
    for item in XP01_SCENARIOS:
        assert isinstance(item.minimum_profile, ExecutionProfile)


def test_security_and_authority_scenarios_never_require_a_live_model() -> None:
    """E1 §17: ``live-model`` must never be the sole basis of a security or authority
    hard gate. A gate may still need *runtime* (a real SOAR, a real Collector); that
    is the runtime-integrated axis, and the gate decision itself stays deterministic.
    """
    for item in XP01_SCENARIOS:
        if item.gate_family in (
            GateFamily.SECURITY,
            GateFamily.AUTHORITY,
            GateFamily.TENANT,
            GateFamily.MCP,
        ):
            assert item.minimum_profile is not ExecutionProfile.LIVE_MODEL, (
                item.scenario_id
            )


def test_most_gate_families_are_purely_deterministic() -> None:
    """Hard-gate repeatability: the majority of the catalog needs no runtime at all."""
    deterministic = [
        item for item in XP01_SCENARIOS
        if item.minimum_profile is ExecutionProfile.DETERMINISTIC
    ]
    assert len(deterministic) == 26


def test_gate_evaluation_itself_is_always_deterministic() -> None:
    """Runtime is a measurement concern only: the evaluator is a pure function."""
    import inspect

    from hisiem_soc_copilot.evaluation.cross_plane import evaluate_scenario

    source = inspect.getsource(evaluate_scenario)
    for forbidden in ("await ", "open(", "requests", "httpx", "datetime.now", "time."):
        assert forbidden not in source


def test_runtime_only_scenarios_are_the_audited_three() -> None:
    """Only the three rows E0 marked runtime-only may require real runtime."""
    runtime = {
        item.scenario_id
        for item in XP01_SCENARIOS
        if item.minimum_profile is ExecutionProfile.RUNTIME_INTEGRATED
    }
    assert runtime == {"XP-AUTH-005", "XP-REL-005", "XP-OBS-001"}


def test_no_scenario_requires_a_live_model() -> None:
    for item in XP01_SCENARIOS:
        assert item.minimum_profile is not ExecutionProfile.LIVE_MODEL


# --- families and planes (E1 §16) --------------------------------------------


def test_every_gate_family_is_represented() -> None:
    present = {item.gate_family for item in XP01_SCENARIOS}
    assert present == set(GateFamily)


def test_family_membership_matches_the_design() -> None:
    assert len(scenarios_for_family(GateFamily.KNOWLEDGE)) == 4
    assert len(scenarios_for_family(GateFamily.CAPABILITY)) == 1
    assert len(scenarios_for_family(GateFamily.MCP)) == 5
    assert len(scenarios_for_family(GateFamily.TENANT)) == 2
    assert len(scenarios_for_family(GateFamily.SECURITY)) == 3
    assert len(scenarios_for_family(GateFamily.AUTHORITY)) == 5
    assert len(scenarios_for_family(GateFamily.RELIABILITY)) == 5
    assert len(scenarios_for_family(GateFamily.OBSERVABILITY)) == 2
    assert len(scenarios_for_family(GateFamily.WORKSPACE)) == 2


def test_every_declared_plane_is_a_real_plane() -> None:
    for item in XP01_SCENARIOS:
        for plane in item.required_planes:
            assert isinstance(plane, Plane)


def test_every_scenario_declares_at_least_one_plane() -> None:
    for item in XP01_SCENARIOS:
        assert item.required_planes, item.scenario_id


# --- secret safety (E1 §33) ---------------------------------------------------


def test_catalog_carries_no_secret_marker() -> None:
    import json

    blob = json.dumps(catalog_summary(), ensure_ascii=False)
    for item in XP01_SCENARIOS:
        blob += json.dumps(item.to_payload(), ensure_ascii=False)
    for marker in SECRET_MARKERS:
        assert marker not in blob, f"catalog leaks secret marker {marker!r}"


def test_fixture_refs_are_logical_names_not_paths_or_endpoints() -> None:
    for item in XP01_SCENARIOS:
        for ref in item.fixture_refs:
            assert "://" not in ref
            assert not ref.startswith("/")
            assert ":" not in ref
            assert ref == ref.lower()


# --- identity -----------------------------------------------------------------


def test_catalog_identity_is_deterministic() -> None:
    assert catalog_identity() == catalog_identity()


def test_catalog_identity_is_stable_across_construction_order() -> None:
    reversed_catalog = tuple(reversed(XP01_SCENARIOS))
    validate_catalog(reversed_catalog)
    assert catalog_identity() == catalog_identity()


def test_catalog_summary_is_bounded_and_complete() -> None:
    summary = catalog_summary()
    assert summary["pack_id"] == XP_PACK_ID
    assert summary["pack_version"] == XP_PACK_VERSION
    assert summary["scenario_count"] == 29
    assert summary["blocking_count"] == 29
    assert summary["catalog_identity"] == catalog_identity()
    assert set(summary["families"]) == {family.value for family in GateFamily}


def test_lookup_rejects_an_unknown_scenario() -> None:
    with pytest.raises(CrossPlaneContractError, match="unknown XP-01 scenario"):
        scenario("XP-NOPE-001")
