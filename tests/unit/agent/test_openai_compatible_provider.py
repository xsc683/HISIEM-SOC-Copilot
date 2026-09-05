"""Unit tests for the OpenAI-compatible ModelProvider adapter + wire schemas.

These never touch a real provider. A fake OpenAI client (``client_factory``) stands
in for ``AsyncOpenAI`` so we can script: json_schema/json_object support, malformed
JSON, schema-invalid output, timeout/429/5xx retries, auth/config errors, refusals,
and assert that the API key never leaks into the messages/logs/usage we record.

Default test provider remains ``ScriptedModelProvider`` — this file only exercises
the adapter in isolation (plus a couple of runtime fallback tests).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from hisiem_soc_copilot.agent.evidence.normalizer import EvidenceNormalizer
from hisiem_soc_copilot.agent.graph.builder import build_investigation_graph, thread_config
from hisiem_soc_copilot.agent.graph.runtime import GraphRuntime
from hisiem_soc_copilot.agent.tools.executor import ToolExecutor
from hisiem_soc_copilot.agent.tools.registry import ToolRegistry
from hisiem_soc_copilot.application.handlers.workflow import InvestigationWorkflowHandler
from hisiem_soc_copilot.application.ports.model_provider import (
    AssessRequest,
    DecideNextRequest,
    PlanRequest,
)
from hisiem_soc_copilot.contracts.llm.errors import (
    ModelConfigurationError,
    ModelOutputValidationError,
    ModelProviderError,
    ModelRateLimitedError,
    ModelRefusalError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.enums import InvestigationStatus
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from hisiem_soc_copilot.infrastructure.llm.openai_compatible import (
    OpenAICompatibleModelProvider,
)
from tests.fixtures.fakes import FakeUnitOfWorkFactory
from tests.fixtures.hisiem_fake import FakeHisiem


def _set_key(value: str = "test-key") -> None:
    os.environ["CMD_API_KEY"] = value


# ---------------------------------------------------------------------------
# fake OpenAI-compatible client
# ---------------------------------------------------------------------------


@dataclass
class _FakeCompletions:
    """Scriptable ``chat.completions`` surface used by the adapter."""

    parent: _FakeClient
    handler: Any = None

    async def create(self, **kwargs: Any) -> Any:
        self.parent.recorded_kwargs.append(kwargs)
        return await self.handler(kwargs)


@dataclass
class _FakeChat:
    completions: _FakeCompletions


@dataclass
class _FakeClient:
    """Fake ``AsyncOpenAI`` standing in via ``client_factory``.

    ``handler`` is an async callable ``(kwargs) -> response|raise``; inspect
    ``recorded_kwargs`` to assert the request shape (model, response_format,
    x-cmd-zdr default header is set by the client_factory wrapper, not here).
    """

    handler: Any
    recorded_kwargs: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.chat = _FakeChat(_FakeCompletions(self, self.handler))

    @property
    def completions(self) -> _FakeCompletions:
        return self.chat.completions


def _response(
    content: str,
    *,
    request_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
) -> object:
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=total_tokens,
    )
    return SimpleNamespace(
        choices=[choice],
        id=request_id or f"req-{uuid4()}",
        usage=usage,
    )


def _client_factory(fake: _FakeClient) -> Any:
    def _factory(**kwargs: Any) -> Any:
        # Assert the SDK-retry + ZDR setup the adapter promises.
        assert kwargs.get("max_retries") == 0
        assert kwargs.get("default_headers") == {"x-cmd-zdr": "1"}
        return fake

    return _factory


def _provider(fake: _FakeClient, **overrides: Any) -> OpenAICompatibleModelProvider:
    _set_key()
    return OpenAICompatibleModelProvider(
        base_url="https://api.commandcode.ai/provider/v1",
        model="deepseek/deepseek-v4-flash",
        client_factory=_client_factory(fake),
        **overrides,
    )


def _plan_request() -> PlanRequest:
    return PlanRequest(
        investigation_id="inv-1",
        alert_summary="SSH brute force alert",
        tool_names=["hisiem.search_events", "hisiem.get_detection_rule"],
    )


def _decide_request() -> DecideNextRequest:
    return DecideNextRequest(
        investigation_id="inv-1",
        iteration=0,
        plan_goal="Investigate the alert",
        evidence_summary=[],
        tool_names=["hisiem.search_events", "hisiem.get_detection_rule"],
    )


def _assess_request() -> AssessRequest:
    return AssessRequest(
        investigation_id="inv-1",
        hypotheses=[{"id": "hyp-1", "statement": "account compromise"}],
        evidence=[{"id": "evt-1", "summary": "success after failures", "operation": "auth"}],
    )


# ---------------------------------------------------------------------------
# 1-4: wire → contract mapping (json_schema path)
# ---------------------------------------------------------------------------


def _strict_response_for(operation: str) -> str:
    if operation == "plan":
        return json.dumps(
            {"goal": "Determine compromise", "steps": [{"step_id": "s1", "objective": "read rule"}]}
        )
    if operation == "decide":
        return json.dumps(
            {"decision": "CONTINUE", "tool_name": "hisemi.search_events", "reason": "look"}
        )
    raise AssertionError(operation)


async def test_plan_wire_maps_to_investigation_plan() -> None:
    async def handler(kwargs: dict[str, Any]) -> object:
        # The first auto call probes json_schema (response_format present).
        assert kwargs["response_format"]["type"] == "json_schema"
        return _response(
            json.dumps(
                {
                    "goal": "Determine whether the SSH brute force escalated",
                    "steps": [
                        {"step_id": "read_rule", "objective": "Read the rule"},
                        {"step_id": "search", "objective": "Search successes"},
                    ],
                }
            )
        )

    fake = _FakeClient(handler)
    provider = _provider(fake)
    plan = await provider.plan(_plan_request())
    assert plan.goal.startswith("Determine whether")
    assert [s.step_id for s in plan.steps] == ["read_rule", "search"]
    assert fake.recorded_kwargs[0]["model"] == "deepseek/deepseek-v4-flash"
    assert fake.recorded_kwargs[0]["temperature"] == 0.0


async def test_decide_wire_maps_to_next_step() -> None:
    async def handler(kwargs: dict[str, Any]) -> object:
        return _response(
            json.dumps(
                {
                    "decision": "CONTINUE",
                    "tool_name": "hisiem.search_events",
                    "arguments": {"from": "x", "conditions": [{"field": "event.action"}]},
                    "reason": "look for successes",
                }
            )
        )

    provider = _provider(_FakeClient(handler))
    step = await provider.decide_next(_decide_request())
    assert step.decision == "CONTINUE"
    assert step.tool_name == "hisiem.search_events"


async def test_assess_wire_maps_to_assessment_summary() -> None:
    async def handler(kwargs: dict[str, Any]) -> object:
        return _response(
            json.dumps(
                {
                    "assessments": [
                        {
                            "hypothesis_id": "hyp-1",
                            "status": "SUPPORTED",
                            "reason_summary": "success after failures",
                            "evidence_relations": [
                                {"evidence_id": "evt-1", "relation": "SUPPORTS"}
                            ],
                        }
                    ],
                    "findings": [
                        {
                            "statement": "account was accessed",
                            "evidence_citations": ["evt-1"],
                        }
                    ],
                }
            )
        )

    provider = _provider(_FakeClient(handler))
    summary = await provider.assess(_assess_request())
    assert summary.assessments[0].status == "SUPPORTED"
    assert summary.assessments[0].evidence_relations[0].evidence_id == "evt-1"
    assert summary.findings[0].evidence_citations == ["evt-1"]


async def test_verdict_wire_maps_to_verdict_candidate() -> None:
    async def handler(kwargs: dict[str, Any]) -> object:
        return _response(
            json.dumps(
                {
                    "disposition": "MALICIOUS",
                    "summary": "compromised",
                    "confidence": 0.9,
                    "uncertainty": None,
                }
            )
        )

    provider = _provider(_FakeClient(handler))
    verdict = await provider.verdict(_assess_request())
    assert verdict.disposition == "MALICIOUS"
    assert verdict.confidence == 0.9


# ---------------------------------------------------------------------------
# 5-6: json_schema support + fallback
# ---------------------------------------------------------------------------


async def test_json_schema_supported_single_call() -> None:
    async def handler(kwargs: dict[str, Any]) -> object:
        return _response('{"goal": "g", "steps": []}')

    fake = _FakeClient(handler)
    provider = _provider(fake)
    await provider.plan(_plan_request())
    # auto: first call probes json_schema and it is honored.
    assert fake.recorded_kwargs[0]["response_format"]["type"] == "json_schema"
    # Second call reuses the cached working mode (no ladder re-probe).
    await provider.plan(_plan_request())
    assert len(fake.recorded_kwargs) == 2
    assert fake.recorded_kwargs[1]["response_format"]["type"] == "json_schema"


async def test_json_schema_rejected_falls_back_to_json_object() -> None:
    class _BadRequestError(Exception):
        status_code = 400

    async def handler(kwargs: dict[str, Any]) -> object:
        rf = kwargs.get("response_format") or {}
        if rf.get("type") == "json_schema":
            raise _BadRequestError("response_format json_schema is not supported")
        # json_object honored → return a valid object body.
        assert rf.get("type") == "json_object"
        return _response('{"goal": "g", "steps": [{"step_id": "s", "objective": "o"}]}')

    fake = _FakeClient(handler)
    provider = _provider(fake)
    plan = await provider.plan(_plan_request())
    assert plan.goal == "g"
    # Ladder walked json_schema → json_object within ONE call; mode cached to json_object.
    assert fake.recorded_kwargs[-1]["response_format"]["type"] == "json_object"
    # Subsequent calls skip straight to json_object.
    await provider.plan(_plan_request())
    assert all(
        k["response_format"]["type"] == "json_object" for k in fake.recorded_kwargs[1:]
    )


async def test_json_object_unavailable_falls_back_to_json_only_prompt() -> None:
    class _BadRequestError(Exception):
        status_code = 400

    async def handler(kwargs: dict[str, Any]) -> object:
        rf = kwargs.get("response_format") or {}
        if rf.get("type") == "json_schema":
            raise _BadRequestError("response_format json_schema is not supported")
        if rf.get("type") == "json_object":
            raise _BadRequestError("response_format json_object is not supported")
        # JSON-only: no response_format; honor the prompt.
        return _response('{"goal": "g", "steps": []}')

    provider = _provider(_FakeClient(handler))
    plan = await provider.plan(_plan_request())
    assert plan.goal == "g"


async def test_json_only_mode_rebuilds_messages_with_only_json_instruction() -> None:
    """Fix: json_only must NOT reuse the json_only=False messages — it rebuilds them
    via the prompt builder with json_only=True, so the final request carries no
    response_format and the messages contain an explicit ONLY-JSON instruction."""
    class _BadRequestError(Exception):
        status_code = 400

    async def handler(kwargs: dict[str, Any]) -> object:
        rf = kwargs.get("response_format") or {}
        if rf.get("type") == "json_schema":
            raise _BadRequestError("response_format json_schema is not supported")
        if rf.get("type") == "json_object":
            raise _BadRequestError("response_format json_object is not supported")
        # json_only: no response_format key at all.
        assert "response_format" not in kwargs
        return _response('{"goal": "g", "steps": []}')

    fake = _FakeClient(handler)
    provider = _provider(fake)
    plan = await provider.plan(_plan_request())
    assert plan.goal == "g"
    final_kwargs = fake.recorded_kwargs[-1]
    assert "response_format" not in final_kwargs
    # The json_only=True user message was rebuilt (not the json_only=False one) and
    # carries the explicit ONLY-JSON instruction.
    last_content = final_kwargs["messages"][-1]["content"]
    assert "only a single valid json object" in last_content.lower()
    assert any(
        "only a single valid json object" in m["content"].lower()
        for m in final_kwargs["messages"]
    )


# ---------------------------------------------------------------------------
# 7-8: malformed / schema-invalid → ModelOutputValidationError
# ---------------------------------------------------------------------------


async def test_malformed_json_raises_output_validation() -> None:
    async def handler(kwargs: dict[str, Any]) -> object:
        return _response("{not valid json")

    provider = _provider(_FakeClient(handler))
    with pytest.raises(ModelOutputValidationError):
        await provider.plan(_plan_request())


async def test_schema_invalid_output_raises_output_validation() -> None:
    async def handler(kwargs: dict[str, Any]) -> object:
        return _response('{"goal": "g", "steps": [{"objective": "missing step_id"}]}')

    provider = _provider(_FakeClient(handler))
    with pytest.raises(ModelOutputValidationError):
        await provider.plan(_plan_request())


async def test_empty_completion_raises_output_validation() -> None:
    async def handler(kwargs: dict[str, Any]) -> object:
        return _response("   ")

    provider = _provider(_FakeClient(handler))
    with pytest.raises(ModelOutputValidationError):
        await provider.plan(_plan_request())


async def test_wrong_enum_raises_output_validation() -> None:
    async def handler(kwargs: dict[str, Any]) -> object:
        return _response('{"disposition": "SUSPICIOUS", "summary": "x", "confidence": 0.5}')

    provider = _provider(_FakeClient(handler))
    with pytest.raises(ModelOutputValidationError):
        await provider.verdict(_assess_request())


# ---------------------------------------------------------------------------
# 9-11: transient retries (timeout / 429 / retryable 5xx)
# ---------------------------------------------------------------------------


class APITimeoutError(Exception):
    """Named to match the SDK's timeout error class name (adapter maps by name)."""


