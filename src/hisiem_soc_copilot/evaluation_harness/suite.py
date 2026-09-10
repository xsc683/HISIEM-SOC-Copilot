"""E1-C5 repeatability collector + E1-C6 evaluation suite summary (evaluation-only).

The suite answers the REPEATABILITY question: run the sealed GP-01 dataset through
the real evaluation chain (E1-C2 → E1-C3 → E1-C4) repeatedly and aggregate the
outcome into ONE bounded ``suite-summary.json`` — never overwriting a prior suite:

    <executions_dir>/gp-01/<dataset_run_id>/suites/<suite_id>/suite-summary.json

Each attempt gets a NEW execution id (owned by the harness). A VALID sample (a real
semantic execution, PASS or FAIL) counts toward the required valid runs and is NEVER
discarded or replaced; a provider-transient attempt (§9/§10
``INVALID_PROVIDER_TRANSIENT``) is preserved and reported but never counted, and may
be replaced ONLY within ``max_attempts``. An ABORT (config/contract/preflight/infra)
stops the suite immediately — there is no retry-until-success. The suite collects
exactly ``valid_runs`` valid samples unless ``max_attempts`` is exhausted (→
``INSUFFICIENT_VALID_RUNS``) or the suite aborts.

Efficiency metrics (duration / model calls / tool calls / search_events calls /
total tokens) are INFORMATIONAL only (§7) and never affect ``suite_gate``; a
provider that reports no token counts stays ``null`` — never guessed.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..evaluation.contracts import SealedManifest
from .classification import (
    CLASS_ABORT,
    CLASS_INVALID_PROVIDER_TRANSIENT,
    CLASS_VALID_FAIL,
    CLASS_VALID_PASS,
    AttemptClassification,
    classify_attempt,
)
from .record import atomic_write_json
from .score import MAX_FIELD_LEN
from .score_harness import ScoringOutcome, score_execution

SUITE_SCHEMA_VERSION = "evaluation-suite-summary/v1"
_SUITE_ARTIFACT = "suite-summary.json"

GATE_PASS = "PASS"
GATE_FAIL = "FAIL"

# Bounded run counts (§8): no unlimited loop.
DEFAULT_VALID_RUNS = 3
DEFAULT_MAX_ATTEMPTS = 6
HARD_CAP = 50  # both valid_runs and max_attempts must satisfy 1 <= n <= HARD_CAP

# Efficiency metrics aggregated over the VALID attempts (informational only).
_EFFICIENCY_METRICS: tuple[str, ...] = (
    "duration_ms",
    "model_calls",
    "tool_calls",
    "search_events_calls",
    "total_tokens",
)

# Suite gate failure tokens.
SUITE_FAIL_ABORTED = "SUITE_ABORTED"
SUITE_FAIL_INSUFFICIENT_VALID_RUNS = "INSUFFICIENT_VALID_RUNS"
SUITE_FAIL_VALID_FAILURES_PRESENT = "VALID_FAILURES_PRESENT"
SUITE_FAIL_VERDICT_MATCH_NOT_ALL = "VERDICT_MATCH_NOT_ALL"
SUITE_FAIL_REQUIRED_EVIDENCE_NOT_ALL = "REQUIRED_EVIDENCE_NOT_ALL"
SUITE_FAIL_GROUNDING_NOT_ALL = "GROUNDING_NOT_ALL"
SUITE_FAIL_CONTROL_EXCLUSION_NOT_ALL = "CONTROL_EXCLUSION_NOT_ALL"
SUITE_FAIL_CITATION_INTEGRITY_NOT_ALL = "CITATION_INTEGRITY_NOT_ALL"
SUITE_FAIL_RESULT_FINDING_INTEGRITY_NOT_ALL = "RESULT_FINDING_INTEGRITY_NOT_ALL"
SUITE_FAIL_ORACLE_FIREWALL_NOT_ALL = "ORACLE_FIREWALL_NOT_ALL"
SUITE_FAIL_SECRET_SCAN_VIOLATION = "SECRET_SCAN_VIOLATION"

# Secret markers that must never appear in the suite artifact (mirrors §18/§29).
_SECRET_MARKERS: tuple[str, ...] = (
    "api_key",
    "CMD_API_KEY",
    "Authorization",
    "Bearer",
    "password",
    "postgresql://",
    "sk-",
)

# Abort categories surfaced by the suite controller itself (bounded).
ABORT_PREFLIGHT_FAILED = "PREFLIGHT_FAILED"

_ATTEMPT_KEYS: tuple[str, ...] = (
    "attempt_number",
    "execution_id",
    "classification",
    "execution_status",
    "model_gate",
    "quality_gate",
    "correctness_gate",
    "score_artifact",
)

_SUMMARY_KEYS: tuple[str, ...] = (
    "schema_version",
    "suite_id",
    "scenario_id",
    "dataset_run_id",
    "dataset_manifest_sha256",
    "requested_valid_runs",
    "max_attempts",
    "total_attempts",
    "valid_attempts",
    "invalid_provider_transient_attempts",
    "valid_passes",
    "valid_failures",
    "correctness_pass_rate",
    "verdict_match_count",
    "required_evidence_pass_count",
    "grounding_pass_count",
    "control_exclusion_pass_count",
    "citation_integrity_pass_count",
    "result_finding_integrity_pass_count",
    "attempts",
    "abort_category",
    "efficiency",
    "secret_scan_pass",
    "suite_gate",
    "gate_failures",
)


class SuiteSchemaError(ValueError):
    """A persisted suite payload carries an unsupported/legacy schema version."""


def suite_summary_path(
    executions_dir: str | Path, dataset_run_id: str, suite_id: str
) -> Path:
    """The unique per-suite summary artifact (never overwrites a prior suite)."""
    return (
        Path(executions_dir)
        / "gp-01"
        / dataset_run_id
        / "suites"
        / suite_id
        / _SUITE_ARTIFACT
    )


# ---------------------------------------------------------------------------
# Per-attempt result (the aggregation input) + suite summary artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptResult:
    """The bounded per-attempt facts the suite aggregates + records."""

    attempt_number: int
    execution_id: str | None
    classification: str
    execution_status: str | None
    model_gate: str
    quality_gate: str
    correctness_gate: str
    score_artifact: str | None = None
    abort_category: str | None = None
    verdict_match: bool = False
    required_evidence_pass: bool = False
    grounding_pass: bool = False
    control_exclusion_pass: bool = False
    citation_integrity_pass: bool = False
    result_finding_integrity_pass: bool = False
    oracle_firewall_pass: bool = False
    duration_ms: int | None = None
    model_calls: int | None = None
    tool_calls: int | None = None
    search_events_calls: int | None = None
    total_tokens: int | None = None

    @property
    def counts_as_valid(self) -> bool:
        return self.classification in (CLASS_VALID_PASS, CLASS_VALID_FAIL)

    def to_payload(self) -> dict[str, Any]:
        return {
            "attempt_number": self.attempt_number,
            "execution_id": self.execution_id,
            "classification": self.classification,
            "execution_status": self.execution_status,
            "model_gate": self.model_gate,
            "quality_gate": self.quality_gate,
            "correctness_gate": self.correctness_gate,
            "score_artifact": self.score_artifact,
        }


def attempt_result_from_scoring(
    *,
    attempt_number: int,
    outcome: ScoringOutcome | None,
    classification: AttemptClassification,
) -> AttemptResult:
    """Project ONE scored attempt (or an abort with no score) into an AttemptResult."""
    if outcome is None:
        return AttemptResult(
            attempt_number=attempt_number,
            execution_id=None,
            classification=classification.classification,
            execution_status=None,
            model_gate=GATE_FAIL,
            quality_gate=GATE_FAIL,
            correctness_gate=GATE_FAIL,
            abort_category=classification.abort_category,
        )
    score = outcome.score
    record = outcome.record
    quality_gate = (
        outcome.quality.gate_status if outcome.quality is not None else GATE_FAIL
    )
    model_gate = (
        outcome.telemetry.gate_status if outcome.telemetry is not None else GATE_FAIL
    )
    return AttemptResult(
        attempt_number=attempt_number,
        execution_id=record.execution_id,
        classification=classification.classification,
        execution_status=record.execution_status.value,
        model_gate=model_gate,
        quality_gate=quality_gate,
        correctness_gate=score.correctness_gate,
        score_artifact=str(outcome.score_artifact),
        abort_category=classification.abort_category,
        verdict_match=score.verdict_match,
        required_evidence_pass=len(score.matched_evidence_roles)
        >= len(score.required_evidence_roles)
        and bool(score.required_evidence_roles),
        grounding_pass=score.grounded_required_evidence,
        control_exclusion_pass=score.control_exclusion_pass,
        citation_integrity_pass=score.citation_integrity_pass,
        result_finding_integrity_pass=score.result_finding_integrity_pass,
        oracle_firewall_pass=score.oracle_firewall_pass,
        duration_ms=score.duration_ms,
        model_calls=score.model_calls,
        tool_calls=score.tool_calls,
        search_events_calls=score.search_events_calls,
        total_tokens=score.total_tokens,
    )


@dataclass(frozen=True)
class SuiteSummary:
    """The bounded E1-C6 suite summary (immutable)."""

    schema_version: str = SUITE_SCHEMA_VERSION
    suite_id: str = ""
    scenario_id: str = ""
    dataset_run_id: str = ""
    dataset_manifest_sha256: str = ""
    requested_valid_runs: int = DEFAULT_VALID_RUNS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    total_attempts: int = 0
    valid_attempts: int = 0
    invalid_provider_transient_attempts: int = 0
    valid_passes: int = 0
    valid_failures: int = 0
    correctness_pass_rate: float = 0.0
    verdict_match_count: int = 0
    required_evidence_pass_count: int = 0
    grounding_pass_count: int = 0
    control_exclusion_pass_count: int = 0
    citation_integrity_pass_count: int = 0
    result_finding_integrity_pass_count: int = 0
    attempts: tuple[dict[str, Any], ...] = ()
    abort_category: str | None = None
    efficiency: dict[str, Any] = field(default_factory=dict)
    secret_scan_pass: bool = True
    suite_gate: str = GATE_FAIL
    gate_failures: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "suite_id": self.suite_id,
            "scenario_id": self.scenario_id,
            "dataset_run_id": self.dataset_run_id,
            "dataset_manifest_sha256": self.dataset_manifest_sha256,
            "requested_valid_runs": self.requested_valid_runs,
            "max_attempts": self.max_attempts,
            "total_attempts": self.total_attempts,
            "valid_attempts": self.valid_attempts,
            "invalid_provider_transient_attempts": (
                self.invalid_provider_transient_attempts
            ),
            "valid_passes": self.valid_passes,
            "valid_failures": self.valid_failures,
            "correctness_pass_rate": self.correctness_pass_rate,
            "verdict_match_count": self.verdict_match_count,
            "required_evidence_pass_count": self.required_evidence_pass_count,
            "grounding_pass_count": self.grounding_pass_count,
            "control_exclusion_pass_count": self.control_exclusion_pass_count,
            "citation_integrity_pass_count": self.citation_integrity_pass_count,
            "result_finding_integrity_pass_count": (
                self.result_finding_integrity_pass_count
            ),
            "attempts": list(self.attempts),
            "abort_category": self.abort_category,
            "efficiency": self.efficiency,
            "secret_scan_pass": self.secret_scan_pass,
            "suite_gate": self.suite_gate,
            "gate_failures": list(self.gate_failures),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SuiteSummary:
        schema = payload.get("schema_version")
        if schema != SUITE_SCHEMA_VERSION:
            raise SuiteSchemaError(
                f"unsupported suite schema {schema!r}; this reader only accepts "
                f"{SUITE_SCHEMA_VERSION}"
            )
        safe = {k: payload[k] for k in _SUMMARY_KEYS if k in payload}
        return cls(
            suite_id=_bounded_str(safe.get("suite_id", "")),
            scenario_id=_bounded_str(safe.get("scenario_id", "")),
            dataset_run_id=_bounded_str(safe.get("dataset_run_id", "")),
            dataset_manifest_sha256=_bounded_str(
                safe.get("dataset_manifest_sha256", "")
            ),
            requested_valid_runs=_bounded_int(
                safe.get("requested_valid_runs", DEFAULT_VALID_RUNS)
            ),
            max_attempts=_bounded_int(safe.get("max_attempts", DEFAULT_MAX_ATTEMPTS)),
            total_attempts=_bounded_int(safe.get("total_attempts", 0)),
            valid_attempts=_bounded_int(safe.get("valid_attempts", 0)),
            invalid_provider_transient_attempts=_bounded_int(
                safe.get("invalid_provider_transient_attempts", 0)
            ),
            valid_passes=_bounded_int(safe.get("valid_passes", 0)),
            valid_failures=_bounded_int(safe.get("valid_failures", 0)),
            correctness_pass_rate=_bounded_float(
                safe.get("correctness_pass_rate", 0.0)
            ),
            verdict_match_count=_bounded_int(safe.get("verdict_match_count", 0)),
            required_evidence_pass_count=_bounded_int(
                safe.get("required_evidence_pass_count", 0)
            ),
            grounding_pass_count=_bounded_int(safe.get("grounding_pass_count", 0)),
            control_exclusion_pass_count=_bounded_int(
                safe.get("control_exclusion_pass_count", 0)
            ),
            citation_integrity_pass_count=_bounded_int(
                safe.get("citation_integrity_pass_count", 0)
            ),
            result_finding_integrity_pass_count=_bounded_int(
                safe.get("result_finding_integrity_pass_count", 0)
            ),
            attempts=_bounded_attempt_tuple(safe.get("attempts")),
            abort_category=_optional_bounded_str(safe.get("abort_category")),
            efficiency=_bounded_efficiency(safe.get("efficiency")),
            secret_scan_pass=bool(safe.get("secret_scan_pass", True)),
            suite_gate=_bounded_str(safe.get("suite_gate", GATE_FAIL)),
            gate_failures=_bounded_id_tuple(safe.get("gate_failures")),
        )


# ---------------------------------------------------------------------------
# Bounded coercion helpers
# ---------------------------------------------------------------------------


def _bounded_str(value: Any) -> str:
    return str(value)[:MAX_FIELD_LEN]


def _optional_bounded_str(value: Any) -> str | None:
    return None if value is None else str(value)[:MAX_FIELD_LEN]


def _bounded_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bounded_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bounded_id_tuple(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, (str, bytes)):
        return (_bounded_str(value),)
    return tuple(_bounded_str(item) for item in value)


def _bounded_attempt_tuple(value: Any) -> tuple[dict[str, Any], ...]:
    if not value:
        return ()
    entries: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        entries.append(
            {k: _bounded_scalar(item.get(k)) for k in _ATTEMPT_KEYS if k in item}
        )
    return tuple(entries)


def _bounded_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_FIELD_LEN]


def _bounded_efficiency(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {}
    out: dict[str, Any] = {}
    for metric in _EFFICIENCY_METRICS:
        stats = value.get(metric)
        if not isinstance(stats, dict):
            out[metric] = None
            continue
        out[metric] = {
            "min": _bounded_scalar(stats.get("min")),
            "median": _bounded_scalar(stats.get("median")),
            "max": _bounded_scalar(stats.get("max")),
        }
    return out


# ---------------------------------------------------------------------------
# Pure aggregation
# ---------------------------------------------------------------------------


def _efficiency(values: list[int | None]) -> dict[str, Any] | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return {
        "min": min(present),
        "median": statistics.median(present),
        "max": max(present),
    }


def validate_bounds(valid_runs: int, max_attempts: int) -> None:
    """Fail closed on out-of-range run counts (§8): never an unbounded loop."""
    if not (1 <= valid_runs <= HARD_CAP):
        raise ValueError(f"valid_runs must satisfy 1 <= n <= {HARD_CAP}, got {valid_runs}")
    if not (1 <= max_attempts <= HARD_CAP):
        raise ValueError(
            f"max_attempts must satisfy 1 <= n <= {HARD_CAP}, got {max_attempts}"
        )
    if valid_runs > max_attempts:
        raise ValueError(
            f"valid_runs ({valid_runs}) must be <= max_attempts ({max_attempts})"
        )


def build_suite_summary(
    *,
    suite_id: str,
    scenario_id: str,
    dataset_run_id: str,
    dataset_manifest_sha256: str,
    requested_valid_runs: int,
    max_attempts: int,
    attempts: tuple[AttemptResult, ...],
    abort_category: str | None = None,
    secret_scan_pass: bool = True,
) -> SuiteSummary:
    """Aggregate attempt outcomes into the bounded suite summary + gate (pure)."""
    valid = [a for a in attempts if a.counts_as_valid]
    transient = [a for a in attempts if a.classification == CLASS_INVALID_PROVIDER_TRANSIENT]
    valid_passes = sum(1 for a in valid if a.classification == CLASS_VALID_PASS)
    valid_failures = len(valid) - valid_passes

    def _count(predicate: str) -> int:
        return sum(1 for a in valid if getattr(a, predicate))

    valid_attempts = len(valid)
    correctness_pass_rate = (
        valid_passes / valid_attempts if valid_attempts else 0.0
    )

    failures: list[str] = []
    if abort_category is not None:
        failures.append(SUITE_FAIL_ABORTED)
    if valid_attempts < requested_valid_runs:
        failures.append(SUITE_FAIL_INSUFFICIENT_VALID_RUNS)
    if valid_failures:
        failures.append(SUITE_FAIL_VALID_FAILURES_PRESENT)
    if _count("verdict_match") != valid_attempts:
        failures.append(SUITE_FAIL_VERDICT_MATCH_NOT_ALL)
    if _count("required_evidence_pass") != valid_attempts:
        failures.append(SUITE_FAIL_REQUIRED_EVIDENCE_NOT_ALL)
    if _count("grounding_pass") != valid_attempts:
        failures.append(SUITE_FAIL_GROUNDING_NOT_ALL)
    if _count("control_exclusion_pass") != valid_attempts:
        failures.append(SUITE_FAIL_CONTROL_EXCLUSION_NOT_ALL)
    if _count("citation_integrity_pass") != valid_attempts:
        failures.append(SUITE_FAIL_CITATION_INTEGRITY_NOT_ALL)
    if _count("result_finding_integrity_pass") != valid_attempts:
        failures.append(SUITE_FAIL_RESULT_FINDING_INTEGRITY_NOT_ALL)
    if _count("oracle_firewall_pass") != valid_attempts:
        failures.append(SUITE_FAIL_ORACLE_FIREWALL_NOT_ALL)
    if not secret_scan_pass:
        failures.append(SUITE_FAIL_SECRET_SCAN_VIOLATION)

    efficiency = {
        metric: _efficiency([getattr(a, metric) for a in valid])
        for metric in _EFFICIENCY_METRICS
    }

    return SuiteSummary(
        suite_id=_bounded_str(suite_id),
        scenario_id=_bounded_str(scenario_id),
        dataset_run_id=_bounded_str(dataset_run_id),
        dataset_manifest_sha256=_bounded_str(dataset_manifest_sha256),
        requested_valid_runs=requested_valid_runs,
        max_attempts=max_attempts,
        total_attempts=len(attempts),
        valid_attempts=valid_attempts,
        invalid_provider_transient_attempts=len(transient),
        valid_passes=valid_passes,
        valid_failures=valid_failures,
        correctness_pass_rate=correctness_pass_rate,
        verdict_match_count=_count("verdict_match"),
        required_evidence_pass_count=_count("required_evidence_pass"),
        grounding_pass_count=_count("grounding_pass"),
        control_exclusion_pass_count=_count("control_exclusion_pass"),
        citation_integrity_pass_count=_count("citation_integrity_pass"),
        result_finding_integrity_pass_count=_count("result_finding_integrity_pass"),
        attempts=tuple(a.to_payload() for a in attempts),
        abort_category=abort_category,
        efficiency=efficiency,
        secret_scan_pass=secret_scan_pass,
        suite_gate=GATE_PASS if not failures else GATE_FAIL,
        gate_failures=tuple(failures),
    )


def _suite_secret_scan_pass(summary: SuiteSummary) -> bool:
    text = json.dumps(summary.to_payload())
    return not any(marker in text for marker in _SECRET_MARKERS)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def write_suite_summary(path: str | Path, summary: SuiteSummary) -> None:
    """Atomically persist the suite summary (never sealed; unique per suite_id)."""
    atomic_write_json(path, summary.to_payload())


def read_suite_summary(path: str | Path) -> SuiteSummary:
    """Load + validate a persisted suite summary artifact."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return SuiteSummary.from_payload(payload)


