"""E1-C1 evaluation execution harness — drive a sealed GP-01 manifest through the
REAL production investigation pipeline.

The harness is the ONLY component allowed to hold a full
:class:`SealedManifest` and start a production investigation from it. Oracle
firewall (E1-B.4 §14): production code receives ONLY the typed launch projection
(provider / resource_type / address_id / business_id) via
``StartAlertInvestigation`` — never the oracle, events, control events, or the
sealed object. The graph never sees the manifest.

The runtime stack is REAL: real HISIEM (or an injected fake for tests), real
Copilot PostgreSQL, real LangGraph Postgres checkpoint, real domain/outbox/runner
code, real graph. The model provider is EXPLICIT and SCRIPTED (E1-C1; the real
provider is E1-C2). ``COPILOT_APP_ENABLE_DISPATCHER`` is disabled by the caller;
the harness drives :meth:`drain_once` manually.

The harness is deterministic-bounded: every execution has its own execution_id
and idempotency key ``eval:<dataset_run_id>:<execution_id>:start``; the actor is
``evaluation-runner`` / ``Evaluation Runner``; the produced artifact lives under
``<executions_dir>/gp-01/<dataset_run_id>/<execution_id>/execution.json``.

The dataset is authoritative-only: a manifest that does not verify (schema /
integrity / missing file) fails closed with NO investigation, and a dirty code
revision is NON_AUTHORITATIVE and never launched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from ..application.commands.investigation import StartAlertInvestigation
from ..application.errors import NotFoundError
from ..domain.investigation.value_objects import ExternalResourceRef
from ..evaluation.contracts import SealedManifest
from ..evaluation.launch_projection import launch_ref
from ..evaluation.sealer import verify_sealed_manifest
from ..infrastructure.llm.scripted import ScriptedModelProvider
from .record import (
    EvaluationExecutionRecord,
    ExecutionStatus,
    LaunchProjection,
    execution_artifact_path,
    rfc3339_utc,
    write_record,
)

if TYPE_CHECKING:
    from ..bootstrap.container import Container

logger = logging.getLogger(__name__)

# Scenario segment under which datasets + executions live (E1-C1 targets GP-01).
SCENARIO_SEGMENT = "gp-01"

# The evaluation-runner actor identity stamped on every started investigation.
ACTOR_SUBJECT = "evaluation-runner"
ACTOR_DISPLAY_NAME = "Evaluation Runner"

# Failure categories (bounded — never free-form secrets).
CAT_MANIFEST_NOT_FOUND = "MANIFEST_NOT_FOUND"
CAT_MANIFEST_VERIFY_FAILURE = "MANIFEST_VERIFY_FAILURE"
CAT_MANIFEST_NOT_AUTHORITATIVE = "MANIFEST_NOT_AUTHORITATIVE"
CAT_SOURCE_ALERT_MISSING = "SOURCE_ALERT_MISSING"
CAT_START_FAILED = "START_FAILED"
CAT_DRIVE_FAILED = "DRIVE_FAILED"
CAT_DRIVE_TIMEOUT = "DRIVE_TIMEOUT"
CAT_INVESTIGATION_NOT_COMPLETED = "INVESTIGATION_NOT_COMPLETED"
CAT_INVESTIGATION_RESULT_MISSING = "INVESTIGATION_RESULT_MISSING"

# Terminal investigation statuses (mirrors InvestigationStatus.is_terminal).
_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

_DRAIN_POLL_SECONDS = 0.25
_DRAIN_MAX_IDLE_POLLS = 40  # upper bound on no-op polls before giving up

# E1-C1 deterministic convergence model: immediate FINALIZE, no tool calls. The
# scripted verdict is INCONCLUSIVE WITH an explicit uncertainty, because the
# domain FinalizeInvestigationResult invariant requires >= 1 Uncertainty for an
# INCONCLUSIVE disposition — a bare INCONCLUSIVE verdict would fail the finalize
# step. E1-C1 PASS must NOT depend on matching the oracle, so the neutral
# INCONCLUSIVE convergence is the honest default.
_E1C1_SCRIPTED_VERDICT: dict[str, Any] = {
    "disposition": "INCONCLUSIVE",
    "summary": "Insufficient evidence to reach a firm disposition",
    "confidence": 0.3,
    "uncertainty": (
        "The deterministic E1-C1 model gathered no additional evidence and could "
        "not prove or disprove escalation to a compromise"
    ),
}


# ---------------------------------------------------------------------------
# Pure seam helpers (no container / DB) — unit-testable without Postgres
# ---------------------------------------------------------------------------


def dataset_manifest_path(settings: Any, dataset_run_id: str) -> Path:
    """The authoritative sealed manifest for one dataset run (E1-C1 §7)."""
    return (
        Path(settings.runs_dir)
        / SCENARIO_SEGMENT
        / dataset_run_id
        / "manifest.json"
    )


def verify_dataset_manifest(path: str | Path) -> SealedManifest:
    """Verify the persisted sealed manifest; raises on any schema/integrity break.

    Fail-closed: the harness calls this BEFORE any side effect. A non-verifying
    manifest can never start an investigation.
    """
    return verify_sealed_manifest(path)


def launch_projection(manifest: SealedManifest) -> LaunchProjection:
    """The bounded production-safe projection recorded into the execution record.

    Maps 1:1 onto the production ``ExternalResourceRef`` used to start the
    investigation; never oracle/events content.
    """
    ref = launch_ref(manifest)
    return LaunchProjection(
        provider=ref.provider,
        resource_type=ref.resource_type,
        address_id=ref.address_id,
        business_id=ref.business_id,
    )


def to_external_resource_ref(launch: LaunchProjection) -> ExternalResourceRef:
    return ExternalResourceRef(
        provider=launch.provider,
        resource_type=launch.resource_type,
        address_id=launch.address_id,
        business_id=launch.business_id,
    )


def build_start_command(
    *,
    launch: LaunchProjection,
    tenant_id: str,
    dataset_run_id: str,
    execution_id: str,
) -> StartAlertInvestigation:
    """Build the ONLY production command the graph side ever receives."""
    return StartAlertInvestigation(
        tenant_id=tenant_id,
        source_alert_ref=to_external_resource_ref(launch),
        initiated_by_subject=ACTOR_SUBJECT,
        initiated_by_display_name=ACTOR_DISPLAY_NAME,
        idempotency_key=f"eval:{dataset_run_id}:{execution_id}:start",
    )


def record_from_manifest(
    manifest: SealedManifest, *, execution_id: str, dataset_run_id: str
) -> EvaluationExecutionRecord:
    """Stamp the CREATED execution-record skeleton from a verified manifest.

    Only the launch projection + bounded dataset/code identity are taken from the
    manifest — never oracle/events/control_events. The record is persisted before
    the investigation starts so a crash mid-run still leaves a recoverable record.
    """
    launch = launch_projection(manifest)
    return EvaluationExecutionRecord(
        execution_id=execution_id,
        scenario_id=manifest.scenario.id,
        dataset_run_id=dataset_run_id,
        dataset_manifest_sha256=manifest.integrity.get("manifest_sha256", ""),
        copilot_git_commit=manifest.code.git_commit,
        copilot_dirty=manifest.code.dirty,
        model_provider="scripted",
        tenant_id=str(manifest.scope.get("tenant_id", "")),
        started_at=rfc3339_utc(),
        launch=launch,
        execution_status=ExecutionStatus.CREATED,
    )


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


async def execute_execution(
    *,
    dataset_run_id: str,
    container: Container,
    model: Any | None = None,
    hisiem: Any | None = None,
) -> EvaluationExecutionRecord:
    """Run one E1-C1 execution for ``dataset_run_id`` against an OPEN container.

    Fail-closed + authoritative-only: verification and the clean-code check run
    BEFORE the investigation is started. Returns the terminal (COMPLETED or
    FAILED) record, persisted atomically under ``<executions_dir>/gp-01/...``.

    ``model`` defaults to the deterministic :class:`ScriptedModelProvider` (E1-C1
    never enables a real provider). ``hisiem`` defaults to the container's real
    HISIEM HTTP adapter; tests inject a fake. The container MUST be opened by the
    caller (the DB phase requires its session factory).
    """
    settings = container.settings.evaluation
    execution_id = uuid4().hex
    started_at = rfc3339_utc()
    artifact = execution_artifact_path(
        settings.executions_dir, dataset_run_id, execution_id
    )

    # --- Phase 1: verify manifest, fail closed (no investigation yet). ---------
    manifest_path = dataset_manifest_path(settings, dataset_run_id)
    if not manifest_path.is_file():
        record = EvaluationExecutionRecord(
            execution_id=execution_id,
            dataset_run_id=dataset_run_id,
            started_at=started_at,
            execution_status=ExecutionStatus.CREATED,
        )
        record.fail(
            category=CAT_MANIFEST_NOT_FOUND,
            failure_type="FileNotFoundError",
            message=f"no sealed manifest at {manifest_path}",
        )
        write_record(artifact, record)
        return record

    try:
        manifest = verify_dataset_manifest(manifest_path)
    except Exception as exc:  # noqa: BLE001 — mapped to a bounded FAILED record
        record = EvaluationExecutionRecord(
            execution_id=execution_id,
            dataset_run_id=dataset_run_id,
            started_at=started_at,
            execution_status=ExecutionStatus.CREATED,
        )
        record.fail(
            category=CAT_MANIFEST_VERIFY_FAILURE,
            failure_type=type(exc).__name__,
            message=str(exc),
        )
        write_record(artifact, record)
        return record

    record = record_from_manifest(
        manifest, execution_id=execution_id, dataset_run_id=dataset_run_id
    )
    if record.copilot_dirty or manifest.code.dirty:
        record.fail(
            category=CAT_MANIFEST_NOT_AUTHORITATIVE,
            failure_type="NonAuthoritativeManifest",
            message=(
                "manifest.code.dirty=true — this dataset was sealed from an "
                "uncommitted worktree and is not an authoritative benchmark record"
            ),
        )
        write_record(artifact, record)
        return record

    # Persist CREATED BEFORE any side effect (crash-safe recoverable record).
    write_record(artifact, record)

    # --- Phase 2: start the real investigation via the production command. -----
    if hisiem is not None:
        container.hisiem_adapter = hisiem
    try:
        handler = container.investigation_command_handler()
        investigation = await handler.start_alert_investigation(
            build_start_command(
                launch=record.launch,
                tenant_id=record.tenant_id,
                dataset_run_id=dataset_run_id,
                execution_id=execution_id,
            )
        )
    except NotFoundError as exc:
        record.fail(
            category=CAT_SOURCE_ALERT_MISSING,
            failure_type=type(exc).__name__,
            message=f"source alert {record.launch.address_id!r} not found or not accessible",
        )
        write_record(artifact, record)
        return record
    except Exception as exc:  # noqa: BLE001 — bounded, never leaks the exception object
        record.fail(
            category=CAT_START_FAILED,
            failure_type=type(exc).__name__,
            message=str(exc),
        )
        write_record(artifact, record)
        return record

    investigation_id = str(investigation.id)
    record.investigation_id = investigation_id
    record.thread_id = f"inv:{investigation_id}"
    record.execution_status = ExecutionStatus.RUNNING
    write_record(artifact, record)

    # --- Phase 3: drive the durable outbox dispatcher to a terminal state. -----
    model_provider = (
        model
        if model is not None
        else ScriptedModelProvider(script={"verdict": _E1C1_SCRIPTED_VERDICT})
    )
    dispatcher = container.outbox_dispatcher(hisiem=hisiem, model=model_provider)

    deadline = time.monotonic() + float(container.settings.agent_budget.max_duration_seconds)
    idle_polls = 0
    while True:
        processed = await dispatcher.drain_once()
        snapshot = await _read_snapshot(
            container, tenant_id=record.tenant_id, investigation_id=investigation_id
        )
        if snapshot is not None and snapshot["status"] in _TERMINAL_STATUSES:
            break
        if time.monotonic() >= deadline:
            break
        if processed == 0:
            idle_polls += 1
            if idle_polls >= _DRAIN_MAX_IDLE_POLLS:
                break
        else:
            idle_polls = 0
        await asyncio.sleep(_DRAIN_POLL_SECONDS)

    # --- Phase 4: read the terminal state through repository ports. ------------
    snapshot = await _read_snapshot(
        container, tenant_id=record.tenant_id, investigation_id=investigation_id
    )
    await _finalize_record(record, snapshot)

    # --- Phase 5: persist the terminal record atomically. ----------------------
    write_record(artifact, record)
    logger.info(
        "evaluation execution %s → %s (investigation %s, status %s)",
        execution_id,
        record.execution_status.value,
        investigation_id,
        record.investigation_status,
    )
    return record


async def _read_snapshot(
    container: Container, *, tenant_id: str, investigation_id: str
) -> dict[str, Any] | None:
    """Read the aggregate + counts + result strictly through repository ports."""
    uow = container.unit_of_work()
    try:
        investigation = await uow.investigations.get(
            tenant_id=tenant_id, investigation_id=UUID(investigation_id)
        )
        if investigation is None:
            return None
        evidence = await uow.evidence.list_by_investigation(
            tenant_id=tenant_id, investigation_id=UUID(investigation_id)
        )
        findings = await uow.findings.list_by_investigation(
            tenant_id=tenant_id, investigation_id=UUID(investigation_id)
        )
        hypotheses = await uow.hypotheses.list_by_investigation(
            tenant_id=tenant_id, investigation_id=UUID(investigation_id)
        )
        result = await uow.results.get_by_investigation(
            tenant_id=tenant_id, investigation_id=UUID(investigation_id)
        )
        verdict = result.verdict if result is not None else None
        return {
            "status": investigation.status.value,
            "phase": investigation.phase.value if investigation.phase is not None else None,
            "termination_reason": (
                investigation.termination_reason.value
                if investigation.termination_reason is not None
                else None
            ),
            "evidence_count": len(evidence),
            "finding_count": len(findings),
            "hypothesis_count": len(hypotheses),
            "result_disposition": verdict.disposition.value if verdict is not None else None,
            "result_summary": verdict.summary if verdict is not None else None,
            "result_confidence": verdict.confidence if verdict is not None else None,
        }
    finally:
        await uow.close()


async def _finalize_record(
    record: EvaluationExecutionRecord, snapshot: dict[str, Any] | None
) -> None:
    """Map the terminal DB state onto the record (COMPLETED or FAILED)."""
    if snapshot is None:
        record.fail(
            category=CAT_DRIVE_FAILED,
            failure_type="InvestigationGone",
            message="investigation disappeared from the repository after start",
        )
        return

    record.investigation_status = snapshot["status"]
    record.investigation_phase = snapshot["phase"]
    record.termination_reason = snapshot["termination_reason"]
    record.evidence_count = snapshot["evidence_count"]
    record.finding_count = snapshot["finding_count"]
    record.hypothesis_count = snapshot["hypothesis_count"]
    record.result_disposition = snapshot["result_disposition"]
    record.result_summary = snapshot["result_summary"]
    record.result_confidence = snapshot["result_confidence"]

    if snapshot["status"] == "COMPLETED":
        if record.result_disposition is None:
            record.fail(
                category=CAT_INVESTIGATION_RESULT_MISSING,
                failure_type="ResultNotFound",
                message=(
                    "investigation COMPLETED but no persisted InvestigationResult "
                    "was found through the repository port"
                ),
            )
            return
        record.execution_status = ExecutionStatus.COMPLETED
        record.finished_at = rfc3339_utc()
        return

    if snapshot["status"] in _TERMINAL_STATUSES:
        record.fail(
            category=CAT_INVESTIGATION_NOT_COMPLETED,
            failure_type="TerminalNotCompleted",
            message=f"investigation reached terminal status {snapshot['status']}",
        )
        return

    record.fail(
        category=CAT_DRIVE_TIMEOUT,
        failure_type="DriveTimeout",
        message=(
            f"investigation did not reach a terminal state "
            f"(status {snapshot['status']}) within the drive deadline"
        ),
    )


def report(record: EvaluationExecutionRecord) -> list[str]:
    """A concise human-readable execution report (E1-C1 §22)."""
    lines = [
        f"execution_id={record.execution_id}",
        f"dataset_run_id={record.dataset_run_id}",
        f"execution_status={record.execution_status.value}",
        f"tenant_id={record.tenant_id}",
        f"launch_ref={record.launch.provider}:{record.launch.resource_type} "
        f"address_id={record.launch.address_id}",
        f"investigation_id={record.investigation_id}",
        f"thread_id={record.thread_id}",
        f"investigation_status={record.investigation_status}",
        f"investigation_phase={record.investigation_phase}",
        f"termination_reason={record.termination_reason}",
        f"result_disposition={record.result_disposition} "
        f"result_confidence={record.result_confidence}",
        f"evidence_count={record.evidence_count} "
        f"finding_count={record.finding_count} "
        f"hypothesis_count={record.hypothesis_count}",
    ]
    if record.execution_status == ExecutionStatus.FAILED:
        lines.append(
            f"failure.category={record.failure.category} "
            f"failure.type={record.failure.failure_type} "
            f"failure.message={record.failure.message}"
        )
    return lines


async def execute_cli(*, dataset_run_id: str) -> int:
    """Operator-facing E1-C1 driver: open the real Container, execute, print, exit.

    Lives in the harness (not in ``evaluation.cli``) so the production-infra
    imports (Container/bootstrap) stay OUT of the boundary-checked evaluation
    package. The container is opened here with the process settings; the harness
    fails closed (verify + authoritative gate) before any side effect. Returns 0
    only when the execution COMPLETED.
    """
    from ..bootstrap.container import Container
    from ..config import get_settings

    root = get_settings()
    container = Container(root)
    await container.open()
    try:
        record = await execute_execution(
            dataset_run_id=dataset_run_id, container=container
        )
    finally:
        await container.close()

    for line in report(record):
        print(line)
    artifact = execution_artifact_path(
        root.evaluation.executions_dir, dataset_run_id, record.execution_id
    )
    print(f"execution_artifact={artifact}")
    if record.execution_status == ExecutionStatus.COMPLETED:
        return 0
    return 1
