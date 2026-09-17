"""E4 observability adapter tests (Stage E / E4 §20, §22, §29 Layer 1).

The adapters turn real telemetry facts into E1 measurements. These tests prove they
report the facts the OBSERVABILITY gates need — and that safety/cardinality rules
are produced by the REAL production rule (``sanitize_metric_attributes``,
``validate_traceparent``, the frozen secret scan) rather than by a second
implementation of it in the evaluation plane.
"""

from __future__ import annotations

import pytest

from hisiem_soc_copilot.evaluation.cross_plane import GateStatus, evaluate_gate, scenario
from hisiem_soc_copilot.evaluation_harness.cross_plane_observability import (
    FORBIDDEN_METRIC_LABEL_KEYS,
    MetricLabelOutcome,
    SpanFact,
    business_outcome_equivalent,
    evaluate_metric_labels,
    metric_label_safety,
    missing_required_operations,
    required_spans_present,
    span_projection_safe,
    telemetry_safety_facts,
    telemetry_secret_scan,
    telemetry_surface_violations,
    trace_continuation_facts,
)

_VALID_TRACEPARENT = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"


def _span(operation: str, **kwargs: object) -> SpanFact:
    return SpanFact(operation=operation, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Bounded span projection
# ---------------------------------------------------------------------------


def test_span_fact_carries_keys_but_never_values() -> None:
    """The projection is the E4 §21 boundary: names and keys, no payload."""
    span = _span("tool.execute", attribute_keys=("tool_name", "tool_provider"))
    assert span.attribute_keys == ("tool_name", "tool_provider")
    assert not hasattr(span, "attributes")
    assert not hasattr(span, "events")


def test_a_root_span_has_no_parent() -> None:
    assert _span("investigation.run").is_root is True
    assert _span("graph.invoke", parent_present=True).is_root is False


def test_sensitive_attribute_keys_are_detected_case_insensitively() -> None:
    span = _span("llm.call", attribute_keys=("Raw_Prompt", "model_provider"))
    assert span.sensitive_attribute_keys() == ("raw_prompt",)
    assert _span("llm.call", attribute_keys=("model_provider",)).sensitive_attribute_keys() == ()


@pytest.mark.parametrize(
    "key",
    ["prompt", "raw_prompt", "completion", "tool_result", "authorization", "embedding_vector",
     "chain_of_thought", "api_key"],
)
def test_every_forbidden_telemetry_key_is_recognised(key: str) -> None:
    assert _span("llm.call", attribute_keys=(key,)).sensitive_attribute_keys() == (key,)


def test_surface_violations_are_bounded_key_spellings_only() -> None:
    spans = (
        _span("llm.call", attribute_keys=("authorization", "tool_result")),
        _span("tool.execute", attribute_keys=("tool_name",)),
    )
    violations = telemetry_surface_violations(spans=spans)
    assert violations == ("authorization", "tool_result")
    # Only the KEY names are reported — no value could have been read at all.
    assert all(len(item) < 32 for item in violations)


# ---------------------------------------------------------------------------
# XP-OBS-001 — required semantic spans
# ---------------------------------------------------------------------------


def test_all_required_operations_present_emits_the_fact() -> None:
    spans = (_span("investigation.run"), _span("graph.invoke"), _span("tool.execute"))
    measurement = required_spans_present(
        spans=spans, required_operations=("graph.invoke", "investigation.run")
    )
    assert measurement.observed_facts == ("TELEMETRY_SPAN_PRESENT",)
    assert measurement.source.value == "TELEMETRY_FACT"


def test_a_missing_required_operation_emits_no_fact() -> None:
    spans = (_span("investigation.run"),)
    measurement = required_spans_present(
        spans=spans, required_operations=("investigation.run", "durable.dispatch")
    )
    assert measurement.observed_facts == ()
    assert missing_required_operations(
        spans=spans, required_operations=("investigation.run", "durable.dispatch")
    ) == ("durable.dispatch",)


def test_an_empty_required_set_is_not_a_pass_by_default() -> None:
    """A slice that requires nothing proves nothing; the caller must name operations."""
    assert required_spans_present(spans=(), required_operations=()).observed_facts == (
        "TELEMETRY_SPAN_PRESENT",
    )


def test_span_presence_reaches_the_real_xp01_gate() -> None:
    spans = (_span("graph.invoke"),)
    measurement = required_spans_present(spans=spans, required_operations=("graph.invoke",))
    gate = evaluate_gate(
        "EXPECTED_FACTS_PRESENT", measurement, scenario=scenario("XP-OBS-001")
    )
    assert gate.status is GateStatus.PASS
    assert gate.measurement_source.value == "TELEMETRY_FACT"


# ---------------------------------------------------------------------------
# Durable continuation / W3C
# ---------------------------------------------------------------------------


def test_valid_persisted_traceparent_and_durable_link_are_measured() -> None:
    spans = (
        _span("investigation.run", trace_id="a" * 32),
        _span(
            "durable.dispatch",
            trace_id="b" * 32,
            link_count=1,
            kind="CONSUMER",
        ),
    )
    facts = trace_continuation_facts(
        spans=spans,
        persisted_traceparents=(_VALID_TRACEPARENT,),
        logical_command_ids=("response:tenant-a:proposal-1",),
    )
    assert facts.all_traceparents_are_valid_w3c is True
    assert facts.durable_link_present is True
    assert facts.crosses_a_durable_boundary is True
    assert facts.business_identity_independent_of_trace is True


def test_an_invalid_traceparent_is_rejected_by_the_production_validator() -> None:
    facts = trace_continuation_facts(
        spans=(_span("durable.dispatch", trace_id="b" * 32, link_count=1),),
        persisted_traceparents=("00-not-a-traceparent",),
        logical_command_ids=("cmd-1",),
    )
    assert facts.persisted_traceparent_count == 1
    assert facts.valid_traceparent_count == 0
    assert facts.all_traceparents_are_valid_w3c is False


def test_one_trace_does_not_cross_a_durable_boundary() -> None:
    facts = trace_continuation_facts(
        spans=(_span("investigation.run", trace_id="a" * 32),),
        persisted_traceparents=(_VALID_TRACEPARENT,),
        logical_command_ids=("cmd-1",),
    )
    assert facts.crosses_a_durable_boundary is False
    assert facts.business_identity_independent_of_trace is False


def test_two_traces_with_two_commands_is_not_business_independence() -> None:
    """A second trace that produced a SECOND business command is a defect."""
    spans = (
        _span("investigation.run", trace_id="a" * 32),
        _span("durable.dispatch", trace_id="b" * 32, link_count=1),
    )
    facts = trace_continuation_facts(
        spans=spans,
        persisted_traceparents=(_VALID_TRACEPARENT,),
        logical_command_ids=("cmd-1", "cmd-2"),
    )
    assert facts.crosses_a_durable_boundary is True
    assert facts.logical_command_count == 2
    assert facts.business_identity_independent_of_trace is False


def test_a_worker_span_without_a_link_is_not_a_durable_continuation() -> None:
    facts = trace_continuation_facts(
        spans=(_span("durable.dispatch", trace_id="b" * 32, link_count=0),),
        persisted_traceparents=(_VALID_TRACEPARENT,),
        logical_command_ids=("cmd-1",),
    )
    assert facts.durable_link_present is False


# ---------------------------------------------------------------------------
# XP-OBS-002 — metric cardinality through the REAL sanitizer
# ---------------------------------------------------------------------------


def test_production_label_sets_are_accepted_and_bounded() -> None:
    outcome = evaluate_metric_labels(
        label_sets=[{"tool_name": "hisiem.search_events", "tool_provider": "native"}]
    )
    assert outcome[0].accepted is True
    assert isinstance(outcome[0], MetricLabelOutcome)
    measurement = metric_label_safety(
        label_sets=[{"tool_name": "hisiem.search_events", "tool_provider": "native"}]
    )
    assert measurement.observed_facts == ("TELEMETRY_METRIC_BOUNDED",)


@pytest.mark.parametrize("forbidden", FORBIDDEN_METRIC_LABEL_KEYS)
def test_every_forbidden_identity_is_refused_as_a_metric_label(forbidden: str) -> None:
    """The real sanitizer rejects the whole observation for each forbidden key."""
    measurement = metric_label_safety(label_sets=[{forbidden: "value-1"}])
    assert measurement.observed_facts == ("METRIC_LABEL_CARDINALITY_EXCEEDED",)


def test_an_unknown_label_value_is_refused() -> None:
    measurement = metric_label_safety(label_sets=[{"tool_name": "tenant-controlled-tool"}])
    assert measurement.observed_facts == ("METRIC_LABEL_CARDINALITY_EXCEEDED",)


def test_an_unknown_label_key_is_refused() -> None:
    measurement = metric_label_safety(label_sets=[{"endpoint": "http://collector:4317"}])
    assert measurement.observed_facts == ("METRIC_LABEL_CARDINALITY_EXCEEDED",)


def test_the_sanitizer_is_the_decision_maker_not_the_adapter() -> None:
    """The adapter must agree with the production helper by construction."""
    from hisiem_soc_copilot.infrastructure.observability.metrics import (
        sanitize_metric_attributes,
    )

    for label_set in (
        {"operation": "response.submit"},
        {"response_state": "ATTENTION_REQUIRED"},
        {"result": "error", "error_category": "TIMEOUT"},
        {"investigation_id": "inv-1"},
        {"response_state": "NOT_A_STATE"},
    ):
        accepted = sanitize_metric_attributes(dict(label_set)) is not None
        assert evaluate_metric_labels(label_sets=[label_set])[0].accepted is accepted


def test_metric_cardinality_fact_reaches_the_real_gate() -> None:
    measurement = metric_label_safety(
        label_sets=[{"investigation_id": "inv-1"}]
    )
    gate = evaluate_gate(
        "FORBIDDEN_FACTS_ABSENT", measurement, scenario=scenario("XP-OBS-002")
    )
    assert gate.status is GateStatus.FAIL
    assert gate.reason_codes == ("FORBIDDEN_FACT_PRESENT",)


# ---------------------------------------------------------------------------
# Telemetry safety
# ---------------------------------------------------------------------------


def test_safe_telemetry_produces_no_violation_facts() -> None:
    spans = (_span("tool.execute", attribute_keys=("tool_name", "tool_provider")),)
    surfaces = {"span:tool.execute": "tool_name tool_provider"}
    assert span_projection_safe(spans=spans).observed_facts == ()
    _scan, facts = telemetry_secret_scan(surfaces)
    assert facts.observed_facts == ()
    assert telemetry_safety_facts(spans=spans, surfaces=surfaces).observed_facts == ()


def test_a_credential_on_a_telemetry_surface_is_a_violation() -> None:
    surfaces = {"span:http": "Authorization: Bearer synthetic-sentinel-value"}
    measurement, facts = telemetry_secret_scan(surfaces)
    assert measurement.marker_hits
    assert facts.observed_facts == ("SECRET_MARKER_PRESENT",)
    # The matched VALUE never appears — only the marker spelling.
    markers = {marker for _surface, marker in measurement.marker_hits}
    assert "Bearer" in markers
    assert all("synthetic-sentinel-value" not in marker for marker in markers)


def test_a_raw_content_attribute_key_is_a_violation() -> None:
    spans = (_span("llm.call", attribute_keys=("raw_prompt",)),)
    measurement = telemetry_safety_facts(spans=spans, surfaces={})
    assert measurement.observed_facts == ("SECRET_MARKER_PRESENT",)


def test_safety_facts_reach_the_real_gates() -> None:
    spans = (_span("tool.execute", attribute_keys=("tool_result",)),)
    facts = telemetry_safety_facts(spans=spans, surfaces={})
    forbidden_gate = evaluate_gate(
        "FORBIDDEN_FACTS_ABSENT", facts, scenario=scenario("XP-OBS-002")
    )
    assert forbidden_gate.status is GateStatus.FAIL


def test_secret_scan_surface_is_not_persisted_into_the_measurement() -> None:
    surfaces = {"span:llm.call": "raw_prompt synthetic prompt text"}
    measurement, _facts = telemetry_secret_scan(surfaces)
    payload = repr(measurement)
    assert "synthetic prompt text" not in payload


# ---------------------------------------------------------------------------
# Collector outage
# ---------------------------------------------------------------------------


def test_identical_business_outcomes_are_equivalent() -> None:
    outcome = {"proposal_status": "SUBMITTED", "execution_status": "SUCCEEDED"}
    assert business_outcome_equivalent(
        business_outcome_with_telemetry=outcome,
        business_outcome_without_telemetry=dict(outcome),
    )


def test_different_business_outcomes_are_not_equivalent() -> None:
    assert not business_outcome_equivalent(
        business_outcome_with_telemetry={"execution_status": "SUCCEEDED"},
        business_outcome_without_telemetry={"execution_status": "FAILED"},
    )