# ---------------------------------------------------------------------------
# Orchestration (E1-C5 collector)
# ---------------------------------------------------------------------------

AttemptRunner = Callable[[int], Awaitable[AttemptResult]]


@dataclass(frozen=True)
class SuiteRunResult:
    """Outcome of ONE suite collection run."""

    exit_code: int
    summary: SuiteSummary
    suite_artifact: Path


async def _run_one_attempt(
    *, dataset_run_id: str, settings: Any, provider_factory: Any | None
) -> ScoringOutcome:
    """Run ONE real attempt (E1-C2 → E1-C3 → E1-C4) and score it."""
    from .quality_harness import execute_tool_evidence_run

    provider = provider_factory() if provider_factory is not None else None
    tool_result = await execute_tool_evidence_run(
        dataset_run_id=dataset_run_id,
        settings=settings,
        provider=provider,
    )
    execution_id = tool_result.execution_id
    if execution_id is None:
        raise RuntimeError("attempt produced no execution id")
    return await score_execution(
        dataset_run_id=dataset_run_id,
        execution_id=execution_id,
        settings=settings,
    )


def _classify_scoring(outcome: ScoringOutcome) -> AttemptClassification:
    """Classify ONE scored attempt from its stable bounded facts (§9/§10)."""
    record = outcome.record
    execution_failed = record.execution_status.value == "FAILED"
    telemetry = outcome.telemetry
    return classify_attempt(
        execution_failed=execution_failed,
        failure_category=record.failure.category,
        telemetry_gate=(telemetry.gate_status if telemetry is not None else None),
        telemetry_gate_failures=(
            telemetry.gate_failures if telemetry is not None else ()
        ),
        usage_records=(telemetry.usage_records if telemetry is not None else ()),
        correctness_gate=outcome.score.correctness_gate,
    )


