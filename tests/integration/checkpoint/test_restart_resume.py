"""AsyncPostgresSaver restart/resume over real PostgreSQL.

A graph run that crashes mid-way (after committing rows but before finishing) must,
on a fresh process/run with the SAME thread (via OrchestrationBinding), resume from
the LangGraph checkpoint to COMPLETED without duplicating Plan/Evidence/Result rows
— the receipt-idempotent workflow commands + evidence dedup + immutable result make
re-execution safe.

Skipped when Postgres is unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hisiem_soc_copilot.agent.evidence.normalizer import EvidenceNormalizer
from hisiem_soc_copilot.agent.graph.builder import build_investigation_graph
from hisiem_soc_copilot.agent.graph.runtime import GraphRuntime
from hisiem_soc_copilot.agent.tools.executor import ToolExecutor
from hisiem_soc_copilot.agent.tools.registry import ToolRegistry
from hisiem_soc_copilot.application.handlers.workflow import InvestigationWorkflowHandler
from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from hisiem_soc_copilot.infrastructure.durable.investigation_runner import (
    AsyncInvestigationGraphRunner,
)
from hisiem_soc_copilot.infrastructure.llm.scripted import ScriptedModelProvider
from hisiem_soc_copilot.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork
from tests.fixtures.hisiem_fake import FakeHisiem
from tests.fixtures.ssh_models import GroundedSshModel

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


def _settings() -> Settings:
    s = Settings()
    s.database.database_url = (
        "postgresql+psycopg://copilot:copilot@127.0.0.1:5432/copilot"
    )
    s.langgraph.database_url = s.database.database_url
    return s


async def _db_reachable() -> bool:
    try:
        import psycopg
        from sqlalchemy.engine import make_url

        url = make_url(_settings().database.database_url)
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


@pytest_asyncio.fixture
async def pg_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if not await _db_reachable():
        import pytest

        pytest.skip("PostgreSQL not reachable — skipping checkpoint resume test")
    settings = _settings()
    engine = create_async_engine(settings.database.database_url)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        from sqlalchemy import text

        await session.execute(
            text(f"TRUNCATE copilot.{', copilot.'.join(_TRUNCATE)} RESTART IDENTITY CASCADE")
        )
        await session.commit()
    yield factory
    async with factory() as session:
        from sqlalchemy import text

        await session.execute(
            text(f"TRUNCATE copilot.{', copilot.'.join(_TRUNCATE)} RESTART IDENTITY CASCADE")
        )
        await session.commit()
    await engine.dispose()


class _CrashOnceOnVerdict(GroundedSshModel):
    """Grounded model that raises on the FIRST ``verdict`` call (mid-run crash).

    Extends GroundedSshModel so run 1's ``assess`` grounds its Finding on the real
    evidence ids (strict grounding, Fix #2) exactly as run 2's healthy model does —
    otherwise run 1 would commit no Finding and the no-duplicate comparison below
    would be meaningless (0 pre vs 1 post). The verdict is consulted by
    ``finalize_result`` — AFTER the evidence + findings nodes have committed their
    rows but BEFORE ``finalize_result``/``complete`` checkpoint. Raising here
    simulates a process death whose already-committed work has no checkpoint yet:
    the resume must not duplicate that work.
    """

    def __init__(self, *, script: dict[str, object]) -> None:
        super().__init__(script=dict(script))
        self._crashed = False

    async def verdict(self, request: object) -> object:
        if not self._crashed:
            self._crashed = True
            raise RuntimeError("simulated process crash before verdict")
        return await super().verdict(request)


def _runner(
    factory: async_sessionmaker[AsyncSession],
    *,
    hisiem: FakeHisiem,
    model: ScriptedModelProvider | None = None,
) -> AsyncInvestigationGraphRunner:
    uow_factory = lambda: SqlAlchemyUnitOfWork(factory)  # noqa: E731
    handler = InvestigationWorkflowHandler(unit_of_work_factory=uow_factory)

    def _runtime(tenant_id: str) -> GraphRuntime:
        return GraphRuntime(
            uow_factory=uow_factory,
            workflow_handler=handler,
            model=model or _scripted_ssh_model(),
            executor=ToolExecutor(hisiem=hisiem),
            normalizer=EvidenceNormalizer(),
            registry=ToolRegistry(),
            hisiem=hisiem,
            tenant_id=tenant_id,
        )

    return AsyncInvestigationGraphRunner(
        unit_of_work_factory=uow_factory,
        workflow_handler=handler,
        runtime_factory=_runtime,
        compile_graph=build_investigation_graph,
        checkpoint_settings=_settings().langgraph,
    )


def _ssh_script() -> dict[str, object]:
    return {
        "decide": [
            {
                "tool_name": "hisiem.get_detection_rule",
                "arguments": {"rule_id": "ssh_brute_force"},
            },
            {
                "tool_name": "hisiem.search_events",
                "arguments": {
                    "from": "2026-09-01T09:55:00Z",
                    "to": "2026-09-01T10:05:00Z",
                    "conditions": [
                        {
                            "field": "event.action",
                            "operator": "is",
                            "value": "authentication_success",
                        }
                    ],
                },
            },
        ],
        "findings": ["root login after brute force"],
        "verdict": {
            "disposition": "MALICIOUS",
            "summary": "SSH compromise confirmed",
            "confidence": 0.9,
        },
    }


def _scripted_ssh_model() -> ScriptedModelProvider:
    return GroundedSshModel(script=dict(_ssh_script()))


async def test_postgres_checkpoint_restart_resume_no_duplicates(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A crash after committing rows resumes on the same thread without dup rows."""
    inv = Investigation.create(
        id=uuid4(),
        tenant_id="tenant-a",
        source_alert_ref=ExternalResourceRef(
            provider="hisiem", resource_type="alert", address_id="resume-alert-1"
        ),
        initiated_by=ActorRef(subject_id="analyst", tenant_id="tenant-a"),
        budget_limits=BudgetLimits(),
    )
    uow = SqlAlchemyUnitOfWork(pg_factory)
    try:
        await uow.investigations.add(inv)
        await uow.commit()
    finally:
        await uow.close()

    # Run 1: the model crashes at the verdict call — evidence + findings have
    # already committed, but finalize/complete never checkpointed. A fresh process
    # would lose that node's checkpoint while the domain rows stay.
    hisiem = FakeHisiem(alert_id="resume-alert-1")
    crashing_model = _CrashOnceOnVerdict(script=dict(_ssh_script()))
    runner = _runner(pg_factory, hisiem=hisiem, model=crashing_model)
    try:
        await runner.run_investigation(
            investigation_id=str(inv.id), tenant_id="tenant-a"
        )
    except RuntimeError:
        pass  # simulated process death
    else:
        raise AssertionError("expected the simulated crash to abort run 1")

    # Snapshot what committed before the crash.
    uow = SqlAlchemyUnitOfWork(pg_factory)
    try:
        pre_evidence = await uow.evidence.list_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        pre_findings = await uow.findings.list_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        pre_binding = await uow.bindings.get(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        pre_status = await uow.investigations.get(
            tenant_id="tenant-a", investigation_id=inv.id
        )
    finally:
        await uow.close()
    assert pre_binding is not None
    assert pre_status is not None and pre_status.status.value != "COMPLETED"
    assert len(pre_evidence) >= 1  # evidence committed before the crash
    # Run 1's assess already grounded + committed its Finding before the verdict
    # crash (strict grounding), so the resume's no-duplicate check is meaningful.
    assert len(pre_findings) == 1

    # Run 2 (fresh process): a healthy model resumes the SAME thread from the
    # AsyncPostgresSaver checkpoint and reaches COMPLETED.
    resume_runner = _runner(pg_factory, hisiem=hisiem, model=_scripted_ssh_model())
    await resume_runner.run_investigation(
        investigation_id=str(inv.id), tenant_id="tenant-a"
    )

    uow = SqlAlchemyUnitOfWork(pg_factory)
    try:
        post_evidence = await uow.evidence.list_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        post_findings = await uow.findings.list_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        post_result = await uow.results.get_by_investigation(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        post_binding = await uow.bindings.get(
            tenant_id="tenant-a", investigation_id=inv.id
        )
        final = await uow.investigations.get(
            tenant_id="tenant-a", investigation_id=inv.id
        )
    finally:
        await uow.close()

    # Domain reached COMPLETED with a result, the binding is stable, and rows that
    # committed before the crash were NOT duplicated by the resume.
    assert final is not None and final.status.value == "COMPLETED"
    assert post_result is not None and post_result.verdict.disposition.value == "MALICIOUS"
    assert post_binding is not None and post_binding.thread_id == pre_binding.thread_id
    # Dedup: evidence committed before the crash was not re-created, and findings
    # (receipt-idempotent on the convergence key) were not re-applied.
    pre_keys = {e.dedup_key for e in pre_evidence}
    post_keys = {e.dedup_key for e in post_evidence}
    assert pre_keys <= post_keys  # no evidence lost
    assert len(post_evidence) == len(post_keys)  # no duplicate dedup keys
    assert len(post_findings) == len(pre_findings)  # no duplicate findings
