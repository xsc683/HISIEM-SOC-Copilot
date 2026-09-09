"""E1-C1 execute chain over real Postgres (E1-C1 §9/§22 live proof, scripted HISIEM).

Drives one sealed GP-01 manifest through the REAL production pipeline: real
Copilot PostgreSQL (5433), real LangGraph Postgres checkpoint, real
domain/outbox/runner code, real graph — with an injected FakeHisiem (alert
hydration) and the explicit deterministic ScriptedModelProvider. Asserts the
sealed manifest -> verify -> launch projection ONLY -> StartAlertInvestigation ->
outbox -> dispatcher drain -> binding/thread -> graph -> COMPLETED -> persisted
InvestigationResult -> COMPLETED Evaluation Execution Record, including the
OBSERVED OrchestrationBinding + LangGraph checkpoint. Also asserts active-
investigation collision fail-closed and drive-exception terminalization (secret-
safe).

Skipped when Postgres is unreachable (same convention as the durable API chain).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hisiem_soc_copilot.agent.graph.builder import thread_config
from hisiem_soc_copilot.application.commands.investigation import StartAlertInvestigation
from hisiem_soc_copilot.bootstrap.container import Container
from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.domain.investigation.value_objects import ExternalResourceRef
from hisiem_soc_copilot.evaluation_harness import harness as harness_mod
from hisiem_soc_copilot.evaluation_harness.harness import (
    CAT_ACTIVE_INVESTIGATION_EXISTS,
    CAT_BINDING_MISSING,
    CAT_CHECKPOINT_MISSING,
    CAT_DRIVE_FAILED,
    CAT_EVALUATION_INVESTIGATION_OWNERSHIP_CONFLICT,
    execute_execution,
)
from hisiem_soc_copilot.evaluation_harness.record import (
    ExecutionStatus,
    execution_artifact_path,
    read_record,
)
from hisiem_soc_copilot.infrastructure.checkpoint.postgres import PostgresCheckpointer
from tests.fixtures.hisiem_fake import FakeHisiem
from tests.unit.evaluation_harness._seal_helpers import seal_dataset

_DATASET_RUN_ID = "e1c1-chain-run"
_EXECUTION_CODE = ("integration-exec-head", False)  # deterministic clean worktree

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


def _settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.database.database_url = (
        "postgresql+psycopg://copilot:copilot@127.0.0.1:5433/copilot"
    )
    s.langgraph.database_url = s.database.database_url
    s.evaluation.runs_dir = str(tmp_path / "runs")
    s.evaluation.executions_dir = str(tmp_path / "executions")
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


async def _investigation_count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return int(
            (
                await session.execute(text("SELECT count(*) FROM copilot.investigation"))
            ).scalar()
        )


async def _assert_checkpoint_observed(settings: Settings, thread_id: str) -> None:
    """The LangGraph Postgres checkpointer holds state for the observed thread."""
    async with PostgresCheckpointer(settings.langgraph) as saver:
        checkpoint = await saver.aget_tuple(thread_config(thread_id))  # type: ignore[arg-type]
        assert checkpoint is not None


async def _open_env(
    tmp_path: Path,
) -> AsyncIterator[tuple[Settings, Any, Any, Container]]:
    """Open a real-Postgres environment (truncated) and yield it for one test."""
    settings = _settings(tmp_path)
    if not await _db_reachable(settings):
        pytest.skip("PostgreSQL not reachable — skipping E1-C1 integration test")
    seal_dataset(
        runs_dir=Path(settings.evaluation.runs_dir),
        dataset_run_id=_DATASET_RUN_ID,
    )
    engine = create_async_engine(settings.database.database_url)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await _truncate(factory)
    container = Container(settings)
    await container.open()
    try:
        yield settings, engine, factory, container
    finally:
        await container.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def real_env(
    tmp_path: Path,
) -> AsyncIterator[tuple[Settings, Any, Any, Container]]:
    async for env in _open_env(tmp_path):
        yield env


async def test_execute_chain_reaches_completed(real_env) -> None:
    settings, engine, factory, container = real_env
    hisiem = FakeHisiem(alert_id="es-doc-0001")  # matches make_verified's alert
    record = await execute_execution(
        dataset_run_id=_DATASET_RUN_ID,
        container=container,
        hisiem=hisiem,
        execution_code=_EXECUTION_CODE,
    )
    artifact = execution_artifact_path(
        settings.evaluation.executions_dir, _DATASET_RUN_ID, record.execution_id
    )

    # The execution record is terminal + complete, with the persisted result.
    assert record.execution_status == ExecutionStatus.COMPLETED
    assert record.investigation_id is not None
    assert record.investigation_status == "COMPLETED"
    assert record.thread_id == f"inv:{record.investigation_id}"
    assert record.result_disposition == "INCONCLUSIVE"  # scripted verdict
    assert record.result_confidence == 0.3
    assert record.evidence_count == 0
    assert record.finding_count == 0
    assert record.hypothesis_count >= 1
    # Dataset provenance (sealed) is NOT mislabeled as execution provenance.
    assert record.dataset_code_git_commit == "test-commit"
    assert record.execution_code_git_commit == "integration-exec-head"
    assert record.dataset_code_git_commit != record.execution_code_git_commit

    # The artifact file is the record (readable + atomic, no temp leftovers).
    assert artifact.is_file()
    restored = read_record(artifact)
    assert restored.execution_id == record.execution_id
    assert restored.execution_status == ExecutionStatus.COMPLETED

    # Real DB: one investigation + binding; published outbox; INCONCLUSIVE result.
    assert await _investigation_count(factory) == 1
    async with factory() as session:
        thread_id = (
            await session.execute(
                text(
                    "SELECT thread_id FROM copilot.orchestration_binding "
                    "WHERE investigation_id=:iid"
                ),
                {"iid": record.investigation_id},
            )
        ).scalar()
        outbox_published = (
            await session.execute(
                text(
                    "SELECT count(*) FROM copilot.outbox_message o "
                    "JOIN copilot.domain_event e ON e.event_id=o.event_id "
                    "WHERE e.aggregate_id=:iid AND o.status='PUBLISHED'"
                ),
                {"iid": record.investigation_id},
            )
        ).scalar()
        disposition = (
            await session.execute(
                text(
                    "SELECT verdict_disposition FROM copilot.investigation_result "
                    "WHERE investigation_id=:iid"
                ),
                {"iid": record.investigation_id},
            )
        ).scalar()
    assert thread_id == record.thread_id
    assert outbox_published == 1
    assert disposition == "INCONCLUSIVE"

    # Observed LangGraph Postgres checkpoint for the observed thread.
    await _assert_checkpoint_observed(settings, record.thread_id)

    # A second drain is a no-op for the terminal investigation (never re-run).
    from hisiem_soc_copilot.infrastructure.llm.scripted import ScriptedModelProvider

    dispatcher = container.outbox_dispatcher(
        hisiem=hisiem, model=ScriptedModelProvider()
    )
    await dispatcher.drain_once()
    await dispatcher.drain_once()
    assert await _investigation_count(factory) == 1


async def test_active_investigation_collision_fails_closed(real_env) -> None:
    settings, engine, factory, container = real_env
    hisiem = FakeHisiem(alert_id="es-doc-0001")
    container.hisiem_adapter = hisiem
    # A REAL existing ACTIVE (CREATED, never dispatched) investigation for the
    # same tenant + source alert — created OUTSIDE this execution.
    handler = container.investigation_command_handler()
    await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_ref=ExternalResourceRef(
                provider="hisiem",
                resource_type="alert",
                address_id="es-doc-0001",
                business_id="biz-0001",
            ),
            initiated_by_subject="tester",
        )
    )
    assert await _investigation_count(factory) == 1

    # A new execution for the same dataset must FAIL CLOSED, not silently attach.
    record = await execute_execution(
        dataset_run_id=_DATASET_RUN_ID,
        container=container,
        hisiem=hisiem,
        execution_code=_EXECUTION_CODE,
    )
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_ACTIVE_INVESTIGATION_EXISTS
    assert record.investigation_id is None
    # No new investigation, no reuse/cancel of the pre-existing one.
    assert await _investigation_count(factory) == 1


async def test_drive_exception_terminalizes_and_no_secret(real_env, monkeypatch) -> None:
    settings, engine, factory, container = real_env
    hisiem = FakeHisiem(alert_id="es-doc-0001")
    secrets = [
        "Bearer abc-secret-token",
        "postgresql://user:password@host/db",
        "CMD_API_KEY=secret123",
    ]

    class _RaisingDispatcher:
        async def drain_once(self) -> int:
            raise RuntimeError(f"boom {'; '.join(secrets)}")

    monkeypatch.setattr(
        container,
        "outbox_dispatcher",
        lambda hisiem=None, model=None: _RaisingDispatcher(),
    )

    record = await execute_execution(
        dataset_run_id=_DATASET_RUN_ID,
        container=container,
        hisiem=hisiem,
        execution_code=_EXECUTION_CODE,
    )
    # Start succeeded (an investigation row exists) but the drive threw BEFORE the
    # runner ever persisted an OrchestrationBinding → the record must be a terminal
    # FAILED artifact (never left RUNNING) with investigation_id present and
    # thread_id NULL (no binding was observed — never fabricate inv:<id>).
    assert await _investigation_count(factory) == 1
    assert record.investigation_id is not None
    assert record.thread_id is None
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_DRIVE_FAILED
    assert record.failure.failure_type == "RuntimeError"

    # The raw exception message (which contained the fake secrets) was NOT persisted.
    artifact = execution_artifact_path(
        settings.evaluation.executions_dir, _DATASET_RUN_ID, record.execution_id
    )
    assert artifact.is_file()
    payload = artifact.read_text(encoding="utf-8")
    for secret in secrets:
        assert secret not in payload
    restored = read_record(artifact)
    assert restored.execution_status == ExecutionStatus.FAILED
    assert restored.failure.category == CAT_DRIVE_FAILED
    assert restored.thread_id is None


async def _investigation_status(factory, investigation_id: str) -> str | None:
    async with factory() as session:
        return (
            await session.execute(
                text("SELECT status FROM copilot.investigation WHERE id=:iid"),
                {"iid": UUID(investigation_id)},
            )
        ).scalar()


async def test_concurrent_foreign_winner_ownership_conflict(
    real_env, monkeypatch
) -> None:
    """§7-F: the Evaluation precheck sees NONE; a competing command then creates the
    Investigation; the Evaluation production start CONVERGES on that foreign winner.
    The post-start ownership proof (real command-receipt authority) must detect that
    this execution does not exclusively own the returned aggregate →
    EVALUATION_INVESTIGATION_OWNERSHIP_CONFLICT, fail closed, and leave the foreign
    Investigation untouched (no cancel / reuse / second investigation)."""
    settings, engine, factory, container = real_env
    hisiem = FakeHisiem(alert_id="es-doc-0001")
    container.hisiem_adapter = hisiem
    foreign_id: dict[str, str] = {}

    async def _precheck_then_foreign_wins(
        c: Container, tenant_id: str, external_ref: ExternalResourceRef
    ) -> bool:
        # The harness precheck observed NO active investigation at its instant; a
        # CONCURRENT competing command (real production handler + real receipt
        # semantics — only the interleaving point is injected) now wins the create.
        competing = c.investigation_command_handler()
        inv = await competing.start_alert_investigation(
            StartAlertInvestigation(
                tenant_id=tenant_id,
                source_alert_ref=external_ref,
                initiated_by_subject="foreign-caller",
                initiated_by_display_name="Foreign Caller",
                idempotency_key="foreign:competing:start",
            )
        )
        foreign_id["id"] = str(inv.id)
        return False  # this execution's precheck genuinely saw none

    monkeypatch.setattr(
        harness_mod, "_active_investigation_exists", _precheck_then_foreign_wins
    )

    record = await execute_execution(
        dataset_run_id=_DATASET_RUN_ID,
        container=container,
        hisiem=hisiem,
        execution_code=_EXECUTION_CODE,
    )
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_EVALUATION_INVESTIGATION_OWNERSHIP_CONFLICT
    # The start returned the foreign winner (a real observed fact), but the record
    # never fabricated a thread and the foreign aggregate was never driven/reused.
    assert record.investigation_id == foreign_id["id"]
    assert record.thread_id is None
    assert record.investigation_status is None
    # Exactly ONE investigation exists (the foreign one), still in its CREATED
    # state — untouched by this execution.
    assert await _investigation_count(factory) == 1
    assert await _investigation_status(factory, foreign_id["id"]) == "CREATED"
    artifact = execution_artifact_path(
        settings.evaluation.executions_dir, _DATASET_RUN_ID, record.execution_id
    )
    restored = read_record(artifact)
    assert restored.failure.category == CAT_EVALUATION_INVESTIGATION_OWNERSHIP_CONFLICT


async def test_binding_missing_fails_closed(real_env, monkeypatch) -> None:
    """§7-D/§13: the investigation COMPLETES for real, but no OrchestrationBinding
    can be observed → ORCHESTRATION_BINDING_MISSING, execution FAILED, thread_id NULL,
    secret-safe artifact."""
    settings, engine, factory, container = real_env
    hisiem = FakeHisiem(alert_id="es-doc-0001")
    secret = "Bearer abc-secret-token"

    async def _no_binding(c: Container, tenant_id: str, investigation_id: str) -> None:
        return None

    monkeypatch.setattr(harness_mod, "_observed_binding_thread", _no_binding)

    record = await execute_execution(
        dataset_run_id=_DATASET_RUN_ID,
        container=container,
        hisiem=hisiem,
        execution_code=_EXECUTION_CODE,
    )
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_BINDING_MISSING
    assert record.investigation_id is not None
    assert record.thread_id is None
    # The real runner DID drive it to COMPLETED — the harness refused to mark the
    # execution COMPLETED without an observed binding.
    assert await _investigation_status(factory, record.investigation_id) == "COMPLETED"
    artifact = execution_artifact_path(
        settings.evaluation.executions_dir, _DATASET_RUN_ID, record.execution_id
    )
    payload = artifact.read_text(encoding="utf-8")
    assert secret not in payload
    assert "ORCHESTRATION_BINDING_MISSING" in payload


async def test_checkpoint_missing_fails_closed(real_env, monkeypatch) -> None:
    """§7-D: COMPLETED domain state + binding exists + checkpoint missing → the
    harness must NOT record COMPLETED → CHECKPOINT_MISSING, execution FAILED. The
    observed thread is still recorded (it came from the repository), and the real
    checkpointer genuinely holds state — only the harness's observation is forced
    to report missing so the final gate is what is under test."""
    settings, engine, factory, container = real_env
    hisiem = FakeHisiem(alert_id="es-doc-0001")

    async def _no_checkpoint(langgraph_settings: Any, thread_id: str) -> bool:
        return False

    monkeypatch.setattr(harness_mod, "_checkpoint_has_thread", _no_checkpoint)

    record = await execute_execution(
        dataset_run_id=_DATASET_RUN_ID,
        container=container,
        hisiem=hisiem,
        execution_code=_EXECUTION_CODE,
    )
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_CHECKPOINT_MISSING
    assert record.investigation_id is not None
    # thread_id WAS repository-observed (binding existed) before the checkpoint gate.
    assert record.thread_id is not None
    assert await _investigation_status(factory, record.investigation_id) == "COMPLETED"
    # The real LangGraph checkpoint genuinely exists for the observed thread.
    await _assert_checkpoint_observed(settings, record.thread_id)
    # The observed thread equals the persisted binding.
    async with factory() as session:
        bound_thread = (
            await session.execute(
                text(
                    "SELECT thread_id FROM copilot.orchestration_binding "
                    "WHERE investigation_id=:iid"
                ),
                {"iid": UUID(record.investigation_id)},
            )
        ).scalar()
    assert bound_thread == record.thread_id
