"""Typed Evaluation Execution Record (E1-C1 §5).

One Evaluation Execution is one operator-invoked run of the REAL production
investigation pipeline against a sealed GP-01 manifest. The execution record is
the durable, mutable operational artifact describing that run:

- schema_version ``evaluation-execution/v1``
- dataset identity (scenario/dataset_run_id/manifest sha256 + code revision)
- the ONLY projection of the manifest a production investigation sees
  (provider / resource_type / address_id / business_id)
- the produced investigation identity + orchestration thread
- the terminal investigation status/phase/termination_reason and its persisted
  result (disposition/summary/confidence)
- evidence/finding/hypothesis counts
- execution_status (CREATED → RUNNING → COMPLETED | FAILED) and, on failure, a
  bounded failure category/type/message.

The record NEVER contains oracle data, dataset events/control events, or secrets
— structurally: the dataclass has no fields for them, and ``to_payload`` only
emits the bounded fields above. The record is a MUTABLE operational artifact
(rewritten atomically on each transition); it is never sealed and never written
through :func:`seal_manifest`. It lives under
``<executions_dir>/gp-01/<dataset_run_id>/<execution_id>/execution.json``.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

EXECUTION_SCHEMA_VERSION = "evaluation-execution/v1"

# Directory name under which an execution's mutable artifact file lives.
_EXECUTION_ARTIFACT = "execution.json"

# Bounded diagnostic message length — failures never dump full exceptions/state.
_MAX_FAILURE_MESSAGE_CHARS = 600


class ExecutionStatus(StrEnum):
    """Lifecycle of one evaluation execution record (mutable, not sealed)."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def rfc3339_utc() -> str:
    """Current UTC instant as an RFC3339 UTC string (seconds precision)."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class LaunchProjection:
    """The production-safe launch projection recorded for one execution.

    Mirrors ``EvaluationLaunchRef`` / the production ``ExternalResourceRef``:
    provider / resource_type / address_id / business_id only. Never oracle data.
    """

    provider: str = ""
    resource_type: str = ""
    address_id: str = ""
    business_id: str | None = None


@dataclass
class ExecutionFailure:
    """Bounded failure diagnostics (never secrets, never raw exceptions)."""

    category: str = ""
    failure_type: str = ""
    message: str = ""


@dataclass
class EvaluationExecutionRecord:
    """The typed execution record (mutable operational artifact).

    Code provenance is deliberately split (E1-C1 correctness patch): ``dataset_*``
    describes the code revision that created the SEALED DATASET (from
    ``manifest.code``); ``execution_*`` describes the code revision executing the
    Agent NOW (current Copilot HEAD/worktree). They are allowed to differ; neither
    is ever presented as the other's provenance.
    """

    schema_version: str = EXECUTION_SCHEMA_VERSION
    execution_id: str = ""
    scenario_id: str = ""
    dataset_run_id: str = ""
    dataset_manifest_sha256: str = ""
    dataset_code_git_commit: str = ""
    dataset_code_dirty: bool = True
    execution_code_git_commit: str = ""
    execution_code_dirty: bool = True
    model_provider: str = "scripted"
    tenant_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    launch: LaunchProjection = field(default_factory=LaunchProjection)
    investigation_id: str | None = None
    thread_id: str | None = None
    investigation_status: str | None = None
    investigation_phase: str | None = None
    termination_reason: str | None = None
    result_disposition: str | None = None
    result_summary: str | None = None
    result_confidence: float | None = None
    evidence_count: int = 0
    finding_count: int = 0
    hypothesis_count: int = 0
    execution_status: ExecutionStatus = ExecutionStatus.CREATED
    failure: ExecutionFailure = field(default_factory=ExecutionFailure)

    # ------------------------------------------------------------------
    # payload serialization (stable, bounded — never oracle/events/secrets)
    # ------------------------------------------------------------------

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "scenario_id": self.scenario_id,
            "dataset_run_id": self.dataset_run_id,
            "dataset_manifest_sha256": self.dataset_manifest_sha256,
            "dataset_code_git_commit": self.dataset_code_git_commit,
            "dataset_code_dirty": self.dataset_code_dirty,
            "execution_code_git_commit": self.execution_code_git_commit,
            "execution_code_dirty": self.execution_code_dirty,
            "model_provider": self.model_provider,
            "tenant_id": self.tenant_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "launch_ref": {
                "provider": self.launch.provider,
                "resource_type": self.launch.resource_type,
                "address_id": self.launch.address_id,
                "business_id": self.launch.business_id,
            },
            "investigation_id": self.investigation_id,
            "thread_id": self.thread_id,
            "investigation_status": self.investigation_status,
            "investigation_phase": self.investigation_phase,
            "termination_reason": self.termination_reason,
            "result": {
                "disposition": self.result_disposition,
                "summary": self.result_summary,
                "confidence": self.result_confidence,
            },
            "evidence_count": self.evidence_count,
            "finding_count": self.finding_count,
            "hypothesis_count": self.hypothesis_count,
            "execution_status": str(self.execution_status.value)
            if isinstance(self.execution_status, ExecutionStatus)
            else str(self.execution_status),
            "failure": {
                "category": self.failure.category,
                "type": self.failure.failure_type,
                "message": self.failure.message,
            },
        }
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> EvaluationExecutionRecord:
        launch = payload.get("launch_ref") or {}
        result = payload.get("result") or {}
        failure = payload.get("failure") or {}
        status = ExecutionStatus(str(payload.get("execution_status", "CREATED")))
        return cls(
            schema_version=str(payload.get("schema_version", EXECUTION_SCHEMA_VERSION)),
            execution_id=str(payload.get("execution_id", "")),
            scenario_id=str(payload.get("scenario_id", "")),
            dataset_run_id=str(payload.get("dataset_run_id", "")),
            dataset_manifest_sha256=str(payload.get("dataset_manifest_sha256", "")),
            dataset_code_git_commit=str(payload.get("dataset_code_git_commit", "")),
            dataset_code_dirty=bool(payload.get("dataset_code_dirty", True)),
            execution_code_git_commit=str(payload.get("execution_code_git_commit", "")),
            execution_code_dirty=bool(payload.get("execution_code_dirty", True)),
            model_provider=str(payload.get("model_provider", "scripted")),
            tenant_id=str(payload.get("tenant_id", "")),
            started_at=str(payload.get("started_at", "")),
            finished_at=str(payload.get("finished_at", "")),
            launch=LaunchProjection(
                provider=str(launch.get("provider", "")),
                resource_type=str(launch.get("resource_type", "")),
                address_id=str(launch.get("address_id", "")),
                business_id=(
                    str(launch["business_id"])
                    if launch.get("business_id") is not None
                    else None
                ),
            ),
            investigation_id=_optional_str(payload.get("investigation_id")),
            thread_id=_optional_str(payload.get("thread_id")),
            investigation_status=_optional_str(payload.get("investigation_status")),
            investigation_phase=_optional_str(payload.get("investigation_phase")),
            termination_reason=_optional_str(payload.get("termination_reason")),
            result_disposition=_optional_str(result.get("disposition")),
            result_summary=_optional_str(result.get("summary")),
            result_confidence=(
                float(result["confidence"])
                if result.get("confidence") is not None
                else None
            ),
            evidence_count=int(payload.get("evidence_count", 0)),
            finding_count=int(payload.get("finding_count", 0)),
            hypothesis_count=int(payload.get("hypothesis_count", 0)),
            execution_status=status,
            failure=ExecutionFailure(
                category=str(failure.get("category", "")),
                failure_type=str(failure.get("type", "")),
                message=str(failure.get("message", "")),
            ),
        )

    # ------------------------------------------------------------------
    # failure helpers
    # ------------------------------------------------------------------

    def fail(self, *, category: str, failure_type: str = "", message: str = "") -> None:
        """Move this record to FAILED with bounded diagnostics."""
        self.execution_status = ExecutionStatus.FAILED
        self.finished_at = rfc3339_utc()
        self.failure.category = category
        self.failure.failure_type = failure_type
        self.failure.message = _bounded(message)


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _bounded(message: str) -> str:
    text = message or ""
    text = " ".join(text.split())  # collapse control chars/newlines
    return text[:_MAX_FAILURE_MESSAGE_CHARS]


# ---------------------------------------------------------------------------
# Atomic file persistence (mutable operational artifact — never sealed)
# ---------------------------------------------------------------------------

# JSON keys the payload may contain. Kept as a frozen allow-list so a record that
# (by construction) gained an unexpected field can never serialize it.
_PAYLOAD_KEYS = frozenset(
    {
        "schema_version", "execution_id", "scenario_id", "dataset_run_id",
        "dataset_manifest_sha256", "dataset_code_git_commit", "dataset_code_dirty",
        "execution_code_git_commit", "execution_code_dirty", "model_provider",
        "tenant_id", "started_at", "finished_at", "launch_ref",
        "investigation_id", "thread_id", "investigation_status",
        "investigation_phase", "termination_reason", "result", "evidence_count",
        "finding_count", "hypothesis_count", "execution_status", "failure",
    }
)


def execution_artifact_path(
    executions_dir: str | Path, dataset_run_id: str, execution_id: str
) -> Path:
    """The mutable ``execution.json`` artifact for one execution (E1-C1 §5)."""
    return (
        Path(executions_dir)
        / "gp-01"
        / dataset_run_id
        / execution_id
        / _EXECUTION_ARTIFACT
    )


def write_record(path: str | Path, record: EvaluationExecutionRecord) -> None:
    """Atomically replace the record artifact (same-dir temp + fsync + os.replace).

    The execution record is mutable operational state owned by the single harness
    process; each transition rewrites the whole file atomically so a reader never
    observes a torn JSON document. Never uses the sealer — this is NOT a sealed
    manifest and MUST NOT be seal_manifest()'d.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(record.to_payload(), ensure_ascii=False, indent=2).encode("utf-8")
    _atomic_write(target, data)


def _atomic_write(target: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        _fsync_dir(target.parent)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


def _fsync_dir(directory: Path) -> None:
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def read_record(path: str | Path) -> EvaluationExecutionRecord:
    """Load + validate a persisted execution record artifact."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return EvaluationExecutionRecord.from_payload(payload)
