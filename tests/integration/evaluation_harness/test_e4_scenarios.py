"""E4 end-to-end scenario execution (Stage E / E4 §6, §22, §23, §29).

Every E4-owned XP-01 scenario is driven end to end:

```text
real captured telemetry + real production label sets + real surfaces
        -> E4 measurement adapter (cross_plane_observability)
        -> E1 typed measurement
        -> E1 deterministic hard gate (evaluate_scenario)
        -> cross-plane-gate-results/v1
```

The focused integration here runs the REAL observability pipeline (the production
``setup_telemetry`` bootstrap, ``start_span``, ``linked_worker_span``, the real
metric recorder and sanitizer) and captures what it emits in-process — the only
substitution is the export destination, exactly as the Stage B bootstrap tests do.

``XP-OBS-002`` declares the ``deterministic`` profile and is executed here.
``XP-OBS-001`` declares ``runtime-integrated``: its authoritative artifact comes
from the runtime slice against a real collector and a real Copilot worker, and the
driver checks in this module use the focused span emissions only to prove the
driver supplies every required gate.

The negative half (§22/§23) proves each safety/cardinality invariant can FAIL, so a
green suite is not a vacuous one.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
import pytest_asyncio

from hisiem_soc_copilot.agent.tools.provider_router import ProviderRouter
from hisiem_soc_copilot.contracts.tools.types import ToolCandidate
from hisiem_soc_copilot.evaluation.cross_plane import (
    GATE_EXPECTED_FACTS_PRESENT,
    GATE_FORBIDDEN_FACTS_ABSENT,
    GATE_IDS,
    GATE_ORACLE_FIREWALL,
    GATE_SECRET_LEAK,
    GateFamily,
    GateStatus,
    evaluate_gate,
    scenario,
)
from hisiem_soc_copilot.evaluation_harness import read_gate_results
from hisiem_soc_copilot.evaluation_harness.cross_plane_adapter import evaluate_and_build
from hisiem_soc_copilot.evaluation_harness.cross_plane_e4_scenarios import (
    E4_SCENARIO_DRIVERS,
    E4_SCENARIO_IDS,
    REPRESENTATIVE_OPERATIONS,
    TOOL_PATH_OPERATIONS,
    E4Fixture,
    declared_fact_codes,
    e4_inventory,
    evaluate_e4_scenario,
    measured_facts,
    missing_scenarios,
    run_e4_scenario_to_artifact,
    run_e4_suite,
    scenario_ids_for_family,
)
from hisiem_soc_copilot.evaluation_harness.cross_plane_measure import (
    fact_set,
    oracle_firewall_of,
    registry_for,
    secret_scan_of,
)
from hisiem_soc_copilot.evaluation_harness.cross_plane_observability import (
    SpanFact,
    metric_label_safety,
    required_spans_present,
    telemetry_safety_facts,
)
from hisiem_soc_copilot.infrastructure.observability.tools import ObservedToolExecutor
from tests.fixtures.hisiem_fake import FakeHisiem
from tests.support.e4_observability_fixture import (
    metric_label_capture,
    project_spans,
    span_capture,
    telemetry_surfaces,
)
from tests.support.mcp_scenario_fixture import deterministic_mcp_server

TENANT = "tenant-a"
ALERT = "e4-alert-1"
_SCENARIO_IDS = E4_SCENARIO_IDS
_RUNTIME_IDS = tuple(
    sid for sid in _SCENARIO_IDS if scenario(sid).minimum_profile.value == "runtime-integrated"
)
_DETERMINISTIC_IDS = tuple(sid for sid in _SCENARIO_IDS if sid not in _RUNTIME_IDS)


async def _mcp_lookup(query: str) -> dict[str, str]:
    return {"query": query, "hit": "bounded"}


async def _run_real_tool_paths() -> None:
    """Drive the REAL tool/knowledge/MCP paths while the real pipeline is installed."""
    hisiem = FakeHisiem(alert_id=ALERT)
    context = {
        "tenant_id": TENANT,
        "source_alert_ref": {"provider": "hisiem", "resource_type": "alert", "address_id": ALERT},
    }

    async with deterministic_mcp_server({"e2e_lookup": _mcp_lookup}) as server:
        admission = await server.admit("mcp.e2e_lookup", "e2e_lookup")
        provider = server.provider([admission])
        await provider.start()
        try:
            executor = ObservedToolExecutor(
                hisiem=hisiem,  # type: ignore[arg-type]
                knowledge=None,
                provider_router=ProviderRouter([(admission, provider)]),
                registry=registry_for([admission]),
            )
            # Native path → tool.execute
            await executor.execute(
                candidate=ToolCandidate(
                    tool_name="hisiem.search_events",
                    arguments={
                        "from": "2026-09-01T09:55:00Z",
                        "to": "2026-09-01T10:05:00Z",
                        "conditions": [
                            {"field": "event.action", "operator": "is", "value": "auth_failure"}
                        ],
                    },
                ),
                tool_call_id="e4-native-1",
                **context,
            )
            # MCP path → mcp.call (nested inside tool.execute)
            await executor.execute(
                candidate=ToolCandidate(
                    tool_name="mcp.e2e_lookup", arguments={"query": "bounded"}
                ),
                tool_call_id="e4-mcp-1",
                **context,
            )
            # Knowledge path → knowledge.retrieve (no catalog wired: a typed
            # unavailable result, which is still a real emission).
            await executor.execute(
                candidate=ToolCandidate(
                    tool_name="knowledge.retrieve_security_guidance",
                    arguments={"topic": "T1110 brute force", "context_terms": ["sshd"]},
                ),
                tool_call_id="e4-knowledge-1",
                **context,
            )
        finally:
            await provider.close()


@pytest_asyncio.fixture
async def focused_telemetry(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """The REAL pipeline, captured in-process; returns bounded projections."""
    with span_capture(monkeypatch) as exporter, metric_label_capture(monkeypatch) as labels:
        await _run_real_tool_paths()
        spans = project_spans(exporter.get_finished_spans())
    return {
        "spans": spans,
        "metric_label_sets": tuple(labels),
    }


def _focused_fixture(captured: Mapping[str, object]) -> E4Fixture:
    spans = tuple(captured["spans"])  # type: ignore[arg-type]
    labels = tuple(captured["metric_label_sets"])  # type: ignore[arg-type]
    return E4Fixture(
        spans=spans,
        required_operations=TOOL_PATH_OPERATIONS,
        metric_label_sets=labels,
        surfaces=telemetry_surfaces(spans=spans, metric_label_sets=labels),
        oracle_tokens=("XP-OBS-001", "XP-OBS-002", "GATE_SECRET_LEAK"),
    )


# ---------------------------------------------------------------------------
# Inventory / executable-path coverage (E4 §4)
# ---------------------------------------------------------------------------


def test_e4_owns_exactly_the_observability_family() -> None:
    inventory = e4_inventory()
    assert inventory["families"] == {
        "OBSERVABILITY": list(scenario_ids_for_family(GateFamily.OBSERVABILITY))
    }
    assert inventory["total"] == len(_SCENARIO_IDS) == 2
    assert scenario_ids_for_family(GateFamily.OBSERVABILITY) == ("XP-OBS-001", "XP-OBS-002")
    assert not any(
        scenario(sid).gate_family is GateFamily.WORKSPACE for sid in _SCENARIO_IDS
    )


def test_no_e4_scenario_requires_a_gate_outside_the_frozen_catalog() -> None:
    for scenario_id in _SCENARIO_IDS:
        assert set(scenario(scenario_id).required_gate_ids) <= GATE_IDS


def test_the_e4_profiles_match_the_catalog() -> None:
    assert scenario("XP-OBS-001").minimum_profile.value == "runtime-integrated"
    assert scenario("XP-OBS-002").minimum_profile.value == "deterministic"
    assert missing_scenarios({}) == ("XP-OBS-001", "XP-OBS-002")


# ---------------------------------------------------------------------------
# Focused integration — the real pipeline emits the tool-path spans
# ---------------------------------------------------------------------------


def test_real_pipeline_emits_the_tool_path_spans(focused_telemetry: Mapping[str, object]) -> None:
    spans = tuple(focused_telemetry["spans"])  # type: ignore[arg-type]
    operations = {span.operation for span in spans}
    assert {"tool.execute", "mcp.call", "knowledge.retrieve"} <= operations
    measurement = required_spans_present(
        spans=spans, required_operations=TOOL_PATH_OPERATIONS
    )
    assert measurement.observed_facts == ("TELEMETRY_SPAN_PRESENT",)


def test_the_mcp_span_nests_inside_tool_execute(focused_telemetry: Mapping[str, object]) -> None:
    spans = tuple(focused_telemetry["spans"])  # type: ignore[arg-type]
    mcp = [span for span in spans if span.operation == "mcp.call"]
    tool = [span for span in spans if span.operation == "tool.execute"]
    assert mcp and tool
    # Both live in the SAME trace: the MCP call is a child of the tool execution.
    assert {span.trace_id for span in mcp} <= {span.trace_id for span in tool}
    assert all(span.parent_present for span in mcp)


def test_the_emitted_spans_carry_only_bounded_attribute_keys(
    focused_telemetry: Mapping[str, object],
) -> None:
    spans = tuple(focused_telemetry["spans"])  # type: ignore[arg-type]
    tool = next(span for span in spans if span.operation == "tool.execute")
    # The real emission surface: capability identity + provider class + outcome
    # category. Nothing else — no argument, no payload, no result body.
    assert set(tool.attribute_keys) <= {
        "tool_name",
        "tool_provider",
        "server_category",
        "result",
    }
    assert tool.sensitive_attribute_keys() == ()
    assert telemetry_safety_facts(spans=spans, surfaces={}).observed_facts == ()


def test_real_production_metric_labels_are_bounded(
    focused_telemetry: Mapping[str, object],
) -> None:
    labels = tuple(focused_telemetry["metric_label_sets"])  # type: ignore[arg-type]
    assert labels, "the real path recorded no metric label sets"
    measurement = metric_label_safety(label_sets=labels)
    assert measurement.observed_facts == ("TELEMETRY_METRIC_BOUNDED",)
    for label_set in labels:
        assert not (
            {"investigation_id", "tenant_id", "trace_id", "span_id"} & set(label_set)
        )


def test_the_tool_path_scenario_passes_on_real_spans(
    focused_telemetry: Mapping[str, object],
) -> None:
    """The XP-OBS-001 driver supplies every required gate (driver-level check)."""
    fixture = _focused_fixture(focused_telemetry)
    result = evaluate_e4_scenario("XP-OBS-001", fixture)
    assert result.overall_gate is GateStatus.PASS


def test_the_safety_scenario_passes_on_real_telemetry(
    focused_telemetry: Mapping[str, object],
) -> None:
    fixture = _focused_fixture(focused_telemetry)
    result = evaluate_e4_scenario("XP-OBS-002", fixture)
    assert result.overall_gate is GateStatus.PASS


def test_every_declared_fact_of_every_e4_scenario_is_measured() -> None:
    fixture = _deterministic_fixture()
    for scenario_id in _SCENARIO_IDS:
        expected, forbidden = declared_fact_codes(scenario_id)
        observed = set(measured_facts(scenario_id, fixture).observed_facts)
        assert set(expected) <= observed, f"{scenario_id}: missing {set(expected) - observed}"
        assert not (set(forbidden) & observed), (
            f"{scenario_id}: forbidden facts observed {set(forbidden) & observed}"
        )


# ---------------------------------------------------------------------------
# Deterministic scenario execution + artifacts
# ---------------------------------------------------------------------------


def _deterministic_fixture() -> E4Fixture:
    """A fully deterministic fixture: real label sets, real surfaces, real spans."""
    spans = (
        SpanFact(operation="tool.execute", attribute_keys=("tool_name", "tool_provider")),
        SpanFact(operation="mcp.call", parent_present=True, attribute_keys=("tool_name",)),
        SpanFact(operation="knowledge.retrieve", attribute_keys=("tool_name",)),
    )
    label_sets = (
        {"tool_name": "hisiem.search_events", "tool_provider": "native"},
        {"operation": "response.submit"},
        {"response_state": "ATTENTION_REQUIRED"},
        {"result": "error", "error_category": "TIMEOUT"},
        {"retrieval_mode": "LEXICAL_ONLY"},
    )
    return E4Fixture(
        spans=spans,
        required_operations=TOOL_PATH_OPERATIONS,
        metric_label_sets=label_sets,
        surfaces=telemetry_surfaces(spans=spans, metric_label_sets=label_sets),
        oracle_tokens=("XP-OBS-001", "XP-OBS-002"),
    )


def test_the_deterministic_e4_scenario_passes() -> None:
    fixture = _deterministic_fixture()
    results = run_e4_suite({"XP-OBS-002": fixture})
    assert results["XP-OBS-002"] is GateStatus.PASS
    assert missing_scenarios({"XP-OBS-002": fixture}) == ("XP-OBS-001",)


def test_the_deterministic_scenario_persists_and_reloads_an_artifact(tmp_path: Path) -> None:
    fixture = _deterministic_fixture()
    result, path = run_e4_scenario_to_artifact(
        "XP-OBS-002", fixture, executions_dir=tmp_path
    )
    assert result.overall_gate is GateStatus.PASS
    restored = read_gate_results(path)
    assert restored.scenario_id == "XP-OBS-002"
    assert restored.pack_id == "XP-01"
    assert restored.gate_family is GateFamily.OBSERVABILITY
    assert "xp-01" in path.as_posix()


def test_e4_artifacts_are_deterministic(tmp_path: Path) -> None:
    fixture = _deterministic_fixture()
    _r1, first = run_e4_scenario_to_artifact(
        "XP-OBS-002", fixture, executions_dir=tmp_path / "a"
    )
    _r2, second = run_e4_scenario_to_artifact(
        "XP-OBS-002", fixture, executions_dir=tmp_path / "b"
    )
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# §22 falsifiability — direct invalid measurements
# ---------------------------------------------------------------------------


def _status(gate_id: str, measurement: object, scenario_id: str) -> GateStatus:
    return evaluate_gate(gate_id, measurement, scenario=scenario(scenario_id)).status


def test_a_required_semantic_span_absent_fails_the_gate() -> None:
    measurement = required_spans_present(
        spans=(SpanFact(operation="graph.invoke"),),
        required_operations=REPRESENTATIVE_OPERATIONS,
    )
    assert _status(GATE_EXPECTED_FACTS_PRESENT, measurement, "XP-OBS-001") is GateStatus.FAIL


def test_a_secret_in_span_attributes_fails_the_gate() -> None:
    spans = (SpanFact(operation="llm.call", attribute_keys=("authorization",)),)
    facts = telemetry_safety_facts(spans=spans, surfaces={})
    assert _status(GATE_FORBIDDEN_FACTS_ABSENT, facts, "XP-OBS-002") is GateStatus.FAIL


def test_a_raw_tool_result_projected_into_telemetry_fails_the_gate() -> None:
    spans = (SpanFact(operation="tool.execute", attribute_keys=("tool_result",)),)
    facts = telemetry_safety_facts(spans=spans, surfaces={})
    assert _status(GATE_FORBIDDEN_FACTS_ABSENT, facts, "XP-OBS-002") is GateStatus.FAIL


def test_a_secret_marker_on_a_surface_fails_the_secret_gate() -> None:
    surfaces = {"span:http": "Authorization: Bearer synthetic-sentinel"}
    measurement = secret_scan_of(surfaces)
    assert _status(GATE_SECRET_LEAK, measurement, "XP-OBS-002") is GateStatus.FAIL


def test_a_high_cardinality_metric_label_fails_the_gate() -> None:
    measurement = metric_label_safety(label_sets=[{"investigation_id": "inv-1"}])
    assert _status(GATE_FORBIDDEN_FACTS_ABSENT, measurement, "XP-OBS-002") is GateStatus.FAIL


def test_an_oracle_token_on_a_production_surface_fails_the_gate() -> None:
    surfaces = {"span:graph.invoke": "XP-OBS-001 expected fact"}
    measurement = oracle_firewall_of(surfaces, tokens=("XP-OBS-001",))
    assert _status(GATE_ORACLE_FIREWALL, measurement, "XP-OBS-001") is GateStatus.FAIL


def test_a_forbidden_fact_always_fails_the_forbidden_gate() -> None:
    for scenario_id in _SCENARIO_IDS:
        _expected, forbidden = declared_fact_codes(scenario_id)
        if not forbidden:
            continue
        assert (
            _status(GATE_FORBIDDEN_FACTS_ABSENT, fact_set(*forbidden), scenario_id)
            is GateStatus.FAIL
        ), scenario_id


def test_a_missing_expected_fact_always_fails_the_expected_gate() -> None:
    for scenario_id in _SCENARIO_IDS:
        expected, _forbidden = declared_fact_codes(scenario_id)
        if not expected:
            continue
        assert (
            _status(GATE_EXPECTED_FACTS_PRESENT, fact_set("SOMETHING_ELSE"), scenario_id)
            is GateStatus.FAIL
        ), scenario_id


# ---------------------------------------------------------------------------
# §23 positive + negative pairs, through the real drivers
# ---------------------------------------------------------------------------


def test_required_trace_operations_present_pass_and_absent_fail() -> None:
    fixture = _deterministic_fixture()
    assert evaluate_e4_scenario("XP-OBS-001", fixture).overall_gate is GateStatus.PASS
    degraded = E4Fixture(
        spans=tuple(
            span for span in fixture.spans if span.operation != "knowledge.retrieve"
        ),
        required_operations=fixture.required_operations,
        metric_label_sets=fixture.metric_label_sets,
        surfaces=fixture.surfaces,
    )
    assert evaluate_e4_scenario("XP-OBS-001", degraded).overall_gate is GateStatus.FAIL


def test_safe_attributes_pass_and_a_synthetic_secret_fails() -> None:
    safe = _deterministic_fixture()
    assert evaluate_e4_scenario("XP-OBS-002", safe).overall_gate is GateStatus.PASS
    leaked = E4Fixture(
        spans=(SpanFact(operation="llm.call", attribute_keys=("raw_prompt",)),),
        metric_label_sets=safe.metric_label_sets,
        surfaces={"span:llm.call": "raw_prompt"},
    )
    result = evaluate_e4_scenario("XP-OBS-002", leaked)
    assert result.overall_gate is GateStatus.FAIL
    assert GATE_FORBIDDEN_FACTS_ABSENT in result.gate_failures


def test_low_cardinality_labels_pass_and_an_identity_label_fails() -> None:
    fixture = _deterministic_fixture()
    assert evaluate_e4_scenario("XP-OBS-002", fixture).overall_gate is GateStatus.PASS
    leaked = E4Fixture(
        spans=fixture.spans,
        metric_label_sets=(
            {"tool_name": "hisiem.search_events"},
            {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"},
        ),
        surfaces=fixture.surfaces,
    )
    result = evaluate_e4_scenario("XP-OBS-002", leaked)
    assert result.overall_gate is GateStatus.FAIL


def test_no_e4_artifact_embeds_raw_telemetry() -> None:
    """The bounded projection survives all the way into the artifact."""
    fixture = _deterministic_fixture()
    _result, payload = evaluate_and_build(
        "XP-OBS-002", E4_SCENARIO_DRIVERS["XP-OBS-002"](fixture)
    )
    text = repr(payload)
    assert "attribute_keys" not in text
    assert "raw_prompt" not in text
    assert "Bearer " not in text
