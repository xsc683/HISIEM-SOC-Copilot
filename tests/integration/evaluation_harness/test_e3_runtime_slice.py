"""E3 runtime-integrated scenarios (Stage E / E3 §21, §25, §34).

Two E3 scenarios declare the ``runtime-integrated`` profile in the frozen catalog,
and only those two are executed against real processes here:

``XP-AUTH-005`` — HISIEM observed result is execution truth
    Real HISIEM control-api + SOAR worker, a real ``HisiemSoarAdapter``, a real
    provider execution id, and the provider's own observed state read back from
    HISIEM. Copilot's submission state is never treated as the execution result.

``XP-REL-005`` — Observability backend outage
    The real OpenTelemetry Collector (repo config) and two real Copilot worker
    processes — one with the collector reachable, one with it down — whose
    PERSISTED business outcomes must be identical.

Both are skipped, with an explicit reason, when the required runtime is absent. The
runtime is the established Stage D local slice: no production code is changed to
make either scenario executable, and no credential is printed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.entities import (
    Evidence,
    EvidenceSource,
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
from hisiem_soc_copilot.evaluation.cross_plane import GateStatus
from hisiem_soc_copilot.evaluation_harness.cross_plane_authority import (
    observed_execution_truth,
    submission_truth,
    telemetry_isolation,
    telemetry_isolation_facts,
)
from hisiem_soc_copilot.evaluation_harness.cross_plane_e3_scenarios import (
    E3Fixture,
    evaluate_e3_scenario,
    run_e3_scenario_to_artifact,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env.local"
HISIEM_BASE_URL = "http://127.0.0.1:8080"
HISIEM_TENANT = "default"
PLAYBOOK_ID = "pb-a3b539a1-fa97-478e-8344-7a36d8e87816"
COLLECTOR_NAME = "e3-otel-collector"
COLLECTOR_IMAGE = "otel/opentelemetry-collector-contrib:0.115.1"
COLLECTOR_CONFIG = REPO_ROOT / "infra" / "otel-collector" / "collector.yaml"
OTLP_ENDPOINT = "http://127.0.0.1:4317"
TOKEN_ENV = "HISIEM_COPILOT_SERVICE_TOKEN"


_ENV_CACHE: dict[str, str] | None = None


def _env_values() -> dict[str, str]:
    """Read the gitignored dev env WITHOUT printing it and WITHOUT touching os.environ.

    Deliberately NOT loaded into the process environment: doing so would change what
    every later ``Settings()`` in the same pytest session resolves to, which is a
    cross-test side effect rather than a runtime requirement. The one value a child
    process needs is passed to that child explicitly.
    """
    global _ENV_CACHE
    if _ENV_CACHE is None:
        values: dict[str, str] = {}
        if ENV_FILE.is_file():
            for raw in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key:
                    values[key] = value.strip().strip('"').strip("'")
        _ENV_CACHE = values
    return _ENV_CACHE


def _service_token() -> str:
    return os.environ.get(TOKEN_ENV, "").strip() or _env_values().get(TOKEN_ENV, "")


def _docker() -> str | None:
    return shutil.which("docker")


def _hisiem_ready() -> bool:
    import httpx

    try:
        response = httpx.get(f"{HISIEM_BASE_URL}/actuator/health", timeout=5.0)
    except Exception:
        return False
    # Readiness is judged by the SOAR API being reachable, not by the aggregate
    # health indicator (Elasticsearch is deliberately not part of this slice).
    try:
        probe = httpx.get(
            f"{HISIEM_BASE_URL}/api/internal/soar/executions/e3-nonexistent",
            headers={"X-Tenant-ID": HISIEM_TENANT},
            timeout=5.0,
        )
    except Exception:
        return False
    return response.status_code in {200, 503} and probe.status_code in {401, 403, 404}


def _soar_worker_running() -> bool:
    import httpx

    token = _service_token()
    if not token:
        return False
    try:
        response = httpx.post(
            f"{HISIEM_BASE_URL}/api/internal/soar/executions",
            headers={
                "X-Tenant-ID": HISIEM_TENANT,
                "Idempotency-Key": f"e3-readiness-{uuid4()}",
            },
            json={},
            timeout=10.0,
        )
    except Exception:
        return False
    # A 400 means the boundary is alive and validating; the worker itself is proven
    # by the scenario reaching a terminal state.
    return response.status_code in {400, 401, 403}


@pytest.fixture(scope="module")
def hisiem_runtime() -> Iterator[None]:
    if not _service_token():
        pytest.skip("E3 runtime slice: no HISIEM service token is configured")
    if not _hisiem_ready():
        pytest.skip("E3 runtime slice: HISIEM control-api is not reachable on 8080")
    if not _soar_worker_running():
        pytest.skip("E3 runtime slice: the HISIEM internal SOAR boundary is not answering")
    yield


@pytest.fixture
def collector() -> Iterator[str]:
    """A real OTel collector running the repo's own configuration."""
    docker = _docker()
    if docker is None:
        pytest.skip("E3 runtime slice: the docker CLI is not available")
    if not COLLECTOR_CONFIG.is_file():
        pytest.skip("E3 runtime slice: the repo collector configuration is missing")

    subprocess.run(
        [docker, "rm", "-f", COLLECTOR_NAME], capture_output=True, check=False
    )
    run = subprocess.run(
        [
            docker,
            "run",
            "-d",
            "--name",
            COLLECTOR_NAME,
            "-p",
            "4317:4317",
            "-p",
            "4318:4318",
            "-v",
            f"{COLLECTOR_CONFIG}:/etc/otelcol-contrib/config.yaml:ro",
            "-e",
            "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4319",
            COLLECTOR_IMAGE,
            "--config=/etc/otelcol-contrib/config.yaml",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        pytest.skip(f"E3 runtime slice: collector did not start ({run.stderr.strip()[:120]})")
    try:
        yield COLLECTOR_NAME
    finally:
        subprocess.run(
            [docker, "rm", "-f", COLLECTOR_NAME], capture_output=True, check=False
        )


def _container_settings(database_url: str) -> Any:
    from hisiem_soc_copilot.config import Settings

    settings = Settings()
    settings.database.database_url = database_url
    settings.langgraph.database_url = database_url
    settings.auth.trusted_context_provider = "hisiem_bearer"
    settings.auth.hisiem_service_token_env = TOKEN_ENV
    settings.soar.base_url = HISIEM_BASE_URL
    settings.soar.bearer_token = _service_token()
    settings.app.response_observe_interval_seconds = 0.0
    return settings


async def _seed(container: Any) -> tuple[UUID, UUID]:
    from hisiem_soc_copilot.domain.shared.identifiers import utc_now

    now = utc_now()
    investigation_id = uuid4()
    uow = container.unit_of_work()
    try:
        investigation = Investigation.create(
            id=investigation_id,
            tenant_id=HISIEM_TENANT,
            source_alert_ref=ExternalResourceRef(
                provider="hisiem",
                resource_type="alert",
                address_id=f"e3-runtime-alert-{uuid4().hex[:8]}",
                business_id="AL-E3",
            ),
            initiated_by=ActorRef(subject_id="analyst", tenant_id=HISIEM_TENANT),
            budget_limits=BudgetLimits(),
            now=now,
        )
        investigation.start(
            actor=ActorRef(subject_id="analyst", tenant_id=HISIEM_TENANT), now=now
        )
        investigation.complete_without_response()
        await uow.investigations.add(investigation)
        await uow.commit()

        evidence = Evidence(
            id=uuid4(),
            investigation_id=investigation_id,
            source=EvidenceSource(
                type=EvidenceSourceType.HISIEM_LOG_SEARCH,
                provider="hisiem",
                operation="log_search",
            ),
            collected_at=now,
            observation={"summary": "sudo auth failure from 203.0.113.9"},
        )
        await uow.evidence.add(evidence)
        result = InvestigationResult(
            id=uuid4(),
            investigation_id=investigation_id,
            verdict=Verdict(
                disposition=VerdictDisposition.MALICIOUS,
                summary="confirmed",
                confidence=0.9,
            ),
            finding_ids=[],
            created_at=now,
        )
        await uow.results.add(result)
        await uow.commit()
    finally:
        await uow.close()
    return investigation_id, evidence.id


# ---------------------------------------------------------------------------
# XP-AUTH-005 — HISIEM observed result is execution truth
# ---------------------------------------------------------------------------


async def test_xp_auth_005_real_hisiem_execution_is_the_final_truth(
    hisiem_runtime: None,
    scratch_db_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from asgi_lifespan import LifespanManager
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from hisiem_soc_copilot.api.app import create_app
    from hisiem_soc_copilot.application.commands.response import (
        CreateResponseProposal,
        DecideResponseApproval,
    )
    from hisiem_soc_copilot.domain.response.enums import ApprovalDecisionKind

    tenant = HISIEM_TENANT
    # Scoped to THIS test: the container reads its service credential from the
    # environment and fails closed without it, and monkeypatch restores the
    # environment afterwards so no later test inherits a different configuration.
    monkeypatch.setenv(TOKEN_ENV, _service_token())
    engine = create_async_engine(scratch_db_url)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        await session.execute(
            text(
                "TRUNCATE copilot.response_submission, copilot.response_execution_ref, "
                "copilot.approval_decision, copilot.approval_request, "
                "copilot.response_proposal_evidence, copilot.response_proposal_target, "
                "copilot.response_proposal, copilot.outbox_message, copilot.domain_event, "
                "copilot.investigation_result, copilot.evidence, copilot.investigation "
                "RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()

    settings = _container_settings(scratch_db_url)
    app = create_app(settings)
    async with LifespanManager(app):
        container = app.state.container
        investigation_id, evidence_id = await _seed(container)

        # The REAL SOAR adapter against the REAL HISIEM internal boundary.
        soar = container.soar()
        handler = container.response_command_handler()
        proposal = await handler.create_response_proposal(
            CreateResponseProposal(
                tenant_id=tenant,
                investigation_id=investigation_id,
                action_key="START_SOAR_PLAYBOOK",
                evidence_ids=(str(evidence_id),),
                parameters={"playbook_id": PLAYBOOK_ID},
                reason="E3 runtime slice: observe real execution truth",
                initiated_by_subject="analyst",
            )
        )
        uow = container.unit_of_work()
        try:
            request = await uow.response_approvals.get_request_by_proposal(
                tenant_id=tenant, proposal_id=proposal.id
            )
        finally:
            await uow.close()
        assert request is not None
        await handler.decide_response_approval(
            DecideResponseApproval(
                tenant_id=tenant,
                approval_request_id=request.id,
                decision=ApprovalDecisionKind.APPROVE,
                expected_revision=proposal.content_revision,
                expected_content_hash=proposal.content_hash,
                initiated_by_subject="operator",
            )
        )

        submit = container.response_submit_outbox_dispatcher(soar=soar)
        assert await submit.drain_once() >= 1

        uow = container.unit_of_work()
        try:
            approved = await uow.response_proposals.get(
                tenant_id=tenant, proposal_id=proposal.id
            )
            submission = await uow.response_submissions.get_by_proposal(
                tenant_id=tenant, proposal_id=proposal.id
            )
            execution = await uow.response_executions.get_by_proposal(
                tenant_id=tenant, proposal_id=proposal.id
            )
            decision = await uow.response_approvals.get_decision(
                tenant_id=tenant, approval_request_id=request.id
            )
        finally:
            await uow.close()

        # HISIEM accepted the submission and returned a REAL provider identity.
        assert execution is not None, "HISIEM did not accept the submission"
        provider_execution_id = execution.execution_id
        assert provider_execution_id and provider_execution_id != str(proposal.id)
        assert submission is not None and submission.status == "SUBMITTED"

        # Before a terminal observation, the local submission is NOT a success.
        pending_truth = submission_truth(submission=submission, execution=execution)
        assert pending_truth.submission_treated_as_success is False
        early = evaluate_e3_scenario(
            "XP-AUTH-004",
            E3Fixture(
                proposal=approved,
                approval_request=request,
                approval_decision=decision,
                submission=submission,
                execution=execution,
            ),
        )
        assert early.overall_gate is GateStatus.PASS

        # Observe until HISIEM reports a terminal state.
        observe = container.response_observe_outbox_dispatcher(soar=soar)
        observed_provider_status = execution.status
        final_execution = execution
        for _ in range(20):
            # Let the REAL observe runner reconcile, then read HISIEM independently.
            async with factory() as session:
                await session.execute(
                    text(
                        "UPDATE copilot.outbox_message SET "
                        "available_at = now() - interval '1 hour', status = 'PENDING', "
                        "locked_at = NULL, locked_by = NULL, lease_token = NULL "
                        "WHERE destination = 'response.execution.observe' "
                        "AND status <> 'PUBLISHED'"
                    )
                )
                await session.commit()
            await observe.drain_once()

            # Independent read from HISIEM itself, never from Copilot's projection.
            provider = await soar.get_execution_status(
                tenant_id=tenant, execution_id=provider_execution_id
            )
            observed_provider_status = provider.status
            uow = container.unit_of_work()
            try:
                final_execution = await uow.response_executions.get_by_proposal(
                    tenant_id=tenant, proposal_id=proposal.id
                )
            finally:
                await uow.close()
            if (
                observed_provider_status in {"SUCCEEDED", "FAILED"}
                and final_execution is not None
                and final_execution.status == observed_provider_status
            ):
                break
        await submit.stop()
        await observe.stop()
        await soar.close()

        uow = container.unit_of_work()
        try:
            final_submission = await uow.response_submissions.get_by_proposal(
                tenant_id=tenant, proposal_id=proposal.id
            )
        finally:
            await uow.close()

        assert final_execution is not None
        truths = observed_execution_truth(
            execution=final_execution, hisiem_observed_status=observed_provider_status
        )
        assert "EXECUTION_OBSERVED_FROM_HISIEM" in truths.observed_facts, (
            f"Copilot projected {final_execution.status!r} while HISIEM observed "
            f"{observed_provider_status!r}"
        )

        fixture = E3Fixture(
            proposal=approved,
            approval_request=request,
            approval_decision=decision,
            submission=final_submission,
            execution=final_execution,
            execution_observed=True,
            hisiem_observed_status=observed_provider_status,
        )
        result, path = run_e3_scenario_to_artifact(
            "XP-AUTH-005", fixture, executions_dir=tmp_path
        )
        assert result.overall_gate is GateStatus.PASS
        assert path.is_file()
        # A provider terminal state is never invented by Copilot: whether HISIEM
        # succeeded or failed, the Copilot row carries that same state.
        assert final_execution.status == observed_provider_status

    await engine.dispose()


async def test_xp_auth_005_copilot_cannot_claim_a_state_hisiem_did_not_report(
    hisiem_runtime: None, scratch_db_url: str
) -> None:
    """The adversarial half: a projection that disagrees with HISIEM is caught."""
    from hisiem_soc_copilot.domain.response.value_objects import ResponseExecutionRef

    settings = _container_settings(scratch_db_url)
    # A projection claiming SUCCEEDED while HISIEM says the execution is RUNNING.
    fabricated = ResponseExecutionRef(
        proposal_id=uuid4(),
        provider="hisiem",
        execution_id="exec-fabricated",
        submission_key="response:default:fabricated",
        status="SUCCEEDED",
        submitted_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        last_observed_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    facts = observed_execution_truth(execution=fabricated, hisiem_observed_status="RUNNING")
    assert facts.observed_facts == ("SUBMISSION_TREATED_AS_SUCCESS_PRESENT",)
    del settings


# ---------------------------------------------------------------------------
# XP-REL-005 — observability backend outage
# ---------------------------------------------------------------------------


def _run_worker(database_url: str, otlp_endpoint: str) -> dict[str, Any]:
    """One real Copilot worker process; returns its persisted business outcome."""
    # The child needs the configured credential; it is passed explicitly and never
    # printed, and the parent's environment is left untouched.
    child_env = {**os.environ, TOKEN_ENV: _service_token()}
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.support.e3_runtime_telemetry_driver",
            database_url,
            otlp_endpoint,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=child_env,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            return dict(json.loads(line)["business_outcome"])
    raise AssertionError("the E3 telemetry driver produced no business outcome")


async def test_xp_rel_005_a_telemetry_outage_does_not_change_the_business_outcome(
    collector: str, scratch_db_url: str, tmp_path: Path
) -> None:
    docker = _docker()
    assert docker is not None

    # Run 1: the real collector is up and accepting the Copilot's OTLP export.
    with_collector = _run_worker(scratch_db_url, OTLP_ENDPOINT)

    # Run 2: the telemetry backend is gone entirely.
    # Blocking on purpose: the telemetry backend must be genuinely gone before the
    # second real worker process starts.
    subprocess.run(  # noqa: ASYNC221
        [docker, "stop", collector], capture_output=True, check=False
    )
    without_collector = _run_worker(scratch_db_url, OTLP_ENDPOINT)

    measurement = telemetry_isolation(
        business_outcome_with_telemetry=with_collector,
        business_outcome_without_telemetry=without_collector,
    )
    facts = telemetry_isolation_facts(
        business_outcome_with_telemetry=with_collector,
        business_outcome_without_telemetry=without_collector,
    )
    assert "TELEMETRY_OUTAGE_ISOLATED" in facts.observed_facts, (
        f"telemetry changed the business outcome: {with_collector} != {without_collector}"
    )
    assert (
        measurement.business_fact_fingerprint_with_telemetry
        == measurement.business_fact_fingerprint_without_telemetry
    )

    fixture = E3Fixture(
        business_outcome_with_telemetry=with_collector,
        business_outcome_without_telemetry=without_collector,
    )
    result, path = run_e3_scenario_to_artifact(
        "XP-REL-005", fixture, executions_dir=tmp_path
    )
    assert result.overall_gate is GateStatus.PASS
    assert path.is_file()


async def test_xp_rel_005_a_business_outcome_that_depended_on_telemetry_fails() -> None:
    """The negative half: outcomes that differ under outage fail the gate."""
    healthy = {
        "proposal_status": "SUBMITTED",
        "submission_status": "SUBMITTED",
        "execution_status": "SUCCEEDED",
    }
    degraded = dict(healthy, execution_status="FAILED")
    result = evaluate_e3_scenario(
        "XP-REL-005",
        E3Fixture(
            business_outcome_with_telemetry=healthy,
            business_outcome_without_telemetry=degraded,
        ),
    )
    assert result.overall_gate is GateStatus.FAIL
    assert result.gate_failures == ("TELEMETRY_CHANGED_BUSINESS_STATE",)
