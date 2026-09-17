"""E4 observability measurement adapters: real telemetry facts -> E1 measurements.

Stage E / E4 owns the OBSERVABILITY family. This module is the middle of the E4
flow, exactly as ``cross_plane_measure`` is for E2 and ``cross_plane_authority`` is
for E3:

```text
existing runtime / telemetry facts
        -> THIS MODULE (measure facts)
        -> E1 typed measurement contract
        -> E1 deterministic hard gate
        -> cross-plane-gate-results/v1
```

The frozen rule this module exists to keep measurable (E4 §5):

```text
Telemetry explains runtime.
Telemetry does not authorize, decide, persist, or override business truth.
```

Three rules shape every function here:

* **Measure, do not decide.** These adapters collect facts and hand them to E1's
  gates. ``evaluation.cross_plane.evaluate_scenario`` is the only decision point.
* **Use the production rule, never a second copy of it.** Metric-label safety is
  decided by calling ``infrastructure.observability.metrics.sanitize_metric_attributes``
  itself; W3C validity is decided by calling ``observability.context.validate_traceparent``
  itself. The evaluation plane does not re-implement the sanitizer or the
  traceparent grammar (E4 §14).
* **Bounded projections only.** No raw span, no raw telemetry dump, and no metric
  label ever reaches an artifact: spans are projected to an operation name, a
  status *category*, parent/link presence, and attribute **keys** (E4 §21); scan
  results carry the matching marker *spelling*, never the matched value.

Read-only with respect to production: nothing here emits, exports, or mutates
telemetry, and nothing reads telemetry to influence a business outcome.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..evaluation.cross_plane import (
    FactSetMeasurement,
    MeasurementReferences,
    MeasurementSource,
    SecretScanMeasurement,
    dedupe_bounded_ids,
)
from ..infrastructure.observability.context import validate_traceparent
from ..infrastructure.observability.metrics import sanitize_metric_attributes
from .cross_plane_measure import secret_scan_of

#: Frozen XP-01 fact tokens this module may emit. Every one is declared in
#: ``evaluation.cross_plane.gates``.
FACT_TELEMETRY_SPAN_PRESENT = "TELEMETRY_SPAN_PRESENT"
FACT_TELEMETRY_METRIC_BOUNDED = "TELEMETRY_METRIC_BOUNDED"
FACT_TELEMETRY_OUTAGE_ISOLATED = "TELEMETRY_OUTAGE_ISOLATED"
FACT_TELEMETRY_ALTERED_BUSINESS_STATE = "TELEMETRY_ALTERED_BUSINESS_STATE"
FACT_ORACLE_DATA_LEAKED = "ORACLE_DATA_LEAKED_TO_PRODUCTION"
FACT_METRIC_LABEL_CARDINALITY_EXCEEDED = "METRIC_LABEL_CARDINALITY_EXCEEDED"
FACT_SECRET_MARKER_PRESENT = "SECRET_MARKER_PRESENT"

#: Attribute keys that must never appear on a span: the raw material of the LLM
#: exchange, the full tool result, credentials, embeddings, and reasoning traces
#: (E4 §11). Matched case-insensitively against span attribute KEYS only — the
#: value is never read, copied, or persisted.
FORBIDDEN_TELEMETRY_ATTRIBUTE_KEYS: tuple[str, ...] = (
    "prompt",
    "raw_prompt",
    "completion",
    "raw_completion",
    "messages",
    "tool_result",
    "raw_tool_result",
    "authorization",
    "bearer",
    "api_key",
    "password",
    "credential",
    "dsn",
    "embedding",
    "embedding_vector",
    "chain_of_thought",
    "reasoning",
    "env",
)

#: Metric label keys that must never exist (00 §5.5, 06 §7.7, E4 §14). These are
#: safe TRACE/log correlation attributes; they are not metric dimensions.
FORBIDDEN_METRIC_LABEL_KEYS: tuple[str, ...] = (
    "investigation_id",
    "tenant_id",
    "trace_id",
    "span_id",
    "invocation_id",
    "tool_invocation_id",
    "command_id",
    "execution_command_id",
    "request_id",
    "user_id",
    "alert_id",
    "evidence_id",
    "finding_id",
    "response_proposal_id",
    "correlation_id",
)


@dataclass(frozen=True)
class SpanFact:
    """A BOUNDED projection of one finished span (E4 §21).

    Deliberately carries no attribute *values* and no span events: the evaluation
    plane may reason about which semantic operation ran, how it ended, and which
    attribute keys were attached — never about the payload.
    """

    operation: str
    kind: str = "INTERNAL"
    status_category: str = "unset"
    parent_present: bool = False
    link_count: int = 0
    trace_id: str = ""
    span_id: str = ""
    attribute_keys: tuple[str, ...] = ()

    @property
    def is_root(self) -> bool:
        return not self.parent_present

    def sensitive_attribute_keys(self) -> tuple[str, ...]:
        """The keys on this span that must never carry telemetry content."""
        lowered = {key.lower() for key in self.attribute_keys}
        return tuple(
            sorted(key for key in FORBIDDEN_TELEMETRY_ATTRIBUTE_KEYS if key in lowered)
        )


@dataclass(frozen=True)
class TraceContinuationFacts:
    """Bounded facts about durable trace continuation and business independence.

    A representative cross-plane lifecycle crosses a durable boundary: the worker
    runs in a NEW trace (or at least a new span tree) and is linked to the producer
    by the traceparent persisted with the outbox row. Business identity must not
    follow the trace: one logical command stays one logical command however many
    traces the work is split across (E4 §10).
    """

    persisted_traceparent_count: int
    valid_traceparent_count: int
    distinct_trace_count: int
    linked_worker_span_count: int
    logical_command_count: int

    @property
    def all_traceparents_are_valid_w3c(self) -> bool:
        return (
            self.persisted_traceparent_count > 0
            and self.valid_traceparent_count == self.persisted_traceparent_count
        )

    @property
    def crosses_a_durable_boundary(self) -> bool:
        return self.distinct_trace_count > 1

    @property
    def durable_link_present(self) -> bool:
        return self.linked_worker_span_count > 0

    @property
    def business_identity_independent_of_trace(self) -> bool:
        """More than one trace, still exactly one logical business command."""
        return self.crosses_a_durable_boundary and self.logical_command_count == 1


def _refs(*, trace_id: str = "", span_id: str = "") -> MeasurementReferences:
    """Bounded correlation references — pointers, never business truth (E4 §8)."""
    return MeasurementReferences(
        trace_id=trace_id or None,
        span_id=span_id or None,
    )


def _facts(*, present: Sequence[str] = (), forbidden: Sequence[str] = ()) -> FactSetMeasurement:
    """A bounded fact set sourced from TELEMETRY (E1 §26 provenance category)."""
    return FactSetMeasurement(
        source=MeasurementSource.TELEMETRY_FACT,
        observed_facts=dedupe_bounded_ids(
            [*present, *forbidden], what="observed_facts"
        ),
    )


# ---------------------------------------------------------------------------
# XP-OBS-001 — representative trace correlation
# ---------------------------------------------------------------------------


def required_spans_present(
    *,
    spans: Sequence[SpanFact],
    required_operations: Sequence[str],
    trace_id: str = "",
    span_id: str = "",
) -> FactSetMeasurement:
    """`TELEMETRY_SPAN_PRESENT` iff every required semantic operation was observed.

    Only semantic operation NAMES are compared (00 §5.3: span names stay stable and
    never mirror LangGraph internals). A missing operation is a missing fact — the
    gate fails rather than the measurement inventing a span.
    """
    observed = {span.operation for span in spans}
    missing = sorted(set(required_operations) - observed)
    if missing:
        return _facts()
    return FactSetMeasurement(
        source=MeasurementSource.TELEMETRY_FACT,
        references=_refs(trace_id=trace_id, span_id=span_id),
        observed_facts=(FACT_TELEMETRY_SPAN_PRESENT,),
    )


def missing_required_operations(
    *, spans: Sequence[SpanFact], required_operations: Sequence[str]
) -> tuple[str, ...]:
    """The required operations that were NOT observed (bounded, for reporting)."""
    observed = {span.operation for span in spans}
    return tuple(sorted(set(required_operations) - observed))


def trace_continuation_facts(
    *,
    spans: Sequence[SpanFact],
    persisted_traceparents: Sequence[str],
    logical_command_ids: Sequence[str],
    durable_operation: str = "durable.dispatch",
) -> TraceContinuationFacts:
    """Measure durable continuation and business/trace independence.

    ``persisted_traceparents`` are read from the durable store (the outbox rows), and
    validated with production's own ``validate_traceparent`` — the evaluation plane
    must not carry a second idea of what W3C trace context is.
    """
    valid = [value for value in persisted_traceparents if validate_traceparent(value) is not None]
    linked = [
        span
        for span in spans
        if span.operation == durable_operation and span.link_count > 0
    ]
    return TraceContinuationFacts(
        persisted_traceparent_count=len(list(persisted_traceparents)),
        valid_traceparent_count=len(valid),
        distinct_trace_count=len({span.trace_id for span in spans if span.trace_id}),
        linked_worker_span_count=len(linked),
        logical_command_count=len(set(logical_command_ids)),
    )


def telemetry_surface_violations(*, spans: Sequence[SpanFact]) -> tuple[str, ...]:
    """Sensitive attribute KEYS found on the observed spans.

    Returns the forbidden key spellings only. The values are never inspected, so
    this cannot itself leak prompt/completion/ToolResult content (E4 §11/§21).
    """
    found: set[str] = set()
    for span in spans:
        found.update(span.sensitive_attribute_keys())
    return tuple(sorted(found))


def span_projection_safe(*, spans: Sequence[SpanFact]) -> FactSetMeasurement:
    """`SECRET_MARKER_PRESENT` iff a span carried a key that must never be there.

    The token is the frozen "sensitive content reached a telemetry surface" marker;
    E4 uses it for both credential markers (from the real secret scan) and the
    raw-content/credential attribute keys above, so a single frozen gate decides
    telemetry safety rather than a second vocabulary being invented for E4.
    """
    violations = telemetry_surface_violations(spans=spans)
    if violations:
        return _facts(forbidden=(FACT_SECRET_MARKER_PRESENT,))
    return _facts()


# ---------------------------------------------------------------------------
# XP-OBS-002 — metrics cardinality and telemetry safety
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricLabelOutcome:
    """The real sanitizer's verdict on one label set (bounded, no raw dump)."""

    label_keys: tuple[str, ...]
    accepted: bool
    rejected_keys: tuple[str, ...]
    forbidden_keys_accepted: tuple[str, ...]