async def execute_gp01_suite(
    *,
    dataset_run_id: str,
    settings: Any,
    valid_runs: int = DEFAULT_VALID_RUNS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    provider_factory: Any | None = None,
    attempt_runner: AttemptRunner | None = None,
    on_attempt: Callable[[AttemptResult], None] | None = None,
) -> SuiteRunResult:
    """Collect bounded valid GP-01 samples and persist ONE unique suite summary (§8).

    ``attempt_runner``/``provider_factory`` are injection seams (tests); the operator
    CLI passes none, so each attempt uses a FRESH provider + a NEW execution id.
    """
    validate_bounds(valid_runs, max_attempts)
    suite_id = uuid4().hex
    executions_dir = settings.evaluation.executions_dir

    # preflight: the sealed manifest must verify (fail-closed). The manifest is never
    # materialized/sealed by the suite, so it cannot change mid-suite.
    from .harness import dataset_manifest_path, verify_dataset_manifest
    from .quality_harness import resolve_scenario_identities

    abort_category: str | None = None
    scenario_id = ""
    manifest_sha = ""
    try:
        manifest: SealedManifest = verify_dataset_manifest(
            dataset_manifest_path(settings.evaluation, dataset_run_id)
        )
        resolve_scenario_identities(manifest)  # required role must resolve
        scenario_id = manifest.scenario.id
        manifest_sha = str(manifest.integrity.get("manifest_sha256", ""))
    except Exception:  # noqa: BLE001 — any preflight break aborts (no retry)
        abort_category = ABORT_PREFLIGHT_FAILED

    attempts: list[AttemptResult] = []

    async def _default_runner(attempt_number: int) -> AttemptResult:
        outcome = await _run_one_attempt(
            dataset_run_id=dataset_run_id,
            settings=settings,
            provider_factory=provider_factory,
        )
        return attempt_result_from_scoring(
            attempt_number=attempt_number,
            outcome=outcome,
            classification=_classify_scoring(outcome),
        )

    runner: AttemptRunner = attempt_runner or _default_runner

    valid_collected = 0
    attempt_number = 0
    while abort_category is None and valid_collected < valid_runs and attempt_number < max_attempts:
        attempt_number += 1
        attempt = await runner(attempt_number)
        attempts.append(attempt)
        if on_attempt is not None:
            on_attempt(attempt)
        if attempt.classification == CLASS_ABORT:
            abort_category = attempt.abort_category or "EXECUTION_FAILED"
            break
        if attempt.counts_as_valid:
            valid_collected += 1
        # transient attempts never count and are replaceable within max_attempts

    summary = build_suite_summary(
        suite_id=suite_id,
        scenario_id=scenario_id,
        dataset_run_id=dataset_run_id,
        dataset_manifest_sha256=manifest_sha,
        requested_valid_runs=valid_runs,
        max_attempts=max_attempts,
        attempts=tuple(attempts),
        abort_category=abort_category,
        secret_scan_pass=True,
    )
    if not _suite_secret_scan_pass(summary):
        summary = build_suite_summary(
            suite_id=suite_id,
            scenario_id=scenario_id,
            dataset_run_id=dataset_run_id,
            dataset_manifest_sha256=manifest_sha,
            requested_valid_runs=valid_runs,
            max_attempts=max_attempts,
            attempts=tuple(attempts),
            abort_category=abort_category,
            secret_scan_pass=False,
        )

    artifact = suite_summary_path(executions_dir, dataset_run_id, suite_id)
    write_suite_summary(artifact, summary)

    exit_code = 0 if summary.suite_gate == GATE_PASS else 1
    return SuiteRunResult(
        exit_code=exit_code, summary=summary, suite_artifact=artifact
    )


