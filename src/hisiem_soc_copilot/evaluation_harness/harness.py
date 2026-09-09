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
provider is E1-C2). ``COPILOT_APP_ENABLE_DISPATCHER`` must be disabled BEFORE the
Container opens (E1-C1 isolation) and the harness drives :meth:`drain_once`
manually.

E1-C1 correctness guarantees:
- Dataset vs execution provenance are SEPARATE: ``dataset_*`` comes from
  ``manifest.code``; ``execution_*`` is the CURRENT clean Copilot HEAD/worktree
  resolved before any side effect. Execution provenance is anchored to the Git
  repository CONTAINING the executed Copilot source (derived from the package
  path, never the caller CWD); a worktree that is dirty fails closed, and
  provenance that cannot be proven fails closed as ``EXECUTION_PROVENANCE_UNAVAILABLE``.
- Fail-closed gates before any Investigation side effect: manifest verify +
  dataset authority, dataset identity (``dataset_run_id`` == ``manifest.run.run_id``),
  environment isolation, clean/provable execution provenance, and no pre-existing
  ACTIVE investigation for the same tenant/source alert.
- ``thread_id`` is an OBSERVED fact only — NULL until the production
  OrchestrationBinding is read back through the repository; the deterministic
  ``inv:<id>`` value is never fabricated into a RUNNING/FAILED record.
- Post-start ownership proof: after ``StartAlertInvestigation`` returns, the
  returned Investigation is verified (via the execution-scoped command receipt +
  exclusive aggregate binding) to have been created by THIS execution. A
  concurrent foreign Investigation that won after the precheck is never silently
  reused → ``EVALUATION_INVESTIGATION_OWNERSHIP_CONFLICT``, fail closed.
- Every unexpected post-start runtime exception is terminalized to a FAILED
  record (``DRIVE_FAILED``) — never left RUNNING. Failure diagnostics persist
  only bounded safe category/type/fixed-message — never raw ``str(exc)``.
- On COMPLETED the OrchestrationBinding and the LangGraph Postgres checkpoint for
  the OBSERVED thread are verified through the existing persistence APIs before
  the execution may be COMPLETED.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from ..application.commands.investigation import StartAlertInvestigation
from ..application.errors import NotFoundError
from ..domain.investigation.content import compute_content_hash
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
from .telemetry import (
    GATE_PASS,
    build_model_telemetry,
    model_telemetry_path,
    write_model_telemetry,
)

if TYPE_CHECKING:
    from ..bootstrap.container import Container

logger = logging.getLogger(__name__)

# Scenario segment under which datasets + executions live (E1-C1 targets GP-01).
SCENARIO_SEGMENT = "gp-01"

# The evaluation-runner actor identity stamped on every started investigation.
ACTOR_SUBJECT = "evaluation-runner"
ACTOR_DISPLAY_NAME = "Evaluation Runner"


class EvaluationProfile(StrEnum):
    """Typed evaluation-harness profile (policy only — the production graph and
    Container stay provider-neutral).

    ``E1_C1_SCRIPTED`` (the ``execute`` command) requires the deterministic
    scripted provider; ``E1_C2_REAL_MODEL`` (the ``execute-real-model`` command)
    requires the REAL OpenAI-compatible provider. Both require the background
    dispatcher disabled so the harness drives :meth:`drain_once` manually.
    """

    E1_C1_SCRIPTED = "E1_C1_SCRIPTED"
    E1_C2_REAL_MODEL = "E1_C2_REAL_MODEL"

# Failure categories (bounded — never free-form secrets).
CAT_MANIFEST_NOT_FOUND = "MANIFEST_NOT_FOUND"
CAT_MANIFEST_VERIFY_FAILURE = "MANIFEST_VERIFY_FAILURE"
CAT_MANIFEST_NOT_AUTHORITATIVE = "MANIFEST_NOT_AUTHORITATIVE"
CAT_DATASET_IDENTITY_MISMATCH = "DATASET_IDENTITY_MISMATCH"
CAT_DISPATCHER_ENABLED = "DISPATCHER_ENABLED"
CAT_NON_SCRIPTED_PROVIDER = "NON_SCRIPTED_PROVIDER"
CAT_REAL_MODEL_PROVIDER_REQUIRED = "REAL_MODEL_PROVIDER_REQUIRED"
CAT_MODEL_CONFIGURATION = "MODEL_CONFIGURATION"
CAT_EXECUTION_WORKTREE_DIRTY = "EXECUTION_WORKTREE_DIRTY"
CAT_EXECUTION_PROVENANCE_UNAVAILABLE = "EXECUTION_PROVENANCE_UNAVAILABLE"
CAT_ACTIVE_INVESTIGATION_EXISTS = "ACTIVE_INVESTIGATION_EXISTS"
CAT_EVALUATION_INVESTIGATION_OWNERSHIP_CONFLICT = (
    "EVALUATION_INVESTIGATION_OWNERSHIP_CONFLICT"
)
CAT_SOURCE_ALERT_MISSING = "SOURCE_ALERT_MISSING"
CAT_START_FAILED = "START_FAILED"
CAT_DRIVE_FAILED = "DRIVE_FAILED"
CAT_DRIVE_TIMEOUT = "DRIVE_TIMEOUT"
CAT_INVESTIGATION_NOT_COMPLETED = "INVESTIGATION_NOT_COMPLETED"
CAT_INVESTIGATION_RESULT_MISSING = "INVESTIGATION_RESULT_MISSING"
CAT_BINDING_MISSING = "ORCHESTRATION_BINDING_MISSING"
CAT_CHECKPOINT_MISSING = "CHECKPOINT_MISSING"

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

