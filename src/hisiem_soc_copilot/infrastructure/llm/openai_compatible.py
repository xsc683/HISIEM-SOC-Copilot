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
  format (the decision is cached per process, so only the first call probes). The
  json_only mode REBUILDS the messages via the operation's prompt builder with
  ``json_only=True`` so a clear ONLY-JSON instruction is present (never the
  json_only=False messages);
- collect REAL bounded usage from each chat completion (request id + tokens when the
  provider reports them — missing → None, never guessed) into a bounded buffer;
- apply a bounded retry ONLY to transient failures (timeout / connection / 429 /
  retryable 5xx) — never to refusal, schema-invalid output, auth, or config errors;
- translate every SDK/HTTP/Command Code error into the provider-neutral taxonomy
  (contracts/llm/errors.py). 401/403 → ModelConfigurationError (a deployment bug:
  never retried, never silently downgraded); genuine content refusal →
  ModelRefusalError; a 400 against a requested response_format is a capability
  fallback, not a config error;
- never store the API key, the full prompt, a raw response, or chain-of-thought.

The openai SDK is a project dependency; the import is deferred to construction time
so merely importing this module never requires the SDK. ScriptedModelProvider remains
the default for tests.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from collections.abc import Callable
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
_USAGE_MAXLEN = 200


@dataclass(frozen=True)
class ProviderCompletion:
    """The bounded result of ONE chat completion.

    ``content`` is the assistant text; ``provider_request_id`` / token counts are
    read from the REAL provider response when present, else ``None`` (never guessed).
    """

    content: str | None
    provider_request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


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


