"""E3 scenario drivers: one executable XP-01 path per E3-owned scenario.

E3 owns the 10 XP-01 scenarios in the AUTHORITY and RELIABILITY families (derived
from the implemented E1 catalog, never hard-coded here). Each driver turns an
:class:`E3Fixture` — built by the caller out of real persisted/domain production
objects, real provider results, and the facts only a real run can establish — into
the E1 typed measurements its scenario's gates require, and the shared runner feeds
those through E1's deterministic gates into a ``cross-plane-gate-results/v1``
artifact.

```text
E3Fixture (real persisted/domain facts, real provider results, runtime facts)
        -> driver  (this module: select the measurements this scenario needs)
        -> E1 typed measurement
        -> E1 evaluate_scenario        (the ONLY decision point)
        -> cross-plane-gate-results/v1
```

The drivers contain **no gate logic and no authority policy**. They decide *which
facts to measure*, never what the facts mean; the E1 gates own that, and the
production lifecycle owns the behaviour being measured. Where a fact is produced by
a production rule (the workspace projection, the failure-to-``ToolResult`` mapping,
the "a failure is never Evidence" rule) the measurement is taken by calling that
rule — see ``cross_plane_authority``.

Profiles: eight of these ten scenarios run under the ``deterministic`` profile,
which is what their catalog entry declares. ``XP-AUTH-005`` and ``XP-REL-005``
declare ``runtime-integrated`` (real HISIEM observed execution truth; a real
telemetry backend outage) and are driven from the E3 runtime slice, which supplies
the same fixture fields from real processes. The drivers are identical either way —
only the fixture's provenance changes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..contracts.tools.types import ToolResult
from ..domain.investigation.entities import InvestigationResult
from ..domain.response.aggregate import ResponseProposal
from ..domain.response.events import ResponseEvent
from ..domain.response.value_objects import (
    ApprovalDecision,
    ApprovalRequest,
    ResponseExecutionRef,
    ResponseSubmission,
)
from ..evaluation.cross_plane import (
    GATE_EXECUTION_WITHOUT_APPROVAL,
    GATE_EXPECTED_FACTS_PRESENT,
    GATE_FORBIDDEN_FACTS_ABSENT,
    GATE_IDS,
    GATE_ORACLE_FIREWALL,
    GATE_SUBMISSION_TREATED_AS_SUCCESS,
    GATE_TELEMETRY_CHANGED_BUSINESS_STATE,
    CrossPlaneScenarioResult,
    FactSetMeasurement,
    GateFamily,
    GateStatus,
    build_gate_results_payload,
    catalog_summary,
    evaluate_scenario,
    scenario,
    scenarios_for_family,
)
from .cross_plane_authority import (
    authority_command_facts,
    authority_role_separation,
    authorized_execution_authority,
    failure_facts,
    logical_command_identities,
    observed_execution_truth,
    policy_and_approval_facts,
    retrieval_failure_facts,
    submission_presentation_facts,
    submission_truth,
    telemetry_isolation,
    telemetry_isolation_facts,
)
from .cross_plane_measure import fact_set, oracle_firewall_of

#: The families E3 owns. Derived, not guessed: this is the E3 §4 ownership rule.
E3_FAMILIES: tuple[GateFamily, ...] = (GateFamily.AUTHORITY, GateFamily.RELIABILITY)

#: XP-01 facts E3 can only learn from a real run (supplied by the fixture), as
#: opposed to the ones the drivers derive from the objects they are handed.
RUN_FACTS = frozenset(
    {
        "EXECUTION_OBSERVED_FROM_HISIEM",
        "SUBMISSION_ATTENTION_REQUIRED",
        "SUBMISSION_NOT_TREATED_AS_SUCCESS",
        "TELEMETRY_OUTAGE_ISOLATED",
        "TOOL_INVOCATION_FAILED_TYPED",
        "RETRIEVAL_UNAVAILABLE_TYPED",
    }
)


@dataclass(frozen=True)
class E3Fixture:
    """Deterministic-or-runtime collaborators for the E3 drivers.

    Built by the caller out of real production objects — a real
    ``InvestigationResult``, a real ``ResponseProposal`` aggregate, real
    ``ApprovalRequest``/``ApprovalDecision``/``ResponseSubmission``/``ResponseExecutionRef``
    records, the real ``ResponseEvent`` ledger, and real ``ToolResult`` envelopes
    produced by the production executor path — plus the facts only a real run can
    establish. Nothing here is a mock of a production *decision*: the decisions are
    made by the real code the adapters call.

    ``proposal`` is the live persisted intent. The durable command identities are
    DERIVED from the persisted event ledger and submission record rather than taken
    from a caller-supplied list, so a fixture cannot assert a command it did not
    record.
    """

    # --- AUTHORITY --------------------------------------------------------
    result: InvestigationResult | None = None
    proposal: ResponseProposal | None = None
    approval_request: ApprovalRequest | None = None
    approval_decision: ApprovalDecision | None = None
    submission: ResponseSubmission | None = None
    execution: ResponseExecutionRef | None = None
    events: tuple[ResponseEvent, ...] = ()
    execution_observed: bool = False
    hisiem_observed_status: str | None = None
    # --- RELIABILITY ------------------------------------------------------
    failed_result: ToolResult | None = None
    knowledge_backend_available: bool = True
    surfaces: Mapping[str, str] = field(default_factory=dict)
    oracle_tokens: tuple[str, ...] = ()
    business_outcome_with_telemetry: Mapping[str, object] = field(default_factory=dict)
    business_outcome_without_telemetry: Mapping[str, object] = field(default_factory=dict)
    # --- run facts only a real execution can establish --------------------
    observed_facts: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def durable_command_ids(self) -> tuple[str, ...]:
        """Distinct logical durable execution intents for this authorization."""
        if self.proposal is None:
            return ()
        return logical_command_identities(
            proposal=self.proposal,
            events=self.events,
            submission=self.submission,
        )

    def runtime_facts(self, scenario_id: str) -> tuple[str, ...]:
        return tuple(self.observed_facts.get(scenario_id, ()))


def _facts_for(
    fixture: E3Fixture, scenario_id: str, measurement: FactSetMeasurement
) -> FactSetMeasurement:
    """Merge run-supplied facts with the facts the adapter derived."""
    return fact_set(*fixture.runtime_facts(scenario_id), *measurement.observed_facts)


def _oracle(fixture: E3Fixture) -> object:
    return oracle_firewall_of(dict(fixture.surfaces), tokens=fixture.oracle_tokens)


def _require_result(fixture: E3Fixture) -> InvestigationResult:
    if fixture.result is None:
        raise AssertionError("XP-AUTH-001 requires the finalized InvestigationResult")
    return fixture.result


def _require_proposal(fixture: E3Fixture) -> ResponseProposal:
    if fixture.proposal is None:
        raise AssertionError("this scenario requires the persisted ResponseProposal")
    return fixture.proposal


def _require_tool_result(fixture: E3Fixture) -> ToolResult:
    if fixture.failed_result is None:
        raise AssertionError("this reliability scenario requires a real ToolResult")
    return fixture.failed_result


# ---------------------------------------------------------------------------
# AUTHORITY
# ---------------------------------------------------------------------------


def drive_xp_auth_001(fixture: E3Fixture) -> Mapping[str, object]:
    """The agent's verdict and the analyst's decision stay separate authority facts."""
    facts = _facts_for(
        fixture,
        "XP-AUTH-001",
        authority_role_separation(
            result=_require_result(fixture),
            request=fixture.approval_request,
            decision=fixture.approval_decision,
        ),
    )
    return {
        GATE_EXPECTED_FACTS_PRESENT: facts,
        GATE_FORBIDDEN_FACTS_ABSENT: facts,
    }


def drive_xp_auth_002(fixture: E3Fixture) -> Mapping[str, object]:
    """A policy decision is recorded as a decision; only a human decision authorizes."""
    facts = _facts_for(
        fixture,
        "XP-AUTH-002",
        policy_and_approval_facts(
            proposal=_require_proposal(fixture),
            events=fixture.events,
            request=fixture.approval_request,
            decision=fixture.approval_decision,
        ),
    )
    return {
        GATE_EXPECTED_FACTS_PRESENT: facts,
        GATE_FORBIDDEN_FACTS_ABSENT: facts,
    }


def drive_xp_auth_003(fixture: E3Fixture) -> Mapping[str, object]:
    """No durable command — and no observed execution — implies a recorded approval."""
    proposal = _require_proposal(fixture)
    return {
        GATE_EXECUTION_WITHOUT_APPROVAL: authorized_execution_authority(
            proposal=proposal,
            request=fixture.approval_request,
            decision=fixture.approval_decision,
            durable_command_ids=fixture.durable_command_ids,
            execution_observed=fixture.execution_observed,
            provider_execution_ref=(
                fixture.execution.execution_id
                if fixture.execution is not None and fixture.execution.execution_id
                else None
            ),
        )
    }


def drive_xp_auth_004(fixture: E3Fixture) -> Mapping[str, object]:
    """A local submission is never presented as an execution success."""
    return {
        GATE_SUBMISSION_TREATED_AS_SUCCESS: submission_truth(
            submission=fixture.submission, execution=fixture.execution
        )
    }


def drive_xp_auth_005(fixture: E3Fixture) -> Mapping[str, object]:
    """HISIEM's observed execution state is the final execution truth."""
    truths = submission_truth(submission=fixture.submission, execution=fixture.execution)
    facts = _facts_for(
        fixture,
        "XP-AUTH-005",
        observed_execution_truth(
            execution=fixture.execution,
            hisiem_observed_status=fixture.hisiem_observed_status,
        ),
    )
    return {
        GATE_SUBMISSION_TREATED_AS_SUCCESS: truths,
        GATE_EXPECTED_FACTS_PRESENT: facts,
    }