# Fixed, secret-safe failure messages. Generic runtime exceptions NEVER have their
# ``str(exc)`` persisted; only the exception TYPE NAME (safe) + these constants.
_FIXED_MESSAGES: dict[str, str] = {
    CAT_MANIFEST_NOT_FOUND: "no sealed manifest found for the dataset run",
    CAT_MANIFEST_VERIFY_FAILURE: "sealed manifest failed verification (schema or integrity)",
    CAT_MANIFEST_NOT_AUTHORITATIVE: (
        "dataset code is not authoritative (manifest.code.dirty=true); a sealed "
        "dataset sealed from an uncommitted worktree is not an authoritative "
        "benchmark record"
    ),
    CAT_DATASET_IDENTITY_MISMATCH: (
        "dataset_run_id does not match the sealed manifest run identity"
    ),
    CAT_DISPATCHER_ENABLED: (
        "the durable outbox dispatcher is enabled "
        "(COPILOT_APP_ENABLE_DISPATCHER); E1-C1 requires it disabled"
    ),
    CAT_NON_SCRIPTED_PROVIDER: (
        "the configured LLM provider is not 'scripted'; E1-C1 requires "
        "LLM_PROVIDER=scripted"
    ),
    CAT_REAL_MODEL_PROVIDER_REQUIRED: (
        "the configured LLM provider is not 'openai_compatible'; the E1-C2 "
        "real-model profile requires LLM_PROVIDER=openai_compatible"
    ),
    CAT_MODEL_CONFIGURATION: (
        "the real model provider could not be constructed from configuration "
        "(missing API key / SDK / invalid settings); E1-C2 fails closed before any "
        "investigation"
    ),
    CAT_EXECUTION_WORKTREE_DIRTY: (
        "the execution worktree has uncommitted changes; E1-C1 requires a clean "
        "authoritative Copilot HEAD at execution time"
    ),
    CAT_EXECUTION_PROVENANCE_UNAVAILABLE: (
        "execution provenance could not be proven (the executed Copilot source is "
        "not inside a resolvable Git checkout); E1-C1 fails closed on unprovable "
        "provenance"
    ),
    CAT_ACTIVE_INVESTIGATION_EXISTS: (
        "an active investigation already exists for this tenant/source alert; "
        "not silently reusing it"
    ),
    CAT_EVALUATION_INVESTIGATION_OWNERSHIP_CONFLICT: (
        "the investigation returned by StartAlertInvestigation is not bound "
        "exclusively to this execution — a concurrent foreign command created it; "
        "not reusing a foreign investigation"
    ),
    CAT_SOURCE_ALERT_MISSING: (
        "source alert not found or not accessible through HISIEM"
    ),
    CAT_START_FAILED: "failed to start the investigation",
    CAT_DRIVE_FAILED: "evaluation drive failed",
    CAT_DRIVE_TIMEOUT: (
        "investigation did not reach a terminal state within the drive deadline"
    ),
    CAT_INVESTIGATION_NOT_COMPLETED: "investigation reached a non-COMPLETED terminal status",
    CAT_INVESTIGATION_RESULT_MISSING: (
        "investigation COMPLETED but no persisted InvestigationResult was found"
    ),
    CAT_BINDING_MISSING: (
        "investigation completed but no OrchestrationBinding was observed"
    ),
    CAT_CHECKPOINT_MISSING: (
        "investigation completed but no LangGraph checkpoint state was observed "
        "for the orchestration thread"
    ),
}

# Failure type names for deterministic (non-exception) fail-closed gates.
_FIXED_TYPES: dict[str, str] = {
    CAT_MANIFEST_NOT_FOUND: "ManifestNotFound",
    CAT_MANIFEST_NOT_AUTHORITATIVE: "NonAuthoritativeManifest",
    CAT_DATASET_IDENTITY_MISMATCH: "DatasetIdentityMismatch",
    CAT_DISPATCHER_ENABLED: "DispatcherEnabled",
    CAT_NON_SCRIPTED_PROVIDER: "NonScriptedProvider",
    CAT_REAL_MODEL_PROVIDER_REQUIRED: "RealModelProviderRequired",
    CAT_MODEL_CONFIGURATION: "ModelConfiguration",
    CAT_EXECUTION_WORKTREE_DIRTY: "DirtyExecutionWorktree",
    CAT_EXECUTION_PROVENANCE_UNAVAILABLE: "ExecutionProvenanceUnavailable",
    CAT_ACTIVE_INVESTIGATION_EXISTS: "ActiveInvestigationExists",
    CAT_EVALUATION_INVESTIGATION_OWNERSHIP_CONFLICT: (
        "EvaluationInvestigationOwnershipConflict"
    ),
    CAT_DRIVE_TIMEOUT: "DriveTimeout",
    CAT_INVESTIGATION_NOT_COMPLETED: "TerminalNotCompleted",
    CAT_INVESTIGATION_RESULT_MISSING: "ResultNotFound",
    CAT_BINDING_MISSING: "OrchestrationBindingMissing",
    CAT_CHECKPOINT_MISSING: "CheckpointMissing",
}


