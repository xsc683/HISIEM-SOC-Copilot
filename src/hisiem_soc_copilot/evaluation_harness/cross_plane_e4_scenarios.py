"""E4 scenario drivers: one executable XP-01 path per E4-owned scenario.

E4 owns the 2 XP-01 scenarios in the OBSERVABILITY family (derived from the
implemented E1 catalog, never hard-coded here). Each driver turns an
:class:`E4Fixture` — built by the caller out of REAL captured telemetry and the
facts only a real runtime can establish — into the E1 typed measurements its
scenario's gates require, and the shared runner feeds those through E1's
deterministic gates into a ``cross-plane-gate-results/v1`` artifact.

```text
E4Fixture (bounded span projections, real metric label sets, real surfaces,
           persisted traceparents, real business outcomes)
        -> driver  (this module: select the measurements this scenario needs)
        -> E1 typed measurement
        -> E1 evaluate_scenario        (the ONLY decision point)
        -> cross-plane-gate-results/v1
```

The drivers contain **no gate logic and no telemetry policy**. They decide *which
facts to measure*, never what the facts mean; the E1 gates own that, and the
production observability layer owns the behaviour being measured. Where a rule is
already implemented in production the measurement calls that rule — see
``cross_plane_observability``.

Profiles: ``XP-OBS-002`` declares ``deterministic`` (the metric/safety rules are
pure functions of the label sets and surfaces). ``XP-OBS-001`` declares
``runtime-integrated``: a representative cross-plane trace and the collector-outage
invariant are facts only a real runtime run can establish, and the E4 runtime slice
supplies the same fixture fields from real processes. The drivers are identical
either way — only the fixture's provenance changes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..evaluation.cross_plane import (
    GATE_EXPECTED_FACTS_PRESENT,
    GATE_FORBIDDEN_FACTS_ABSENT,
    GATE_IDS,
    GATE_ORACLE_FIREWALL,
    GATE_SECRET_LEAK,
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
from .cross_plane_measure import oracle_firewall_of, secret_scan_of
from .cross_plane_observability import (
    SpanFact,
    collector_outage_facts,
    metric_label_safety,
    required_spans_present,
    telemetry_safety_facts,
)

#: The family E4 owns. Derived, not guessed: this is the E4 §4 ownership rule.
E4_FAMILIES: tuple[GateFamily, ...] = (GateFamily.OBSERVABILITY,)

#: XP-01 facts E4 can only learn from a real run (supplied by the fixture), as
#: opposed to the ones the drivers derive from the objects they are handed.
RUN_FACTS = frozenset(
    {
        "TELEMETRY_SPAN_PRESENT",
        "TELEMETRY_METRIC_BOUNDED",
        "TELEMETRY_OUTAGE_ISOLATED",
    }
)

#: The semantic operations a REPRESENTATIVE cross-plane lifecycle must exhibit
#: (00 §5.3, 06 §7.7). Only operations the slice actually traverses are required —
#: E4 never fabricates a span to satisfy a checklist (E4 §7).
#:
#: ``llm.call`` is deliberately NOT required here: the runtime slice runs the
#: production deterministic ``ScriptedModelProvider`` (config-selected), which is
#: not instrumented, so requiring the span would force either a fabricated trace or
#: new production telemetry added for acceptance. The real provider's ``llm.call``
#: emission is covered by the Stage B observability regression, and its telemetry
#: SAFETY is measured by this family's own span/attribute checks.
REPRESENTATIVE_OPERATIONS: tuple[str, ...] = (
    "durable.dispatch",
    "graph.invoke",
    "investigation.persist",
    "investigation.run",
    "response.observe",
    "response.submit",
    "tool.execute",
)

#: The tool/knowledge/MCP side of the livecycle: required by the focused
#: integration slice, which drives those real paths directly.
TOOL_PATH_OPERATIONS: tuple[str, ...] = (
    "knowledge.retrieve",
    "mcp.call",
    "tool.execute",
)


@dataclass(frozen=True)
class E4Fixture:
    """Real telemetry facts and bounded projections for the E4 drivers.

    Built by the caller from REAL captured spans, the REAL production metric label
    sets, the REAL sanitizer/validator answers, and the REAL business outcomes of a
    representative lifecycle. Nothing here is a mock of a telemetry decision: the
    decisions (which label sets are bounded, which traceparents are valid) are made
    by the production code the adapters call.
    """

    spans: tuple[SpanFact, ...] = ()
    required_operations: tuple[str, ...] = ()
    metric_label_sets: tuple[Mapping[str, str], ...] = ()
    surfaces: Mapping[str, str] = field(default_factory=dict)
    oracle_tokens: tuple[str, ...] = ()
    persisted_traceparents: tuple[str, ...] = ()
    logical_command_ids: tuple[str, ...] = ()
    business_outcome_with_telemetry: Mapping[str, object] = field(default_factory=dict)
    business_outcome_without_telemetry: Mapping[str, object] = field(default_factory=dict)
    observed_facts: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    representative_trace_id: str = ""

    def runtime_facts(self, scenario_id: str) -> tuple[str, ...]:
        return tuple(self.observed_facts.get(scenario_id, ()))


def _merge(*measurements: FactSetMeasurement, extra: Sequence[str] = ()) -> FactSetMeasurement:
    """One bounded fact set from several measurements plus run-supplied facts."""
    facts: list[str] = list(extra)
    for measurement in measurements:
        facts.extend(measurement.observed_facts)
    return FactSetMeasurement(
        source=MeasurementSource.TELEMETRY_FACT,
        observed_facts=dedupe_bounded_ids(facts, what="observed_facts"),
    )


def _facts_for(
    fixture: E4Fixture, scenario_id: str, *measurements: FactSetMeasurement
) -> FactSetMeasurement:
    return _merge(*measurements, extra=fixture.runtime_facts(scenario_id))


def _oracle(fixture: E4Fixture) -> object:
    return oracle_firewall_of(dict(fixture.surfaces), tokens=fixture.oracle_tokens)


def _spans(fixture: E4Fixture) -> FactSetMeasurement:
    return required_spans_present(
        spans=fixture.spans,
        required_operations=fixture.required_operations,
        trace_id=fixture.representative_trace_id,
    )


# ---------------------------------------------------------------------------
# XP-OBS-001 — representative trace correlation
# ---------------------------------------------------------------------------


def drive_xp_obs_001(fixture: E4Fixture) -> Mapping[str, object]:
    """The representative lifecycle emits its required semantic spans, and no
    evaluation-only identity reached a production telemetry surface."""
    return {
        GATE_EXPECTED_FACTS_PRESENT: _facts_for(fixture, "XP-OBS-001", _spans(fixture)),
        GATE_ORACLE_FIREWALL: _oracle(fixture),
    }


# ---------------------------------------------------------------------------
# XP-OBS-002 — metrics cardinality and telemetry safety
# ---------------------------------------------------------------------------


def drive_xp_obs_002(fixture: E4Fixture) -> Mapping[str, object]:
    """Metric labels stay low-cardinality and no sensitive content reached telemetry."""
    metric_facts = metric_label_safety(label_sets=fixture.metric_label_sets)
    safety_facts = telemetry_safety_facts(spans=fixture.spans, surfaces=fixture.surfaces)
    combined = _facts_for(fixture, "XP-OBS-002", metric_facts, safety_facts)
    return {
        GATE_EXPECTED_FACTS_PRESENT: combined,
        GATE_FORBIDDEN_FACTS_ABSENT: combined,
        GATE_SECRET_LEAK: secret_scan_of(dict(fixture.surfaces)),
    }


# ---------------------------------------------------------------------------
# Driver table
# ---------------------------------------------------------------------------

E4_SCENARIO_DRIVERS: Mapping[str, Callable[[E4Fixture], Mapping[str, object]]] = {
    "XP-OBS-001": drive_xp_obs_001,
    "XP-OBS-002": drive_xp_obs_002,
}

#: Every E4-owned scenario's DECLARED fact set, measured from the same fixture.
E4_SCENARIO_FACTS: Mapping[str, Callable[[E4Fixture], FactSetMeasurement]] = {
    "XP-OBS-001": lambda f: _facts_for(f, "XP-OBS-001", _spans(f)),
    "XP-OBS-002": lambda f: _facts_for(
        f,
        "XP-OBS-002",
        metric_label_safety(label_sets=f.metric_label_sets),
        telemetry_safety_facts(spans=f.spans, surfaces=f.surfaces),
    ),
}

#: The exact E4 scenario set, derived from the E1 catalog (E4 §4).
E4_SCENARIO_IDS: tuple[str, ...] = tuple(
    sorted(
        spec.scenario_id
        for family in E4_FAMILIES
        for spec in scenarios_for_family(family)
    )
)


def scenario_ids_for_family(family: GateFamily) -> tuple[str, ...]:
    """The E4-owned scenario ids in one family, derived from the E1 catalog."""
    if family not in E4_FAMILIES:
        raise KeyError(f"{family.value} is not an E4-owned gate family")
    return tuple(sorted(spec.scenario_id for spec in scenarios_for_family(family)))


def gate_coverage() -> Mapping[str, tuple[str, ...]]:
    """Every E4 scenario id mapped to the E1 gates its driver supplies."""
    return {
        scenario_id: tuple(scenario(scenario_id).required_gate_ids)
        for scenario_id in E4_SCENARIO_IDS
    }


def measured_facts(scenario_id: str, fixture: E4Fixture) -> FactSetMeasurement:
    """The scenario's declared facts, measured from the real fixture."""
    measure = E4_SCENARIO_FACTS.get(scenario_id)
    if measure is None:
        raise KeyError(f"{scenario_id!r} is not an E4-owned scenario")
    return measure(fixture)


