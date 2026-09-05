"""Opt-in live compatibility smoke tests against the real Command Code API.

NOT part of the default suite: these only run when BOTH ``RUN_LIVE_LLM_TESTS=1``
AND ``CMD_API_KEY`` are present (see docs/model-provider-contract.md §23). They
exercise the real OpenAI-compatible adapter against
``https://api.commandcode.ai/provider/v1`` + ``deepseek/deepseek-v4-flash`` and
record which structured-output mode the provider honors (json_schema → json_object).

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
    return OpenAICompatibleModelProvider(
        base_url=_BASE_URL,
        model=_MODEL,
        api_key_env="CMD_API_KEY",
        timeout_seconds=60.0,
        max_retries=1,
        zdr=True,
        structured_output_mode="auto",
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


async def test_live_plan() -> None:
    plan = await live_provider.plan(_plan_request())
    assert plan.goal
    assert isinstance(plan.steps, list)


async def test_live_decide() -> None:
    step = await live_provider.decide_next(_decide_request())
    assert step.decision in ("CONTINUE", "FINALIZE")


async def test_live_assess() -> None:
    summary = await live_provider.assess(_assess_request())
    assert isinstance(summary.assessments, list)


async def test_live_verdict() -> None:
    verdict = await live_provider.verdict(_assess_request())
    assert verdict.disposition in ("MALICIOUS", "BENIGN", "INCONCLUSIVE")
    assert 0.0 <= verdict.confidence <= 1.0


async def test_live_probe_reports_structured_mode() -> None:
    """Probe json_schema on the real model; verify json_object fallback works."""
    provider = OpenAICompatibleModelProvider(
        base_url=_BASE_URL,
        model=_MODEL,
        api_key_env="CMD_API_KEY",
        timeout_seconds=60.0,
        max_retries=1,
        zdr=True,
        structured_output_mode="auto",
    )
    # Force json_object explicitly to prove it works independently of json_schema.
    obj_provider = OpenAICompatibleModelProvider(
        base_url=_BASE_URL,
        model=_MODEL,
        api_key_env="CMD_API_KEY",
        timeout_seconds=60.0,
        max_retries=1,
        zdr=True,
        structured_output_mode="json_object",
    )
    plan = await obj_provider.plan(_plan_request())
    assert plan.goal

    # The auto provider caches the mode it actually used on the first real call.
    await provider.plan(_plan_request())
    print(f"\n[LIVE] structured_output_mode auto resolved to: {provider._mode}")