# ---------------------------------------------------------------------------
# Code provenance resolution (execution time, NOT dataset seal time)
# ---------------------------------------------------------------------------


def _provenance_anchor() -> Path:
    """Default provenance anchor: this executed module's real file location.

    The Copilot repository root is derived by walking UP from this anchor until a
    ``.git`` is found — never from ``os.getcwd()``/``Path.cwd()``, because the CLI
    may be invoked from an arbitrary directory or even from inside a different Git
    repository. Tests may point the anchor at a disposable checkout to exercise
    the real resolver hermetically.
    """
    return Path(__file__).resolve()


def _git_capture(*args: str, repo_root: Path) -> str | None:
    """Best-effort ``git -C <repo_root> <args>`` (explicit root, never the CWD)."""
    try:
        out = subprocess.run(
            ("git", "-C", str(repo_root), *args),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def resolve_execution_provenance(
    anchor: Path | None = None,
) -> tuple[str, bool] | None:
    """Resolve ``(current_commit, dirty)`` for the checkout containing the executed
    Copilot source.

    The repository root is the nearest ancestor of ``anchor`` (default: this
    module's file) that contains ``.git``; Git then runs with an explicit
    ``git -C <repo_root>`` so caller CWD can never influence the result. Returns
    None when the root cannot be proven (the executed source is not inside a Git
    checkout, or Git is unavailable/errors) — the caller must then fail closed
    with ``EXECUTION_PROVENANCE_UNAVAILABLE`` (NOT merely treat the tree as dirty).
    """
    start = (anchor or _provenance_anchor()).resolve()
    directory = start if start.is_dir() else start.parent
    repo_root: Path | None = None
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            repo_root = candidate
            break
    if repo_root is None:
        return None
    commit = _git_capture("rev-parse", "HEAD", repo_root=repo_root)
    if commit is None:
        return None
    porcelain = _git_capture("status", "--porcelain", repo_root=repo_root)
    if porcelain is None:
        return None
    return commit, porcelain != ""


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


def execution_isolation_violation(
    settings: Any, profile: EvaluationProfile = EvaluationProfile.E1_C1_SCRIPTED
) -> str | None:
    """Return the isolation-failure category, or None when the profile's isolation
    holds.

    Both evaluation profiles must run with the durable outbox dispatcher DISABLED
    and the harness drives :meth:`drain_once` manually. The provider requirement is
    profile-specific: E1_C1 requires ``scripted``; E1_C2_REAL_MODEL requires
    ``openai_compatible``. This MUST be checked before the Container opens (so
    ``Container.open()`` never starts a background dispatcher that could race the
    harness's manual dispatcher using the real provider).
    """
    if settings.app.enable_dispatcher:
        return CAT_DISPATCHER_ENABLED
    if profile == EvaluationProfile.E1_C2_REAL_MODEL:
        if settings.llm.provider != "openai_compatible":
            return CAT_REAL_MODEL_PROVIDER_REQUIRED
        return None
    if settings.llm.provider != "scripted":
        return CAT_NON_SCRIPTED_PROVIDER
    return None


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


def _start_idempotency_key(dataset_run_id: str, execution_id: str) -> str:
    """The execution-scoped idempotency key binding one start to one execution."""
    return f"eval:{dataset_run_id}:{execution_id}:start"


def _launch_fingerprint(launch: LaunchProjection) -> str:
    """The bounded fingerprint of the launch request (same payload the production
    handler stores on the command receipt for a StartAlertInvestigation)."""
    return compute_content_hash(
        {
            "provider": launch.provider,
            "resource_type": launch.resource_type,
            "address_id": launch.address_id,
            "business_id": launch.business_id,
        }
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
        idempotency_key=_start_idempotency_key(dataset_run_id, execution_id),
    )


def record_from_manifest(
    manifest: SealedManifest, *, execution_id: str, dataset_run_id: str
) -> EvaluationExecutionRecord:
    """Stamp the CREATED execution-record skeleton from a verified manifest.

    DATASET code provenance comes from ``manifest.code`` (the revision that
    created the dataset). EXECUTION code provenance is stamped separately from the
    CURRENT Copilot worktree — never from the manifest. Only the launch projection
    + bounded dataset/code identity are taken from the manifest — never
    oracle/events/control_events. The record is persisted before the investigation
    starts so a crash mid-run still leaves a recoverable record.
    """
    launch = launch_projection(manifest)
    return EvaluationExecutionRecord(
        execution_id=execution_id,
        scenario_id=manifest.scenario.id,
        dataset_run_id=dataset_run_id,
        dataset_manifest_sha256=manifest.integrity.get("manifest_sha256", ""),
        dataset_code_git_commit=manifest.code.git_commit,
        dataset_code_dirty=manifest.code.dirty,
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
    execution_code: tuple[str, bool] | None = None,
    profile: EvaluationProfile = EvaluationProfile.E1_C1_SCRIPTED,
) -> EvaluationExecutionRecord:
    """Run one evaluation execution for ``dataset_run_id`` against an OPEN container.

    Fail-closed + authoritative-only: profile isolation, verification, dataset
    identity, dataset authority, clean execution provenance, and the active-
    investigation guard all run BEFORE the investigation is started. Returns the
    terminal (COMPLETED or FAILED) record, persisted atomically under
    ``<executions_dir>/gp-01/...``. Any unexpected post-start runtime exception is
    terminalized to a FAILED ``DRIVE_FAILED`` record (never left RUNNING).

    ``profile`` selects the harness policy: ``E1_C1_SCRIPTED`` (deterministic
    scripted model) or ``E1_C2_REAL_MODEL`` (REAL provider — ``model`` MUST be a
    single provider instance obtained from :meth:`Container.model_provider`, so the
    graph calls and the E1-C2 telemetry share ONE usage buffer). When ``model`` is
    None under ``E1_C2_REAL_MODEL`` the provider is built from the container's
    single construction path HERE — before any Investigation — and a provider
    configuration failure (e.g. missing API key) fails closed with NO investigation.
    ``hisiem`` defaults to the container's real HISIEM HTTP adapter; tests inject a
    fake. ``execution_code`` overrides the CURRENT Copilot revision (tests inject
    it; the CLI/operator path resolves the real HEAD). The container MUST be opened
    by the caller (the DB phase requires its session factory).
    """
    settings = container.settings
    eval_settings = settings.evaluation
    execution_id = uuid4().hex
    artifact = execution_artifact_path(
        eval_settings.executions_dir, dataset_run_id, execution_id
    )

    def persist(record: EvaluationExecutionRecord) -> EvaluationExecutionRecord:
        write_record(artifact, record)
        return record

    # --- Phase 0: profile isolation (must hold before Container side effects). ---
    violation = execution_isolation_violation(settings, profile)
    if violation is not None:
        record = EvaluationExecutionRecord(
            execution_id=execution_id,
            dataset_run_id=dataset_run_id,
            model_provider=str(settings.llm.provider),
            started_at=rfc3339_utc(),
            execution_status=ExecutionStatus.CREATED,
        )
        record.fail(
            category=violation,
            failure_type=_FIXED_TYPES[violation],
            message=_FIXED_MESSAGES[violation],
        )
        return persist(record)

    # --- Phase 0.5: resolve the ONE model provider BEFORE any investigation. -----
    if model is not None:
        model_provider = model
    elif profile == EvaluationProfile.E1_C2_REAL_MODEL:
        try:
            model_provider = container.model_provider()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — configuration error, bounded
            # Provider construction failed (missing API key / SDK / invalid
            # settings): fail closed with NO investigation. Only the bounded
            # category/type is persisted — never str(exc)/the key.
            record = EvaluationExecutionRecord(
                execution_id=execution_id,
                dataset_run_id=dataset_run_id,
                model_provider=str(settings.llm.provider),
                started_at=rfc3339_utc(),
                execution_status=ExecutionStatus.CREATED,
            )
            record.fail(
                category=CAT_MODEL_CONFIGURATION,
                failure_type=type(exc).__name__,
                message=_FIXED_MESSAGES[CAT_MODEL_CONFIGURATION],
            )
            return persist(record)
    else:
        model_provider = ScriptedModelProvider(
            script={"verdict": _E1C1_SCRIPTED_VERDICT}
        )

    # --- Phase 1: verify manifest (fail closed, no investigation). ---------------
    manifest_path = dataset_manifest_path(eval_settings, dataset_run_id)
    if not manifest_path.is_file():
        record = EvaluationExecutionRecord(
            execution_id=execution_id,
            dataset_run_id=dataset_run_id,
            started_at=rfc3339_utc(),
            execution_status=ExecutionStatus.CREATED,
        )
        record.fail(
            category=CAT_MANIFEST_NOT_FOUND,
            failure_type=_FIXED_TYPES[CAT_MANIFEST_NOT_FOUND],
            message=_FIXED_MESSAGES[CAT_MANIFEST_NOT_FOUND],
        )
        return persist(record)

    try:
        manifest = verify_dataset_manifest(manifest_path)
    except Exception as exc:  # noqa: BLE001 — type name only, never str(exc)
        record = EvaluationExecutionRecord(
            execution_id=execution_id,
            dataset_run_id=dataset_run_id,
            started_at=rfc3339_utc(),
            execution_status=ExecutionStatus.CREATED,
        )
        record.fail(
            category=CAT_MANIFEST_VERIFY_FAILURE,
            failure_type=type(exc).__name__,
            message=_FIXED_MESSAGES[CAT_MANIFEST_VERIFY_FAILURE],
        )
        return persist(record)

    # --- Phase 2: dataset identity + provenance + authority (no investigation). ---
    record = record_from_manifest(
        manifest, execution_id=execution_id, dataset_run_id=dataset_run_id
    )
    # The record's model_provider mirrors the ACTUAL configured provider (scripted
    # under E1-C1; openai_compatible under the E1-C2 real-model profile).
    record.model_provider = str(settings.llm.provider)

    if dataset_run_id != manifest.run.run_id:
        record.fail(
            category=CAT_DATASET_IDENTITY_MISMATCH,
            failure_type=_FIXED_TYPES[CAT_DATASET_IDENTITY_MISMATCH],
            message=_FIXED_MESSAGES[CAT_DATASET_IDENTITY_MISMATCH],
        )
        return persist(record)

    if record.dataset_code_dirty or manifest.code.dirty:
        record.fail(
            category=CAT_MANIFEST_NOT_AUTHORITATIVE,
            failure_type=_FIXED_TYPES[CAT_MANIFEST_NOT_AUTHORITATIVE],
            message=_FIXED_MESSAGES[CAT_MANIFEST_NOT_AUTHORITATIVE],
        )
        return persist(record)

    if execution_code is None:
        resolved = resolve_execution_provenance()
        if resolved is None:
            # Provenance is UNPROVABLE (not merely dirty) → a distinct fail-closed
            # category. The executed Copilot source could not be tied to a Git
            # checkout, so no authoritative execution provenance exists.
            record.execution_code_git_commit = "unknown"
            record.execution_code_dirty = True
            record.fail(
                category=CAT_EXECUTION_PROVENANCE_UNAVAILABLE,
                failure_type=_FIXED_TYPES[CAT_EXECUTION_PROVENANCE_UNAVAILABLE],
                message=_FIXED_MESSAGES[CAT_EXECUTION_PROVENANCE_UNAVAILABLE],
            )
            return persist(record)
        commit, dirty = resolved
    else:
        commit, dirty = execution_code
    record.execution_code_git_commit = commit
    record.execution_code_dirty = bool(dirty)
    if record.execution_code_dirty:
        record.fail(
            category=CAT_EXECUTION_WORKTREE_DIRTY,
            failure_type=_FIXED_TYPES[CAT_EXECUTION_WORKTREE_DIRTY],
            message=_FIXED_MESSAGES[CAT_EXECUTION_WORKTREE_DIRTY],
        )
        return persist(record)

    # Persist CREATED BEFORE any side effect (crash-safe recoverable record).
    persist(record)

    # --- Phase 3: active-investigation isolation + start + ownership (bounded). ---
    launch = record.launch
    external_ref = to_external_resource_ref(launch)
    if hisiem is not None:
        container.hisiem_adapter = hisiem
    owned = False
    try:
        if await _active_investigation_exists(container, record.tenant_id, external_ref):
            record.fail(
                category=CAT_ACTIVE_INVESTIGATION_EXISTS,
                failure_type=_FIXED_TYPES[CAT_ACTIVE_INVESTIGATION_EXISTS],
                message=_FIXED_MESSAGES[CAT_ACTIVE_INVESTIGATION_EXISTS],
            )
            return persist(record)
        handler = container.investigation_command_handler()
        investigation = await handler.start_alert_investigation(
            build_start_command(
                launch=launch,
                tenant_id=record.tenant_id,
                dataset_run_id=dataset_run_id,
                execution_id=execution_id,
            )
        )
        investigation_id = str(investigation.id)
        # §3: prove THIS execution created/bound the returned Investigation through
        # the production command-receipt authority. A concurrent foreign command
        # that won between the precheck and the start leaves its own receipt bound
        # to the returned aggregate → ownership cannot be proven → fail closed.
        owned = await _execution_owns_investigation(
            container=container,
            launch=launch,
            tenant_id=record.tenant_id,
            dataset_run_id=dataset_run_id,
            execution_id=execution_id,
            investigation_id=investigation_id,
        )
    except NotFoundError as exc:  # source alert gone / not accessible
        record.fail(
            category=CAT_SOURCE_ALERT_MISSING,
            failure_type=type(exc).__name__,
            message=_FIXED_MESSAGES[CAT_SOURCE_ALERT_MISSING],
        )
        return persist(record)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — type name only, never str(exc)
        record.fail(
            category=CAT_START_FAILED,
            failure_type=type(exc).__name__,
            message=_FIXED_MESSAGES[CAT_START_FAILED],
        )
        return persist(record)

    record.investigation_id = investigation_id
    if not owned:
        record.fail(
            category=CAT_EVALUATION_INVESTIGATION_OWNERSHIP_CONFLICT,
            failure_type=_FIXED_TYPES[CAT_EVALUATION_INVESTIGATION_OWNERSHIP_CONFLICT],
            message=_FIXED_MESSAGES[CAT_EVALUATION_INVESTIGATION_OWNERSHIP_CONFLICT],
        )
        return persist(record)

    # §2: thread_id stays NULL until the OrchestrationBinding is observed — never
    # predict the deterministic inv:<id> value.
    record.execution_status = ExecutionStatus.RUNNING
    persist(record)

    # --- Phase 4: drive the durable outbox dispatcher to a terminal state. -------
    # ``model_provider`` was resolved in Phase 0.5 (E1-C1 scripted default / E1-C2
    # real provider) — ONE instance shared by the graph and the E1-C2 telemetry.
    dispatcher = container.outbox_dispatcher(hisiem=hisiem, model=model_provider)

    try:
        snapshot = await _drain_to_terminal(container, record, dispatcher)
        if snapshot is not None and snapshot["status"] == "COMPLETED":
            # Observe the real OrchestrationBinding thread + LangGraph checkpoint
            # through the existing persistence APIs BEFORE this may become COMPLETED.
            observed_thread = await _observed_binding_thread(
                container, record.tenant_id, investigation_id
            )
            if observed_thread is None:
                record.fail(
                    category=CAT_BINDING_MISSING,
                    failure_type=_FIXED_TYPES[CAT_BINDING_MISSING],
                    message=_FIXED_MESSAGES[CAT_BINDING_MISSING],
                )
                return persist(record)
            record.thread_id = observed_thread
            if not await _checkpoint_has_thread(settings.langgraph, observed_thread):
                record.fail(
                    category=CAT_CHECKPOINT_MISSING,
                    failure_type=_FIXED_TYPES[CAT_CHECKPOINT_MISSING],
                    message=_FIXED_MESSAGES[CAT_CHECKPOINT_MISSING],
                )
                return persist(record)
        await _finalize_record(record, snapshot)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — terminalize, never leave RUNNING
        record.fail(
            category=CAT_DRIVE_FAILED,
            failure_type=type(exc).__name__,
            message=_FIXED_MESSAGES[CAT_DRIVE_FAILED],
        )
        return persist(record)

    # --- Phase 5: persist the terminal record atomically. ------------------------
    persist(record)
    logger.info(
        "evaluation execution %s → %s (investigation %s, status %s)",
        execution_id,
        record.execution_status.value,
        investigation_id,
        record.investigation_status,
    )
    return record


async def _active_investigation_exists(
    container: Container, tenant_id: str, external_ref: ExternalResourceRef
) -> bool:
    """True when an ACTIVE investigation already exists for tenant/source alert."""
    uow = container.unit_of_work()
    try:
        existing = await uow.investigations.find_active_by_alert(
            tenant_id=tenant_id, source_alert_ref=external_ref
        )
    finally:
        await uow.close()
    return existing is not None


async def _execution_owns_investigation(
    *,
    container: Container,
    launch: LaunchProjection,
    tenant_id: str,
    dataset_run_id: str,
    execution_id: str,
    investigation_id: str,
) -> bool:
    """Prove THIS execution created/bound the Investigation returned by start (§3).

    Ownership authority is the production command receipt for this execution's
    scoped idempotency key ``eval:<dataset>:<execution>:start``. The returned
    aggregate is OURS only when ALL hold (reads only — never mutates/cancels/
    reuses a foreign investigation):

      - a receipt exists for (tenant, ``StartAlertInvestigation``, our key);
      - its aggregate_id == the returned investigation;
      - its request_fingerprint == this execution's launch fingerprint (a key
        rebound to a different source alert is never claimed);
      - the returned aggregate is bound EXCLUSIVELY by our receipt.

    A concurrent foreign command that won between the harness precheck and the
    production start leaves ITS OWN earlier receipt bound to the returned
    aggregate too (the production handler converges on the winner and binds our
    key to it) → we do NOT own it → ``EVALUATION_INVESTIGATION_OWNERSHIP_CONFLICT``.
    Never inferred from actor subject (all evaluation executions share one actor)
    nor from source-alert equality.
    """
    key = _start_idempotency_key(dataset_run_id, execution_id)
    expected_fingerprint = _launch_fingerprint(launch)
    uow = container.unit_of_work()
    try:
        receipt = await uow.command_receipts.find(
            tenant_id=tenant_id,
            command_type="StartAlertInvestigation",
            idempotency_key=key,
        )
        if receipt is None or receipt.aggregate_id is None:
            return False
        if str(receipt.aggregate_id) != investigation_id:
            return False
        if receipt.request_fingerprint != expected_fingerprint:
            return False
        bound = await uow.command_receipts.list_for_aggregate(
            tenant_id=tenant_id,
            aggregate_type="investigation",
            aggregate_id=UUID(investigation_id),
        )
    finally:
        await uow.close()
    ours = [r for r in bound if r.idempotency_key == key]
    return len(bound) == 1 and len(ours) == 1


async def _drain_to_terminal(
    container: Container,
    record: EvaluationExecutionRecord,
    dispatcher: Any,
) -> dict[str, Any] | None:
    """Drain the outbox until the investigation is terminal (or bounded)."""
    deadline = time.monotonic() + float(
        container.settings.agent_budget.max_duration_seconds
    )
    idle_polls = 0
    investigation_id = record.investigation_id or ""
    while True:
        processed = await dispatcher.drain_once()
        snapshot = await _read_snapshot(
            container, tenant_id=record.tenant_id, investigation_id=investigation_id
        )
        if snapshot is not None and snapshot["status"] in _TERMINAL_STATUSES:
            return snapshot
        if time.monotonic() >= deadline:
            return snapshot
        if processed == 0:
            idle_polls += 1
            if idle_polls >= _DRAIN_MAX_IDLE_POLLS:
                return snapshot
        else:
            idle_polls = 0
        await asyncio.sleep(_DRAIN_POLL_SECONDS)


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
            "phase": (
                investigation.phase.value if investigation.phase is not None else None
            ),
            "termination_reason": (
                investigation.termination_reason.value
                if investigation.termination_reason is not None
                else None
            ),
            "evidence_count": len(evidence),
            "finding_count": len(findings),
            "hypothesis_count": len(hypotheses),
            "result_disposition": (
                verdict.disposition.value if verdict is not None else None
            ),
            "result_summary": verdict.summary if verdict is not None else None,
            "result_confidence": verdict.confidence if verdict is not None else None,
        }
    finally:
        await uow.close()


