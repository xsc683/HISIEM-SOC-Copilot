"""OpenAI-compatible ModelProvider for the Command Code API.

Layering (docs/model-provider-contract.md §2):
    Graph/Application → ModelProvider Protocol
    OpenAICompatibleModelProvider (this adapter) → AsyncOpenAI → Command Code API

The adapter is the ONLY place that speaks to the OpenAI SDK (agent/application never
import it). Responsibilities:

- build an ``AsyncOpenAI`` client (api_key from ``CMD_API_KEY``, base_url, timeout),
  with the SDK's OWN retry disabled so total retry count is controlled here;
- set the ZDR header (``x-cmd-zdr: 1``) when ``llm.zdr`` is enabled;
- structured output: in ``auto`` mode probe ``response_format=json_schema`` first and
  downgrade to ``json_object`` then JSON-only prompt when the provider rejects the
  format (the decision is cached per process, so only the first call probes);
- apply a bounded retry ONLY to transient failures (timeout / connection / 429 /
  retryable 5xx) — never to refusal, schema-invalid output, auth, or config errors;
- translate every SDK/HTTP/Command Code error into the provider-neutral taxonomy
  (contracts/llm/errors.py);
- emit bounded usage metadata (never the key, the full prompt, a raw response, or
  chain-of-thought).

The openai SDK is a project dependency; the import is deferred to construction time
so merely importing this module never requires the SDK. ScriptedModelProvider remains
the default for tests.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ...application.ports.model_provider import (
    AssessRequest,
    DecideNextRequest,
    PlanRequest,
)
from ...contracts.llm.errors import (
    ModelConfigurationError,
    ModelOutputValidationError,
    ModelProviderError,
    ModelRateLimitedError,
    ModelRefusalError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from ...contracts.llm.types import (
    AssessmentSummary,
    InvestigationPlan,
    NextStep,
    VerdictCandidate,
)
from ..llm.prompts import assess as assess_prompt
from ..llm.prompts import decide as decide_prompt
from ..llm.prompts import plan as plan_prompt
from ..llm.prompts import verdict as verdict_prompt
from ..llm.schemas import (
    AssessmentOutput,
    NextStepOutput,
    PlanOutput,
    VerdictOutput,
    parse_wire,
    to_assessment,
    to_next_step,
    to_plan,
    to_verdict,
)

_PROVIDER = "command_code"
_PROTOCOL = "openai_compatible_chat_completions"
_ZDR_HEADER = "x-cmd-zdr"
_BACKOFF_STEP_SECONDS = (1.0, 2.0)
_MODE_ORDER = ("auto", "json_schema", "json_object", "json_only")


@dataclass(frozen=True)
class ModelUsage:
    """Bounded usage/outcome metadata for ONE model call (operational, not domain).

    Fields the provider did not report stay ``None`` — never guessed. No key, no
    prompt, no raw response, no chain-of-thought ever appears here.
    """

    provider: str = _PROVIDER
    protocol: str = _PROTOCOL
    model: str = ""
    operation: str = ""
    provider_request_id: str | None = None
    latency_ms: int | None = None
    attempt_count: int = 1
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    outcome: str = ""
    error_category: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "protocol": self.protocol,
            "model": self.model,
            "operation": self.operation,
            "provider_request_id": self.provider_request_id,
            "latency_ms": self.latency_ms,
            "attempt_count": self.attempt_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "outcome": self.outcome,
            "error_category": self.error_category,
        }


class OpenAICompatibleModelProvider:
    """OpenAI Chat Completions adapter implementing the ModelProvider Protocol."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key_env: str = "CMD_API_KEY",
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        zdr: bool = True,
        structured_output_mode: str = "auto",
        client_factory: Any = None,
    ) -> None:
        if not model:
            raise ModelConfigurationError("llm.model must not be empty")
        if max_retries < 0:
            raise ModelConfigurationError("llm.max_retries must be >= 0")
        self._model = model
        self._timeout_seconds = float(timeout_seconds)
        self._max_retries = int(max_retries)
        self._zdr = bool(zdr)
        self._structured_output_mode = structured_output_mode
        if structured_output_mode not in _MODE_ORDER:
            raise ModelConfigurationError(
                "llm.structured_output_mode must be one of "
                f"{'/'.join(_MODE_ORDER)}, got {structured_output_mode!r}"
            )
        self._client_factory = client_factory
        self._api_key = _load_api_key(api_key_env)
        self._base_url = base_url
        self._client = self._build_client()
        # Capability cache: None = untested, else the current best mode.
        self._mode: str | None = None
        # Bounded in-memory usage records (operational metadata only).
        self.usage: list[ModelUsage] = []

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build_client(self) -> Any:
        headers = {_ZDR_HEADER: "1"} if self._zdr else None
        if self._client_factory is not None:
            return self._client_factory(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                max_retries=0,  # SDK retry disabled — this adapter owns retrying.
                default_headers=headers,
            )
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - guarded import
            raise ModelConfigurationError(
                "the openai SDK is not installed; cannot build the OpenAI-compatible "
                "provider"
            ) from exc
        return AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            max_retries=0,  # SDK retry disabled — this adapter owns retrying.
            default_headers=headers,
        )

    # ------------------------------------------------------------------
    # ModelProvider protocol
    # ------------------------------------------------------------------
    async def plan(self, request: PlanRequest) -> InvestigationPlan:
        wire = await self._complete(
            operation="plan",
            messages=plan_prompt.build_messages(request),
            schema=plan_prompt.strict_schema(),
            wire_model=PlanOutput,
        )
        return to_plan(wire)

    async def decide_next(self, request: DecideNextRequest) -> NextStep:
        wire = await self._complete(
            operation="decide",
            messages=decide_prompt.build_messages(request),
            schema=decide_prompt.strict_schema(),
            wire_model=NextStepOutput,
        )
        return to_next_step(wire)

    async def assess(self, request: AssessRequest) -> AssessmentSummary:
        wire = await self._complete(
            operation="assess",
            messages=assess_prompt.build_messages(request),
            schema=assess_prompt.strict_schema(),
            wire_model=AssessmentOutput,
        )
        return to_assessment(wire)

    async def verdict(self, request: AssessRequest) -> VerdictCandidate:
        wire = await self._complete(
            operation="verdict",
            messages=verdict_prompt.build_messages(request),
            schema=verdict_prompt.strict_schema(),
            wire_model=VerdictOutput,
        )
        return to_verdict(wire)

    # ------------------------------------------------------------------
    # one bounded completion (transient retry loop)
    # ------------------------------------------------------------------
    async def _complete(
        self,
        *,
        operation: str,
        messages: list[dict[str, str]],
        schema: dict[str, object],
        wire_model: type[Any],
    ) -> Any:
        attempt = 0
        while True:
            attempt += 1
            started = time.monotonic()
            try:
                raw = await self._request_structured(operation, messages, schema)
            except ModelProviderError as exc:
                # Transient taxonomy errors (timeout/connection/429/retryable-5xx)
                # are retried up to max_retries; deterministic errors are not.
                self._record_failure(operation, exc, attempt)
                if exc.retryable and attempt <= self._max_retries:
                    await asyncio.sleep(_bounded_backoff(attempt))
                    continue
                raise exc
            latency_ms = int((time.monotonic() - started) * 1000)
            try:
                wire = parse_wire(raw, wire_model)
            except ModelOutputValidationError as exc:
                # Deterministic: never retried. The runtime applies its fallback.
                self._record_failure(operation, exc, attempt, latency_ms=latency_ms)
                raise
            self._record_success(operation, latency_ms=latency_ms)
            return wire

    async def _request_structured(
        self,
        operation: str,
        messages: list[dict[str, str]],
        schema: dict[str, object],
    ) -> str:
        """One Chat Completions call with the structured-output format ladder.

        Walks json_schema → json_object → json_only, downgrading ONLY when the
        provider rejects the requested ``response_format``. A transient SDK error
        (network/timeout/429/5xx) is raised for the outer bounded-retry loop; a
        deterministic refusal/parse is raised as-is.
        """
        modes = self._ladder()
        for mode in modes:
            try:
                content = await self._create_in_mode(mode, operation, messages, schema)
            except ModelProviderError as exc:
                if _is_format_rejection(exc) and mode != modes[-1]:
                    # Provider does not honor this format → try the next one.
                    continue
                raise
            if content is None:
                raise ModelOutputValidationError(
                    "provider returned a completion with no content",
                    code="EMPTY_COMPLETION",
                )
            # Remember the working mode so later calls skip the ladder.
            self._mode = mode
            return content
        raise ModelOutputValidationError(
            "no structured-output mode was accepted by the provider",
            code="STRUCTURED_OUTPUT_UNAVAILABLE",
        )

    def _ladder(self) -> list[str]:
        """The ordered modes to try, honoring config pin + the capability cache."""
        mode = self._structured_output_mode
        if mode != "auto":
            return [mode]
        if self._mode is not None:
            return [self._mode]
        return ["json_schema", "json_object", "json_only"]

    async def _create_in_mode(
        self,
        mode: str,
        operation: str,
        messages: list[dict[str, str]],
        schema: dict[str, object],
    ) -> str | None:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.0,
        }
        used_response_format = False
        if mode == "json_schema":
            used_response_format = True
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": _schema_name(operation),
                    "strict": True,
                    "schema": schema,
                },
            }
        elif mode == "json_object":
            used_response_format = True
            kwargs["response_format"] = {"type": "json_object"}
        else:  # json_only — no response_format; the prompt already says JSON-only.
            pass
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            mapped = _map_sdk_error(exc)
            if used_response_format and _is_format_refusal(mapped):
                # Provider does not honor this response_format (a 400 refusal raised
                # against a response_format request) → the ladder downgrades to the
                # next mode in the SAME call.
                raise _FormatRejectedError(str(mapped)) from exc
            raise mapped from exc
        return _extract_content(response)

    # ------------------------------------------------------------------
    # bounded usage records (never secrets/prompts/raw responses)
    # ------------------------------------------------------------------
    def _record_success(self, operation: str, *, latency_ms: int | None) -> None:
        self.usage.append(
            ModelUsage(
                model=self._model, operation=operation,
                latency_ms=latency_ms, outcome="ok",
            )
        )

    def _record_failure(
        self,
        operation: str,
        exc: ModelProviderError,
        attempt: int,
        *,
        latency_ms: int | None = None,
    ) -> None:
        self.usage.append(
            ModelUsage(
                model=self._model, operation=operation, latency_ms=latency_ms,
                attempt_count=attempt, outcome="error", error_category=exc.code,
            )
        )