class _RateLimitError(Exception):
    status_code = 429


class _ServerError(Exception):
    status_code = 503


async def _retry_then_succeed(first: Exception) -> None:
    calls = {"n": 0}

    async def handler(kwargs: dict[str, Any]) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise first
        return _response('{"goal": "g", "steps": []}')

    provider = _provider(_FakeClient(handler))
    plan = await provider.plan(_plan_request())
    assert plan.goal == "g"
    assert calls["n"] == 2  # one retry happened


async def test_timeout_is_retried_then_succeeds() -> None:
    await _retry_then_succeed(APITimeoutError())


async def test_rate_limit_is_retried_then_succeeds() -> None:
    await _retry_then_succeed(_RateLimitError())


async def test_retryable_5xx_is_retried_then_succeeds() -> None:
    await _retry_then_succeed(_ServerError())


async def test_timeout_exhausted_raises_model_timeout() -> None:
    async def handler(kwargs: dict[str, Any]) -> object:
        raise APITimeoutError()

    provider = _provider(_FakeClient(handler), max_retries=1)
    with pytest.raises(ModelTimeoutError):
        await provider.plan(_plan_request())
    # attempt_count reflects 2 attempts (1 + 1 retry).
    assert provider.usage[-1].attempt_count == 2
    assert provider.usage[-1].error_category == "MODEL_TIMEOUT"