async def _observed_binding_thread(
    container: Container, tenant_id: str, investigation_id: str
) -> str | None:
    """Return the OBSERVED OrchestrationBinding thread_id (or None if missing)."""
    uow = container.unit_of_work()
    try:
        binding = await uow.bindings.get(
            tenant_id=tenant_id, investigation_id=UUID(investigation_id)
        )
    finally:
        await uow.close()
    return binding.thread_id if binding is not None else None


async def _checkpoint_has_thread(langgraph_settings: Any, thread_id: str) -> bool:
    """True when the LangGraph Postgres checkpointer holds state for ``thread_id``.

    Uses the existing checkpointer + the same ``thread_config`` the graph runner
    uses — never raw SQL.
    """
    from ..agent.graph.builder import thread_config
    from ..infrastructure.checkpoint.postgres import PostgresCheckpointer

    async with PostgresCheckpointer(langgraph_settings) as saver:
        # thread_config() is the exact dict the graph runner binds its run to.
        checkpoint = await saver.aget_tuple(thread_config(thread_id))  # type: ignore[arg-type]
        return checkpoint is not None


async def _finalize_record(
    record: EvaluationExecutionRecord, snapshot: dict[str, Any] | None
) -> None:
    """Map the terminal DB state onto the record (COMPLETED or FAILED)."""
    if snapshot is None:
        record.fail(
            category=CAT_DRIVE_FAILED,
            failure_type="InvestigationGone",
            message=_FIXED_MESSAGES[CAT_DRIVE_FAILED],
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
                failure_type=_FIXED_TYPES[CAT_INVESTIGATION_RESULT_MISSING],
                message=_FIXED_MESSAGES[CAT_INVESTIGATION_RESULT_MISSING],
            )
            return
        record.execution_status = ExecutionStatus.COMPLETED
        record.finished_at = rfc3339_utc()
        return

    if snapshot["status"] in _TERMINAL_STATUSES:
        record.fail(
            category=CAT_INVESTIGATION_NOT_COMPLETED,
            failure_type=_FIXED_TYPES[CAT_INVESTIGATION_NOT_COMPLETED],
            message=_FIXED_MESSAGES[CAT_INVESTIGATION_NOT_COMPLETED],
        )
        return

    record.fail(
        category=CAT_DRIVE_TIMEOUT,
        failure_type=_FIXED_TYPES[CAT_DRIVE_TIMEOUT],
        message=_FIXED_MESSAGES[CAT_DRIVE_TIMEOUT],
    )


