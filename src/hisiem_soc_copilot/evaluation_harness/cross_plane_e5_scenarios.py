"""E5 scenario drivers: one executable XP-01 path per E5-owned scenario.

E5 owns the 2 XP-01 scenarios in the WORKSPACE family (derived from the implemented
E1 catalog, never hard-coded here). Each driver turns an :class:`E5Fixture` — built
by the caller out of the REAL workspace projection, the REAL frontend authority
derivation, and the persisted facts behind them — into the E1 typed measurements its
scenario's gates require, and the shared runner feeds those through E1's
deterministic gates into a ``cross-plane-gate-results/v1`` artifact.

```text
E5Fixture (real workspace projection + real frontend derivation + persisted truth)
        -> driver  (this module: select the measurements this scenario needs)
        -> E1 typed measurement
        -> E1 evaluate_scenario        (the ONLY decision point)
        -> cross-plane-gate-results/v1
```

The drivers contain **no gate logic and no presentation policy**. They decide *which
facts to measure*, never what the facts mean; the E1 gates own that, and the workspace
projection + frontend own the behaviour being measured. Where a rule is already
implemented in production the measurement reads that implementation — see
``cross_plane_workspace``.

Profiles: both WORKSPACE scenarios declare ``deterministic``; the presentation rules
are pure functions of the persisted facts, the projection, and the frontend
derivation, so no runtime process is required to decide them. The browser acceptance
(real Vue workspace, real Chromium) is the E5 §18 layering's outermost level and is
reported separately.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..evaluation.cross_plane import (
    GATE_EXPECTED_FACTS_PRESENT,
    GATE_FORBIDDEN_FACTS_ABSENT,
    GATE_IDS,
    CrossPlaneScenarioResult,
    FactSetMeasurement,
    GateFamily,
    GateStatus,
    MeasurementSource,
    build_gate_results_payload,
    catalog_summary,
    dedupe_bounded_ids,
    evaluate_scenario,
    scenario,
    scenarios_for_family,
)
from .cross_plane_workspace import (
    PersistedWorkspaceFacts,
    PresentedWorkspace,
    workspace_authority_facts,
    workspace_reconstruction_facts,
)

#: The family E5 owns. Derived, not guessed: this is the E5 §9 ownership rule.
E5_FAMILIES: tuple[GateFamily, ...] = (GateFamily.WORKSPACE,)

#: XP-01 facts E5 can only learn from a real run/browser (supplied by the fixture).
RUN_FACTS = frozenset(
    {
        "WORKSPACE_AUTHORITY_LABELS_PRESENT",
        "WORKSPACE_RECONSTRUCTED_FROM_DURABLE_STATE",
        "WORKSPACE_STALE_OVERRIDDEN_BY_REFRESH",
    }
)


@dataclass(frozen=True)
class E5Fixture:
    """The real projection + persisted truth for the E5 drivers.

    ``presented`` is derived from the REAL workspace read model (and, for the
    authority classes, from the REAL frontend derivation), never hand-written here.
    ``persisted`` is read from the same repositories the projection was built from,
    so "what the workspace shows" is always compared against "what is persisted".
    """

    presented: PresentedWorkspace = field(default_factory=PresentedWorkspace)
    persisted: PersistedWorkspaceFacts = field(default_factory=PersistedWorkspaceFacts)
    replayed: PresentedWorkspace = field(default_factory=PresentedWorkspace)
    original: PresentedWorkspace = field(default_factory=PresentedWorkspace)
    stale_client: PresentedWorkspace = field(default_factory=PresentedWorkspace)
    server_truth: PresentedWorkspace = field(default_factory=PresentedWorkspace)
    observed_facts: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def runtime_facts(self, scenario_id: str) -> tuple[str, ...]:
        return tuple(self.observed_facts.get(scenario_id, ()))


def _merge(*measurements: FactSetMeasurement, extra: Sequence[str] = ()) -> FactSetMeasurement:
    """One bounded fact set from several measurements plus run-supplied facts."""
    facts: list[str] = list(extra)
    for measurement in measurements:
        facts.extend(measurement.observed_facts)
    return FactSetMeasurement(
        source=MeasurementSource.WORKSPACE_PROJECTION_FACT,
        observed_facts=dedupe_bounded_ids(facts, what="observed_facts"),
    )


def _authority(fixture: E5Fixture) -> FactSetMeasurement:
    return workspace_authority_facts(
        presented=fixture.presented, persisted=fixture.persisted
    )


def _reconstruction(fixture: E5Fixture) -> FactSetMeasurement:
    return workspace_reconstruction_facts(
        replayed=fixture.replayed or fixture.presented,
        original=fixture.original or fixture.presented,
        stale_client=fixture.stale_client,
        server_truth=fixture.server_truth,
        persisted=fixture.persisted,
    )


# ---------------------------------------------------------------------------
# XP-UX-001 — workspace authority semantics
# ---------------------------------------------------------------------------


def drive_xp_ux_001(fixture: E5Fixture) -> Mapping[str, object]:
    """Every authority class is presented distinctly, and none is invented."""
    facts = _merge(_authority(fixture), extra=fixture.runtime_facts("XP-UX-001"))
    return {
        GATE_EXPECTED_FACTS_PRESENT: facts,
        GATE_FORBIDDEN_FACTS_ABSENT: facts,
    }


# ---------------------------------------------------------------------------
# XP-UX-002 — refresh and stale reconstruction
# ---------------------------------------------------------------------------


def drive_xp_ux_002(fixture: E5Fixture) -> Mapping[str, object]:
    """A refresh reconstructs persisted truth and never resurrects stale client state."""
    facts = _merge(_reconstruction(fixture), extra=fixture.runtime_facts("XP-UX-002"))
    return {
        GATE_EXPECTED_FACTS_PRESENT: facts,
        GATE_FORBIDDEN_FACTS_ABSENT: facts,
    }


# ---------------------------------------------------------------------------
# Driver table
# ---------------------------------------------------------------------------

E5_SCENARIO_DRIVERS: Mapping[str, Callable[[E5Fixture], Mapping[str, object]]] = {
    "XP-UX-001": drive_xp_ux_001,
    "XP-UX-002": drive_xp_ux_002,
}

#: Every E5-owned scenario's DECLARED fact set, measured from the same fixture.
E5_SCENARIO_FACTS: Mapping[str, Callable[[E5Fixture], FactSetMeasurement]] = {
    "XP-UX-001": lambda f: _merge(_authority(f), extra=f.runtime_facts("XP-UX-001")),
    "XP-UX-002": lambda f: _merge(_reconstruction(f), extra=f.runtime_facts("XP-UX-002")),
}

#: The exact E5 scenario set, derived from the E1 catalog (E5 §9).
E5_SCENARIO_IDS: tuple[str, ...] = tuple(
    sorted(
        spec.scenario_id
        for family in E5_FAMILIES
        for spec in scenarios_for_family(family)
    )
)


def scenario_ids_for_family(family: GateFamily) -> tuple[str, ...]:
    """The E5-owned scenario ids in one family, derived from the E1 catalog."""
    if family not in E5_FAMILIES:
        raise KeyError(f"{family.value} is not an E5-owned gate family")
    return tuple(sorted(spec.scenario_id for spec in scenarios_for_family(family)))


def gate_coverage() -> Mapping[str, tuple[str, ...]]:
    """Every E5 scenario id mapped to the E1 gates its driver supplies."""
    return {
        scenario_id: tuple(scenario(scenario_id).required_gate_ids)
        for scenario_id in E5_SCENARIO_IDS
    }


def measured_facts(scenario_id: str, fixture: E5Fixture) -> FactSetMeasurement:
    """The scenario's declared facts, measured from the real fixture."""
    measure = E5_SCENARIO_FACTS.get(scenario_id)
    if measure is None:
        raise KeyError(f"{scenario_id!r} is not an E5-owned scenario")
    return measure(fixture)