async def test_rate_limit_exhausted_raises_model_rate_limited() -> None:
    async def handler(kwargs: dict[str, Any]) -> object:
        raise _RateLimitError()

    provider = _provider(_FakeClient(handler), max_retries=0)
    with pytest.raises(ModelRateLimitedError):
        await provider.plan(_plan_request())


async def test_server_error_exhausted_raises_model_unavailable() -> None:
    async def handler(kwargs: dict[str, Any]) -> object:
        raise _ServerError()

    provider = _provider(_FakeClient(handler), max_retries=1)
    with pytest.raises(ModelUnavailableError):
        await provider.plan(_plan_request())


# ---------------------------------------------------------------------------
# 12-13: auth/config → ModelConfigurationError, never retried; refusal → typed
# ---------------------------------------------------------------------------


class _AuthError(Exception):
    status_code = 401


class _ForbiddenError(Exception):
    status_code = 403


class _ContentRefusalError(Exception):
    status_code = 400


async def test_auth_error_is_configuration_and_not_retried() -> None:
    """401 → ModelConfigurationError (a deployment bug), never retried, never
    silently downgraded (docs/model-provider-contract.md §14)."""
    calls = {"n": 0}

    async def handler(kwargs: dict[str, Any]) -> object:
        calls["n"] += 1
        raise _AuthError()

    provider = _provider(_FakeClient(handler))
    with pytest.raises(ModelConfigurationError):
        await provider.plan(_plan_request())
    assert calls["n"] == 1  # never retried
    assert provider.usage[-1].error_category == "MODEL_CONFIGURATION"


