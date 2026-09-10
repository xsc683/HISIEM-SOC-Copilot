"""E1-C5 attempt classification (evaluation-only, pure).

A bounded multi-run collector must decide, for EACH provider attempt, whether the
attempt is a SEMANTIC sample (counts toward the required valid runs, whether it
PASSes or FAILs) or a PROVIDER-TRANSIENT attempt (preserved + reported but never
counted, replaceable only within ``max_attempts``) or a suite-ABORTING condition.

The predicate is built ONLY on stable model error codes — never on HTTP status
numbers or exception prose (§9/§10). The code taxonomy comes from the provider's
own usage records (``error_category``) and the E1-C2 telemetry gate failure tokens.

Rules (§9/§10):

- A provider-transient attempt is ``INVALID_PROVIDER_TRANSIENT`` ONLY when ALL hold:
  the provider baseline/config is valid; there IS actual failed model usage; EVERY
  relevant failed usage's ``error_category`` is one of MODEL_UNAVAILABLE /
  MODEL_RATE_LIMITED / MODEL_TIMEOUT; NO MODEL_CONFIGURATION / MODEL_REFUSAL /
  MODEL_OUTPUT_VALIDATION / provider-contract mismatch occurred; and the E1-C2 gate
  failure is explainable by those transient failures.
- MODEL_CONFIGURATION, a provider-contract/baseline mismatch, a dataset/runtime
  preflight failure, or an unexpected infra/runtime failure → ABORT the suite (no
  retry-until-success).
- MODEL_REFUSAL / MODEL_OUTPUT_VALIDATION / any non-transient model behaviour, a
  wrong verdict, a missing S1, a missing grounded Finding, a tool failure, or a
  quality failure → a VALID failure sample (counts toward ``valid_runs``).
- Ambiguous (e.g. a mix of transient + deterministic errors, or a gate failure not
  explainable by transients) → VALID failure (fail conservative).
"""

from __future__ import annotations

from dataclasses import dataclass

# Attempt classifications.
CLASS_VALID_PASS = "VALID_PASS"
CLASS_VALID_FAIL = "VALID_FAIL"
CLASS_INVALID_PROVIDER_TRANSIENT = "INVALID_PROVIDER_TRANSIENT"
CLASS_ABORT = "ABORT"

ALL_CLASSIFICATIONS: tuple[str, ...] = (
    CLASS_VALID_PASS,
    CLASS_VALID_FAIL,
    CLASS_INVALID_PROVIDER_TRANSIENT,
    CLASS_ABORT,
)

# Stable model error codes (mirrors contracts/llm/errors.py — never HTTP/message).
TRANSIENT_MODEL_CODES: frozenset[str] = frozenset(
    {"MODEL_UNAVAILABLE", "MODEL_RATE_LIMITED", "MODEL_TIMEOUT"}
)
# Non-transient model codes: a MODEL_CONFIGURATION is an operator/config fault that
# must ABORT the suite (never retried); a MODEL_REFUSAL / MODEL_OUTPUT_VALIDATION is
# a genuine model behaviour → a VALID failure sample (§9/§20).
ABORT_MODEL_CODES: frozenset[str] = frozenset({"MODEL_CONFIGURATION"})
VALID_FAIL_MODEL_CODES: frozenset[str] = frozenset(
    {"MODEL_REFUSAL", "MODEL_OUTPUT_VALIDATION"}
)

# E1-C2 telemetry gate failures that prove the provider contract/baseline is NOT
# valid → ABORT (never a transient retry).
CONTRACT_GATE_FAILURES: frozenset[str] = frozenset(
    {
        "UNEXPECTED_PROVIDER_ADAPTER",
        "UNEXPECTED_PROVIDER",
        "UNEXPECTED_PROTOCOL",
        "UNEXPECTED_MODEL",
        "ZDR_DISABLED",
        "UNEXPECTED_STRUCTURED_OUTPUT_CONFIG",
        "MODEL_CONFIGURATION_FAILURE",
    }
)

# Telemetry gate failures that a provider-transient outage CAN explain: a required
# operation never completed successfully, or the structured-output mode never
# resolved because every attempt failed at the transport layer.
_TRANSIENT_EXPLAINABLE_EXACT: frozenset[str] = frozenset(
    {"STRUCTURED_OUTPUT_MODE_UNRESOLVED"}
)
_MISSING_SUCCESSFUL_PREFIX = "MISSING_SUCCESSFUL_"