class _TransientModelError(ModelProviderError):
    """Internal marker: a transient failure the bounded retry loop may re-attempt."""

    retryable = True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _schema_name(operation: str) -> str:
    return {
        "plan": "plan_output",
        "decide": "next_step_output",
        "assess": "assessment_output",
        "verdict": "verdict_output",
    }.get(operation, "structured_output")


def _extract_content(response: Any) -> str | None:
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return None
    return content if isinstance(content, str) and content.strip() else None


def _load_api_key(api_key_env: str) -> str:
    value = os.environ.get(api_key_env) or ""
    if not value:
        raise ModelConfigurationError(
            f"API key environment variable {api_key_env} is not set; refusing to "
            "build a real model client without credentials"
        )
    return value


def _bounded_backoff(attempt: int) -> float:
    index = min(attempt - 1, len(_BACKOFF_STEP_SECONDS) - 1)
    return float(_BACKOFF_STEP_SECONDS[index])


def _is_format_refusal(mapped: ModelProviderError) -> bool:
    """True when a provider error is a 400 against a requested response_format.

    In ``auto`` the ladder is allowed to downgrade on such a refusal; a genuine
    401/403 (auth) or a transient error is never treated as format-unsupported.
    """
    return isinstance(mapped, ModelRefusalError) and "HTTP 400" in mapped.message


