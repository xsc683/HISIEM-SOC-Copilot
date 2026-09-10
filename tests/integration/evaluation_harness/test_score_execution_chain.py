"""E1-C4 scorer + result→finding integrity over real Postgres (§4, §16, §22).

Drives the SAME ``score_execution`` the operator CLI calls against a REAL Copilot
Postgres (schema ``copilot`` on :5433):

- the golden E1-C3 chain (a grounded provider over the real durable pipeline) is
  scored end to end: the persisted ``InvestigationResult`` verdict matches the sealed
  oracle, the grounding Finding PARTICIPATES in ``InvestigationResult.finding_ids``,
  and result→finding integrity holds → E1-C4 PASS;
- a result whose finding set references a Finding owned by ANOTHER investigation is
  rejected (the new tenant-scoped Finding lineage port resolves the foreign owner);
- the new ``find_investigation_ids_by_finding_ids`` port is proven tenant-scoped.

Skipped when Postgres is unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.entities import (
    Evidence,
    EvidenceSource,
    Finding,
    InvestigationResult,
    Verdict,
)
from hisiem_soc_copilot.domain.investigation.enums import (
    EvidenceSourceType,
    VerdictDisposition,
)
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from hisiem_soc_copilot.domain.shared.identifiers import utc_now
from hisiem_soc_copilot.evaluation_harness.harness import dataset_manifest_path
from hisiem_soc_copilot.evaluation_harness.quality import (
    EXPECTED_SOURCE_OPERATION,
    EXPECTED_SOURCE_PROVIDER,
    EXPECTED_SOURCE_TYPE,
    GATE_PASS,
    SEARCH_EVENTS_TOOL_NAME,
    STATUS_SUCCEEDED,
    EvidenceFact,
    FindingFact,
    ToolInvocationFact,
    build_tool_evidence_quality,
    tool_evidence_quality_path,
    write_tool_evidence_quality,
)
from hisiem_soc_copilot.evaluation_harness.quality_harness import (
    execute_tool_evidence_run,
    resolve_scenario_identities,
    verify_dataset_manifest,
)
from hisiem_soc_copilot.evaluation_harness.record import (
    EvaluationExecutionRecord,
    ExecutionStatus,
    execution_artifact_path,
    rfc3339_utc,
    write_record,
)
from hisiem_soc_copilot.evaluation_harness.score import (
    FAIL_RESULT_FINDING_CROSS_INVESTIGATION,
    read_score,
    score_path,
)
from hisiem_soc_copilot.evaluation_harness.score_harness import score_execution
from hisiem_soc_copilot.evaluation_harness.telemetry import (
    ModelTelemetry,
    model_telemetry_path,
    write_model_telemetry,
)
from hisiem_soc_copilot.infrastructure.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from tests.fixtures.ssh_models import GroundedSshModel
from tests.integration.evaluation_harness.test_execute_tool_evidence_chain import (
    _DATASET_RUN_ID,
    _SSH_SCRIPT,
    _TENANT,
    _db_reachable,
    _RecordingScripted,
    _SealedS1Hisiem,
    _settings,
    _truncate,
)
from tests.unit.evaluation_harness._seal_helpers import seal_dataset


def _grounded_provider() -> _RecordingScripted:
    return _RecordingScripted(scripted=GroundedSshModel(script=dict(_SSH_SCRIPT)))


@pytest_asyncio.fixture
async def real_settings(tmp_path: Path) -> AsyncIterator[tuple[Settings, object]]:
    settings = _settings(tmp_path)
    if not await _db_reachable(settings):
        import pytest

        pytest.skip("PostgreSQL not reachable — skipping E1-C4 integration test")
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


async def test_score_execution_golden_chain_passes(real_settings) -> None:
    """The real grounded chain → E1-C4 correctness PASS, with the grounded Finding
    participating in the result."""
    settings, _factory = real_settings
    manifest = verify_dataset_manifest(
        dataset_manifest_path(settings.evaluation, _DATASET_RUN_ID)
    )
    ids = resolve_scenario_identities(manifest)
    hisiem = _SealedS1Hisiem(
        alert_id="es-doc-0001",
        s1_index=ids.s1_index,
        s1_document_id=ids.s1_document_id,
    )
    run = await execute_tool_evidence_run(
        dataset_run_id=_DATASET_RUN_ID,
        settings=settings,
        provider=_grounded_provider(),
        hisiem=hisiem,
        execution_code=("integration-exec-head", False),
    )
    assert run.exit_code == 0, run.quality
    assert run.execution_id is not None

    outcome = await score_execution(
        dataset_run_id=_DATASET_RUN_ID,
        execution_id=run.execution_id,
        settings=settings,
    )
    score = outcome.score
    assert score.correctness_gate == GATE_PASS, score.gate_failures
    assert score.verdict_match is True
    assert score.grounded_required_evidence is True
    assert score.grounded_findings_in_result, "grounded Finding not in the result"
    assert score.result_finding_integrity_pass is True
    assert score.control_exclusion_pass is True
    assert score.citation_integrity_pass is True
    assert score.oracle_firewall_pass is True

    artifact = score_path(
        settings.evaluation.executions_dir, _DATASET_RUN_ID, run.execution_id
    )
    assert artifact.is_file()
    assert read_score(artifact).correctness_gate == GATE_PASS


async def test_cross_investigation_result_finding_is_rejected(real_settings) -> None:
    """A result referencing a Finding owned by ANOTHER investigation is rejected, and
    the tenant-scoped Finding lineage port resolves the foreign owner."""
    settings, factory = real_settings
    executions_dir = settings.evaluation.executions_dir
    execution_id = uuid4().hex
    s1_index = "siem-events-gp01"

    inv_a_id, inv_b_id = uuid4(), uuid4()
    ev_b_id = uuid4()
    finding_a_id, finding_b_id = uuid4(), uuid4()

    uow = SqlAlchemyUnitOfWork(factory)
    try:
        inv_a = Investigation.create(
            id=inv_a_id,
            tenant_id=_TENANT,
            source_alert_ref=ExternalResourceRef(
                provider="hisiem", resource_type="alert", address_id="x-a"
            ),
            initiated_by=ActorRef(subject_id="analyst", tenant_id=_TENANT),
            budget_limits=BudgetLimits(),
        )
        inv_b = Investigation.create(
            id=inv_b_id,
            tenant_id=_TENANT,
            source_alert_ref=ExternalResourceRef(
                provider="hisiem", resource_type="alert", address_id="x-b"
            ),
            initiated_by=ActorRef(subject_id="analyst", tenant_id=_TENANT),
            budget_limits=BudgetLimits(),
        )
        await uow.investigations.add(inv_a)
        await uow.investigations.add(inv_b)
        await uow.commit()

        await uow.findings.add(
            Finding(
                id=finding_a_id,
                investigation_id=inv_a_id,
                statement="foreign finding",
                evidence_citations=[],
            )
        )
        await uow.findings.add(
            Finding(
                id=finding_b_id,
                investigation_id=inv_b_id,
                statement="grounded on the investigation's own S1 evidence",
                evidence_citations=[ev_b_id],
            )
        )
        await uow.evidence.add(
            Evidence(
                id=ev_b_id,
                investigation_id=inv_b_id,
                source=EvidenceSource(
                    type=EvidenceSourceType.HISIEM_LOG_SEARCH,
                    provider=EXPECTED_SOURCE_PROVIDER,
                    operation=EXPECTED_SOURCE_OPERATION,
                ),
                collected_at=utc_now(),
                observation={"event.action": "authentication_success"},
                raw_reference={
                    "index": s1_index,
                    "document_id": "es-doc-S1-x",
                    "query_fingerprint": "fp",
                },
                content_hash="hash",
                dedup_key=str(ev_b_id),
            )
        )
        await uow.commit()

        # Investigation B's result references its OWN grounded finding AND a Finding
        # owned by investigation A (FK-valid, but cross-investigation).
        await uow.results.add(
            InvestigationResult(
                id=uuid4(),
                investigation_id=inv_b_id,
                verdict=Verdict(
                    disposition=VerdictDisposition.MALICIOUS,
                    summary="malicious",
                    confidence=0.9,
                ),
                finding_ids=[finding_b_id, finding_a_id],
            )
        )
        await uow.commit()
    finally:
        await uow.close()

    # The new tenant-scoped Finding lineage port resolves the foreign owner.
    reader = SqlAlchemyUnitOfWork(factory)
    try:
        owners = await reader.findings.find_investigation_ids_by_finding_ids(
            tenant_id=_TENANT, finding_ids=[finding_a_id, uuid4()]
        )
        assert owners == {finding_a_id: inv_a_id}
        assert (
            await reader.findings.find_investigation_ids_by_finding_ids(
                tenant_id="tenant-z", finding_ids=[finding_a_id]
            )
            == {}
        )
    finally:
        await reader.close()

    # Persist the sidecars the scorer reads: a PASS quality + a PASS telemetry, so the
    # ONLY failures come from the result→finding integrity check.
    write_tool_evidence_quality(
        tool_evidence_quality_path(executions_dir, _DATASET_RUN_ID, execution_id),
        build_tool_evidence_quality(
            execution_id=execution_id,
            dataset_run_id=_DATASET_RUN_ID,
            investigation_id=str(inv_b_id),
            execution_completed=True,
            result_present=True,
            model_telemetry_gate=GATE_PASS,
            required_role="S1",
            expected_s1_index=s1_index,
            expected_s1_document_id="es-doc-S1-x",
            expected_control_index="",
            expected_control_document_id="",
            evidence=(
                EvidenceFact(
                    evidence_id=str(ev_b_id),
                    investigation_id=str(inv_b_id),
                    source_type=EXPECTED_SOURCE_TYPE,
                    source_provider=EXPECTED_SOURCE_PROVIDER,
                    source_operation=EXPECTED_SOURCE_OPERATION,
                    source_tool_invocation_id="tool-1",
                    raw_index=s1_index,
                    raw_document_id="es-doc-S1-x",
                    raw_query_fingerprint="fp",
                    content_hash="hash",
                ),
            ),
            findings=(
                FindingFact(
                    finding_id=str(finding_b_id),
                    investigation_id=str(inv_b_id),
                    cited_evidence_ids=(str(ev_b_id),),
                ),
            ),
            tool_invocations=(
                ToolInvocationFact(
                    invocation_id="tool-1",
                    investigation_id=str(inv_b_id),
                    tool_name=SEARCH_EVENTS_TOOL_NAME,
                    status=STATUS_SUCCEEDED,
                ),
            ),
            citation_owners={},
        ),
    )
    write_model_telemetry(
        model_telemetry_path(executions_dir, _DATASET_RUN_ID, execution_id),
        ModelTelemetry(
            execution_id=execution_id,
            dataset_run_id=_DATASET_RUN_ID,
            provider_adapter="openai_compatible",
            provider="command_code",
            protocol="openai_compatible_chat_completions",
            model="deepseek/deepseek-v4-flash",
            resolved_structured_output_mode="json_schema",
            gate_status=GATE_PASS,
        ),
    )
    write_record(
        execution_artifact_path(executions_dir, _DATASET_RUN_ID, execution_id),
        EvaluationExecutionRecord(
            execution_id=execution_id,
            dataset_run_id=_DATASET_RUN_ID,
            tenant_id=_TENANT,
            started_at=rfc3339_utc(),
            finished_at=rfc3339_utc(),
            execution_status=ExecutionStatus.COMPLETED,
            investigation_id=str(inv_b_id),
            result_disposition="MALICIOUS",
        ),
    )

    outcome = await score_execution(
        dataset_run_id=_DATASET_RUN_ID,
        execution_id=execution_id,
        settings=settings,
    )
    score = outcome.score
    assert score.result_finding_integrity_pass is False
    assert FAIL_RESULT_FINDING_CROSS_INVESTIGATION in score.gate_failures
    assert score.correctness_gate != GATE_PASS
    # The scorer resolved the result rows over real PG (its own read path).
    assert set(score.result_finding_ids) == {str(finding_b_id), str(finding_a_id)}
