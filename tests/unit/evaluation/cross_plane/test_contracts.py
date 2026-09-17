"""XP-01 contract tests (Stage E / E1 §42, §43).

Covers contract immutability, pack/scenario identity, schema-version rejection,
bounded fields, and the non-compensating verdict — including the mandatory negative
cases.
"""

from __future__ import annotations

import dataclasses
from dataclasses import replace

import pytest

from hisiem_soc_copilot.evaluation.cross_plane import (
    GATE_RESULTS_SCHEMA_VERSION,
    MAX_ID_LEN,
    SCENARIO_RESULT_SCHEMA_VERSION,
    XP_PACK_ID,
    XP_PACK_VERSION,
    BoundsViolation,
    CrossPlaneContractError,
    CrossPlaneGateResult,
    CrossPlaneScenarioResult,
    CrossPlaneScenarioSpec,
    ExecutionProfile,
    GateFamily,
    GateStatus,
    MeasurementReferences,
    MeasurementSource,
    Plane,
    ScenarioSchemaError,
    bounded_id,
    identity_hash,
    non_compensating_verdict,
    profile_satisfies,
)


def _spec(**overrides: object) -> CrossPlaneScenarioSpec:
    base = {
        "scenario_id": "XP-TEST-001",
        "title": "a test scenario",
        "gate_family": GateFamily.SECURITY,
        "minimum_profile": ExecutionProfile.DETERMINISTIC,
        "required_planes": (Plane.PERSISTENCE,),
        "required_gate_ids": ("SECRET_LEAK",),
    }
    return CrossPlaneScenarioSpec(**{**base, **overrides})  # type: ignore[arg-type]


def _result(**overrides: object) -> CrossPlaneScenarioResult:
    base = {
        "scenario_id": "XP-TEST-001",
        "scenario_version": "1",
        "gate_family": GateFamily.SECURITY,
        "execution_profile": ExecutionProfile.DETERMINISTIC,
        "gate_results": (
            CrossPlaneGateResult(gate_id="SECRET_LEAK", status=GateStatus.PASS),
        ),
        "overall_gate": GateStatus.PASS,
        "gate_failures": (),
    }
    return CrossPlaneScenarioResult(**{**base, **overrides})  # type: ignore[arg-type]


# --- identity -----------------------------------------------------------------


def test_pack_identity_is_frozen() -> None:
    assert XP_PACK_ID == "XP-01"
    assert XP_PACK_VERSION == "1"


def test_scenario_spec_is_immutable() -> None:
    spec = _spec()
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.scenario_id = "XP-OTHER-001"  # type: ignore[misc]


def test_scenario_identity_is_deterministic_and_order_independent() -> None:
    first = _spec(required_gate_ids=("SECRET_LEAK", "ORACLE_FIREWALL"))
    second = _spec(required_gate_ids=("ORACLE_FIREWALL", "SECRET_LEAK"))
    assert first.identity() == second.identity()


def test_scenario_identity_changes_with_semantics() -> None:
    assert _spec().identity() != _spec(scenario_version="2").identity()
    assert _spec().identity() != _spec(required_gate_ids=("ORACLE_FIREWALL",)).identity()


def test_scenario_identity_ignores_the_human_title() -> None:
    """Identity is semantic, not presentational.

    ``title`` is human-facing metadata, so two entries differing only in prose are
    the same scenario — which is exactly why the catalog validator rejects them as
    duplicates rather than shipping what would be a second copy of one contract.
    """
    assert _spec().identity() == _spec(title="an entirely different title").identity()


def test_identity_hash_ignores_key_order() -> None:
    assert identity_hash({"a": 1, "b": 2}) == identity_hash({"b": 2, "a": 1})


def test_scenario_identity_has_no_clock_or_randomness() -> None:
    # Two independently constructed specs must agree; nothing ambient may enter.
    assert _spec().identity() == _spec().identity()


# --- pack identity mismatch (E1 §43) -----------------------------------------


def test_scenario_rejects_wrong_pack_id() -> None:
    with pytest.raises(CrossPlaneContractError, match="pack_id must be"):
        _spec(pack_id="GP-01")


# --- bounds (E1 §43) ---------------------------------------------------------