def evaluate_metric_labels(
    *,
    label_sets: Sequence[Mapping[str, str]],
    forbidden_label_keys: Sequence[str] = FORBIDDEN_METRIC_LABEL_KEYS,
) -> tuple[MetricLabelOutcome, ...]:
    """Run every label set through the REAL production sanitizer.

    ``sanitize_metric_attributes`` is the canonical production helper (E4 §14): it
    rejects the WHOLE observation when any label key is unknown or any value falls
    outside the allowlisted value set. Reimplementing that rule in evaluation would
    let the two drift, so the adapter calls it.
    """
    outcomes: list[MetricLabelOutcome] = []
    forbidden = {key.lower() for key in forbidden_label_keys}
    for label_set in label_sets:
        safe = sanitize_metric_attributes(dict(label_set))
        accepted = safe is not None
        rejected = () if accepted else dedupe_bounded_ids(
            sorted(str(key) for key in label_set), what="rejected_label_keys"
        )
        accepted_forbidden = (
            tuple(sorted(key for key in label_set if key.lower() in forbidden))
            if accepted
            else ()
        )
        outcomes.append(
            MetricLabelOutcome(
                label_keys=dedupe_bounded_ids(
                    sorted(str(key) for key in label_set), what="label_keys"
                ),
                accepted=accepted,
                rejected_keys=tuple(rejected),
                forbidden_keys_accepted=accepted_forbidden,
            )
        )
    return tuple(outcomes)


