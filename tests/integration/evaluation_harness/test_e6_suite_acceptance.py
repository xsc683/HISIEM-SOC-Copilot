"""E6 acceptance: aggregate a complete XP-01 acceptance run (Stage E / E6 §29, §33).

This module is the E6 acceptance gate. It reads the machine-readable
``gate-results.json`` artifacts an acceptance run produced for the XP-01 families and
turns them into the single deterministic Stage E aggregate artifact.

It is driven by an explicit run, because the 29 scenarios span four execution
profiles - deterministic, focused integration, and two runtime-integrated slices
(real HISIEM + real OTel Collector). The command is::

    pytest tests/unit/evaluation/cross_plane tests/unit/evaluation_harness \
           tests/integration/evaluation_harness \
           --basetemp=<artifacts dir> -q
    E6_ARTIFACTS_DIR=<artifacts dir> pytest \
           tests/integration/evaluation_harness/test_e6_suite_acceptance.py -q

Without ``E6_ARTIFACTS_DIR`` the acceptance test skips with that reason: E6 must
aggregate a REAL run's artifacts, never a synthesized set.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hisiem_soc_copilot.evaluation.cross_plane import GateStatus
from hisiem_soc_copilot.evaluation_harness.cross_plane_suite import (
    SUITE_RESULTS_SCHEMA_VERSION,
    build_suite_payload,
    collect_gate_results_tree,
    family_counts,
    read_suite_results,
    require_complete,
    scenario_ids,
    suite_identity,
    suite_results_path,
    suite_verdict,
    unexpected_and_missing,
    write_suite_results,
)

_ARTIFACTS_ENV = "E6_ARTIFACTS_DIR"


@pytest.fixture(scope="module")
def acceptance_artifacts() -> Path:
    raw = os.environ.get(_ARTIFACTS_ENV, "").strip()
    if not raw:
        pytest.skip(
            "E6 acceptance: no acceptance run artifacts supplied "
            f"({_ARTIFACTS_ENV} is unset)"
        )
    root = Path(raw)
    if not root.is_dir():
        pytest.skip(f"E6 acceptance: {_ARTIFACTS_ENV} does not exist: {raw}")
    return root


def test_the_acceptance_run_covers_all_29_scenarios(acceptance_artifacts: Path) -> None:
    collected = collect_gate_results_tree(acceptance_artifacts)
    unknown, missing = unexpected_and_missing(collected.results)
    assert unknown == (), f"the run produced results for non-catalog scenarios: {unknown}"
    assert missing == (), f"the run did not produce results for: {missing}"
    require_complete(collected.results)
    assert len(collected.results) == len(scenario_ids()) == 29


def test_every_required_scenario_passes(acceptance_artifacts: Path) -> None:
    collected = collect_gate_results_tree(acceptance_artifacts)
    failed = {
        scenario_id: result.overall_gate.value
        for scenario_id, result in collected.results.items()
        if result.overall_gate is not GateStatus.PASS
    }
    assert not failed, f"scenarios did not pass: {failed}"


def test_the_suite_verdict_is_pass_and_non_compensating(acceptance_artifacts: Path) -> None:
    collected = collect_gate_results_tree(acceptance_artifacts)
    verdict = suite_verdict(collected.results)
    assert verdict.overall is GateStatus.PASS
    assert verdict.failure_codes == ()


def test_the_aggregate_artifact_is_written_and_valid(
    acceptance_artifacts: Path, tmp_path: Path
) -> None:
    collected = collect_gate_results_tree(acceptance_artifacts)
    payload = build_suite_payload(collected.results)
    assert payload["schema_version"] == SUITE_RESULTS_SCHEMA_VERSION
    assert payload["complete"] is True
    assert payload["overall_gate"] == GateStatus.PASS.value
    assert payload["scenario_count"] == 29
    assert sum(payload["families"].values()) == 29
    path = write_suite_results(suite_results_path(tmp_path), payload)
    restored = read_suite_results(path)
    assert restored["suite_identity"] == payload["suite_identity"]
    # Every invariant the aggregate surfaces must be decided and satisfied.
    for name, entry in restored["invariants"].items():
        assert entry["status"] == GateStatus.PASS.value, name


def test_the_aggregate_is_deterministic_across_two_reads(acceptance_artifacts: Path) -> None:
    first = build_suite_payload(collect_gate_results_tree(acceptance_artifacts).results)
    second = build_suite_payload(collect_gate_results_tree(acceptance_artifacts).results)
    assert suite_identity(first) == suite_identity(second)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_the_aggregate_matches_the_catalog_family_counts(acceptance_artifacts: Path) -> None:
    collected = collect_gate_results_tree(acceptance_artifacts)
    assert family_counts(collected.results) == {
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


def test_a_single_missing_artifact_fails_the_suite(acceptance_artifacts: Path) -> None:
    """The negative half: dropping one scenario's evidence fails the aggregate."""
    collected = collect_gate_results_tree(acceptance_artifacts)
    results = dict(collected.results)
    results.pop(next(iter(results)))
    assert suite_verdict(results).overall is GateStatus.FAIL


def test_a_single_failed_scenario_fails_the_suite(acceptance_artifacts: Path) -> None:
    """28 PASS + 1 FAIL is a FAIL — the real artifacts with one scenario failed.

    The failed scenario is built as a CONTRACT-VALID result (the frozen model refuses
    a scenario whose ``overall_gate`` is not the non-compensating verdict of its own
    gates), so this degrades the suite rather than corrupting one artifact.
    """
    from hisiem_soc_copilot.evaluation.cross_plane import (
        CrossPlaneGateResult,
        CrossPlaneScenarioResult,
        MeasurementSource,
        scenario,
    )

    collected = collect_gate_results_tree(acceptance_artifacts)
    results = dict(collected.results)
    victim = next(iter(results))
    spec = scenario(victim)
    results[victim] = CrossPlaneScenarioResult(
        scenario_id=victim,
        scenario_version=spec.scenario_version,
        gate_family=spec.gate_family,
        execution_profile=spec.minimum_profile,
        gate_results=tuple(
            CrossPlaneGateResult(
                gate_id=gate_id,
                status=GateStatus.FAIL,
                reason_codes=("FORBIDDEN_FACT_PRESENT",),
                measurement_source=MeasurementSource.PERSISTED_DOMAIN_FACT,
            )
            for gate_id in spec.required_gate_ids
        ),
        overall_gate=GateStatus.FAIL,
        gate_failures=tuple(spec.required_gate_ids),
    )
    verdict = suite_verdict(results)
    assert verdict.overall is GateStatus.FAIL