ABORT_EXECUTION_FAILED = "EXECUTION_FAILED"
ABORT_MODEL_TELEMETRY_MISSING = "MODEL_TELEMETRY_MISSING"
ABORT_PROVIDER_CONTRACT_MISMATCH = "PROVIDER_CONTRACT_MISMATCH"
ABORT_MODEL_CONFIGURATION = "MODEL_CONFIGURATION"


@dataclass(frozen=True)
class AttemptClassification:
    """The classification of ONE attempt + a bounded abort category (ABORT only)."""

    classification: str
    abort_category: str | None = None

    @property
    def counts_as_valid(self) -> bool:
        return self.classification in (CLASS_VALID_PASS, CLASS_VALID_FAIL)

    @property
    def is_abort(self) -> bool:
        return self.classification == CLASS_ABORT


def _transient_explainable(gate_failures: tuple[str, ...]) -> bool:
    """True when EVERY gate failure is explainable by a transport-layer outage."""
    for failure in gate_failures:
        if failure in _TRANSIENT_EXPLAINABLE_EXACT:
            continue
        if failure.startswith(_MISSING_SUCCESSFUL_PREFIX):
            continue
        return False
    return True


def _failed_error_categories(
    usage_records: tuple[dict[str, object], ...],
) -> set[str]:
    """The set of ``error_category`` values on FAILED usage records (None dropped)."""
    categories: set[str] = set()
    for record in usage_records:
        if record.get("outcome") != "error":
            continue
        category = record.get("error_category")
        if isinstance(category, str) and category:
            categories.add(category)
    return categories


def classify_attempt(
    *,
    execution_failed: bool,
    failure_category: str,
    telemetry_gate: str | None,
    telemetry_gate_failures: tuple[str, ...],
    usage_records: tuple[dict[str, object], ...],
    correctness_gate: str,
) -> AttemptClassification:
    """Classify ONE attempt from stable, bounded facts (§9/§10). Pure.

    ``telemetry_gate`` is ``None`` when no telemetry artifact exists. ``correctness_gate``
    is the E1-C4 gate for the attempt (``PASS``/``FAIL``); it only distinguishes
    VALID_PASS from VALID_FAIL — the classification itself is driven by the model
    telemetry + execution facts.
    """
    # 1. An execution-level FAILED record is a dataset/runtime/config/infra failure
    #    (a provider outage degrades to COMPLETED, never a FAILED record) → ABORT.
    if execution_failed:
        return AttemptClassification(
            CLASS_ABORT, abort_category=failure_category or ABORT_EXECUTION_FAILED
        )

    # 2. A COMPLETED execution MUST carry telemetry for a meaningful score.
    if telemetry_gate is None:
        return AttemptClassification(CLASS_ABORT, abort_category=ABORT_MODEL_TELEMETRY_MISSING)

    # 3. The full provider contract held → a real semantic sample.
    if telemetry_gate == "PASS":
        classification = (
            CLASS_VALID_PASS if correctness_gate == "PASS" else CLASS_VALID_FAIL
        )
        return AttemptClassification(classification)

    # 4. Provider-contract/baseline mismatch → ABORT (config never retried).
    if any(f in CONTRACT_GATE_FAILURES for f in telemetry_gate_failures):
        return AttemptClassification(
            CLASS_ABORT, abort_category=ABORT_PROVIDER_CONTRACT_MISMATCH
        )

    categories = _failed_error_categories(usage_records)

    # 5. A MODEL_CONFIGURATION anywhere is an operator/config fault → ABORT the
    #    suite (config is never retried — §9/§20).
    if categories & ABORT_MODEL_CODES:
        return AttemptClassification(
            CLASS_ABORT, abort_category=ABORT_MODEL_CONFIGURATION
        )

    # 6. A non-transient model behaviour (refusal / output-validation) → a VALID
    #    (semantic) failure sample.
    if categories & VALID_FAIL_MODEL_CODES:
        return AttemptClassification(CLASS_VALID_FAIL)

    # 7. The transient predicate requires ACTUAL failed model usage whose codes are
    #    ALL transient AND a gate failure fully explainable by the outage.
    if (
        categories
        and categories <= TRANSIENT_MODEL_CODES
        and _transient_explainable(telemetry_gate_failures)
    ):
        return AttemptClassification(CLASS_INVALID_PROVIDER_TRANSIENT)

    # 8. Ambiguous (no failed usage / unknown codes / unexplained gate failure) →
    #    VALID failure (fail conservative).
    return AttemptClassification(CLASS_VALID_FAIL)