def metric_label_safety(
    *,
    label_sets: Sequence[Mapping[str, str]],
    forbidden_label_keys: Sequence[str] = FORBIDDEN_METRIC_LABEL_KEYS,
) -> FactSetMeasurement:
    """`TELEMETRY_METRIC_BOUNDED`, or the forbidden cardinality fact.

    The invariant is about what production actually labels its instruments with: a
    label set that the sanitizer refuses is an instrument whose observation had to
    be dropped, and a label set the sanitizer ACCEPTS while carrying a forbidden
    identity is a high-cardinality dimension that reached the metrics pipeline.
    Either one fails the gate — the second is impossible today, which is exactly
    why it is worth gating.
    """
    outcomes = evaluate_metric_labels(
        label_sets=label_sets, forbidden_label_keys=forbidden_label_keys
    )
    unbounded = [outcome for outcome in outcomes if not outcome.accepted]
    leaked = [outcome for outcome in outcomes if outcome.forbidden_keys_accepted]
    if unbounded or leaked:
        return _facts(forbidden=(FACT_METRIC_LABEL_CARDINALITY_EXCEEDED,))
    return _facts(present=(FACT_TELEMETRY_METRIC_BOUNDED,))


def telemetry_secret_scan(
    surfaces: Mapping[str, str],
) -> tuple[SecretScanMeasurement, FactSetMeasurement]:
    """The REAL secret scan over bounded telemetry surfaces, plus its fact form.

    ``surfaces`` are the transient projections of telemetry surfaces (span name +
    attribute keys, metric label sets, the allowlisted log-context keys). They are
    scanned by the frozen E1 marker vocabulary and only the matching marker
    SPELLINGS survive into a measurement; the surface text itself is never
    persisted into an artifact.
    """
    measurement = secret_scan_of(dict(surfaces))
    if measurement.marker_hits:
        return measurement, _facts(forbidden=(FACT_SECRET_MARKER_PRESENT,))
    return measurement, _facts()


