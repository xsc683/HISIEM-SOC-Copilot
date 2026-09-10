"""E1-C2 real-model evaluation chain over real Postgres (E1-C2 §9/§16/§19).

Drives sealed GP-01 manifests through the REAL production durable pipeline under
the E1_C2_REAL_MODEL profile with an INJECTED provider double that records usage on
the SAME instance the graph consults — proving graph + telemetry share one provider
instance and the E1-C2 model-telemetry gate is separate from the execution status:

- a healthy provider → COMPLETED + telemetry gate PASS (plan/decide/assess/verdict
  all succeeded, resolved structured mode present);
- an UNAVAILABLE provider → COMPLETED + INCONCLUSIVE (runtime fallback intact) but
  the E1-C2 gate FAIL (no real success);
- a ModelConfigurationError on the first consult → Investigation FAILED/FAILED_FATAL,
  outbox DEAD_LETTER immediately, provider consulted exactly once, no raw provider
  error persisted by the evaluation artifact.

Skipped when Postgres is unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hisiem_soc_copilot.bootstrap.container import Container
from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.contracts.llm.errors import (
    ModelConfigurationError,
    ModelUnavailableError,
)
from hisiem_soc_copilot.evaluation_harness.harness import (
    EvaluationProfile,
    execute_execution,
    execute_real_model_run,
)
from hisiem_soc_copilot.evaluation_harness.record import (
    ExecutionStatus,
    execution_artifact_path,
    read_record,
)
from hisiem_soc_copilot.evaluation_harness.telemetry import (
    GATE_FAIL,
    GATE_PASS,
    REQUIRED_OPERATIONS,
    build_model_telemetry,
    model_telemetry_path,
    read_model_telemetry,
)
from hisiem_soc_copilot.infrastructure.llm.scripted import ScriptedModelProvider
from tests.fixtures.hisiem_fake import FakeHisiem
from tests.unit.evaluation_harness._seal_helpers import seal_dataset

_DATASET_RUN_ID = "e1c2-chain-run"

_TRUNCATE = (
    "tool_invocation",
    "outbox_message",
    "domain_event",
    "command_receipt",
    "orchestration_binding",
    "investigation_result_finding",
    "investigation_result",
    "finding_evidence",
    "finding",
    "evidence",
    "hypothesis_assessment_evidence",
    "hypothesis_assessment",
    "hypothesis",
    "plan_step",
    "plan_revision",
    "investigation",
)

_SCRIPTED_VERDICT: dict[str, object] = {
    "disposition": "INCONCLUSIVE",
    "summary": "Insufficient evidence to reach a firm disposition",
    "confidence": 0.3,
    "uncertainty": "The provider gathered no additional evidence this round",
}


def _settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.database.database_url = (
        "postgresql+psycopg://copilot:copilot@127.0.0.1:5433/copilot"
    )
    s.langgraph.database_url = s.database.database_url
    s.evaluation.runs_dir = str(tmp_path / "runs")
    s.evaluation.executions_dir = str(tmp_path / "executions")
    # E1_C2_REAL_MODEL profile gate requires the openai_compatible provider label;
    # the REAL provider is never built because a double is always injected below.
    s.llm.provider = "openai_compatible"
    return s


async def _db_reachable(settings: Settings) -> bool:
    try:
        import psycopg
        from sqlalchemy.engine import make_url

        url = make_url(settings.database.database_url)
        conn = psycopg.connect(
            host=url.host,
            port=url.port,
            user=url.username,
            password=url.password,
            dbname=url.database,
            connect_timeout=2,
        )
        conn.execute("SELECT 1")
        conn.close()
        return True
    except Exception:
        return False


async def _truncate(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await session.execute(
            text(f"TRUNCATE copilot.{', copilot.'.join(_TRUNCATE)} RESTART IDENTITY CASCADE")
        )
        await session.commit()


async def _outbox_status(factory, investigation_id: str) -> str | None:
    async with factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT o.status FROM copilot.outbox_message o "
                    "JOIN copilot.domain_event e ON e.event_id = o.event_id "
                    "WHERE e.aggregate_id=:iid ORDER BY o.created_at LIMIT 1"
                ),
                {"iid": UUID(investigation_id)},
            )
        ).scalar()


async def _investigation_state(
    factory, investigation_id: str
) -> tuple[str, str | None]:
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, termination_reason FROM copilot.investigation "
                    "WHERE id=:iid"
                ),
                {"iid": UUID(investigation_id)},
            )
        ).fetchone()
    return row[0], row[1]


@pytest_asyncio.fixture
async def real_env(
    tmp_path: Path,
) -> AsyncIterator[tuple[Settings, Any, Container]]:
    settings = _settings(tmp_path)
    if not await _db_reachable(settings):
        import pytest

        pytest.skip("PostgreSQL not reachable — skipping E1-C2 integration test")
    seal_dataset(
        runs_dir=Path(settings.evaluation.runs_dir), dataset_run_id=_DATASET_RUN_ID
    )
    engine = create_async_engine(settings.database.database_url)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await _truncate(factory)
    container = Container(settings)
    await container.open()
    try:
        yield settings, factory, container
    finally:
        await container.close()
        await engine.dispose()


class _RecordingScripted:
    """A Scripted-backed provider double implementing the public introspection
    surface + a real per-call usage buffer on the SAME instance the graph uses."""

    def __init__(self, *, scripted: ScriptedModelProvider) -> None:
        self._scripted = scripted
        self.provider_name = "command_code"
        self.protocol_name = "openai_compatible_chat_completions"
        self.model_name = "deepseek/deepseek-v4-flash"
        self.zdr_enabled = True
        self.configured_structured_output_mode = "auto"
        self._resolved = "json_schema"
        self.usage: list[dict[str, object]] = []

    @property
    def resolved_structured_output_mode(self) -> str | None:
        return self._resolved

    def usage_snapshot(self) -> tuple[dict[str, object], ...]:
        return tuple(self.usage)

    def _rec(self, operation: str) -> None:
        self.usage.append(
            {
                "operation": operation,
                "outcome": "ok",
                "error_category": None,
                "provider_request_id": "chatcmpl-eval",
                "latency_ms": 1,
                "attempt_count": 1,
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
            }
        )

    async def plan(self, request: Any) -> Any:
        self._rec("plan")
        return await self._scripted.plan(request)

    async def decide_next(self, request: Any) -> Any:
        self._rec("decide")
        return await self._scripted.decide_next(request)

    async def assess(self, request: Any) -> Any:
        self._rec("assess")
        return await self._scripted.assess(request)

    async def verdict(self, request: Any) -> Any:
        self._rec("verdict")
        return await self._scripted.verdict(request)


async def test_e1c2_real_model_chain_completed_gate_passes(real_env) -> None:
    """Healthy provider → COMPLETED + result; the SAME instance records all four
    required operations → E1-C2 telemetry gate PASS (separate from execution)."""
    settings, factory, container = real_env
    hisiem = FakeHisiem(alert_id="es-doc-0001")
    provider = _RecordingScripted(
        scripted=ScriptedModelProvider(script={"verdict": dict(_SCRIPTED_VERDICT)})
    )
    record = await execute_execution(
        dataset_run_id=_DATASET_RUN_ID,
        container=container,
        hisiem=hisiem,
        model=provider,
        profile=EvaluationProfile.E1_C2_REAL_MODEL,
        execution_code=("integration-exec-head", False),
    )
    assert record.execution_status == ExecutionStatus.COMPLETED
    assert record.investigation_id is not None
    assert record.investigation_status == "COMPLETED"
    assert record.model_provider == "openai_compatible"
    # The SAME provider instance the graph consulted holds the usage → build the
    # telemetry from it and the gate PASSES (all four real operations succeeded).
    telemetry = build_model_telemetry(
        execution_id=record.execution_id,
        dataset_run_id=_DATASET_RUN_ID,
        provider_adapter="openai_compatible",
        provider=provider,
    )
    assert telemetry.gate_status == GATE_PASS
    assert telemetry.successful_operations == REQUIRED_OPERATIONS
    assert telemetry.resolved_structured_output_mode == "json_schema"
    # Execution.json is a normal COMPLETED artifact (v2, no telemetry overload).
    artifact = execution_artifact_path(
        settings.evaluation.executions_dir, _DATASET_RUN_ID, record.execution_id
    )
    assert artifact.is_file()


async def test_e1c2_runtime_completed_but_no_real_success_is_gate_fail(real_env) -> None:
    """§6: the runtime degrades a provider outage into COMPLETED + INCONCLUSIVE but
    the E1-C2 provider gate is FAIL (no real validated call succeeded)."""
    settings, factory, container = real_env
    hisiem = FakeHisiem(alert_id="es-doc-0001")

    class _UnavailableProvider:
        provider_name = "command_code"
        protocol_name = "openai_compatible_chat_completions"
        model_name = "deepseek/deepseek-v4-flash"
        zdr_enabled = True
        configured_structured_output_mode = "auto"
        _resolved: str | None = None
        usage: list[dict[str, object]] = []

        @property
        def resolved_structured_output_mode(self) -> str | None:
            return self._resolved

        def usage_snapshot(self) -> tuple[dict[str, object], ...]:
            return tuple(self.usage)

        def _err(self, operation: str) -> None:
            self.usage.append(
                {
                    "operation": operation,
                    "outcome": "error",
                    "error_category": "MODEL_UNAVAILABLE",
                }
            )

        async def _raise(self, operation: str) -> Any:
            self._err(operation)
            raise ModelUnavailableError("provider is down")

        async def plan(self, request: Any) -> Any:
            return await self._raise("plan")

        async def decide_next(self, request: Any) -> Any:
            return await self._raise("decide")

        async def assess(self, request: Any) -> Any:
            return await self._raise("assess")

        async def verdict(self, request: Any) -> Any:
            return await self._raise("verdict")

    provider = _UnavailableProvider()
    record = await execute_execution(
        dataset_run_id=_DATASET_RUN_ID,
        container=container,
        hisiem=hisiem,
        model=provider,
        profile=EvaluationProfile.E1_C2_REAL_MODEL,
        execution_code=("integration-exec-head", False),
    )
    # Investigation COMPLETED + INCONCLUSIVE via deterministic fallback — but the
    # real-provider proof is separate: gate FAIL.
    assert record.execution_status == ExecutionStatus.COMPLETED
    assert record.result_disposition == "INCONCLUSIVE"
    telemetry = build_model_telemetry(
        execution_id=record.execution_id,
        dataset_run_id=_DATASET_RUN_ID,
        provider_adapter="openai_compatible",
        provider=provider,
    )
    assert telemetry.gate_status == GATE_FAIL
    assert telemetry.successful_operations == ()


async def test_model_configuration_fails_fatal_dead_letter_and_secret_safe(
    real_env,
) -> None:
    """§9 acceptance: a ModelConfigurationError from the first consult →
    Investigation FAILED/FAILED_FATAL, outbox DEAD_LETTER immediately, the provider
    is consulted exactly ONCE, no outbox retry loop, and NO raw provider error text
    reaches the evaluation artifact."""
    settings, factory, container = real_env
    hisiem = FakeHisiem(alert_id="es-doc-0001")
    secret = "SUPERSECRET1234"

    class _ConfigFailing:
        calls: list[str] = []

        async def plan(self, request: Any) -> Any:
            self.calls.append("plan")
            raise ModelConfigurationError(f"bad api key {secret}")

        async def decide_next(self, request: Any) -> Any:
            raise AssertionError("decide reached after config failure")

        async def assess(self, request: Any) -> Any:
            raise AssertionError("assess reached after config failure")

        async def verdict(self, request: Any) -> Any:
            raise AssertionError("verdict reached after config failure")

    provider = _ConfigFailing()
    record = await execute_execution(
        dataset_run_id=_DATASET_RUN_ID,
        container=container,
        hisiem=hisiem,
        model=provider,
        profile=EvaluationProfile.E1_C2_REAL_MODEL,
        execution_code=("integration-exec-head", False),
    )
    # The durable runtime terminalized the Investigation FATAL and dead-lettered.
    assert provider.calls == ["plan"]
    assert record.investigation_id is not None
    status, reason = await _investigation_state(factory, record.investigation_id)
    assert status == "FAILED"
    assert reason == "FAILED_FATAL"
    assert await _outbox_status(factory, record.investigation_id) == "DEAD_LETTER"
    # The evaluation execution itself is a terminal FAILED artifact (its snapshot
    # showed the FATAL investigation — never COMPLETED).
    assert record.execution_status == ExecutionStatus.FAILED
    artifact = execution_artifact_path(
        settings.evaluation.executions_dir, _DATASET_RUN_ID, record.execution_id
    )
    payload = artifact.read_text(encoding="utf-8")
    assert secret not in payload


# ---------------------------------------------------------------------------
# §4 — the REAL ``execute-real-model`` orchestration path (execute_real_model_run)
#
# These drive the SAME internal function the production CLI calls. ``provider`` /
# ``hisiem`` / ``execution_code`` are the injection seams; the CLI passes none of
# them, so the operator path is identical.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def real_settings(tmp_path: Path) -> AsyncIterator[tuple[Settings, Any]]:
    """Sealed dataset + reachable DB, WITHOUT an open container.

    ``execute_real_model_run`` opens (and closes) its OWN container, so this fixture
    only seals + truncates and yields ``(settings, session_factory)`` for the
    post-run DB assertions.
    """
    settings = _settings(tmp_path)
    if not await _db_reachable(settings):
        import pytest

        pytest.skip("PostgreSQL not reachable — skipping E1-C2 integration test")
    seal_dataset(
        runs_dir=Path(settings.evaluation.runs_dir), dataset_run_id=_DATASET_RUN_ID
    )
    engine = create_async_engine(settings.database.database_url)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await _truncate(factory)
    try:
        yield settings, factory
    finally:
        await engine.dispose()


async def test_execute_real_model_run_healthy_creates_both_artifacts(
    real_settings,
) -> None:
    """§4 healthy: execution.json + model-telemetry.json for the SAME execution_id,
    COMPLETED with an InvestigationResult, telemetry gate PASS, exit 0, and the
    telemetry is sourced from the SAME provider instance the graph consulted."""
    settings, _factory = real_settings
    hisiem = FakeHisiem(alert_id="es-doc-0001")
    provider = _RecordingScripted(
        scripted=ScriptedModelProvider(script={"verdict": dict(_SCRIPTED_VERDICT)})
    )
    result = await execute_real_model_run(
        dataset_run_id=_DATASET_RUN_ID,
        settings=settings,
        provider=provider,
        hisiem=hisiem,
        execution_code=("integration-exec-head", False),
    )
    assert result.exit_code == 0
    assert result.execution_id is not None
    assert result.telemetry is not None
    assert result.telemetry.gate_status == GATE_PASS
    assert result.telemetry.successful_operations == REQUIRED_OPERATIONS
    # The SAME instance the graph used is the telemetry source.
    assert {r["operation"] for r in result.telemetry.usage_records} >= set(
        REQUIRED_OPERATIONS
    )
    assert len(provider.usage) >= len(REQUIRED_OPERATIONS)

    executions_dir = settings.evaluation.executions_dir
    exec_artifact = execution_artifact_path(
        executions_dir, _DATASET_RUN_ID, result.execution_id
    )
    telemetry_artifact = model_telemetry_path(
        executions_dir, _DATASET_RUN_ID, result.execution_id
    )
    assert exec_artifact.is_file()
    assert telemetry_artifact.is_file()
    # BOTH artifacts belong to the ONE execution_id.
    assert telemetry_artifact.parent == exec_artifact.parent
    record = read_record(exec_artifact)
    assert record.execution_id == result.execution_id
    assert record.execution_status == ExecutionStatus.COMPLETED
    assert record.result_disposition is not None
    restored = read_model_telemetry(telemetry_artifact)
    assert restored.execution_id == result.execution_id
    assert restored.gate_status == GATE_PASS


async def test_execute_real_model_run_fallback_completed_but_gate_fail(
    real_settings,
) -> None:
    """§4 fallback: a transient/unavailable provider degrades into COMPLETED +
    INCONCLUSIVE (runtime fallback), but the E1-C2 gate FAILs → exit 1."""
    settings, _factory = real_settings
    hisiem = FakeHisiem(alert_id="es-doc-0001")

    class _UnavailableProvider:
        provider_name = "command_code"
        protocol_name = "openai_compatible_chat_completions"
        model_name = "deepseek/deepseek-v4-flash"
        zdr_enabled = True
        configured_structured_output_mode = "auto"
        _resolved: str | None = None
        usage: list[dict[str, object]] = []

        @property
        def resolved_structured_output_mode(self) -> str | None:
            return self._resolved

        def usage_snapshot(self) -> tuple[dict[str, object], ...]:
            return tuple(self.usage)

        def _err(self, operation: str) -> None:
            self.usage.append(
                {
                    "operation": operation,
                    "outcome": "error",
                    "error_category": "MODEL_UNAVAILABLE",
                }
            )

        async def plan(self, request: Any) -> Any:
            self._err("plan")
            raise ModelUnavailableError("provider is down")

        async def decide_next(self, request: Any) -> Any:
            self._err("decide")
            raise ModelUnavailableError("provider is down")

        async def assess(self, request: Any) -> Any:
            self._err("assess")
            raise ModelUnavailableError("provider is down")

        async def verdict(self, request: Any) -> Any:
            self._err("verdict")
            raise ModelUnavailableError("provider is down")

    provider = _UnavailableProvider()
    result = await execute_real_model_run(
        dataset_run_id=_DATASET_RUN_ID,
        settings=settings,
        provider=provider,
        hisiem=hisiem,
        execution_code=("integration-exec-head", False),
    )
    assert result.exit_code == 1
    assert result.telemetry is not None
    assert result.telemetry.gate_status == GATE_FAIL
    assert result.telemetry.successful_operations == ()
    # Investigation still COMPLETED but INCONCLUSIVE (deterministic fallback).
    exec_artifact = execution_artifact_path(
        settings.evaluation.executions_dir, _DATASET_RUN_ID, result.execution_id
    )
    assert exec_artifact.is_file()
    record = read_record(exec_artifact)
    assert record.execution_status == ExecutionStatus.COMPLETED
    assert record.result_disposition == "INCONCLUSIVE"


async def test_execute_real_model_run_configuration_fatal_no_retry(
    real_settings,
) -> None:
    """§4 configuration fatal: ModelConfigurationError on the first consult →
    provider consulted exactly ONCE, Investigation FAILED/FAILED_FATAL, outbox
    DEAD_LETTER, exit 1 — no retry."""
    settings, factory = real_settings
    hisiem = FakeHisiem(alert_id="es-doc-0001")

    class _ConfigFailing:
        provider_name = "command_code"
        protocol_name = "openai_compatible_chat_completions"
        model_name = "deepseek/deepseek-v4-flash"
        zdr_enabled = True
        configured_structured_output_mode = "auto"
        resolved_structured_output_mode: str | None = "json_schema"
        calls: list[str] = []

        async def plan(self, request: Any) -> Any:
            self.calls.append("plan")
            raise ModelConfigurationError("bad api key")

        async def decide_next(self, request: Any) -> Any:
            raise AssertionError("decide reached after config failure")

        async def assess(self, request: Any) -> Any:
            raise AssertionError("assess reached after config failure")

        async def verdict(self, request: Any) -> Any:
            raise AssertionError("verdict reached after config failure")

    provider = _ConfigFailing()
    result = await execute_real_model_run(
        dataset_run_id=_DATASET_RUN_ID,
        settings=settings,
        provider=provider,
        hisiem=hisiem,
        execution_code=("integration-exec-head", False),
    )
    assert result.exit_code == 1
    assert provider.calls == ["plan"]
    exec_artifact = execution_artifact_path(
        settings.evaluation.executions_dir, _DATASET_RUN_ID, result.execution_id
    )
    record = read_record(exec_artifact)
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.investigation_id is not None
    status, reason = await _investigation_state(factory, record.investigation_id)
    assert status == "FAILED"
    assert reason == "FAILED_FATAL"
    assert await _outbox_status(factory, record.investigation_id) == "DEAD_LETTER"
