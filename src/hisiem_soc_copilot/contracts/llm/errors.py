"""Provider-neutral model errors — the ONLY failures a ModelProvider may raise.

The agent/graph catches these typed errors (never SDK/HTTP exceptions) to apply the
deterministic runtime fallbacks (plan default / decide finalize / assess UNRESOLVED
/ verdict INCONCLUSIVE). They live in ``contracts`` so both ``application`` and
``agent`` (and any future provider) can reference the taxonomy without importing an
SDK or ``infrastructure`` (python-package-boundary.md).

Every error is BOUNDED: the ``message`` is a stable, analyst/runtime-safe summary —
it never carries an API key, Authorization header, prompt text, raw model response,
or chain-of-thought.
"""

from __future__ import annotations


class ModelProviderError(Exception):
    """Base for all model-provider failures (transient and deterministic).

    ``code`` is a stable machine category used by telemetry / retry decisions;
    ``retryable`` marks only transient transport/limit failures that the adapter's
    bounded retry may re-attempt.
    """

    code = "MODEL_PROVIDER_ERROR"
    retryable = False

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class ModelUnavailableError(ModelProviderError):
    """The provider/service is unreachable (connection refused, DNS, retryable 5xx).

    Transient: bounded retries apply; after exhaustion the runtime converges with
    the deterministic fallback (never FAILED).
    """

    code = "MODEL_UNAVAILABLE"
    retryable = True


class ModelRateLimitedError(ModelProviderError):
    """The provider returned a rate-limit (429) or quota signal.

    Transient: bounded retries apply (with backoff). After exhaustion the runtime
    converges with the deterministic fallback.
    """

    code = "MODEL_RATE_LIMITED"
    retryable = True


class ModelTimeoutError(ModelProviderError):
    """The provider call exceeded the configured timeout.

    Transient: bounded retries apply. After exhaustion the runtime converges with
    the deterministic fallback.
    """

    code = "MODEL_TIMEOUT"
    retryable = True


class ModelRefusalError(ModelProviderError):
    """The provider refused the request deterministically (content/policy refusal or
    a non-retryable client error).

    Deterministic: NEVER retried. The runtime converges with the deterministic
    fallback.
    """

    code = "MODEL_REFUSAL"
    retryable = False


class ModelOutputValidationError(ModelProviderError):
    """The provider's output failed strict validation.

    Covers: empty completion, truncated/incomplete JSON, invalid JSON, schema
    mismatch, missing required field, wrong enum, invalid numeric range, and any
    model text that cannot be mapped to a provider-neutral candidate. NEVER repaired
    or guessed — always this typed failure.
    """

    code = "MODEL_OUTPUT_VALIDATION"
    retryable = False


class ModelConfigurationError(ModelProviderError):
    """Provider/client configuration is invalid (missing key, bad base URL, unknown
    model, unsupported option).

    Deterministic: NEVER retried and never silently defaulted — configuration
    problems must surface, not hide.
    """

    code = "MODEL_CONFIGURATION"
    retryable = False
