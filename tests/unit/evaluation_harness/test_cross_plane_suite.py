"""E6 suite-aggregation tests (Stage E / E6 §29, §30, §33, §34).

The aggregate turns 29 machine-readable scenario results into one deterministic,
secret-safe acceptance artifact. These tests prove the completeness rule, the
non-compensating rule, the semantic identity, and the artifact contract — including
that the aggregate can actually FAIL.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hisiem_soc_copilot.evaluation.cross_plane import (
    GATE_RESULTS_SCHEMA_VERSION,
    XP_PACK_ID,
    CrossPlaneGateResult,
    CrossPlaneScenarioResult,
    GateStatus,
    MeasurementSource,
    scenario,
)
from hisiem_soc_copilot.evaluation_harness.cross_plane_suite import (
    SUITE_FAIL_GATE_RESULT_MISSING,
    SUITE_FAIL_MISSING_SCENARIO,
    SUITE_FAIL_SCENARIO_FAILED,
    SUITE_FAIL_UNKNOWN_SCENARIO,
    SUITE_RESULTS_SCHEMA_VERSION,
    SuiteAggregationError,
    build_suite_payload,
    family_counts,
    invariant_summary,
    read_suite_results,
    require_complete,
    scenario_ids,
    suite_identity,
    suite_results_path,
    suite_verdict,
    unexpected_and_missing,
    write_suite_results,
)


def _result(scenario_id: str, *, status: GateStatus = GateStatus.PASS) -> CrossPlaneScenarioResult:
    spec = scenario(scenario_id)
    gates = tuple(
        CrossPlaneGateResult(
            gate_id=gate_id,
            status=status,
            reason_codes=() if status is GateStatus.PASS else ("FORBIDDEN_FACT_PRESENT",),
            measurement_source=MeasurementSource.PERSISTED_DOMAIN_FACT,
        )
        for gate_id in spec.required_gate_ids
    )
    return CrossPlaneScenarioResult(
        scenario_id=scenario_id,
        scenario_version="1",
        gate_family=spec.gate_family,
        execution_profile=spec.minimum_profile,
        gate_results=gates,
        overall_gate=status,
        gate_failures=(
            () if status is GateStatus.PASS else tuple(spec.required_gate_ids)
        ),
    )


def _all_pass() -> dict[str, CrossPlaneScenarioResult]:
    return {scenario_id: _result(scenario_id) for scenario_id in scenario_ids()}


# ---------------------------------------------------------------------------
# Catalog completeness
# ---------------------------------------------------------------------------


def test_the_catalog_has_29_scenarios() -> None:
    assert len(scenario_ids()) == 29


def test_family_counts_come_from_the_catalog() -> None:
    counts = family_counts(_all_pass())
    assert counts == {
        "AUTHORITY": 5,
        "CAPABILITY": 1,
        "KNOWLEDGE": 4,
        "MCP": 5,
        "OBSERVABILITY": 2,
        "RELIABILITY": 5,
        "SECURITY": 3,
        "TENANT": 2,
        "WORKSPACE": 2,
    }
    assert sum(counts.values()) == 29


def test_a_complete_all_pass_set_is_complete() -> None:
    unknown, missing = unexpected_and_missing(_all_pass())
    assert unknown == () and missing == ()
    require_complete(_all_pass())


def test_a_missing_scenario_is_reported_and_raises() -> None:
    results = _all_pass()
    del results["XP-UX-002"]
    unknown, missing = unexpected_and_missing(results)
    assert unknown == () and missing == ("XP-UX-002",)
    with pytest.raises(SuiteAggregationError, match="not complete"):
        require_complete(results)


def test_an_unknown_scenario_is_reported_and_raises() -> None:
    results = _all_pass()
    results["XP-NOPE-001"] = _result("XP-UX-001")
    unknown, missing = unexpected_and_missing(results)
    assert unknown == ("XP-NOPE-001",) and missing == ()
    with pytest.raises(SuiteAggregationError):
        require_complete(results)


# ---------------------------------------------------------------------------
# Non-compensating verdict
# ---------------------------------------------------------------------------


def test_a_complete_all_pass_set_verdicts_pass() -> None:
    verdict = suite_verdict(_all_pass())
    assert verdict.overall is GateStatus.PASS
    assert verdict.failure_codes == ()
    assert verdict.passed is True


def test_one_failing_scenario_fails_the_suite() -> None:
    """28 PASS + 1 FAIL is a FAIL: there is no averaging."""
    results = _all_pass()
    results["XP-SEC-001"] = _result("XP-SEC-001", status=GateStatus.FAIL)
    verdict = suite_verdict(results)
    assert verdict.overall is GateStatus.FAIL
    assert verdict.failure_codes == (SUITE_FAIL_SCENARIO_FAILED,)


def test_one_missing_scenario_fails_the_suite() -> None:
    results = _all_pass()
    del results["XP-REL-005"]
    verdict = suite_verdict(results)
    assert verdict.overall is GateStatus.FAIL
    assert SUITE_FAIL_MISSING_SCENARIO in verdict.failure_codes


def test_an_unknown_scenario_fails_the_suite() -> None:
    results = _all_pass()
    results["XP-NOPE-001"] = _result("XP-UX-001")
    verdict = suite_verdict(results)
    assert verdict.overall is GateStatus.FAIL
    assert SUITE_FAIL_UNKNOWN_SCENARIO in verdict.failure_codes


def test_a_scenario_missing_a_required_gate_result_fails_the_suite() -> None:
    results = _all_pass()
    original = results["XP-MCP-001"]
    results["XP-MCP-001"] = CrossPlaneScenarioResult(
        scenario_id=original.scenario_id,
        scenario_version=original.scenario_version,
        gate_family=original.gate_family,
        execution_profile=original.execution_profile,
        gate_results=(),
        overall_gate=GateStatus.PASS,
        gate_failures=(),
    )
    verdict = suite_verdict(results)
    assert verdict.overall is GateStatus.FAIL
    assert SUITE_FAIL_GATE_RESULT_MISSING in verdict.failure_codes


def test_a_report_claiming_pass_without_gate_evidence_cannot_compensate() -> None:
    """A scenario that self-declares PASS with no gate results still fails."""
    results = _all_pass()
    results["XP-KNOW-001"] = CrossPlaneScenarioResult(
        scenario_id="XP-KNOW-001",
        scenario_version="1",
        gate_family=scenario("XP-KNOW-001").gate_family,
        execution_profile=scenario("XP-KNOW-001").minimum_profile,
        gate_results=(),
        overall_gate=GateStatus.PASS,
        gate_failures=(),
    )
    assert suite_verdict(results).overall is GateStatus.FAIL


# ---------------------------------------------------------------------------
# Invariant summary
# ---------------------------------------------------------------------------


def test_every_invariant_gate_is_satisfied_by_a_complete_pass_set() -> None:
    summary = invariant_summary(_all_pass())
    assert summary
    for name, entry in summary.items():
        assert entry["scenarios"], name
        assert entry["status"] == GateStatus.PASS.value, name


def test_an_invariant_gate_left_undecided_is_not_a_pass() -> None:
    """A gate no scenario requires would be reported FAIL, never silently PASS."""
    summary = invariant_summary({})
    for entry in summary.values():
        assert entry["status"] == GateStatus.FAIL.value


# ---------------------------------------------------------------------------
# Deterministic identity
# ---------------------------------------------------------------------------


def test_the_payload_is_deterministic_and_stable_ordered() -> None:
    first = build_suite_payload(_all_pass())
    second = build_suite_payload(_all_pass())
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert [entry["scenario_id"] for entry in first["scenarios"]] == list(scenario_ids())


def test_the_suite_identity_ignores_input_order() -> None:
    results = _all_pass()
    shuffled = {key: results[key] for key in reversed(list(results))}
    assert suite_identity(build_suite_payload(results)) == suite_identity(
        build_suite_payload(shuffled)
    )


def test_the_suite_identity_changes_when_a_gate_result_changes() -> None:
    results = _all_pass()
    baseline = suite_identity(build_suite_payload(results))
    results["XP-TEN-001"] = _result("XP-TEN-001", status=GateStatus.FAIL)
    assert suite_identity(build_suite_payload(results)) != baseline


def test_the_identity_contains_no_timestamp_or_prose() -> None:
    payload = build_suite_payload(_all_pass())
    material = json.dumps(payload)
    assert "timestamp" not in material.lower()
    assert "generated_at" not in material
    assert "title" not in material


def test_the_payload_binds_to_the_frozen_contracts() -> None:
    payload = build_suite_payload(_all_pass())
    assert payload["schema_version"] == SUITE_RESULTS_SCHEMA_VERSION
    assert payload["pack_id"] == XP_PACK_ID
    assert payload["gate_results_schema"] == GATE_RESULTS_SCHEMA_VERSION
    assert payload["catalog_identity"]
    assert payload["complete"] is True
    assert payload["overall_gate"] == GateStatus.PASS.value


# ---------------------------------------------------------------------------
# Artifact contract
# ---------------------------------------------------------------------------


def test_the_suite_artifact_round_trips(tmp_path: Path) -> None:
    payload = build_suite_payload(_all_pass())
    path = write_suite_results(
        suite_results_path(tmp_path), payload
    )
    assert path.name == "suite-results.json"
    assert path.parent.name == "xp-01"
    restored = read_suite_results(path)
    assert restored["suite_identity"] == payload["suite_identity"]
    assert restored["overall_gate"] == GateStatus.PASS.value


def test_an_unknown_suite_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "suite-results.json"
    path.write_text(
        json.dumps({"schema_version": "cross-plane-suite-results/v2"}), encoding="utf-8"
    )
    with pytest.raises(SuiteAggregationError, match="unsupported suite-results schema"):
        read_suite_results(path)


def test_a_non_xp01_aggregate_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "suite-results.json"
    path.write_text(
        json.dumps(
            {"schema_version": SUITE_RESULTS_SCHEMA_VERSION, "pack_id": "OTHER"}
        ),
        encoding="utf-8",
    )
    with pytest.raises(SuiteAggregationError, match="not an XP-01 aggregate"):
        read_suite_results(path)


def test_the_aggregate_carries_no_raw_telemetry_or_secret(tmp_path: Path) -> None:
    payload = build_suite_payload(_all_pass())
    material = json.dumps(payload)
    for forbidden in ("observation", "payload", "Bearer ", "raw_prompt", "prompt"):
        assert forbidden not in material
    write_suite_results(suite_results_path(tmp_path), payload)


def test_a_secret_in_the_aggregate_is_refused_before_the_write(tmp_path: Path) -> None:
    payload = build_suite_payload(_all_pass())
    payload["scenarios"][0]["scenario_id"] = "Authorization: Bearer synthetic-sentinel"
    with pytest.raises(Exception, match="SECRET|secret|marker"):
        write_suite_results(suite_results_path(tmp_path), payload)
    assert not suite_results_path(tmp_path).exists()


def test_the_aggregate_is_bounded() -> None:
    payload = build_suite_payload(_all_pass())
    assert len(json.dumps(payload)) < 256_000
    assert len(payload["scenarios"]) == 29
