"""E6 cross-plane suite aggregation: 29 scenario results -> one acceptance artifact.

Stage E / E6 does not add product capability. It aggregates the machine-readable
XP-01 evidence the family stages produced into ONE deterministic, secret-safe,
stable-ordered acceptance view:

```text
E2/E3/E4/E5 scenario gate-results/v1 artifacts
        -> THIS MODULE (validate + aggregate)
        -> cross-plane-suite-results/v1
        -> final integration evidence
```

The frozen rules this module exists to keep true (E6 §29/§30/§34):

* **Completeness is required.** Every one of the 29 catalog scenarios must be
  accounted for with a machine-readable result. A missing scenario fails the suite;
  it is never averaged around.
* **Hard gates do not compensate.** Any required scenario FAIL, any required gate
  FAIL, or any missing result makes the suite FAIL. There is no score, no weight, no
  pass percentage that can offset a failure.
* **Identity is semantic.** The suite identity binds to the pack/version, the catalog
  identity, the scenario identities and their gate results, and the artifact schema
  versions — never to a timestamp, a host, or a human title.

The aggregate reuses the E1 artifact conventions (``cross-plane-*`` naming, the E1
secret scan, the shared atomic write) rather than introducing a second evaluation
system.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..evaluation.cross_plane import (
    GATE_RESULTS_FILENAME,
    GATE_RESULTS_SCHEMA_VERSION,
    SCENARIO_RESULT_SCHEMA_VERSION,
    XP01_SCENARIOS,
    XP_PACK_ID,
    XP_PACK_VERSION,
    CrossPlaneGateResult,
    CrossPlaneScenarioResult,
    GateStatus,
    MeasurementSource,
    assert_artifact_safe,
    canonical_json,
    catalog_identity,
    gate_results_from_payload,
    identity_hash,
    non_compensating_verdict,
    scenario,
)
from .cross_plane_adapter import XP01_SUBDIR
from .cross_plane_adapter import gate_results_path as _gate_results_path
from .record import atomic_write_json

#: The E6 aggregate schema. Versioned from the start; an unknown version is refused
#: on read rather than silently interpreted.
SUITE_RESULTS_SCHEMA_VERSION = "cross-plane-suite-results/v1"

#: The single filename an E6 acceptance run produces.
SUITE_RESULTS_FILENAME = "suite-results.json"

#: The permanent cross-plane invariants the aggregate must surface evidence for
#: (E6 §32). Each is decided by exactly one frozen hard gate — the aggregate reports
#: that gate's status across every scenario that requires it, never a new verdict.
INVARIANT_GATES: tuple[tuple[str, str], ...] = (
    ("knowledge_only_cannot_authorize_definitive_verdict", "KNOWLEDGE_ONLY_DEFINITIVE_VERDICT"),
    ("unadmitted_mcp_cannot_be_selected", "UNADMITTED_MCP_SELECTED"),
    ("write_mcp_cannot_be_model_selected", "WRITE_MCP_SELECTED"),
    ("tenant_isolation_holds", "CROSS_TENANT_LEAK"),
    ("credential_marker_absent", "SECRET_LEAK"),
    ("citation_integrity_holds", "DANGLING_CITATION"),
    ("cross_investigation_citation_absent", "CROSS_INVESTIGATION_CITATION"),
    ("execution_without_approval_zero", "EXECUTION_WITHOUT_APPROVAL"),
    ("submission_as_success_zero", "SUBMISSION_TREATED_AS_SUCCESS"),
    ("telemetry_loss_does_not_change_business_outcome", "TELEMETRY_CHANGED_BUSINESS_STATE"),
    ("oracle_firewall_holds", "ORACLE_FIREWALL"),
    ("expected_facts_present", "EXPECTED_FACTS_PRESENT"),
    ("forbidden_facts_absent", "FORBIDDEN_FACTS_ABSENT"),
)

#: Reasons the suite itself can fail. Kept as stable codes so the verdict is
#: machine-readable, not prose.
SUITE_FAIL_MISSING_SCENARIO = "MISSING_SCENARIO_RESULT"
SUITE_FAIL_UNKNOWN_SCENARIO = "UNKNOWN_SCENARIO_RESULT"
SUITE_FAIL_SCENARIO_FAILED = "SCENARIO_FAILED"
SUITE_FAIL_SCENARIO_NOT_EVALUABLE = "SCENARIO_NOT_EVALUABLE"
SUITE_FAIL_GATE_RESULT_MISSING = "GATE_RESULT_MISSING"


class SuiteAggregationError(ValueError):
    """The aggregate input was not a valid, complete XP-01 result set."""


@dataclass(frozen=True)
class SuiteVerdict:
    """The non-compensating Stage E verdict, with the reasons it was reached."""

    overall: GateStatus
    failure_codes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.overall is GateStatus.PASS


def scenario_ids() -> tuple[str, ...]:
    """Every XP-01 scenario id, in stable catalog order."""
    return tuple(sorted(spec.scenario_id for spec in XP01_SCENARIOS))


def _scenario_entry(result: CrossPlaneScenarioResult) -> dict[str, Any]:
    spec = scenario(result.scenario_id)
    return {
        "scenario_id": result.scenario_id,
        "scenario_version": result.scenario_version,
        "gate_family": spec.gate_family.value,
        "execution_profile": result.execution_profile.value,
        "required_gate_ids": list(spec.required_gate_ids),
        "expected_facts": list(spec.expected_facts),
        "forbidden_facts": list(spec.forbidden_facts),
        "overall_gate": result.overall_gate.value,
        "gate_failures": list(result.gate_failures),
        "gate_results": [
            {
                "gate_id": gate.gate_id,
                "status": gate.status.value,
                "reason_codes": list(gate.reason_codes),
                "measurement_source": gate.measurement_source.value,
            }
            for gate in result.gate_results
        ],
    }


def suite_verdict(results: Mapping[str, CrossPlaneScenarioResult]) -> SuiteVerdict:
    """The non-compensating verdict over the WHOLE catalog.

    Missing scenarios, unknown scenarios, non-PASS scenario verdicts, and missing
    per-gate results all fail the suite. Nothing is averaged and nothing compensates.
    """
    catalog = scenario_ids()
    failures: list[str] = []
    missing = [sid for sid in catalog if sid not in results]
    unknown = [sid for sid in results if sid not in catalog]
    if missing:
        failures.append(SUITE_FAIL_MISSING_SCENARIO)
    if unknown:
        failures.append(SUITE_FAIL_UNKNOWN_SCENARIO)

    for scenario_id in catalog:
        result = results.get(scenario_id)
        if result is None:
            continue
        if result.overall_gate is not GateStatus.PASS:
            failures.append(SUITE_FAIL_SCENARIO_FAILED)
        required = set(scenario(scenario_id).required_gate_ids)
        reported = {gate.gate_id for gate in result.gate_results}
        if required - reported:
            failures.append(SUITE_FAIL_GATE_RESULT_MISSING)

    codes = tuple(dict.fromkeys(failures))
    if codes:
        return SuiteVerdict(overall=GateStatus.FAIL, failure_codes=codes)
    # The frozen non-compensating rule, applied to the per-scenario verdicts, is the
    # suite verdict: any FAIL is a FAIL, and a complete all-PASS set is a PASS.
    overall, _ = non_compensating_verdict(
        [
            CrossPlaneGateResult(
                gate_id=scenario_id,
                status=results[scenario_id].overall_gate,
                reason_codes=(),
                measurement_source=results[scenario_id].gate_results[
                    0
                ].measurement_source
                if results[scenario_id].gate_results
                else MeasurementSource.PERSISTED_DOMAIN_FACT,
            )
            for scenario_id in catalog
        ]
    )
    return SuiteVerdict(overall=overall, failure_codes=())


def invariant_summary(
    results: Mapping[str, CrossPlaneScenarioResult],
) -> dict[str, dict[str, Any]]:
    """Per invariant: which scenarios decide it and whether every one passed."""
    summary: dict[str, dict[str, Any]] = {}
    for name, gate_id in INVARIANT_GATES:
        deciders = [
            scenario_id
            for scenario_id in scenario_ids()
            if gate_id in scenario(scenario_id).required_gate_ids
            if scenario_id in results
        ]
        statuses = [
            next(
                gate.status
                for gate in results[scenario_id].gate_results
                if gate.gate_id == gate_id
            )
            for scenario_id in deciders
        ]
        summary[name] = {
            "gate_id": gate_id,
            "scenarios": deciders,
            "status": (
                GateStatus.PASS.value
                if deciders and all(status is GateStatus.PASS for status in statuses)
                else GateStatus.FAIL.value
            ),
        }
    return summary


def family_counts(results: Mapping[str, CrossPlaneScenarioResult]) -> dict[str, int]:
    """Exact scenario counts per family, from the catalog (never from prose)."""
    counts: dict[str, int] = {}
    for spec in XP01_SCENARIOS:
        counts[spec.gate_family.value] = counts.get(spec.gate_family.value, 0) + 1
    return dict(sorted(counts.items()))


def build_suite_payload(results: Mapping[str, CrossPlaneScenarioResult]) -> dict[str, Any]:
    """Build the deterministic aggregate payload (stable order, no timestamps)."""
    verdict = suite_verdict(results)
    entries = [
        _scenario_entry(results[scenario_id])
        for scenario_id in scenario_ids()
        if scenario_id in results
    ]
    payload: dict[str, Any] = {
        "schema_version": SUITE_RESULTS_SCHEMA_VERSION,
        "pack_id": XP_PACK_ID,
        "pack_version": XP_PACK_VERSION,
        "scenario_result_schema": SCENARIO_RESULT_SCHEMA_VERSION,
        "gate_results_schema": GATE_RESULTS_SCHEMA_VERSION,
        "catalog_identity": catalog_identity(),
        "scenario_count": len(entries),
        "catalog_scenario_count": len(XP01_SCENARIOS),
        "complete": len(entries) == len(XP01_SCENARIOS),
        "overall_gate": verdict.overall.value,
        "failure_codes": list(verdict.failure_codes),
        "families": family_counts(results),
        "scenarios": entries,
        "invariants": invariant_summary(results),
    }
    payload["suite_identity"] = suite_identity(payload)
    return payload


def suite_identity(payload: Mapping[str, Any]) -> str:
    """Semantic identity of the aggregate: pack, catalog, scenario + gate results.

    Deliberately excludes anything that is not evidence: no timestamp, no host, no
    title, no prose.
    """
    material = {
        "pack_id": payload.get("pack_id"),
        "pack_version": payload.get("pack_version"),
        "catalog_identity": payload.get("catalog_identity"),
        "gate_results_schema": payload.get("gate_results_schema"),
        "scenarios": [
            {
                "scenario_id": entry["scenario_id"],
                "scenario_version": entry["scenario_version"],
                "execution_profile": entry["execution_profile"],
                "overall_gate": entry["overall_gate"],
                "gate_results": [
                    [gate["gate_id"], gate["status"]] for gate in entry["gate_results"]
                ],
            }
            for entry in payload.get("scenarios", [])
        ],
        "overall_gate": payload.get("overall_gate"),
        "failure_codes": payload.get("failure_codes"),
    }
    return identity_hash(canonical_json(material))


def write_suite_results(path: str | Path, payload: Mapping[str, Any]) -> Path:
    """Secret-scan then atomically write the aggregate (E6 §33)."""
    assert_artifact_safe(payload)
    target = Path(path)
    atomic_write_json(target, dict(payload))
    return target


def suite_results_path(executions_dir: str | Path) -> Path:
    """The canonical aggregate path: ``<executions_dir>/xp-01/suite-results.json``."""
    return Path(executions_dir) / XP01_SUBDIR / SUITE_RESULTS_FILENAME


def read_suite_results(path: str | Path) -> dict[str, Any]:
    """Read and validate an aggregate artifact; an unknown schema is refused."""
    raw = Path(path).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, Mapping):
        raise SuiteAggregationError("suite-results artifact must be a JSON object")
    schema = payload.get("schema_version")
    if schema != SUITE_RESULTS_SCHEMA_VERSION:
        raise SuiteAggregationError(
            f"unsupported suite-results schema {schema!r}; expected "
            f"{SUITE_RESULTS_SCHEMA_VERSION}"
        )
    if payload.get("pack_id") != XP_PACK_ID:
        raise SuiteAggregationError("suite-results artifact is not an XP-01 aggregate")
    return dict(payload)


# ---------------------------------------------------------------------------
# Reading the per-scenario artifacts the family stages produced
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectedResults:
    """Scenario results read from an acceptance run, with the paths they came from."""

    results: Mapping[str, CrossPlaneScenarioResult]
    artifacts: Mapping[str, str]

    @property
    def scenario_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.results))


def collect_gate_results(executions_dir: str | Path) -> CollectedResults:
    """Read every ``<executions_dir>/xp-01/<scenario>/gate-results.json``.

    A missing artifact is simply absent from the result set — the caller’s
    completeness check (``suite_verdict``) decides whether that is a failure.
    """
    results: dict[str, CrossPlaneScenarioResult] = {}
    artifacts: dict[str, str] = {}
    for scenario_id in scenario_ids():
        path = _gate_results_path(executions_dir, scenario_id)
        if not path.is_file():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            continue
        result = gate_results_from_payload(raw)
        if result.scenario_id != scenario_id:
            continue
        results[scenario_id] = result
        artifacts[scenario_id] = path.relative_to(Path(executions_dir)).as_posix()
    return CollectedResults(results=results, artifacts=artifacts)


def collect_gate_results_tree(root: str | Path) -> CollectedResults:
    """Read every ``**/xp-01/<scenario>/gate-results.json`` beneath ``root``.

    An acceptance run executes the family test suites with one shared output root, so
    each scenario's artifact may sit under that run's own directory. Paths are visited
    in stable sorted order and the FIRST artifact per scenario wins, which makes the
    collection deterministic regardless of filesystem order.
    """
    base = Path(root)
    results: dict[str, CrossPlaneScenarioResult] = {}
    artifacts: dict[str, str] = {}
    catalog = set(scenario_ids())
    for path in sorted(base.rglob(f"{XP01_SUBDIR}/**/{GATE_RESULTS_FILENAME}")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, Mapping):
            continue
        scenario_id = str(raw.get("scenario_id", ""))
        if scenario_id not in catalog or scenario_id in results:
            continue
        try:
            result = gate_results_from_payload(raw)
        except Exception:
            continue
        results[scenario_id] = result
        artifacts[scenario_id] = path.relative_to(base).as_posix()
    return CollectedResults(results=results, artifacts=artifacts)


def unexpected_and_missing(
    results: Mapping[str, CrossPlaneScenarioResult],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(unknown_ids, missing_ids)`` relative to the implemented catalog."""
    catalog = set(scenario_ids())
    unknown = tuple(sorted(set(results) - catalog))
    missing = tuple(sorted(catalog - set(results)))
    return unknown, missing


