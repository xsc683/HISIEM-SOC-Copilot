"""Opt-in live compatibility smoke tests against the real Command Code API.

NOT part of the default suite: these only run when BOTH ``RUN_LIVE_LLM_TESTS=1``
AND ``CMD_API_KEY`` are present (see docs/model-provider-contract.md §23). They
exercise the real OpenAI-compatible adapter against
``https://api.commandcode.ai/provider/v1`` + ``deepseek/deepseek-v4-flash`` and
record which structured-output mode the provider honors (json_schema → json_object
→ json_only).

In every other environment these tests SKIP — they never touch the network, never
fail the default run, and never log the API key.
"""

from __future__ import annotations

import os

import pytest

from hisiem_soc_copilot.application.ports.model_provider import (
    AssessRequest,
    DecideNextRequest,
    PlanRequest,
)
from hisiem_soc_copilot.contracts.tools.types import model_tool_specs
from hisiem_soc_copilot.infrastructure.llm.openai_compatible import (
    OpenAICompatibleModelProvider,
)

_BASE_URL = "https://api.commandcode.ai/provider/v1"
_MODEL = "deepseek/deepseek-v4-flash"

_LIVE = os.environ.get("RUN_LIVE_LLM_TESTS") == "1" and bool(
    os.environ.get("CMD_API_KEY")
)

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="set RUN_LIVE_LLM_TESTS=1 and CMD_API_KEY to run live provider tests",
)


@pytest.fixture(scope="module")
def live_provider() -> OpenAICompatibleModelProvider:
    """The real adapter in auto mode (probes json_schema first)."""
    return OpenAICompatibleModelProvider(
        base_url=_BASE_URL,
        model=_MODEL,
        api_key_env="CMD_API_KEY",
        timeout_seconds=60.0,
        max_retries=1,
        zdr=True,
        structured_output_mode="auto",
    )


@pytest.fixture(scope="module")
def json_object_provider() -> OpenAICompatibleModelProvider:
    """A provider pinned to json_object — proves that format independently."""
    return OpenAICompatibleModelProvider(
        base_url=_BASE_URL,
        model=_MODEL,
        api_key_env="CMD_API_KEY",
        timeout_seconds=60.0,
        max_retries=1,
        zdr=True,
        structured_output_mode="json_object",
    )


@pytest.fixture(scope="module")
def json_only_provider() -> OpenAICompatibleModelProvider:
    """A provider pinned to json_only — proves the JSON-only prompt path."""
    return OpenAICompatibleModelProvider(
        base_url=_BASE_URL,
        model=_MODEL,
        api_key_env="CMD_API_KEY",
        timeout_seconds=60.0,
        max_retries=1,
        zdr=True,
        structured_output_mode="json_only",
    )


def _plan_request() -> PlanRequest:
    return PlanRequest(
        investigation_id="live-inv-1",
        alert_summary="SSH brute force alert from 203.0.113.9",
        tool_names=["hisiem.search_events", "hisiem.get_detection_rule"],
    )


def _decide_request() -> DecideNextRequest:
    return DecideNextRequest(
        investigation_id="live-inv-1",
        iteration=0,
        plan_goal="Investigate whether the alert indicates account compromise",
        evidence_summary=[],
        tool_names=["hisiem.search_events", "hisiem.get_detection_rule"],
        tool_specs=model_tool_specs(),
    )


def _assess_request() -> AssessRequest:
    return AssessRequest(
        investigation_id="live-inv-1",
        hypotheses=[
            {"id": "hyp-1", "statement": "The account was compromised via SSH brute force"}
        ],
        evidence=[
            {
                "id": "evt-succ-1",
                "summary": "Successful SSH login after repeated failures",
                "operation": "authentication_success",
            }
        ],
    )


async def test_live_plan(live_provider: OpenAICompatibleModelProvider) -> None:
    plan = await live_provider.plan(_plan_request())
    assert plan.goal
    assert isinstance(plan.steps, list)


async def test_live_decide(live_provider: OpenAICompatibleModelProvider) -> None:
    step = await live_provider.decide_next(_decide_request())
    assert step.decision in ("CONTINUE", "FINALIZE")


async def test_live_assess(live_provider: OpenAICompatibleModelProvider) -> None:
    summary = await live_provider.assess(_assess_request())
    assert isinstance(summary.assessments, list)


async def test_live_verdict(live_provider: OpenAICompatibleModelProvider) -> None:
    verdict = await live_provider.verdict(_assess_request())
    assert verdict.disposition in ("MALICIOUS", "BENIGN", "INCONCLUSIVE")
    assert 0.0 <= verdict.confidence <= 1.0


async def test_live_reports_resolved_structured_mode(
    live_provider: OpenAICompatibleModelProvider,
) -> None:
    """Drive the auto provider once and report which mode it actually used.

    The mode the real Command Code API honored is read from the provider's cache
    after a real call: json_schema (supported) OR the first working fallback.
    This is the compatibility ground truth — never fabricated.
    """
    plan = await live_provider.plan(_plan_request())
    assert plan.goal
    mode = live_provider._mode
    print(f"\n[LIVE] command_code/deepseek-v4-flash resolved structured mode: {mode!r}")
    assert mode in ("json_schema", "json_object", "json_only")


async def test_live_json_object_fallback(
    json_object_provider: OpenAICompatibleModelProvider,
) -> None:
    """The real model honors an explicit json_object request."""
    plan = await json_object_provider.plan(_plan_request())
    assert plan.goal
    print("\n[LIVE] json_object request succeeded")


async def test_live_json_only_fallback(
    json_only_provider: OpenAICompatibleModelProvider,
) -> None:
    """The real model honors the JSON-only prompt path (no response_format)."""
    plan = await json_only_provider.plan(_plan_request())
    assert plan.goal
    print("\n[LIVE] json_only request succeeded")