MessageBuilder = Callable[..., list[dict[str, str]]]


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
        # Bounded usage buffer (operational metadata only; never unbounded).
        self.usage: deque[ModelUsage] = deque(maxlen=_USAGE_MAXLEN)

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
            builder=plan_prompt.build_messages,
            request=request,
            schema=plan_prompt.strict_schema(),
            wire_model=PlanOutput,
        )
        return to_plan(wire)

    async def decide_next(self, request: DecideNextRequest) -> NextStep:
        wire = await self._complete(
            operation="decide",
            builder=decide_prompt.build_messages,
            request=request,
            schema=decide_prompt.strict_schema(),
            wire_model=NextStepOutput,
        )
        return to_next_step(wire)

    async def assess(self, request: AssessRequest) -> AssessmentSummary:
        wire = await self._complete(
            operation="assess",
            builder=assess_prompt.build_messages,
            request=request,
            schema=assess_prompt.strict_schema(),
            wire_model=AssessmentOutput,
        )
        return to_assessment(wire)

    async def verdict(self, request: AssessRequest) -> VerdictCandidate:
        wire = await self._complete(
            operation="verdict",
            builder=verdict_prompt.build_messages,
            request=request,
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
        builder: MessageBuilder,
        request: Any,
        schema: dict[str, object],
        wire_model: type[Any],
    ) -> Any:
        attempt = 0
        while True:
            attempt += 1
            started = time.monotonic()
            try:
                completion = await self._request_structured(
                    operation, builder, request, schema
                )
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
                wire = parse_wire(completion.content or "", wire_model)
            except ModelOutputValidationError as exc:
                # Deterministic: never retried. The runtime applies its fallback.
                self._record_failure(operation, exc, attempt, latency_ms=latency_ms)
                raise
            self._record_success(operation, completion, latency_ms=latency_ms)
            return wire

    async def _request_structured(
        self,
        operation: str,
        builder: MessageBuilder,
        request: Any,
        schema: dict[str, object],
    ) -> ProviderCompletion:
        """One Chat Completions call with the structured-output format ladder.

        Walks json_schema → json_object → json_only, downgrading ONLY when the
        provider rejects the requested ``response_format``. Each mode rebuilds its
        own messages (json_only uses the JSON-only prompt). A transient SDK error is
        raised for the outer bounded-retry loop; a deterministic refusal/parse is
        raised as-is.
        """
        modes = self._ladder()
        for mode in modes:
            try:
                completion = await self._create_in_mode(
                    mode, operation, builder, request, schema
                )
            except ModelProviderError as exc:
                if _is_format_rejection(exc) and mode != modes[-1]:
                    # Provider does not honor this format → try the next one.
                    continue
                raise
            if completion.content is None:
                raise ModelOutputValidationError(
                    "provider returned a completion with no content",
                    code="EMPTY_COMPLETION",
                )
            # Remember the working mode so later calls skip the ladder.
            self._mode = mode
            return completion
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
        builder: MessageBuilder,
        request: Any,
        schema: dict[str, object],
    ) -> ProviderCompletion:
        # Rebuild messages per mode so json_only carries the ONLY-JSON instruction
        # and never reuses the json_only=False messages.
        json_only = mode == "json_only"
        messages = builder(request, json_only=json_only)
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
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            mapped = _map_sdk_error(exc)
            if used_response_format and _is_format_refusal(mapped):
                # Provider does not honor this response_format (a client-error
                # refusal against a response_format request) → the ladder downgrades
                # to the next mode in the SAME call.
                raise _FormatRejectedError(str(mapped)) from exc
            raise mapped from exc
        return _to_completion(response)

    # ------------------------------------------------------------------
    # bounded usage records (never secrets/prompts/raw responses)
    # ------------------------------------------------------------------
    def _record_success(
        self,
        operation: str,
        completion: ProviderCompletion,
        *,
        latency_ms: int | None,
    ) -> None:
        self.usage.append(
            ModelUsage(
                model=self._model,
                operation=operation,
                provider_request_id=completion.provider_request_id,
                latency_ms=latency_ms,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                total_tokens=completion.total_tokens,
                outcome="ok",
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


def _to_completion(response: Any) -> ProviderCompletion:
    """Extract bounded content + real usage metadata from a Chat Completion response.

    Missing fields (no content, no usage) become ``None`` — never guessed.
    """
    content: str | None = None
    try:
        raw_content = response.choices[0].message.content
        if isinstance(raw_content, str) and raw_content.strip():
            content = raw_content
    except (AttributeError, IndexError, TypeError):
        content = None

    request_id: str | None = None
    raw_id = getattr(response, "id", None)
    request_id = str(raw_id) if raw_id else None

    input_tokens = output_tokens = total_tokens = None
    usage = getattr(response, "usage", None)
    if usage is not None:
        pt = getattr(usage, "prompt_tokens", None)
        ct = getattr(usage, "completion_tokens", None)
        tt = getattr(usage, "total_tokens", None)
        input_tokens = int(pt) if isinstance(pt, int) else None
        output_tokens = int(ct) if isinstance(ct, int) else None
        total_tokens = int(tt) if isinstance(tt, int) else None

    return ProviderCompletion(
        content=content,
        provider_request_id=request_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


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
    """True when a mapped error is a structured-output format rejection.

    Only a ``_FormatRejectedError`` (a provider client-error that names the requested
    json_schema / json_object / response_format) downgrades the ladder. A genuine
    content refusal (400 without a format name), an auth/config error, or a transient
    error is never treated as a format rejection.
    """
    return isinstance(mapped, _FormatRejectedError)


def _is_format_rejection(exc: ModelProviderError) -> bool:
    """True when the exception is a structured-output format rejection.

    ``_create_in_mode`` converts a provider client-error against a requested
    ``response_format`` into ``_FormatRejectedError``; the ladder downgrades on that.
    A genuine content refusal / auth error is never a format rejection.
    """
    return isinstance(exc, _FormatRejectedError)


def _map_sdk_error(exc: Exception) -> ModelProviderError:
    """Translate an SDK/HTTP/Command Code exception into the typed taxonomy.

    - timeout / connection / 429 / retryable 5xx → transient retryable types;
    - 401 / 403 → ModelConfigurationError (invalid API key / credentials / endpoint
      — a deployment bug: never retried, never gracefully downgraded);
    - a 400 that names the requested response_format → _FormatRejectedError (the
      structured-output capability fallback, handled inside the format ladder);
    - a genuine model content refusal (400-class that does NOT name the format, or a
      content_filter refusal) → ModelRefusalError (no retry);
    - other config-y signals (unknown model etc.) → ModelConfigurationError.
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

    message = str(exc) or type(exc).__name__
    detail = f"model provider error (HTTP {status})" if status is not None else message
    if status in (401, 403):
        # Authentication / authorization → configuration/deployment failure.
        return ModelConfigurationError(
            "model provider rejected the credentials/endpoint (authentication or "
            "authorization error)"
        )
    if status == 400:
        # Order matters: a config-shaped error (unknown model etc.) is a deployment
        # bug even when the request also used a response_format; a 400 naming the
        # requested format is a capability fallback; anything else is a genuine
        # client/content refusal.
        if _mentions_config(detail + _bounded_exc_text(exc)):
            return ModelConfigurationError(
                "model provider configuration error (unknown model or invalid "
                "configured endpoint)"
            )
        if _mentions_format(detail + _bounded_exc_text(exc)):
            return _FormatRejectedError(detail)
        return ModelRefusalError(detail)
    if "auth" in name or "authentication" in message.lower():
        return ModelConfigurationError(
            "model provider authentication/configuration error"
        )
    if "refus" in message.lower() or "content_filter" in message.lower():
        return ModelRefusalError("model provider refused the request")
    if "unknown model" in message.lower() or "model_not_found" in message.lower():
        return ModelConfigurationError(
            "model provider configuration error (unknown model)"
        )
    # Generic unexpected transport failure → transient (bounded retry).
    return ModelUnavailableError("model provider call failed")


def _mentions_format(text: str) -> bool:
    lowered = text.lower()
    return any(
        m in lowered
        for m in ("json_schema", "json_object", "response_format", "json schema")
    )


def _mentions_config(text: str) -> bool:
    lowered = text.lower()
    return any(
        m in lowered
        for m in (
            "unknown model",
            "model_not_found",
            "invalid model",
            "no such model",
            "model does not exist",
            "does not exist",
            "not a valid model",
            "invalid api key",
            "invalid credentials",
            "invalid endpoint",
            "incorrect api key",
        )
    )


def _bounded_exc_text(exc: Exception) -> str:
    """A bounded, key-safe excerpt of an SDK error for classification."""
    return str(exc)[:200]


class _FormatRejectedError(ModelRefusalError):
    """Private marker: the provider rejected the requested structured-output format.

    Raised inside ``_map_sdk_error`` for a 400 that names the format, and by
    ``_create_in_mode`` when any response_format request is refused. The format
    ladder downgrades to the next mode; it is never re-attempted by the outer
    transient-retry loop (retryable=False).
    """


def _is_timeout(name: str, exc: Exception) -> bool:
    return "timeout" in name or isinstance(exc, httpx.TimeoutException)


def _is_connection(name: str) -> bool:
    return any(m in name for m in ("connection", "connect_error", "apiconnection"))