def require_complete(results: Mapping[str, CrossPlaneScenarioResult]) -> None:
    """Raise unless the result set is exactly the catalog (E6 §29)."""
    unknown, missing = unexpected_and_missing(results)
    if unknown or missing:
        raise SuiteAggregationError(
            f"XP-01 result set is not complete: unknown={list(unknown)} "
            f"missing={list(missing)}"
        )


def scenario_matrix(
    results: Mapping[str, CrossPlaneScenarioResult],
) -> Sequence[Mapping[str, str]]:
    """A stable, bounded per-scenario view for reporting."""
    matrix: list[Mapping[str, str]] = []
    for scenario_id in scenario_ids():
        result = results.get(scenario_id)
        spec = scenario(scenario_id)
        matrix.append(
            {
                "scenario_id": scenario_id,
                "family": spec.gate_family.value,
                "profile": spec.minimum_profile.value,
                "gates": ",".join(spec.required_gate_ids),
                "result": result.overall_gate.value if result is not None else "MISSING",
            }
        )
    return tuple(matrix)


__all__ = [
    "INVARIANT_GATES",
    "SUITE_RESULTS_FILENAME",
    "SUITE_RESULTS_SCHEMA_VERSION",
    "SUITE_FAIL_GATE_RESULT_MISSING",
    "SUITE_FAIL_MISSING_SCENARIO",
    "SUITE_FAIL_SCENARIO_FAILED",
    "SUITE_FAIL_UNKNOWN_SCENARIO",
    "CollectedResults",
    "SuiteAggregationError",
    "SuiteVerdict",
    "build_suite_payload",
    "collect_gate_results",
    "collect_gate_results_tree",
    "family_counts",
    "invariant_summary",
    "read_suite_results",
    "require_complete",
    "scenario_ids",
    "scenario_matrix",
    "suite_identity",
    "suite_results_path",
    "suite_verdict",
    "unexpected_and_missing",
    "write_suite_results",
]