def test_bounded_id_rejects_oversized_value() -> None:
    with pytest.raises(BoundsViolation, match="exceeds"):
        bounded_id("x" * (MAX_ID_LEN + 1), what="scenario_id")


def test_bounded_id_rejects_empty_value() -> None:
    with pytest.raises(BoundsViolation, match="non-empty"):
        bounded_id("   ", what="scenario_id")


def test_scenario_rejects_oversized_title() -> None:
    with pytest.raises(BoundsViolation, match="title exceeds"):
        _spec(title="t" * 201)


def test_scenario_rejects_duplicate_gate_ids() -> None:
    with pytest.raises(CrossPlaneContractError, match="duplicates"):
        _spec(required_gate_ids=("SECRET_LEAK", "SECRET_LEAK"))


def test_blocking_scenario_requires_a_gate() -> None:
    """E1 §43: an empty required blocking gate set must be rejected."""
    with pytest.raises(CrossPlaneContractError, match="must require at least one"):
        _spec(required_gate_ids=())


def test_non_blocking_scenario_may_have_no_gate() -> None:
    spec = _spec(required_gate_ids=(), blocking=False)
    assert spec.blocking is False


# --- gate result contract ----------------------------------------------------


def test_pass_gate_cannot_carry_failure_reasons() -> None:
    with pytest.raises(CrossPlaneContractError, match="must not carry"):
        CrossPlaneGateResult(
            gate_id="SECRET_LEAK",
            status=GateStatus.PASS,
            reason_codes=("SECRET_SCAN_VIOLATION",),
        )


def test_fail_gate_must_carry_a_reason() -> None:
    with pytest.raises(CrossPlaneContractError, match="at least one reason code"):
        CrossPlaneGateResult(gate_id="SECRET_LEAK", status=GateStatus.FAIL)


# --- non-compensation (E1 §18, §43, §46) -------------------------------------


def test_any_failed_required_gate_fails_the_scenario() -> None:
    """The mandatory non-compensation contract: PASS PASS FAIL PASS -> FAIL."""
    results = (
        CrossPlaneGateResult(gate_id="A", status=GateStatus.PASS),
        CrossPlaneGateResult(gate_id="B", status=GateStatus.PASS),
        CrossPlaneGateResult(
            gate_id="C", status=GateStatus.FAIL, reason_codes=("SECRET_SCAN_VIOLATION",)
        ),
        CrossPlaneGateResult(gate_id="D", status=GateStatus.PASS),
    )
    overall, failures = non_compensating_verdict(results)
    assert overall is GateStatus.FAIL
    assert failures == ("C",)


def test_all_passed_gates_pass_the_scenario() -> None:
    results = (
        CrossPlaneGateResult(gate_id="A", status=GateStatus.PASS),
        CrossPlaneGateResult(gate_id="B", status=GateStatus.PASS),
    )
    overall, failures = non_compensating_verdict(results)
    assert overall is GateStatus.PASS
    assert failures == ()


def test_non_compensating_verdict_exposes_no_score_parameter() -> None:
    """There is no argument through which a score could compensate a failure."""
    import inspect

    params = list(inspect.signature(non_compensating_verdict).parameters)
    assert params == ["gate_results"]


def test_result_rejects_an_overall_gate_that_contradicts_its_gates() -> None:
    """E1 §43: a failed required gate must not be recorded as an overall PASS."""
    with pytest.raises(CrossPlaneContractError, match="not the non-compensating"):
        _result(
            gate_results=(
                CrossPlaneGateResult(
                    gate_id="SECRET_LEAK",
                    status=GateStatus.FAIL,
                    reason_codes=("SECRET_SCAN_VIOLATION",),
                ),
            ),
            overall_gate=GateStatus.PASS,
            gate_failures=("SECRET_LEAK",),
        )


def test_result_rejects_gate_failures_that_do_not_match_its_results() -> None:
    with pytest.raises(CrossPlaneContractError, match="do not match"):
        _result(gate_failures=("SECRET_LEAK",))


