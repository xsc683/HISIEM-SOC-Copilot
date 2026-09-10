"""E1-C5 attempt classification unit tests (§9, §10, §20).

The classifier must decide, from STABLE model error codes only (never HTTP status
or exception prose), whether an attempt is a semantic sample (VALID_PASS/FAIL, counts
toward valid_runs), a provider-transient attempt (INVALID_PROVIDER_TRANSIENT,
preserved but never counted), or a suite-ABORTING condition.
"""

from __future__ import annotations

from hisiem_soc_copilot.evaluation_harness.classification import (
    ABORT_MODEL_CONFIGURATION,
    ABORT_MODEL_TELEMETRY_MISSING,
    ALL_CLASSIFICATIONS,
    CLASS_ABORT,
    CLASS_INVALID_PROVIDER_TRANSIENT,
    CLASS_VALID_FAIL,
    CLASS_VALID_PASS,
    AttemptClassification,
    classify_attempt,
)
from hisiem_soc_copilot.evaluation_harness.quality import GATE_FAIL, GATE_PASS


def _error(category: str) -> dict[str, object]:
    return {"outcome": "error", "error_category": category}


def _ok() -> dict[str, object]:
    return {"outcome": "ok", "error_category": None}


def _classify(**overrides: object) -> AttemptClassification:
    kwargs: dict[str, object] = {
        "execution_failed": False,
        "failure_category": "",
        "telemetry_gate": GATE_FAIL,
        "telemetry_gate_failures": ("MISSING_SUCCESSFUL_PLAN",),
        "usage_records": (),
        "correctness_gate": GATE_FAIL,
    }
    kwargs.update(overrides)
    return classify_attempt(**kwargs)  # type: ignore[arg-type]


# --- provider-transient (§10) -------------------------------------------------


def test_model_timeout_is_invalid_provider_transient() -> None:
    result = _classify(usage_records=(_error("MODEL_TIMEOUT"),))
    assert result.classification == CLASS_INVALID_PROVIDER_TRANSIENT
    assert result.counts_as_valid is False
    assert result.is_abort is False


def test_model_unavailable_is_invalid_provider_transient() -> None:
    result = _classify(usage_records=(_error("MODEL_UNAVAILABLE"),))
    assert result.classification == CLASS_INVALID_PROVIDER_TRANSIENT


def test_model_rate_limited_is_invalid_provider_transient() -> None:
    result = _classify(usage_records=(_error("MODEL_RATE_LIMITED"),))
    assert result.classification == CLASS_INVALID_PROVIDER_TRANSIENT


def test_structured_output_unresolved_is_transient_explainable() -> None:
    result = _classify(
        telemetry_gate_failures=("STRUCTURED_OUTPUT_MODE_UNRESOLVED",),
        usage_records=(_error("MODEL_TIMEOUT"),),
    )
    assert result.classification == CLASS_INVALID_PROVIDER_TRANSIENT


def test_transient_codes_but_unexplained_gate_failure_is_valid_fail() -> None:
    # A gate failure that a transport outage cannot explain → ambiguous → VALID FAIL.
    result = _classify(
        telemetry_gate_failures=("SOME_UNKNOWN_GATE_TOKEN",),
        usage_records=(_error("MODEL_TIMEOUT"),),
    )
    assert result.classification == CLASS_VALID_FAIL


def test_transient_codes_but_no_failed_usage_is_valid_fail() -> None:
    # No actual failed model usage → the transient predicate cannot hold (§10).
    result = _classify(
        telemetry_gate_failures=("MISSING_SUCCESSFUL_PLAN",),
        usage_records=(_ok(),),
    )
    assert result.classification == CLASS_VALID_FAIL


# --- deterministic model behaviour → VALID failure (§9) -----------------------


def test_model_refusal_is_valid_failure() -> None:
    result = _classify(usage_records=(_error("MODEL_REFUSAL"),))
    assert result.classification == CLASS_VALID_FAIL
    assert result.counts_as_valid is True
    assert result.is_abort is False


def test_model_output_validation_is_valid_failure() -> None:
    result = _classify(usage_records=(_error("MODEL_OUTPUT_VALIDATION"),))
    assert result.classification == CLASS_VALID_FAIL


def test_mixed_transient_and_deterministic_is_valid_fail() -> None:
    result = _classify(
        usage_records=(_error("MODEL_TIMEOUT"), _error("MODEL_REFUSAL"))
    )
    assert result.classification == CLASS_VALID_FAIL


# --- abort conditions (§9/§20) ------------------------------------------------


def test_model_configuration_aborts() -> None:
    result = _classify(usage_records=(_error("MODEL_CONFIGURATION"),))
    assert result.classification == CLASS_ABORT
    assert result.abort_category == ABORT_MODEL_CONFIGURATION


def test_provider_contract_mismatch_aborts() -> None:
    result = _classify(telemetry_gate_failures=("UNEXPECTED_MODEL",))
    assert result.classification == CLASS_ABORT
    assert result.abort_category == "PROVIDER_CONTRACT_MISMATCH"


def test_execution_failed_aborts() -> None:
    result = _classify(
        execution_failed=True, failure_category="CAT_START_FAILED"
    )
    assert result.classification == CLASS_ABORT
    assert result.abort_category == "CAT_START_FAILED"


def test_missing_telemetry_aborts() -> None:
    result = _classify(telemetry_gate=None)
    assert result.classification == CLASS_ABORT
    assert result.abort_category == ABORT_MODEL_TELEMETRY_MISSING


# --- semantic pass/fail when the full contract held ---------------------------


def test_gate_pass_and_correctness_pass_is_valid_pass() -> None:
    result = _classify(telemetry_gate=GATE_PASS, correctness_gate=GATE_PASS)
    assert result.classification == CLASS_VALID_PASS
    assert result.counts_as_valid is True


def test_gate_pass_and_no_s1_is_valid_failure() -> None:
    # E1-C2 PASS but the tool/evidence gate FAIL (no S1) → VALID failure sample.
    result = _classify(telemetry_gate=GATE_PASS, correctness_gate=GATE_FAIL)
    assert result.classification == CLASS_VALID_FAIL


def test_gate_pass_and_wrong_verdict_is_valid_failure() -> None:
    result = _classify(telemetry_gate=GATE_PASS, correctness_gate=GATE_FAIL)
    assert result.classification == CLASS_VALID_FAIL
    assert result.counts_as_valid is True


def test_all_classifications_constant() -> None:
    assert set(ALL_CLASSIFICATIONS) == {
        CLASS_VALID_PASS,
        CLASS_VALID_FAIL,
        CLASS_INVALID_PROVIDER_TRANSIENT,
        CLASS_ABORT,
    }