async def test_forbidden_error_is_configuration_and_not_retried() -> None:
    """403 → ModelConfigurationError (auth/authorization boundary)."""
    calls = {"n": 0}

    async def handler(kwargs: dict[str, Any]) -> object:
        calls["n"] += 1
        raise _ForbiddenError()

    provider = _provider(_FakeClient(handler))
    with pytest.raises(ModelConfigurationError):
        await provider.plan(_plan_request())
    assert calls["n"] == 1


async def test_missing_key_raises_configuration_error() -> None:
    os.environ.pop("CMD_API_KEY", None)
    with pytest.raises(ModelConfigurationError):
        OpenAICompatibleModelProvider(
            base_url="http://x", model="m", client_factory=lambda **k: None
        )


async def test_unknown_structured_mode_raises_configuration_error() -> None:
    _set_key()
    with pytest.raises(ModelConfigurationError):
        OpenAICompatibleModelProvider(
            base_url="http://x",
            model="m",
            structured_output_mode="bogus",
            client_factory=lambda **k: None,
        )


async def test_content_refusal_is_typed_and_not_retried() -> None:
    """A genuine 400 content refusal (not naming a format) → ModelRefusalError
    (no retry) — NOT a config error."""
    calls = {"n": 0}

    async def handler(kwargs: dict[str, Any]) -> object:
        calls["n"] += 1
        raise _ContentRefusalError("this content is not allowed")

    provider = _provider(_FakeClient(handler))
    with pytest.raises(ModelRefusalError):
        await provider.plan(_plan_request())
    assert calls["n"] == 1  # never retried


