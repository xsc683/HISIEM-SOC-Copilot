"""E1-C5/E1-C6 suite orchestration unit tests (§8, §11, §13, §14, §21).

Drives :func:`execute_gp01_suite` with an injected ``attempt_runner`` (no model, no
DB, no container) to exercise the bounded multi-run collector + the suite summary
aggregation: 3 PASS → PASS; PASS/FAIL/PASS → FAIL; transient replaced within
``max_attempts``; valid failures never discarded; ``max_attempts`` exhausted →
INSUFFICIENT_VALID_RUNS; config failure → immediate ABORT; unique execution ids; all
attempt artifacts retained; summary schema round-trip / unknown-schema rejection;
secret allowlist boundary; bounds validation; and a unique per-suite artifact path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.evaluation_harness.classification import (
    CLASS_ABORT,
    CLASS_INVALID_PROVIDER_TRANSIENT,
    CLASS_VALID_FAIL,
    CLASS_VALID_PASS,
)
from hisiem_soc_copilot.evaluation_harness.quality import GATE_FAIL, GATE_PASS
from hisiem_soc_copilot.evaluation_harness.suite import (
    ABORT_PREFLIGHT_FAILED,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_VALID_RUNS,
    HARD_CAP,
    SUITE_FAIL_INSUFFICIENT_VALID_RUNS,
    SUITE_FAIL_SECRET_SCAN_VIOLATION,
    SUITE_FAIL_VALID_FAILURES_PRESENT,
    SUITE_SCHEMA_VERSION,
    AttemptResult,
    SuiteSchemaError,
    SuiteSummary,
    execute_gp01_suite,
    read_suite_summary,
    suite_summary_path,
    validate_bounds,
    write_suite_summary,
)
from tests.unit.evaluation_harness._seal_helpers import seal_dataset

RUN_ID = "gp01-suite-run"
TENANT = "tenant-a"


def _settings(tmp_path: Path) -> Settings:
    settings = Settings()
    settings.evaluation.runs_dir = str(tmp_path / "runs")
    settings.evaluation.executions_dir = str(tmp_path / "executions")
    return settings


def _valid_pass(n: int, execution_id: str) -> AttemptResult:
    return AttemptResult(
        attempt_number=n,
        execution_id=execution_id,
        classification=CLASS_VALID_PASS,
        execution_status="COMPLETED",
        model_gate=GATE_PASS,
        quality_gate=GATE_PASS,
        correctness_gate=GATE_PASS,
        score_artifact=f"exec/{execution_id}/score.json",
        verdict_match=True,
        required_evidence_pass=True,
        grounding_pass=True,
        control_exclusion_pass=True,
        citation_integrity_pass=True,
        result_finding_integrity_pass=True,
        oracle_firewall_pass=True,
        duration_ms=1000 + n,
        model_calls=5,
        tool_calls=9,
        search_events_calls=9,
        total_tokens=1234,
    )


def _valid_fail(n: int, execution_id: str) -> AttemptResult:
    return AttemptResult(
        attempt_number=n,
        execution_id=execution_id,
        classification=CLASS_VALID_FAIL,
        execution_status="COMPLETED",
        model_gate=GATE_PASS,
        quality_gate=GATE_FAIL,
        correctness_gate=GATE_FAIL,
        score_artifact=f"exec/{execution_id}/score.json",
        verdict_match=False,
        required_evidence_pass=False,
        grounding_pass=False,
        control_exclusion_pass=True,
        citation_integrity_pass=True,
        result_finding_integrity_pass=True,
        oracle_firewall_pass=True,
    )


def _transient(n: int, execution_id: str) -> AttemptResult:
    return AttemptResult(
        attempt_number=n,
        execution_id=execution_id,
        classification=CLASS_INVALID_PROVIDER_TRANSIENT,
        execution_status="COMPLETED",
        model_gate=GATE_FAIL,
        quality_gate=GATE_FAIL,
        correctness_gate=GATE_FAIL,
    )


def _abort(n: int, execution_id: str, category: str = "MODEL_CONFIGURATION") -> AttemptResult:
    return AttemptResult(
        attempt_number=n,
        execution_id=execution_id,
        classification=CLASS_ABORT,
        execution_status="FAILED",
        model_gate=GATE_FAIL,
        quality_gate=GATE_FAIL,
        correctness_gate=GATE_FAIL,
        abort_category=category,
    )


def _runner(
    factory: Callable[[int], AttemptResult]
) -> Callable[[int], Awaitable[AttemptResult]]:
    async def run(attempt_number: int) -> AttemptResult:
        return factory(attempt_number)

    return run


async def _run_suite(
    settings: Settings, runner: Callable[[int], Awaitable[AttemptResult]], **kwargs: object
):
    return await execute_gp01_suite(
        dataset_run_id=RUN_ID,
        settings=settings,
        attempt_runner=runner,
        **kwargs,  # type: ignore[arg-type]
    )


# --- happy paths --------------------------------------------------------------


def test_three_passes_yield_suite_pass(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    seal_dataset(runs_dir=Path(settings.evaluation.runs_dir), dataset_run_id=RUN_ID)
    result = asyncio.run(
        _run_suite(
            settings,
            _runner(lambda n: _valid_pass(n, f"exec-{n}")),
            valid_runs=3,
            max_attempts=6,
        )
    )
    assert result.exit_code == 0
    summary = result.summary
    assert summary.suite_gate == GATE_PASS
    assert summary.total_attempts == 3
    assert summary.valid_attempts == 3
    assert summary.valid_passes == 3
    assert summary.valid_failures == 0
    assert summary.correctness_pass_rate == 1.0
    assert summary.gate_failures == ()


def test_pass_fail_pass_is_suite_fail(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    seal_dataset(runs_dir=Path(settings.evaluation.runs_dir), dataset_run_id=RUN_ID)
    pattern = {
        1: lambda n: _valid_pass(n, "exec-1"),
        2: lambda n: _valid_fail(n, "exec-2"),
        3: lambda n: _valid_pass(n, "exec-3"),
    }
    result = asyncio.run(
        _run_suite(settings, _runner(lambda n: pattern[n](n)), valid_runs=3, max_attempts=6)
    )
    summary = result.summary
    assert summary.suite_gate == GATE_FAIL
    assert summary.valid_attempts == 3
    assert summary.valid_passes == 2
    assert summary.valid_failures == 1
    assert summary.correctness_pass_rate == pytest.approx(2 / 3)
    assert SUITE_FAIL_VALID_FAILURES_PRESENT in summary.gate_failures
    # The valid failure is retained, never replaced.
    classifications = [a["classification"] for a in summary.attempts]
    assert classifications == [CLASS_VALID_PASS, CLASS_VALID_FAIL, CLASS_VALID_PASS]


def test_transient_then_three_passes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    seal_dataset(runs_dir=Path(settings.evaluation.runs_dir), dataset_run_id=RUN_ID)
    pattern = {
        1: lambda n: _transient(n, "exec-1"),
        2: lambda n: _valid_pass(n, "exec-2"),
        3: lambda n: _valid_pass(n, "exec-3"),
        4: lambda n: _valid_pass(n, "exec-4"),
    }
    result = asyncio.run(
        _run_suite(settings, _runner(lambda n: pattern[n](n)), valid_runs=3, max_attempts=6)
    )
    summary = result.summary
    assert summary.total_attempts == 4
    assert summary.valid_attempts == 3
    assert summary.invalid_provider_transient_attempts == 1
    assert summary.valid_passes == 3
    assert summary.suite_gate == GATE_PASS


def test_valid_failure_is_never_replaced(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    seal_dataset(runs_dir=Path(settings.evaluation.runs_dir), dataset_run_id=RUN_ID)
    # A failure first, then passes: the collector must still reach 3 valid samples and
    # keep the failed one.
    pattern = {
        1: lambda n: _valid_fail(n, "exec-1"),
        2: lambda n: _valid_pass(n, "exec-2"),
        3: lambda n: _valid_pass(n, "exec-3"),
    }
    result = asyncio.run(
        _run_suite(settings, _runner(lambda n: pattern[n](n)), valid_runs=3, max_attempts=6)
    )
    summary = result.summary
    assert summary.total_attempts == 3
    assert summary.valid_failures == 1
    assert summary.attempts[0]["classification"] == CLASS_VALID_FAIL
    assert summary.attempts[0]["execution_id"] == "exec-1"


def test_max_attempts_exhausted_is_insufficient_valid_runs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    seal_dataset(runs_dir=Path(settings.evaluation.runs_dir), dataset_run_id=RUN_ID)
    result = asyncio.run(
        _run_suite(
            settings,
            _runner(lambda n: _transient(n, f"exec-{n}")),
            valid_runs=3,
            max_attempts=3,
        )
    )
    summary = result.summary
    assert summary.total_attempts == 3
    assert summary.valid_attempts == 0
    assert summary.invalid_provider_transient_attempts == 3
    assert summary.suite_gate == GATE_FAIL
    assert SUITE_FAIL_INSUFFICIENT_VALID_RUNS in summary.gate_failures


def test_config_failure_aborts_immediately(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    seal_dataset(runs_dir=Path(settings.evaluation.runs_dir), dataset_run_id=RUN_ID)
    result = asyncio.run(
        _run_suite(
            settings,
            _runner(lambda n: _abort(n, f"exec-{n}")),
            valid_runs=3,
            max_attempts=6,
        )
    )
    summary = result.summary
    # Stopped after the first attempt — no retry-until-success.
    assert summary.total_attempts == 1
    assert summary.abort_category == "MODEL_CONFIGURATION"
    assert summary.suite_gate == GATE_FAIL


def test_all_attempts_and_ids_are_retained_distinctly(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    seal_dataset(runs_dir=Path(settings.evaluation.runs_dir), dataset_run_id=RUN_ID)
    pattern = {
        1: lambda n: _transient(n, "exec-a"),
        2: lambda n: _valid_pass(n, "exec-b"),
        3: lambda n: _valid_pass(n, "exec-c"),
        4: lambda n: _valid_pass(n, "exec-d"),
    }
    result = asyncio.run(
        _run_suite(settings, _runner(lambda n: pattern[n](n)), valid_runs=3, max_attempts=6)
    )
    summary = result.summary
    ids = [a["execution_id"] for a in summary.attempts]
    assert ids == ["exec-a", "exec-b", "exec-c", "exec-d"]
    assert len(set(ids)) == len(ids)
    assert summary.total_attempts == len(summary.attempts)


# --- preflight abort ----------------------------------------------------------


def test_missing_manifest_aborts_before_any_attempt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)  # NOTE: no manifest sealed
    result = asyncio.run(
        _run_suite(settings, _runner(lambda n: _valid_pass(n, f"exec-{n}")))
    )
    summary = result.summary
    assert summary.total_attempts == 0
    assert summary.abort_category == ABORT_PREFLIGHT_FAILED
    assert summary.suite_gate == GATE_FAIL


# --- efficiency aggregation (§7, §13) -----------------------------------------


def test_efficiency_is_informational_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    seal_dataset(runs_dir=Path(settings.evaluation.runs_dir), dataset_run_id=RUN_ID)
    result = asyncio.run(
        _run_suite(settings, _runner(lambda n: _valid_pass(n, f"exec-{n}")), valid_runs=3)
    )
    summary = result.summary
    assert summary.efficiency["duration_ms"] == {"min": 1001, "median": 1002, "max": 1003}
    assert summary.efficiency["tool_calls"] == {"min": 9, "median": 9, "max": 9}
    assert summary.efficiency["total_tokens"]["max"] == 1234


def test_efficiency_tokens_null_when_provider_reports_none(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    seal_dataset(runs_dir=Path(settings.evaluation.runs_dir), dataset_run_id=RUN_ID)
    result = asyncio.run(
        _run_suite(
            settings,
            _runner(
                lambda n: AttemptResult(
                    attempt_number=n,
                    execution_id=f"exec-{n}",
                    classification=CLASS_VALID_PASS,
                    execution_status="COMPLETED",
                    model_gate=GATE_PASS,
                    quality_gate=GATE_PASS,
                    correctness_gate=GATE_PASS,
                    verdict_match=True,
                    required_evidence_pass=True,
                    grounding_pass=True,
                    control_exclusion_pass=True,
                    citation_integrity_pass=True,
                    result_finding_integrity_pass=True,
                    oracle_firewall_pass=True,
                    total_tokens=None,
                )
            ),
            valid_runs=3,
        )
    )
    assert result.summary.efficiency["total_tokens"] is None
    assert result.summary.suite_gate == GATE_PASS


# --- artifact schema / uniqueness / secret boundary ---------------------------


def test_suite_summary_round_trip(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    seal_dataset(runs_dir=Path(settings.evaluation.runs_dir), dataset_run_id=RUN_ID)
    result = asyncio.run(
        _run_suite(settings, _runner(lambda n: _valid_pass(n, f"exec-{n}")), valid_runs=3)
    )
    path = suite_summary_path(
        settings.evaluation.executions_dir, RUN_ID, result.summary.suite_id
    )
    restored = read_suite_summary(path)
    assert restored.to_payload() == result.summary.to_payload()


def test_schema_version_constant() -> None:
    assert SUITE_SCHEMA_VERSION == "evaluation-suite-summary/v1"
    assert SuiteSummary().schema_version == SUITE_SCHEMA_VERSION


def test_unknown_schema_rejected() -> None:
    payload = SuiteSummary().to_payload()
    payload["schema_version"] = "evaluation-suite-summary/v99"
    with pytest.raises(SuiteSchemaError):
        SuiteSummary.from_payload(payload)


def test_missing_schema_rejected() -> None:
    payload = SuiteSummary().to_payload()
    del payload["schema_version"]
    with pytest.raises(SuiteSchemaError):
        SuiteSummary.from_payload(payload)


def test_unique_suite_artifact_per_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    seal_dataset(runs_dir=Path(settings.evaluation.runs_dir), dataset_run_id=RUN_ID)
    first = asyncio.run(
        _run_suite(settings, _runner(lambda n: _valid_pass(n, f"a-{n}")), valid_runs=3)
    )
    second = asyncio.run(
        _run_suite(settings, _runner(lambda n: _valid_pass(n, f"b-{n}")), valid_runs=3)
    )
    assert first.summary.suite_id != second.summary.suite_id
    assert first.suite_artifact != second.suite_artifact
    assert first.suite_artifact.is_file()
    assert second.suite_artifact.is_file()  # never overwrites the first


def test_secret_marker_flips_scan_and_gate(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    seal_dataset(runs_dir=Path(settings.evaluation.runs_dir), dataset_run_id=RUN_ID)
    # A secret-shaped execution id leaks into an attempt payload → the suite self-scan
    # must flag it (defense in depth for the artifact boundary).
    result = asyncio.run(
        _run_suite(
            settings,
            _runner(lambda n: _valid_pass(n, "sk-LEAKED")),
            valid_runs=1,
        )
    )
    summary = result.summary
    assert summary.secret_scan_pass is False
    assert SUITE_FAIL_SECRET_SCAN_VIOLATION in summary.gate_failures
    assert summary.suite_gate == GATE_FAIL


def test_clean_summary_never_leaks_secrets(tmp_path: Path) -> None:
    import json

    settings = _settings(tmp_path)
    seal_dataset(runs_dir=Path(settings.evaluation.runs_dir), dataset_run_id=RUN_ID)
    result = asyncio.run(
        _run_suite(settings, _runner(lambda n: _valid_pass(n, f"exec-{n}")), valid_runs=3)
    )
    text = json.dumps(result.summary.to_payload())
    for forbidden in (
        "api_key",
        "CMD_API_KEY",
        "Authorization",
        "Bearer",
        "password",
        "postgresql://",
        "sk-",
    ):
        assert forbidden not in text


def test_manual_write_and_read_round_trip(tmp_path: Path) -> None:
    summary = SuiteSummary(suite_id="s-1", scenario_id="GP-01", dataset_run_id=RUN_ID)
    path = suite_summary_path(tmp_path, RUN_ID, "s-1")
    write_suite_summary(path, summary)
    assert path.is_file()
    assert read_suite_summary(path).to_payload() == summary.to_payload()


# --- bounds validation (§8) ---------------------------------------------------


def test_default_bounds() -> None:
    assert DEFAULT_VALID_RUNS == 3
    assert DEFAULT_MAX_ATTEMPTS == 6


def test_validate_bounds_accepts_valid_ranges() -> None:
    validate_bounds(1, 1)
    validate_bounds(3, 6)
    validate_bounds(HARD_CAP, HARD_CAP)


@pytest.mark.parametrize(
    ("valid_runs", "max_attempts"),
    [
        (0, 6),
        (3, 0),
        (HARD_CAP + 1, HARD_CAP + 1),
        (4, 3),
    ],
)
def test_validate_bounds_rejects_invalid(valid_runs: int, max_attempts: int) -> None:
    with pytest.raises(ValueError):
        validate_bounds(valid_runs, max_attempts)