def declared_fact_codes(scenario_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The catalog's ``(expected_facts, forbidden_facts)`` for one E4 scenario."""
    spec = scenario(scenario_id)
    return tuple(spec.expected_facts), tuple(spec.forbidden_facts)


def missing_scenarios(fixtures: Mapping[str, E4Fixture]) -> tuple[str, ...]:
    """E4-owned scenarios the caller did not supply a fixture for."""
    return tuple(sorted(set(E4_SCENARIO_IDS) - set(fixtures)))


def evaluate_e4_scenario(scenario_id: str, fixture: E4Fixture) -> CrossPlaneScenarioResult:
    """Run one E4 scenario through E1's gates. Pure; writes nothing."""
    driver = E4_SCENARIO_DRIVERS.get(scenario_id)
    if driver is None:
        raise KeyError(f"{scenario_id!r} is not an E4-owned scenario")
    spec = scenario(scenario_id)
    measurements = driver(fixture)
    for gate_id in spec.required_gate_ids:
        if gate_id not in measurements:
            raise AssertionError(
                f"{scenario_id}: driver supplied no measurement for required gate "
                f"{gate_id!r}"
            )
    return evaluate_scenario(spec, measurements)


def run_e4_suite(fixtures: Mapping[str, E4Fixture]) -> Mapping[str, GateStatus]:
    """Run the E4-owned scenarios supplied, returning scenario id → overall gate."""
    unknown = set(fixtures) - set(E4_SCENARIO_IDS)
    if unknown:
        raise KeyError(f"not E4-owned scenarios: {sorted(unknown)}")
    return {
        scenario_id: evaluate_e4_scenario(scenario_id, fixtures[scenario_id]).overall_gate
        for scenario_id in E4_SCENARIO_IDS
        if scenario_id in fixtures
    }


def run_e4_scenario_to_artifact(
    scenario_id: str, fixture: E4Fixture, *, executions_dir: str | Path
) -> tuple[CrossPlaneScenarioResult, Path]:
    """Evaluate one E4 scenario and persist its ``gate-results.json``."""
    from .cross_plane_adapter import run_scenario_to_artifact

    result = evaluate_e4_scenario(scenario_id, fixture)
    _ = build_gate_results_payload(result)  # validated before the write
    return run_scenario_to_artifact(
        scenario_id=scenario_id,
        measurements=E4_SCENARIO_DRIVERS[scenario_id](fixture),
        executions_dir=executions_dir,
    )


def observability_regression_gate(
    *,
    business_outcome_with_telemetry: Mapping[str, object],
    business_outcome_without_telemetry: Mapping[str, object],
) -> FactSetMeasurement:
    """The collector-outage invariant, measured for E4's own runtime slice.

    Reported through the FROZEN ``TELEMETRY_CHANGED_BUSINESS_STATE`` gate — E4 does
    not introduce a second outage gate for the same invariant (E4 §15).
    """
    return collector_outage_facts(
        business_outcome_with_telemetry=business_outcome_with_telemetry,
        business_outcome_without_telemetry=business_outcome_without_telemetry,
    )


def e4_inventory() -> Mapping[str, object]:
    """The exact E4 scenario inventory, derived from the E1 catalog (E4 §4)."""
    families: dict[str, list[str]] = {}
    for scenario_id in E4_SCENARIO_IDS:
        family = scenario(scenario_id).gate_family.value
        families.setdefault(family, []).append(scenario_id)
    return {
        "total": len(E4_SCENARIO_IDS),
        "families": {name: sorted(ids) for name, ids in sorted(families.items())},
        "scenario_ids": list(E4_SCENARIO_IDS),
        "profiles": {
            scenario_id: scenario(scenario_id).minimum_profile.value
            for scenario_id in E4_SCENARIO_IDS
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
    "E4_FAMILIES",
    "E4_SCENARIO_DRIVERS",
    "E4_SCENARIO_FACTS",
    "E4_SCENARIO_IDS",
    "E4Fixture",
    "REPRESENTATIVE_OPERATIONS",
    "RUN_FACTS",
    "TOOL_PATH_OPERATIONS",
    "declared_fact_codes",
    "drive_xp_obs_001",
    "drive_xp_obs_002",
    "e4_inventory",
    "evaluate_e4_scenario",
    "gate_coverage",
    "measured_facts",
    "missing_scenarios",
    "observability_regression_gate",
    "run_e4_scenario_to_artifact",
    "run_e4_suite",
    "scenario_ids_for_family",
]