def report_suite(summary: SuiteSummary) -> list[str]:
    """A concise human-readable E1-C6 suite report."""
    lines = [
        f"e1c6_suite_id={summary.suite_id}",
        f"e1c6_scenario_id={summary.scenario_id} "
        f"dataset_run_id={summary.dataset_run_id}",
        f"e1c6_attempts total={summary.total_attempts} "
        f"valid={summary.valid_attempts} "
        f"invalid_provider_transient={summary.invalid_provider_transient_attempts}",
        f"e1c6_valid_passes={summary.valid_passes} "
        f"valid_failures={summary.valid_failures} "
        f"correctness_pass_rate={summary.correctness_pass_rate}",
        f"e1c6_verdict_match={summary.verdict_match_count} "
        f"required_evidence={summary.required_evidence_pass_count} "
        f"grounding={summary.grounding_pass_count} "
        f"control_exclusion={summary.control_exclusion_pass_count}",
        f"e1c6_citation_integrity={summary.citation_integrity_pass_count} "
        f"result_finding_integrity={summary.result_finding_integrity_pass_count}",
    ]
    if summary.abort_category is not None:
        lines.append(f"e1c6_abort_category={summary.abort_category}")
    lines.append(f"e1c6_suite_gate={summary.suite_gate}")
    if summary.gate_failures:
        lines.append(f"e1c6_suite_gate_failures={','.join(summary.gate_failures)}")
    return lines


async def evaluate_gp01_cli(
    *,
    dataset_run_id: str,
    valid_runs: int = DEFAULT_VALID_RUNS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> int:
    """Operator-facing E1-C5/C6 driver: collect + persist ONE GP-01 suite (§26).

    Returns 0 only when the suite gate PASSES (exactly ``valid_runs`` valid samples,
    all correctness PASS, 3/3 closure for GP-01). The ONE command the operator runs.
    """
    from ..config import get_settings

    def _print(attempt: AttemptResult) -> None:
        print(
            f"e1c6_attempt={attempt.attempt_number} "
            f"execution_id={attempt.execution_id} "
            f"class={attempt.classification} "
            f"record={attempt.execution_status} "
            f"model_gate={attempt.model_gate} "
            f"quality_gate={attempt.quality_gate} "
            f"correctness={attempt.correctness_gate}"
        )

    result = await execute_gp01_suite(
        dataset_run_id=dataset_run_id,
        settings=get_settings(),
        valid_runs=valid_runs,
        max_attempts=max_attempts,
        on_attempt=_print,
    )
    for line in report_suite(result.summary):
        print(line)
    print(f"suite_summary_artifact={result.suite_artifact}")
    return result.exit_code