async def test_unknown_model_is_configuration_error() -> None:
    """A 400 naming an unknown model → ModelConfigurationError (no retry)."""
    calls = {"n": 0}

    class _ModelNotFound(Exception):
        status_code = 400

    async def handler(kwargs: dict[str, Any]) -> object:
        calls["n"] += 1
        raise _ModelNotFound("The model `deepseek/bogus` does not exist")

    provider = _provider(_FakeClient(handler))
    with pytest.raises(ModelConfigurationError):
        await provider.plan(_plan_request())
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# 17: the API key never leaks
# ---------------------------------------------------------------------------


async def test_api_key_never_in_messages_or_usage() -> None:
    async def handler(kwargs: dict[str, Any]) -> object:
        return _response('{"goal": "g", "steps": []}')

    fake = _FakeClient(handler)
    provider = _provider(fake, max_retries=2)
    await provider.plan(_plan_request())

    joined_messages = json.dumps(fake.recorded_kwargs)
    assert "test-key" not in joined_messages
    for record in provider.usage:
        assert "test-key" not in json.dumps(record.as_dict())
    # The client was given the key but never via a request kwarg.
    assert all("api_key" not in k for k in fake.recorded_kwargs)


# ---------------------------------------------------------------------------
# 18: real usage + request id are collected from the provider response
# ---------------------------------------------------------------------------


async def test_success_records_real_usage_and_request_id() -> None:
    async def handler(kwargs: dict[str, Any]) -> object:
        return _response(
            '{"goal": "g", "steps": []}',
            request_id="chatcmpl-REAL-123",
            input_tokens=11,
            output_tokens=4,
            total_tokens=15,
        )

    provider = _provider(_FakeClient(handler))
    await provider.plan(_plan_request())
    assert len(provider.usage) == 1
    record = provider.usage[0]
    assert record.outcome == "ok"
    assert record.provider_request_id == "chatcmpl-REAL-123"
    assert record.input_tokens == 11
    assert record.output_tokens == 4
    assert record.total_tokens == 15
    assert record.operation == "plan"
    assert record.model == "deepseek/deepseek-v4-flash"
    assert record.attempt_count == 1