def declared_fact_codes(scenario_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The catalog's ``(expected_facts, forbidden_facts)`` for one E5 scenario."""
    spec = scenario(scenario_id)
    return tuple(spec.expected_facts), tuple(spec.forbidden_facts)


def missing_scenarios(fixtures: Mapping[str, E5Fixture]) -> tuple[str, ...]:
    """E5-owned scenarios the caller did not supply a fixture for."""
    return tuple(sorted(set(E5_SCENARIO_IDS) - set(fixtures)))


def evaluate_e5_scenario(scenario_id: str, fixture: E5Fixture) -> CrossPlaneScenarioResult:
    """Run one E5 scenario through E1's gates. Pure; writes nothing."""
    driver = E5_SCENARIO_DRIVERS.get(scenario_id)
    if driver is None:
        raise KeyError(f"{scenario_id!r} is not an E5-owned scenario")
    spec = scenario(scenario_id)
    measurements = driver(fixture)
    for gate_id in spec.required_gate_ids:
        if gate_id not in measurements:
            raise AssertionError(
                f"{scenario_id}: driver supplied no measurement for required gate "
                f"{gate_id!r}"
            )
    return evaluate_scenario(spec, measurements)


def run_e5_suite(fixtures: Mapping[str, E5Fixture]) -> Mapping[str, GateStatus]:
    """Run the E5-owned scenarios supplied, returning scenario id → overall gate."""
    unknown = set(fixtures) - set(E5_SCENARIO_IDS)
    if unknown:
        raise KeyError(f"not E5-owned scenarios: {sorted(unknown)}")
    return {
        scenario_id: evaluate_e5_scenario(scenario_id, fixtures[scenario_id]).overall_gate
        for scenario_id in E5_SCENARIO_IDS
        if scenario_id in fixtures
    }


def run_e5_scenario_to_artifact(
    scenario_id: str, fixture: E5Fixture, *, executions_dir: str | Path
) -> tuple[CrossPlaneScenarioResult, Path]:
    """Evaluate one E5 scenario and persist its ``gate-results.json``."""
    from .cross_plane_adapter import run_scenario_to_artifact

    result = evaluate_e5_scenario(scenario_id, fixture)
    _ = build_gate_results_payload(result)  # validated before the write
    return run_scenario_to_artifact(
        scenario_id=scenario_id,
        measurements=E5_SCENARIO_DRIVERS[scenario_id](fixture),
        executions_dir=executions_dir,
    )


def e5_inventory() -> Mapping[str, object]:
    """The exact E5 scenario inventory, derived from the E1 catalog (E5 §9)."""
    families: dict[str, list[str]] = {}
    for scenario_id in E5_SCENARIO_IDS:
        family = scenario(scenario_id).gate_family.value
        families.setdefault(family, []).append(scenario_id)
    return {
        "total": len(E5_SCENARIO_IDS),
        "families": {name: sorted(ids) for name, ids in sorted(families.items())},
        "scenario_ids": list(E5_SCENARIO_IDS),
        "profiles": {
            scenario_id: scenario(scenario_id).minimum_profile.value
            for scenario_id in E5_SCENARIO_IDS
        },
        "gates_used": sorted(
            {gate for ids in gate_coverage().values() for gate in ids} & GATE_IDS
        ),
        "catalog": catalog_summary(),
        "unused_gates": sorted(
            GATE_IDS - {gate for ids in gate_coverage().values() for gate in ids}
        ),
    }


__all__ = [
    "E5_FAMILIES",
    "E5_SCENARIO_DRIVERS",
    "E5_SCENARIO_FACTS",
    "E5_SCENARIO_IDS",
    "E5Fixture",
    "RUN_FACTS",
    "declared_fact_codes",
    "drive_xp_ux_001",
    "drive_xp_ux_002",
    "e5_inventory",
    "evaluate_e5_scenario",
    "gate_coverage",
    "measured_facts",
    "missing_scenarios",
    "run_e5_scenario_to_artifact",
    "run_e5_suite",
    "scenario_ids_for_family",
]
