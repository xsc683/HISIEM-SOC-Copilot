"""Execute fail-closed unit tests (E1-C1 §9). A manifest that is missing, does not
verify, is not authoritative, whose identity mismatches, whose execution worktree
is dirty, or whose environment violates E1-C1 isolation NEVER starts an
investigation. These run without Postgres — every failing gate fires before the
container's DB phase is touched."""

from __future__ import annotations

import json
from pathlib import Path

from hisiem_soc_copilot.bootstrap.container import Container
from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.evaluation_harness import harness as harness_mod
from hisiem_soc_copilot.evaluation_harness.harness import (
    CAT_ACTIVE_INVESTIGATION_EXISTS,
    CAT_DATASET_IDENTITY_MISMATCH,
    CAT_DISPATCHER_ENABLED,
    CAT_EXECUTION_WORKTREE_DIRTY,
    CAT_MANIFEST_NOT_AUTHORITATIVE,
    CAT_MANIFEST_NOT_FOUND,
    CAT_MANIFEST_VERIFY_FAILURE,
    CAT_NON_SCRIPTED_PROVIDER,
    CAT_START_FAILED,
    execute_execution,
)
from hisiem_soc_copilot.evaluation_harness.record import ExecutionStatus, read_record
from tests.unit.evaluation_harness._seal_helpers import seal_dataset, seal_under


def _container(tmp_path: Path, **mutate) -> Container:
    settings = Settings()
    settings.evaluation.runs_dir = str(tmp_path / "runs")
    settings.evaluation.executions_dir = str(tmp_path / "executions")
    for section, attr, value in mutate.get("settings", []):
        getattr(settings, section).__setattr__(attr, value)
    # Not opened: the fail-closed gates below must never need the DB / HISIEM.
    return Container(settings)


def _artifact(container: Container, dataset_run_id: str, record) -> Path:
    return (
        Path(container.settings.evaluation.executions_dir)
        / "gp-01"
        / dataset_run_id
        / record.execution_id
        / "execution.json"
    )


async def test_missing_manifest_fails_closed(tmp_path: Path) -> None:
    container = _container(tmp_path)
    record = await execute_execution(dataset_run_id="no-such-run", container=container)
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_MANIFEST_NOT_FOUND
    assert record.investigation_id is None
    artifact = _artifact(container, "no-such-run", record)
    assert artifact.is_file()
    assert read_record(artifact).failure.category == CAT_MANIFEST_NOT_FOUND


async def test_corrupt_manifest_fails_closed(tmp_path: Path) -> None:
    container = _container(tmp_path)
    target = Path(container.settings.evaluation.runs_dir) / "gp-01" / "bad" / "manifest.json"
    target.parent.mkdir(parents=True)
    target.write_text("{ this is not json ", encoding="utf-8")
    record = await execute_execution(dataset_run_id="bad", container=container)
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_MANIFEST_VERIFY_FAILURE
    assert record.failure.failure_type  # exception TYPE only, never its raw message
    assert record.investigation_id is None


async def test_tampered_manifest_fails_closed_on_integrity(tmp_path: Path) -> None:
    container = _container(tmp_path)
    seal_dataset(
        runs_dir=Path(container.settings.evaluation.runs_dir), dataset_run_id="fw-run"
    )
    target = (
        Path(container.settings.evaluation.runs_dir)
        / "gp-01"
        / "fw-run"
        / "manifest.json"
    )
    raw = bytearray(target.read_bytes())
    for i, byte in enumerate(raw):
        if byte == ord('"'):
            raw[i] = ord("'")
            break
    target.write_bytes(bytes(raw))
    record = await execute_execution(dataset_run_id="fw-run", container=container)
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_MANIFEST_VERIFY_FAILURE
    assert record.investigation_id is None


async def test_dirty_dataset_is_not_authoritative_fails_closed(tmp_path: Path) -> None:
    container = _container(tmp_path)
    seal_dataset(
        runs_dir=Path(container.settings.evaluation.runs_dir),
        dataset_run_id="dirty-run",
        dirty=True,
    )
    record = await execute_execution(dataset_run_id="dirty-run", container=container)
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_MANIFEST_NOT_AUTHORITATIVE
    assert record.dataset_code_dirty is True
    assert record.investigation_id is None


async def test_dispatcher_enabled_fails_closed(tmp_path: Path) -> None:
    container = _container(tmp_path, settings=[("app", "enable_dispatcher", True)])
    record = await execute_execution(dataset_run_id="any-run", container=container)
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_DISPATCHER_ENABLED
    assert record.failure.failure_type
    assert record.investigation_id is None


async def test_non_scripted_provider_fails_closed(tmp_path: Path) -> None:
    container = _container(tmp_path, settings=[("llm", "provider", "openai_compatible")])
    record = await execute_execution(dataset_run_id="any-run", container=container)
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_NON_SCRIPTED_PROVIDER
    assert record.model_provider == "openai_compatible"
    assert record.investigation_id is None