async def test_usage_is_none_when_provider_reports_none() -> None:
    async def handler(kwargs: dict[str, Any]) -> object:
        # No id / no usage on the fake response → the adapter must store None, never
        # guess zeros.
        message = SimpleNamespace(content='{"goal": "g", "steps": []}')
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice], id=None, usage=None)

    provider = _provider(_FakeClient(handler))
    await provider.plan(_plan_request())
    record = provider.usage[0]
    assert record.outcome == "ok"
    assert record.provider_request_id is None
    assert record.input_tokens is None
    assert record.output_tokens is None
    assert record.total_tokens is None


async def test_usage_buffer_is_bounded() -> None:
    from hisiem_soc_copilot.infrastructure.llm.openai_compatible import _USAGE_MAXLEN

    async def handler(kwargs: dict[str, Any]) -> object:
        return _response('{"goal": "g", "steps": []}')

    provider = _provider(_FakeClient(handler))
    # Drive more calls than the buffer cap; the oldest records are dropped.
    for _ in range(_USAGE_MAXLEN + 25):
        await provider.plan(_plan_request())
    assert len(provider.usage) == _USAGE_MAXLEN


# ---------------------------------------------------------------------------
# 14: a provider outage does not fail the investigation (node fallback)
# ---------------------------------------------------------------------------


class _FailingModel:
    """A ModelProvider whose every consult raises the given typed error."""

    def __init__(self, error: ModelProviderError) -> None:
        self.error = error
        self.calls: list[str] = []

    async def plan(self, request: PlanRequest):
        self.calls.append("plan")
        raise self.error

    async def decide_next(self, request: DecideNextRequest):
        self.calls.append("decide_next")
        raise self.error

    async def assess(self, request: AssessRequest):
        self.calls.append("assess")
        raise self.error

    async def verdict(self, request: AssessRequest):
        self.calls.append("verdict")
        raise self.error


async def _run_with_failing_model(
    error: ModelProviderError,
) -> tuple[Any, _FailingModel, FakeUnitOfWorkFactory, Investigation]:
    """Run the full graph with a model that fails every consult; return artifacts."""
    uows = FakeUnitOfWorkFactory()
    inv = Investigation.create(
        id=uuid4(),
        tenant_id="tenant-a",
        source_alert_ref=ExternalResourceRef(
            provider="hisiem", resource_type="alert", address_id="alert-x"
        ),
        initiated_by=ActorRef(subject_id="analyst", tenant_id="tenant-a"),
        budget_limits=BudgetLimits(max_steps=5, max_tool_calls=5, max_llm_calls=20),
    )
    boot = uows()
    await boot.investigations.add(inv)
    await boot.commit()
    inv.start(actor=inv.initiated_by)
    await boot.investigations.update(inv)
    await boot.commit()

    model = _FailingModel(error)
    hisiem = FakeHisiem(alert_id="alert-x")
    runtime = GraphRuntime(
        uow_factory=uows,
        workflow_handler=InvestigationWorkflowHandler(unit_of_work_factory=uows),
        model=model,
        executor=ToolExecutor(hisiem=hisiem),
        normalizer=EvidenceNormalizer(),
        registry=ToolRegistry(),
        hisiem=hisiem,
        tenant_id="tenant-a",
    )
    graph = build_investigation_graph(runtime)
    final = await graph.ainvoke(
        {"investigation_id": str(inv.id)}, thread_config(str(inv.id))
    )
    return final, model, uows, inv


async def test_provider_unavailable_yields_completed_inconclusive() -> None:
    """Every model consult fails (unavailable) → the run still completes as
    COMPLETED + INCONCLUSIVE, never FAILED (docs/model-provider-contract.md §16)."""
    final, model, uows, inv = await _run_with_failing_model(
        ModelUnavailableError("provider down")
    )
    assert final["stop_reason"].startswith("COMPLETED_WITHOUT_RESPONSE")
    # The outage did not stop the model being consulted at all (each node tried).
    assert "plan" in model.calls

    completed = await uows().investigations.get(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert completed is not None
    assert completed.status == InvestigationStatus.COMPLETED  # never FAILED
    result = await uows().results.get_by_investigation(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert result is not None
    assert result.verdict.disposition.value == "INCONCLUSIVE"
    # The outage is surfaced as explicit uncertainty in the finalize result.
    assert any(
        "model provider" in (u.description or "") for u in result.uncertainties
    )