def _is_format_rejection(exc: ModelProviderError) -> bool:
    """True when the exception is a structured-output format rejection.

    ``_create_in_mode`` converts a provider 400 against a requested
    ``response_format`` into ``_FormatRejectedError``; the ladder downgrades on that.
    A genuine refusal (401/403/content) is mapped to ``ModelRefusalError`` and is
    never treated as a format rejection — it propagates as a deterministic refusal.
    """
    return isinstance(exc, _FormatRejectedError)


def _map_sdk_error(exc: Exception) -> ModelProviderError:
    """Translate an SDK/HTTP/Command Code exception into the typed taxonomy.

    Transient buckets (timeout / connection / 429 / retryable 5xx) map to the
    concrete retryable types so the outer bounded-retry loop re-attempts them;
    deterministic client errors become refusal/config errors that are never retried.
    """
    name = type(exc).__name__.lower()
    raw_status = getattr(exc, "status_code", None)
    status: int | None = None
    try:
        status = int(raw_status) if raw_status is not None else None
    except (TypeError, ValueError):
        status = None

    if _is_timeout(name, exc):
        return ModelTimeoutError("model provider call timed out")
    if _is_connection(name):
        return ModelUnavailableError("model provider is unreachable")
    if status == 429:
        return ModelRateLimitedError("model provider rate-limited the request")
    if status is not None and 500 <= status < 600:
        return ModelUnavailableError(f"model provider returned HTTP {status}")
    if status is not None and 400 <= status < 500:
        # A provider 400 that names the requested response_format is a deterministic
        # "format unsupported" — the ladder downgrades and retries a lower format in
        # the SAME call, and it is never re-attempted by the outer retry loop.
        detail = f"model provider refused the request (HTTP {status})"
        if _mentions_format(detail + _bounded_exc_text(exc)):
            return _FormatRejectedError(detail)
        return ModelRefusalError(detail)
    message = str(exc)
    if "auth" in name or "authentication" in message.lower():
        return ModelConfigurationError(
            "model provider authentication/configuration error"
        )
    if "refus" in message.lower() or "content_filter" in message.lower():
        return ModelRefusalError("model provider refused the request")
    # Generic unexpected transport failure → transient (bounded retry).
    return ModelUnavailableError("model provider call failed")


def _bounded_exc_text(exc: Exception) -> str:
    """A bounded, key-safe excerpt of an SDK error for format detection."""
    return str(exc)[:200]


def _mentions_format(text: str) -> bool:
    lowered = text.lower()
    return any(m in lowered for m in ("json_schema", "response_format", "json schema"))


class _FormatRejectedError(ModelRefusalError):
    """Private marker: the provider rejected the requested structured-output format.

    Raised only inside ``_map_sdk_error`` for a provider 400 that names
    json_schema / response_format. The format ladder downgrades to the next mode;
    it is never re-attempted by the outer transient-retry loop (retryable=False).
    """


def _is_timeout(name: str, exc: Exception) -> bool:
    return "timeout" in name or isinstance(exc, httpx.TimeoutException)


def _is_connection(name: str) -> bool:
    return any(m in name for m in ("connection", "connect_error", "apiconnection"))