def test_result_rejects_duplicate_gate_results() -> None:
    with pytest.raises(CrossPlaneContractError, match="duplicate gate result"):
        _result(
            gate_results=(
                CrossPlaneGateResult(gate_id="SECRET_LEAK", status=GateStatus.PASS),
                CrossPlaneGateResult(gate_id="SECRET_LEAK", status=GateStatus.PASS),
            )
        )


# --- schema version (E1 §43) -------------------------------------------------


def test_scenario_result_rejects_unknown_schema_on_read() -> None:
    payload = _result().to_payload()
    payload["schema_version"] = "cross-plane-scenario-result/v999"
    with pytest.raises(ScenarioSchemaError, match="unsupported"):
        CrossPlaneScenarioResult.from_payload(payload)


def test_scenario_result_rejects_unknown_schema_on_construct() -> None:
    with pytest.raises(ScenarioSchemaError, match="unsupported"):
        _result(schema_version="cross-plane-scenario-result/v999")


def test_scenario_result_round_trips() -> None:
    original = _result()
    restored = CrossPlaneScenarioResult.from_payload(original.to_payload())
    assert restored == original


def test_scenario_result_rejects_unknown_enum_values() -> None:
    payload = _result().to_payload()
    payload["execution_profile"] = "not-a-profile"
    with pytest.raises(CrossPlaneContractError, match="unknown execution profile"):
        CrossPlaneScenarioResult.from_payload(payload)


def test_artifact_schema_constants_differ_from_gp01() -> None:
    """XP-01 must not reuse GP-01's schema identity (E1 §15)."""
    from hisiem_soc_copilot.evaluation import MANIFEST_SCHEMA_VERSION

    assert GATE_RESULTS_SCHEMA_VERSION != MANIFEST_SCHEMA_VERSION
    assert SCENARIO_RESULT_SCHEMA_VERSION != MANIFEST_SCHEMA_VERSION


# --- references and source categories ---------------------------------------


def test_measurement_references_round_trip_and_bound() -> None:
    refs = MeasurementReferences(
        investigation_id="inv-1",
        evidence_ids=("e1", "e2"),
        trace_id="trace-1",
    )
    restored = MeasurementReferences.from_payload(refs.to_payload())
    assert restored == refs


def test_measurement_references_reject_oversized_id() -> None:
    with pytest.raises(BoundsViolation, match="exceeds"):
        MeasurementReferences.from_payload({"investigation_id": "x" * (MAX_ID_LEN + 1)})


def test_gate_result_round_trips_with_references() -> None:
    result = CrossPlaneGateResult(
        gate_id="SECRET_LEAK",
        status=GateStatus.FAIL,
        reason_codes=("SECRET_SCAN_VIOLATION",),
        measurement_source=MeasurementSource.TELEMETRY_FACT,
        references=MeasurementReferences(investigation_id="inv-1"),
    )
    assert CrossPlaneGateResult.from_payload(result.to_payload()) == result


def test_gate_result_rejects_unknown_measurement_source() -> None:
    payload = _result().gate_results[0].to_payload()
    payload["measurement_source"] = "NOT_A_SOURCE"
    with pytest.raises(CrossPlaneContractError, match="unknown measurement source"):
        CrossPlaneGateResult.from_payload(payload)


# --- execution profiles (E1 §17) --------------------------------------------


def test_profile_strength_ordering() -> None:
    assert profile_satisfies(
        ExecutionProfile.DETERMINISTIC, ExecutionProfile.DETERMINISTIC
    )
    assert profile_satisfies(
        ExecutionProfile.RUNTIME_INTEGRATED, ExecutionProfile.DETERMINISTIC
    )
    assert not profile_satisfies(
        ExecutionProfile.DETERMINISTIC, ExecutionProfile.RUNTIME_INTEGRATED
    )


def test_scenario_to_payload_is_bounded_and_serializable() -> None:
    import json

    spec = _spec()
    payload = spec.to_payload()
    json.dumps(payload)
    assert payload["pack_id"] == XP_PACK_ID
    assert payload["identity"] == spec.identity()


def test_replace_keeps_contract_frozen() -> None:
    spec = _spec()
    other = replace(spec, scenario_id="XP-TEST-002")
    assert spec.scenario_id == "XP-TEST-001"
    assert other.scenario_id == "XP-TEST-002"