# ---------------------------------------------------------------------------
# RELIABILITY
# ---------------------------------------------------------------------------


def drive_xp_rel_001(fixture: E3Fixture) -> Mapping[str, object]:
    """A tool timeout is a TYPED failure, and it never becomes Evidence."""
    facts = _facts_for(
        fixture,
        "XP-REL-001",
        failure_facts(result=_require_tool_result(fixture), backend_available=True),
    )
    return {
        GATE_EXPECTED_FACTS_PRESENT: facts,
        GATE_FORBIDDEN_FACTS_ABSENT: facts,
    }


def drive_xp_rel_002(fixture: E3Fixture) -> Mapping[str, object]:
    """An unreachable capability fails closed: typed failure, no Evidence, no leak."""
    facts = _facts_for(
        fixture,
        "XP-REL-002",
        failure_facts(result=_require_tool_result(fixture), backend_available=False),
    )
    return {
        GATE_FORBIDDEN_FACTS_ABSENT: facts,
        GATE_ORACLE_FIREWALL: _oracle(fixture),
    }


def drive_xp_rel_003(fixture: E3Fixture) -> Mapping[str, object]:
    """A retrieval outage is typed — never an empty result set, never Evidence."""
    facts = _facts_for(
        fixture,
        "XP-REL-003",
        retrieval_failure_facts(
            result=_require_tool_result(fixture),
            knowledge_backend_available=fixture.knowledge_backend_available,
        ),
    )
    return {
        GATE_EXPECTED_FACTS_PRESENT: facts,
        GATE_FORBIDDEN_FACTS_ABSENT: facts,
    }


