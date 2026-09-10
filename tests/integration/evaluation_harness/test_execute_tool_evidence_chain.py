"""E1-C3 tool/evidence quality chain over real Postgres (E1-C3 §3/§7/§11/§13).

Drives the SAME ``execute_tool_evidence_run`` orchestration the operator CLI calls:
it reuses the E1-C2 real-model path UNCHANGED (an INJECTED grounded provider drives
the REAL durable pipeline over real PG), then evaluates the persisted investigation
against the sealed manifest's expected S1 identity and writes
``tool-evidence-quality.json``.

- a grounded provider + a HISIEM double whose success hit carries the EXACT sealed
  S1 (index/document_id) → the Agent's Evidence matches S1, links to the SUCCEEDED
  ``hisiem.search_events`` invocation, grounds a Finding → E1-C3 PASS (exit 0), and
  all three artifacts (execution / telemetry / quality) live in one directory;
- a HISIEM double whose hit does NOT match the sealed identity → COMPLETED + model
  gate PASS but E1-C3 FAIL (NO_S1_EVIDENCE) — a VALID failure (exit 1);
- the tenant-scoped Evidence→Investigation lineage port is proven against real PG
  and feeds the evaluator's cross-investigation citation check.

Skipped when Postgres is unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hisiem_soc_copilot.application.ports.hisiem import LogEventHit
from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.entities import (
    Evidence,
    EvidenceSource,
    Finding,
)
from hisiem_soc_copilot.domain.investigation.enums import EvidenceSourceType
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
    GATE_FAIL,
    GATE_PASS,
    SEARCH_EVENTS_TOOL_NAME,
    STATUS_SUCCEEDED,
    EvidenceFact,
    FindingFact,
    ToolInvocationFact,
    build_tool_evidence_quality,
    read_tool_evidence_quality,
    tool_evidence_quality_path,
)
from hisiem_soc_copilot.evaluation_harness.quality_harness import (
    execute_tool_evidence_run,
    resolve_scenario_identities,
    verify_dataset_manifest,
)
from hisiem_soc_copilot.evaluation_harness.record import (
    ExecutionStatus,
    execution_artifact_path,
    read_record,
)
from hisiem_soc_copilot.evaluation_harness.telemetry import model_telemetry_path
from hisiem_soc_copilot.infrastructure.llm.scripted import ScriptedModelProvider
from hisiem_soc_copilot.infrastructure.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from tests.fixtures.hisiem_fake import FakeHisiem
from tests.fixtures.ssh_models import GroundedSshModel
from tests.unit.evaluation_harness._seal_helpers import seal_dataset

_DATASET_RUN_ID = "e1c3-chain-run"
_TENANT = "tenant-a"

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

_SSH_SCRIPT: dict[str, Any] = {
    "plan_steps": {
        "read_rule": "Read the detection rule that fired",
        "search_success": "Search for a successful authentication after failures",
    },
    "decide": [
        {
            "tool_name": "hisiem.get_detection_rule",
            "arguments": {"rule_id": "ssh_brute_force"},
            "reason": "Understand the rule that fired",
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
                    },
                    {"field": "user.name", "operator": "is", "value": "root"},
                ],
                "limit": 50,
            },
            "reason": "Look for a successful login after the failures",
        },
    ],
    "findings": [
        "A successful root login followed a burst of SSH brute-force failures "
        "from the same source IP (203.0.113.9)"
    ],
    "verdict": {
        "disposition": "MALICIOUS",
        "summary": "SSH brute force escalated into a successful account compromise",
        "confidence": 0.85,
        "uncertainty": None,
    },
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
    # the REAL provider is never built because a double is always injected.
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


class _RecordingScripted:
    """A grounded-scripted provider double recording usage on the SAME instance the
    graph consults, implementing the E1-C2 introspection surface."""

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


class _SealedS1Hisiem(FakeHisiem):
    """A HISIEM double whose successful-auth hit carries the SEALED S1 identity.

    The Agent has no way to know this identity — it discovers the event by a real
    search; only the provider's own document/index make it the golden S1.
    """

    def __init__(self, *, alert_id: str, s1_index: str, s1_document_id: str) -> None:
        super().__init__(alert_id=alert_id)
        self._s1_index = s1_index
        self._s1_document_id = s1_document_id

    def _hit(self, doc: str, action: str, ts: str) -> LogEventHit:
        hit = super()._hit(doc, action, ts)
        if action == "authentication_success":
            return LogEventHit(
                document_id=self._s1_document_id,
                index=self._s1_index,
                timestamp=hit.timestamp,
                event_category=hit.event_category,
                event_action=hit.event_action,
                source_ip=hit.source_ip,
                user_name=hit.user_name,
                host_name=hit.host_name,
                log_source_id=hit.log_source_id,
            )
        return hit


def _grounded_provider() -> _RecordingScripted:
    return _RecordingScripted(
        scripted=GroundedSshModel(script=dict(_SSH_SCRIPT))
    )


@pytest_asyncio.fixture
async def real_settings(tmp_path: Path) -> AsyncIterator[tuple[Settings, Any]]:
    """Sealed dataset + reachable DB, WITHOUT an open container.

    ``execute_tool_evidence_run`` opens (and closes) its OWN containers, so this
    fixture only seals + truncates and yields ``(settings, session_factory)`` for the
    post-run DB assertions and the lineage test.
    """
    settings = _settings(tmp_path)
    if not await _db_reachable(settings):
        import pytest

        pytest.skip("PostgreSQL not reachable — skipping E1-C3 integration test")
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


def _sealed_identity(settings: Settings):
    manifest = verify_dataset_manifest(
        dataset_manifest_path(settings.evaluation, _DATASET_RUN_ID)
    )
    return resolve_scenario_identities(manifest)


async def test_execute_tool_evidence_run_golden_chain_passes(real_settings) -> None:
    """§11 golden chain: real pipeline → S1 discovered + grounded → E1-C3 PASS."""
    settings, _factory = real_settings
    ids = _sealed_identity(settings)
    hisiem = _SealedS1Hisiem(
        alert_id="es-doc-0001",
        s1_index=ids.s1_index,
        s1_document_id=ids.s1_document_id,
    )
    result = await execute_tool_evidence_run(
        dataset_run_id=_DATASET_RUN_ID,
        settings=settings,
        provider=_grounded_provider(),
        hisiem=hisiem,
        execution_code=("integration-exec-head", False),
    )
    assert result.exit_code == 0
    assert result.quality is not None
    assert result.quality.gate_status == GATE_PASS, result.quality.gate_failures
    assert result.quality.quality_attempt_valid is True
    assert result.quality.successful_search_events_invocation_count >= 1
    assert result.quality.matched_s1_evidence_ids, "no S1 evidence matched"
    assert result.quality.findings_citing_s1_evidence, "no Finding cited S1"
    assert result.quality.dangling_citation_count == 0
    assert result.quality.cross_investigation_citation_count == 0
    assert result.quality.control_event_evidence_ids == ()

    executions_dir = settings.evaluation.executions_dir
    exec_id = result.execution_id
    assert exec_id is not None
    exec_artifact = execution_artifact_path(executions_dir, _DATASET_RUN_ID, exec_id)
    telemetry_artifact = model_telemetry_path(executions_dir, _DATASET_RUN_ID, exec_id)
    quality_artifact = tool_evidence_quality_path(executions_dir, _DATASET_RUN_ID, exec_id)
    # The three artifacts for ONE execution live in the SAME directory.
    assert exec_artifact.parent == telemetry_artifact.parent == quality_artifact.parent
    assert exec_artifact.is_file()
    assert telemetry_artifact.is_file()
    assert quality_artifact.is_file()

    record = read_record(exec_artifact)
    assert record.execution_status == ExecutionStatus.COMPLETED
    assert record.result_disposition == "MALICIOUS"

    restored = read_tool_evidence_quality(quality_artifact)
    assert restored.gate_status == GATE_PASS
    assert restored.matched_s1_evidence_ids == result.quality.matched_s1_evidence_ids

    # The quality artifact carries no secret / oracle marker (§4/§11).
    text_dump = quality_artifact.read_text(encoding="utf-8")
    for forbidden in (
        "api_key",
        "CMD_API_KEY",
        "Bearer",
        "sk-",
        "expected_verdict",
        "required_evidence_roles",
    ):
        assert forbidden not in text_dump, f"quality artifact leaked {forbidden!r}"


async def test_execute_tool_evidence_run_wrong_identity_is_valid_fail(
    real_settings,
) -> None:
    """COMPLETED + model gate PASS but the provider's hit is NOT the sealed S1 → a
    VALID E1-C3 failure (exit 1) whose sidecar is still written."""
    settings, _factory = real_settings
    # The plain fake returns index ``siem-events-2026.09.01`` / document ``evt-succ-1``
    # — NOT the sealed S1 identity.
    hisiem = FakeHisiem(alert_id="es-doc-0001")
    result = await execute_tool_evidence_run(
        dataset_run_id=_DATASET_RUN_ID,
        settings=settings,
        provider=_grounded_provider(),
        hisiem=hisiem,
        execution_code=("integration-exec-head", False),
    )
    assert result.exit_code == 1
    assert result.quality is not None
    assert result.quality.gate_status == GATE_FAIL
    # The attempt itself was valid (COMPLETED + model gate PASS): the Agent simply
    # did not surface the golden S1 event as the sealed provider identity.
    assert result.quality.quality_attempt_valid is True
    assert "NO_S1_EVIDENCE" in result.quality.gate_failures
    assert result.quality.matched_s1_evidence_ids == ()

    exec_id = result.execution_id
    assert exec_id is not None
    quality_artifact = tool_evidence_quality_path(
        settings.evaluation.executions_dir, _DATASET_RUN_ID, exec_id
    )
    assert quality_artifact.is_file()
    assert read_tool_evidence_quality(quality_artifact).gate_status == GATE_FAIL


async def test_repository_lineage_is_tenant_scoped_cross_investigation(
    real_settings,
) -> None:
    """Prove the tenant-scoped Evidence→Investigation lineage port over real PG and
    that its result drives the evaluator's cross-investigation citation check."""
    _settings_obj, factory = real_settings
    s1_index = "siem-events-gp01"
    s1_doc = "es-doc-S1-lineage"

    inv_a_id, ev_a_id = uuid4(), uuid4()
    inv_b_id, ev_b_id, finding_b_id = uuid4(), uuid4(), uuid4()

    uow = SqlAlchemyUnitOfWork(factory)
    try:
        inv_a = Investigation.create(
            id=inv_a_id,
            tenant_id=_TENANT,
            source_alert_ref=ExternalResourceRef(
                provider="hisiem", resource_type="alert", address_id="lineage-a"
            ),
            initiated_by=ActorRef(subject_id="analyst", tenant_id=_TENANT),
            budget_limits=BudgetLimits(),
        )
        inv_b = Investigation.create(
            id=inv_b_id,
            tenant_id=_TENANT,
            source_alert_ref=ExternalResourceRef(
                provider="hisiem", resource_type="alert", address_id="lineage-b"
            ),
            initiated_by=ActorRef(subject_id="analyst", tenant_id=_TENANT),
            budget_limits=BudgetLimits(),
        )
        await uow.investigations.add(inv_a)
        await uow.investigations.add(inv_b)
        await uow.commit()

        def _evidence(eid, inv_id) -> Evidence:
            return Evidence(
                id=eid,
                investigation_id=inv_id,
                source=EvidenceSource(
                    type=EvidenceSourceType.HISIEM_LOG_SEARCH,
                    provider=EXPECTED_SOURCE_PROVIDER,
                    operation=EXPECTED_SOURCE_OPERATION,
                ),
                collected_at=utc_now(),
                observation={"event.action": "authentication_success"},
                raw_reference={
                    "index": s1_index,
                    "document_id": s1_doc,
                    "query_fingerprint": "fp",
                },
                content_hash="hash",
                dedup_key=str(eid),
            )

        await uow.evidence.add(_evidence(ev_a_id, inv_a_id))
        await uow.evidence.add(_evidence(ev_b_id, inv_b_id))
        await uow.commit()

        await uow.findings.add(
            Finding(
                id=finding_b_id,
                investigation_id=inv_b_id,
                statement="cite a foreign investigation's evidence",
                evidence_citations=[ev_a_id],
            )
        )
        await uow.commit()
    finally:
        await uow.close()

    # The tenant-scoped lineage query resolves each cited id to its owning
    # investigation; an unknown/absent id is simply missing.
    reader = SqlAlchemyUnitOfWork(factory)
    try:
        owners = await reader.evidence.find_investigation_ids_by_evidence_ids(
            tenant_id=_TENANT, evidence_ids=[ev_a_id, ev_b_id, uuid4()]
        )
        assert owners == {ev_a_id: inv_a_id, ev_b_id: inv_b_id}
        # Tenant scoping: another tenant sees nothing.
        assert (
            await reader.evidence.find_investigation_ids_by_evidence_ids(
                tenant_id="tenant-z", evidence_ids=[ev_a_id]
            )
            == {}
        )
    finally:
        await reader.close()

    # Feed the real lineage into the evaluator: investigation B's own S1 evidence is
    # fine, but its Finding cites investigation A's evidence → cross-investigation.
    owner_map = {str(k): str(v) for k, v in owners.items()}
    quality = build_tool_evidence_quality(
        execution_id="exec-lineage",
        dataset_run_id=_DATASET_RUN_ID,
        investigation_id=str(inv_b_id),
        execution_completed=True,
        result_present=True,
        model_telemetry_gate=GATE_PASS,
        required_role="S1",
        expected_s1_index=s1_index,
        expected_s1_document_id=s1_doc,
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
                raw_document_id=s1_doc,
                raw_query_fingerprint="fp",
                content_hash="hash",
            ),
        ),
        findings=(
            FindingFact(
                finding_id=str(finding_b_id),
                investigation_id=str(inv_b_id),
                cited_evidence_ids=(str(ev_a_id),),
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
        citation_owners=owner_map,
    )
    assert quality.gate_status == GATE_FAIL
    assert quality.cross_investigation_citation_count == 1
    assert "CROSS_INVESTIGATION_EVIDENCE_CITATION" in quality.gate_failures
    assert "NO_S1_EVIDENCE" not in quality.gate_failures  # S1 itself is matched