def report(record: EvaluationExecutionRecord) -> list[str]:
    """A concise human-readable execution report (E1-C1 §22)."""
    lines = [
        f"execution_id={record.execution_id}",
        f"dataset_run_id={record.dataset_run_id}",
        f"execution_status={record.execution_status.value}",
        f"dataset_code={record.dataset_code_git_commit} "
        f"dirty={record.dataset_code_dirty}",
        f"execution_code={record.execution_code_git_commit} "
        f"dirty={record.execution_code_dirty}",
        f"model_provider={record.model_provider}",
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
    """Operator-facing E1-C1 driver: isolation check, open, execute, print, exit.

    Lives in the harness (not in ``evaluation.cli``) so the production-infra
    imports (Container/bootstrap) stay OUT of the boundary-checked evaluation
    package. The E1-C1 isolation gate runs BEFORE ``Container.open()`` — a
    dispatcher-enabled / non-scripted environment fails closed with NO Container
    side effect and NO investigation. Returns 0 only when the execution COMPLETED.
    """
    from ..bootstrap.container import Container
    from ..config import get_settings

    root = get_settings()
    execution_id = uuid4().hex
    violation = execution_isolation_violation(root)

    if violation is not None:
        record = EvaluationExecutionRecord(
            execution_id=execution_id,
            dataset_run_id=dataset_run_id,
            model_provider=str(root.llm.provider),
            started_at=rfc3339_utc(),
            execution_status=ExecutionStatus.CREATED,
        )
        resolved = resolve_execution_provenance()
        if resolved is None:
            record.execution_code_git_commit = "unknown"
            record.execution_code_dirty = True
        else:
            record.execution_code_git_commit, record.execution_code_dirty = resolved
        record.fail(
            category=violation,
            failure_type=_FIXED_TYPES[violation],
            message=_FIXED_MESSAGES[violation],
        )
        artifact = execution_artifact_path(
            root.evaluation.executions_dir, dataset_run_id, execution_id
        )
        write_record(artifact, record)
        for line in report(record):
            print(line)
        print(f"execution_artifact={artifact}")
        return 1

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


async def execute_real_model_cli(*, dataset_run_id: str) -> int:
    """Operator-facing E1-C2 driver: real-model profile isolation + telemetry.

    Same fail-closed gates as :func:`execute_cli` but under
    :class:`EvaluationProfile.E1_C2_REAL_MODEL`: it requires
    ``LLM_PROVIDER=openai_compatible`` and ``COPILOT_APP_ENABLE_DISPATCHER=false``
    BEFORE ``Container.open()``; a provider configuration failure (missing
    ``CMD_API_KEY`` / SDK / invalid settings) fails closed with NO investigation.
    ONE real provider instance is obtained from :meth:`Container.model_provider`
    and shared by the graph AND the E1-C2 ``model-telemetry.json`` sidecar, so the
    telemetry usage buffer is truthful.

    Returns 0 only when the execution COMPLETED AND the E1-C2 model-telemetry gate
    PASSES (>=1 successful validated real model call for each of plan/decide/assess/
    verdict in a resolved structured-output mode, no MODEL_CONFIGURATION failure) —
    E1-C2 PASS is separate from the Investigation/execution status (§6).
    """
    from ..bootstrap.container import Container
    from ..config import get_settings

    root = get_settings()
    profile = EvaluationProfile.E1_C2_REAL_MODEL
    execution_id = uuid4().hex
    violation = execution_isolation_violation(root, profile)

    def _write_failure(category: str, failure_type: str, message: str) -> int:
        record = EvaluationExecutionRecord(
            execution_id=execution_id,
            dataset_run_id=dataset_run_id,
            model_provider=str(root.llm.provider),
            started_at=rfc3339_utc(),
            execution_status=ExecutionStatus.CREATED,
        )
        resolved = resolve_execution_provenance()
        if resolved is None:
            record.execution_code_git_commit = "unknown"
            record.execution_code_dirty = True
        else:
            record.execution_code_git_commit, record.execution_code_dirty = resolved
        record.fail(category=category, failure_type=failure_type, message=message)
        artifact = execution_artifact_path(
            root.evaluation.executions_dir, dataset_run_id, execution_id
        )
        write_record(artifact, record)
        for line in report(record):
            print(line)
        print(f"execution_artifact={artifact}")
        return 1

    if violation is not None:
        return _write_failure(
            violation, _FIXED_TYPES[violation], _FIXED_MESSAGES[violation]
        )

    container = Container(root)
    try:
        provider = container.model_provider()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — provider configuration error, bounded
        return _write_failure(
            CAT_MODEL_CONFIGURATION,
            type(exc).__name__,
            _FIXED_MESSAGES[CAT_MODEL_CONFIGURATION],
        )
    await container.open()
    try:
        record = await execute_execution(
            dataset_run_id=dataset_run_id,
            container=container,
            model=provider,
            profile=profile,
        )
    finally:
        await container.close()

    telemetry = build_model_telemetry(
        execution_id=record.execution_id,
        dataset_run_id=dataset_run_id,
        provider_adapter=str(root.llm.provider),
        provider=provider,
    )
    artifact = execution_artifact_path(
        root.evaluation.executions_dir, dataset_run_id, record.execution_id
    )
    telemetry_artifact = model_telemetry_path(
        root.evaluation.executions_dir, dataset_run_id, record.execution_id
    )
    write_model_telemetry(telemetry_artifact, telemetry)

    for line in report(record):
        print(line)
    print(f"execution_artifact={artifact}")
    print(f"model_telemetry_artifact={telemetry_artifact}")
    print(f"model_telemetry_gate={telemetry.gate_status}")
    if telemetry.gate_failures:
        print(f"model_telemetry_failures={','.join(telemetry.gate_failures)}")
    print(
        f"model_telemetry_provider={telemetry.provider} "
        f"protocol={telemetry.protocol} model={telemetry.model}"
    )
    print(
        f"model_telemetry_resolved_mode={telemetry.resolved_structured_output_mode}"
    )
    print(f"model_telemetry_successful={','.join(telemetry.successful_operations)}")

    if (
        record.execution_status == ExecutionStatus.COMPLETED
        and telemetry.gate_status == GATE_PASS
    ):
        return 0
    return 1