def drive_xp_rel_004(fixture: E3Fixture) -> Mapping[str, object]:
    """An exhausted uncertain submission stays explicit uncertainty."""
    facts = _facts_for(
        fixture,
        "XP-REL-004",
        submission_presentation_facts(
            proposal=_require_proposal(fixture),
            request=fixture.approval_request,
            decision=fixture.approval_decision,
            submission=fixture.submission,
            execution=fixture.execution,
        ),
    )
    return {
        GATE_SUBMISSION_TREATED_AS_SUCCESS: submission_truth(
            submission=fixture.submission, execution=fixture.execution
        ),
        GATE_EXPECTED_FACTS_PRESENT: facts,
    }


def drive_xp_rel_005(fixture: E3Fixture) -> Mapping[str, object]:
    """A telemetry backend outage cannot change the persisted business outcome."""
    return {
        GATE_TELEMETRY_CHANGED_BUSINESS_STATE: telemetry_isolation(
            business_outcome_with_telemetry=fixture.business_outcome_with_telemetry,
            business_outcome_without_telemetry=fixture.business_outcome_without_telemetry,
        )
    }


# ---------------------------------------------------------------------------
# Driver table
# ---------------------------------------------------------------------------

E3_SCENARIO_DRIVERS: Mapping[str, Callable[[E3Fixture], Mapping[str, object]]] = {
    "XP-AUTH-001": drive_xp_auth_001,
    "XP-AUTH-002": drive_xp_auth_002,
    "XP-AUTH-003": drive_xp_auth_003,
    "XP-AUTH-004": drive_xp_auth_004,
    "XP-AUTH-005": drive_xp_auth_005,
    "XP-REL-001": drive_xp_rel_001,
    "XP-REL-002": drive_xp_rel_002,
    "XP-REL-003": drive_xp_rel_003,
    "XP-REL-004": drive_xp_rel_004,
    "XP-REL-005": drive_xp_rel_005,
}