async def test_dirty_execution_worktree_fails_closed(tmp_path: Path) -> None:
    container = _container(tmp_path)
    seal_dataset(
        runs_dir=Path(container.settings.evaluation.runs_dir), dataset_run_id="clean-run"
    )
    record = await execute_execution(
        dataset_run_id="clean-run",
        container=container,
        execution_code=("HEAD", True),  # dirty execution worktree
    )
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_EXECUTION_WORKTREE_DIRTY
    assert record.dataset_code_dirty is False
    assert record.execution_code_dirty is True
    assert record.investigation_id is None


async def test_dataset_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    container = _container(tmp_path)
    # Manifest whose run.run_id ("real-run") differs from the directory the caller
    # executes under ("wrong-dir").
    seal_under(
        runs_dir=Path(container.settings.evaluation.runs_dir),
        directory_id="wrong-dir",
        run_id="real-run",
    )
    record = await execute_execution(dataset_run_id="wrong-dir", container=container)
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_DATASET_IDENTITY_MISMATCH
    assert record.investigation_id is None


async def test_execution_provenance_is_current_not_dataset(tmp_path: Path) -> None:
    """The record stamps execution provenance from the CURRENT revision, which may
    differ from the dataset's sealed revision — and never labels one as the other."""
    container = _container(tmp_path)
    seal_dataset(
        runs_dir=Path(container.settings.evaluation.runs_dir), dataset_run_id="clean-run"
    )
    # A clean manifest + clean injected execution HEAD; the DB phase fails only
    # because the container was never opened (→ bounded START_FAILED).
    record = await execute_execution(
        dataset_run_id="clean-run",
        container=container,
        execution_code=("current-head", False),
    )
    assert record.dataset_code_git_commit == "test-commit"
    assert record.execution_code_git_commit == "current-head"
    assert record.dataset_code_dirty is False
    assert record.execution_code_dirty is False
    assert record.dataset_code_git_commit != record.execution_code_git_commit
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_START_FAILED
    artifact = _artifact(container, "clean-run", record)
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["dataset_code_git_commit"] == "test-commit"
    assert payload["execution_code_git_commit"] == "current-head"


async def test_clean_authoritative_manifest_persists_created_before_db(
    tmp_path: Path,
) -> None:
    """A clean manifest passes every pure gate; the DB phase (unopened container)
    is mapped to a bounded FAILED record — never a raise."""
    container = _container(tmp_path)
    seal_dataset(
        runs_dir=Path(container.settings.evaluation.runs_dir), dataset_run_id="clean-run"
    )
    record = await execute_execution(
        dataset_run_id="clean-run",
        container=container,
        execution_code=("current-head", False),
    )
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_START_FAILED
    artifact = _artifact(container, "clean-run", record)
    assert artifact.is_file()


async def test_active_investigation_category_is_exposed(tmp_path: Path) -> None:
    """The active-collision category constant is exported (its DB-driven behaviour
    is covered by the real-Postgres integration test)."""
    assert CAT_ACTIVE_INVESTIGATION_EXISTS == "ACTIVE_INVESTIGATION_EXISTS"


# ---------------------------------------------------------------------------
# §7-H: secret safety — injected fake credentials in exception messages must
# NEVER reach the persisted execution.json (bounded category/type/fixed message).
# ---------------------------------------------------------------------------

_SECRET_BLOB = (
    "Bearer abc-secret-token postgresql://user:password@host/db CMD_API_KEY=secret123"
)


async def test_manifest_verify_exception_secret_is_not_persisted(
    tmp_path: Path, monkeypatch
) -> None:
    container = _container(tmp_path)
    seal_dataset(
        runs_dir=Path(container.settings.evaluation.runs_dir), dataset_run_id="sv-run"
    )

    def _boom(_path) -> None:
        raise RuntimeError(f"verify boom {_SECRET_BLOB}")

    monkeypatch.setattr(harness_mod, "verify_dataset_manifest", _boom)
    record = await execute_execution(dataset_run_id="sv-run", container=container)
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_MANIFEST_VERIFY_FAILURE
    assert record.failure.failure_type == "RuntimeError"
    artifact = _artifact(container, "sv-run", record)
    payload = artifact.read_text(encoding="utf-8")
    assert _SECRET_BLOB not in payload
    assert read_record(artifact).failure.category == CAT_MANIFEST_VERIFY_FAILURE


async def test_start_exception_secret_is_not_persisted(
    tmp_path: Path, monkeypatch
) -> None:
    container = _container(tmp_path)
    seal_dataset(
        runs_dir=Path(container.settings.evaluation.runs_dir), dataset_run_id="ss-run"
    )

    async def _no_active(c, tenant_id, external_ref) -> bool:
        return False

    monkeypatch.setattr(harness_mod, "_active_investigation_exists", _no_active)

    class _BoomingHandler:
        async def start_alert_investigation(self, command):
            raise RuntimeError(f"start boom {_SECRET_BLOB}")

    monkeypatch.setattr(
        container, "investigation_command_handler", lambda: _BoomingHandler()
    )

    record = await execute_execution(
        dataset_run_id="ss-run",
        container=container,
        execution_code=("head", False),
    )
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_START_FAILED
    assert record.failure.failure_type == "RuntimeError"
    payload = _artifact(container, "ss-run", record).read_text(encoding="utf-8")
    assert _SECRET_BLOB not in payload
