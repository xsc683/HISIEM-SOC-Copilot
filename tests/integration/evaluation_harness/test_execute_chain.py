"""E1-C1 execute chain over real Postgres (E1-C1 §22 live proof, scripted HISIEM).

Drives one sealed GP-01 manifest through the REAL production pipeline: real
Copilot PostgreSQL (5433), real LangGraph Postgres checkpoint, real
domain/outbox/runner code, real graph — with an injected FakeHisiem (alert
hydration) and the explicit deterministic ScriptedModelProvider. Asserts the
sealed manifest -> verify -> launch projection ONLY -> StartAlertInvestigation ->
outbox -> dispatcher drain -> binding/thread -> graph -> COMPLETED -> persisted
InvestigationResult -> COMPLETED Evaluation Execution Record. Also asserts the
artifact file and that a duplicate drain does not re-run a terminal
investigation.

Skipped when Postgres is unreachable (same convention as the durable API chain).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hisiem_soc_copilot.bootstrap.container import Container
from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.evaluation_harness.harness import execute_execution
from hisiem_soc_copilot.evaluation_harness.record import (
    ExecutionStatus,
    execution_artifact_path,
    read_record,
)
from hisiem_soc_copilot.infrastructure.llm.scripted import ScriptedModelProvider
from tests.fixtures.hisiem_fake import FakeHisiem
from tests.unit.evaluation_harness._seal_helpers import seal_dataset

_DATASET_RUN_ID = "e1c1-chain-run"

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


async def _db_counts(
    factory: async_sessionmaker[AsyncSession], investigation_id: str
) -> dict[str, Any]:
    async with factory() as session:
        return {
            "investigations": (
                await session.execute(text("SELECT count(*) FROM copilot.investigation"))
            ).scalar(),
            "bindings": (
                await session.execute(
                    text(
                        "SELECT count(*) FROM copilot.orchestration_binding "
                        "WHERE investigation_id=:iid"
                    ),
                    {"iid": investigation_id},
                )
            ).scalar(),
            "result_disposition": (
                await session.execute(
                    text(
                        "SELECT verdict_disposition FROM copilot.investigation_result "
                        "WHERE investigation_id=:iid"
                    ),
                    {"iid": investigation_id},
                )
            ).scalar(),
            "outbox_published": (
                await session.execute(
                    text(
                        "SELECT count(*) FROM copilot.outbox_message o "
                        "JOIN copilot.domain_event e ON e.event_id=o.event_id "
                        "WHERE e.aggregate_id=:iid AND o.status='PUBLISHED'"
                    ),
                    {"iid": investigation_id},
                )
            ).scalar(),
        }


async def test_execute_chain_reaches_completed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    if not await _db_reachable(settings):
        pytest.skip("PostgreSQL not reachable — skipping E1-C1 execute chain test")

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
        hisiem = FakeHisiem(alert_id="es-doc-0001")  # matches make_verified's alert
        record = await execute_execution(
            dataset_run_id=_DATASET_RUN_ID,
            container=container,
            hisiem=hisiem,
        )
        artifact = execution_artifact_path(
            settings.evaluation.executions_dir, _DATASET_RUN_ID, record.execution_id
        )

        # The execution record is terminal + complete, with the persisted result.
        assert record.execution_status == ExecutionStatus.COMPLETED
        assert record.investigation_id is not None
        assert record.thread_id == f"inv:{record.investigation_id}"
        assert record.investigation_status == "COMPLETED"
        assert record.result_disposition == "INCONCLUSIVE"  # scripted verdict
        assert record.result_confidence == 0.3
        # Counts reflect the REAL graph: FINALIZE converges with no tool calls
        # (zero evidence/findings) but the pipeline registers its working
        # hypothesis during planning.
        assert record.evidence_count == 0
        assert record.finding_count == 0
        assert record.hypothesis_count >= 1

        # The artifact file is the record (readable + atomic, no temp leftovers).
        assert artifact.is_file()
        restored = read_record(artifact)
        assert restored.execution_id == record.execution_id
        assert restored.execution_status == ExecutionStatus.COMPLETED

        # Real DB: one investigation, one binding, published outbox, a result.
        counts = await _db_counts(factory, record.investigation_id)
        assert counts["investigations"] == 1
        assert counts["bindings"] == 1
        assert counts["outbox_published"] == 1
        assert counts["result_disposition"] == "INCONCLUSIVE"

        # A second drain is a no-op for the terminal investigation (never re-run):
        # the terminal aggregate wins over the checkpoint, so no new rows appear.
        dispatcher = container.outbox_dispatcher(
            hisiem=hisiem, model=ScriptedModelProvider()
        )
        await dispatcher.drain_once()
        await dispatcher.drain_once()
        after = await _db_counts(factory, record.investigation_id)
        assert after["investigations"] == 1
        assert after["bindings"] == 1
        assert after["result_disposition"] == "INCONCLUSIVE"
    finally:
        await container.close()
        await engine.dispose()