#: Every E3-owned scenario's DECLARED fact set, measured from the same fixture.
#: A scenario whose gates do not consume its expected/forbidden facts still has them
#: measured here, so no declared token is left unexecuted.
E3_SCENARIO_FACTS: Mapping[str, Callable[[E3Fixture], FactSetMeasurement]] = {
    "XP-AUTH-001": lambda f: _facts_for(
        f,
        "XP-AUTH-001",
        authority_role_separation(
            result=_require_result(f), request=f.approval_request, decision=f.approval_decision
        ),
    ),
    "XP-AUTH-002": lambda f: _facts_for(
        f,
        "XP-AUTH-002",
        policy_and_approval_facts(
            proposal=_require_proposal(f),
            events=f.events,
            request=f.approval_request,
            decision=f.approval_decision,
        ),
    ),
    "XP-AUTH-003": lambda f: _facts_for(
        f,
        "XP-AUTH-003",
        authority_command_facts(
            decision=f.approval_decision,
            durable_command_ids=f.durable_command_ids,
            execution_observed=f.execution_observed,
        ),
    ),
    "XP-AUTH-004": lambda f: _facts_for(
        f,
        "XP-AUTH-004",
        submission_presentation_facts(
            proposal=_require_proposal(f),
            request=f.approval_request,
            decision=f.approval_decision,
            submission=f.submission,
            execution=f.execution,
        ),
    ),
    "XP-AUTH-005": lambda f: _facts_for(
        f,
        "XP-AUTH-005",
        observed_execution_truth(
            execution=f.execution, hisiem_observed_status=f.hisiem_observed_status
        ),
    ),
    "XP-REL-001": lambda f: _facts_for(
        f,
        "XP-REL-001",
        failure_facts(result=_require_tool_result(f), backend_available=True),
    ),
    "XP-REL-002": lambda f: _facts_for(
        f,
        "XP-REL-002",
        failure_facts(result=_require_tool_result(f), backend_available=False),
    ),
    "XP-REL-003": lambda f: _facts_for(
        f,
        "XP-REL-003",
        retrieval_failure_facts(
            result=_require_tool_result(f),
            knowledge_backend_available=f.knowledge_backend_available,
        ),
    ),
    "XP-REL-004": lambda f: _facts_for(
        f,
        "XP-REL-004",
        submission_presentation_facts(
            proposal=_require_proposal(f),
            request=f.approval_request,
            decision=f.approval_decision,
            submission=f.submission,
            execution=f.execution,
        ),
    ),
    "XP-REL-005": lambda f: _facts_for(
        f,
        "XP-REL-005",
        telemetry_isolation_facts(
            business_outcome_with_telemetry=f.business_outcome_with_telemetry,
            business_outcome_without_telemetry=f.business_outcome_without_telemetry,
        ),
    ),
}

#: The exact E3 scenario set, derived from the E1 catalog (E3 §4).
E3_SCENARIO_IDS: tuple[str, ...] = tuple(
    sorted(
        spec.scenario_id
        for family in E3_FAMILIES
        for spec in scenarios_for_family(family)
    )
)


def scenario_ids_for_family(family: GateFamily) -> tuple[str, ...]:
    """The E3-owned scenario ids in one family, derived from the E1 catalog."""
    if family not in E3_FAMILIES:
        raise KeyError(f"{family.value} is not an E3-owned gate family")
    return tuple(sorted(spec.scenario_id for spec in scenarios_for_family(family)))


def gate_coverage() -> Mapping[str, tuple[str, ...]]:
    """Every E3 scenario id mapped to the E1 gates its driver supplies."""
    return {
        scenario_id: tuple(scenario(scenario_id).required_gate_ids)
        for scenario_id in E3_SCENARIO_IDS
    }


def measured_facts(scenario_id: str, fixture: E3Fixture) -> FactSetMeasurement:
    """The scenario's declared facts, measured from the real fixture."""
    measure = E3_SCENARIO_FACTS.get(scenario_id)
    if measure is None:
        raise KeyError(f"{scenario_id!r} is not an E3-owned scenario")
    return measure(fixture)


def evaluate_e3_scenario(scenario_id: str, fixture: E3Fixture) -> CrossPlaneScenarioResult:
    """Run one E3 scenario through E1's gates. Pure; writes nothing."""
    driver = E3_SCENARIO_DRIVERS.get(scenario_id)
    if driver is None:
        raise KeyError(f"{scenario_id!r} is not an E3-owned scenario")
    spec = scenario(scenario_id)
    measurements = driver(fixture)
    for gate_id in spec.required_gate_ids:
        if gate_id not in measurements:
            raise AssertionError(
                f"{scenario_id}: driver supplied no measurement for required gate "
                f"{gate_id!r}"
            )
    return evaluate_scenario(spec, measurements)