def telemetry_safety_facts(
    *,
    spans: Sequence[SpanFact],
    surfaces: Mapping[str, str],
) -> FactSetMeasurement:
    """Combined telemetry-safety facts for XP-OBS-002.

    Union of the raw-content/credential attribute-key check and the real secret scan.
    """
    violations: list[str] = []
    if telemetry_surface_violations(spans=spans):
        # A raw-content or credential ATTRIBUTE KEY reached a span.
        violations.append(FACT_SECRET_MARKER_PRESENT)
    _scan, scanned = telemetry_secret_scan(surfaces)
    if FACT_SECRET_MARKER_PRESENT in scanned.observed_facts:
        # The real secret scan matched a frozen marker on a telemetry surface.
        violations.append(FACT_SECRET_MARKER_PRESENT)
    return _facts(forbidden=tuple(dict.fromkeys(violations)))


# ---------------------------------------------------------------------------
# E4 §15/§16 — collector outage must not change the business outcome
# ---------------------------------------------------------------------------


def collector_outage_facts(
    *,
    business_outcome_with_telemetry: Mapping[str, object],
    business_outcome_without_telemetry: Mapping[str, object],
) -> FactSetMeasurement:
    """The XP-REL-005 outage invariant, measured for the E4 runtime slice.

    E4 deliberately does NOT invent a second outage gate: the frozen
    ``TELEMETRY_CHANGED_BUSINESS_STATE`` gate already decides this invariant, and
    E4 supplies it with evidence from ITS OWN representative lifecycle rather than
    re-running the E3 slice. The measurement itself is E3's
    ``telemetry_isolation_facts`` — one implementation, two callers.
    """
    from .cross_plane_authority import telemetry_isolation_facts

    return telemetry_isolation_facts(
        business_outcome_with_telemetry=business_outcome_with_telemetry,
        business_outcome_without_telemetry=business_outcome_without_telemetry,
    )


def business_outcome_equivalent(
    *,
    business_outcome_with_telemetry: Mapping[str, object],
    business_outcome_without_telemetry: Mapping[str, object],
) -> bool:
    """True when the two persisted business outcomes are identical."""
    return dict(business_outcome_with_telemetry) == dict(
        business_outcome_without_telemetry
    )


__all__ = [
    "FORBIDDEN_METRIC_LABEL_KEYS",
    "FORBIDDEN_TELEMETRY_ATTRIBUTE_KEYS",
    "MetricLabelOutcome",
    "SpanFact",
    "TraceContinuationFacts",
    "business_outcome_equivalent",
    "collector_outage_facts",
    "evaluate_metric_labels",
    "metric_label_safety",
    "missing_required_operations",
    "required_spans_present",
    "span_projection_safe",
    "telemetry_safety_facts",
    "telemetry_secret_scan",
    "telemetry_surface_violations",
    "trace_continuation_facts",
]