def missing_scenarios(fixtures: Mapping[str, E3Fixture]) -> tuple[str, ...]:
    """E3-owned scenarios the caller did not supply a fixture for.

    Reported explicitly rather than skipped silently: a caller that intends full
    coverage asserts this is empty, and a caller running one profile asserts exactly
    which scenarios the other profile owns.
    """
    return tuple(sorted(set(E3_SCENARIO_IDS) - set(fixtures)))


def run_e3_suite(fixtures: Mapping[str, E3Fixture]) -> Mapping[str, GateStatus]:
    """Run the E3-owned scenarios supplied, returning scenario id → overall gate.

    Each scenario is evaluated against its OWN fixture: an authority scenario needs
    the lifecycle state it measures (approved, rejected, policy-denied, submitted,
    exhausted), and those states are mutually exclusive for one proposal. An unknown
    scenario id is refused; a scenario the caller omitted is reported by
    :func:`missing_scenarios`, never silently treated as passing.
    """
    unknown = set(fixtures) - set(E3_SCENARIO_IDS)
    if unknown:
        raise KeyError(f"not E3-owned scenarios: {sorted(unknown)}")
    return {
        scenario_id: evaluate_e3_scenario(scenario_id, fixtures[scenario_id]).overall_gate
        for scenario_id in E3_SCENARIO_IDS
        if scenario_id in fixtures
    }


def run_e3_scenario_to_artifact(
    scenario_id: str, fixture: E3Fixture, *, executions_dir: str | Path
) -> tuple[CrossPlaneScenarioResult, Path]:
    """Evaluate one E3 scenario and persist its ``gate-results.json``."""
    from .cross_plane_adapter import run_scenario_to_artifact

    result = evaluate_e3_scenario(scenario_id, fixture)
    _ = build_gate_results_payload(result)  # validated before the write
    return run_scenario_to_artifact(
        scenario_id=scenario_id,
        measurements=E3_SCENARIO_DRIVERS[scenario_id](fixture),
        executions_dir=executions_dir,
    )


def declared_fact_codes(scenario_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The catalog's ``(expected_facts, forbidden_facts)`` for one E3 scenario."""
    spec = scenario(scenario_id)
    return tuple(spec.expected_facts), tuple(spec.forbidden_facts)


def e3_inventory() -> Mapping[str, object]:
    """The exact E3 scenario inventory, derived from the E1 catalog (E3 §4)."""
    families: dict[str, list[str]] = {}
    for scenario_id in E3_SCENARIO_IDS:
        family = scenario(scenario_id).gate_family.value
        families.setdefault(family, []).append(scenario_id)
    return {
        "total": len(E3_SCENARIO_IDS),
        "families": {name: sorted(ids) for name, ids in sorted(families.items())},
        "scenario_ids": list(E3_SCENARIO_IDS),
        "profiles": {
            scenario_id: scenario(scenario_id).minimum_profile.value
            for scenario_id in E3_SCENARIO_IDS
        },
        "gates_used": sorted(
            {gate for ids in gate_coverage().values() for gate in ids} & GATE_IDS
        ),
        "catalog": catalog_summary(),
        "unused_gates": sorted(
            GATE_IDS - {gate for ids in gate_coverage().values() for gate in ids}
        ),
    }


def e3_scenario_ids_for_report() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(authority_ids, reliability_ids)`` as implemented — the report's source."""
    return (
        scenario_ids_for_family(GateFamily.AUTHORITY),
        scenario_ids_for_family(GateFamily.RELIABILITY),
    )


__all__ = [
    "E3_FAMILIES",
    "E3_SCENARIO_DRIVERS",
    "E3_SCENARIO_FACTS",
    "E3_SCENARIO_IDS",
    "E3Fixture",
    "RUN_FACTS",
    "declared_fact_codes",
    "drive_xp_auth_001",
    "drive_xp_auth_002",
    "drive_xp_auth_003",
    "drive_xp_auth_004",
    "drive_xp_auth_005",
    "drive_xp_rel_001",
    "drive_xp_rel_002",
    "drive_xp_rel_003",
    "drive_xp_rel_004",
    "drive_xp_rel_005",
    "e3_inventory",
    "e3_scenario_ids_for_report",
    "evaluate_e3_scenario",
    "gate_coverage",
    "measured_facts",
    "missing_scenarios",
    "run_e3_scenario_to_artifact",
    "run_e3_suite",
    "scenario_ids_for_family",
]
