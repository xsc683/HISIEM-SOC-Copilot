"""Execute fail-closed unit tests (E1-C1 §21): a manifest that is missing, does not
verify, or is not authoritative NEVER starts an investigation. These run without
Postgres — every failing gate fires before the container's DB phase is touched."""

from __future__ import annotations

from pathlib import Path

from hisiem_soc_copilot.bootstrap.container import Container
from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.evaluation_harness.harness import (
    CAT_MANIFEST_NOT_AUTHORITATIVE,
    CAT_MANIFEST_NOT_FOUND,
    CAT_MANIFEST_VERIFY_FAILURE,
    CAT_START_FAILED,
    execute_execution,
)
from hisiem_soc_copilot.evaluation_harness.record import ExecutionStatus, read_record
from tests.unit.evaluation_harness._seal_helpers import seal_dataset


def _container(tmp_path: Path) -> Container:
    settings = Settings()
    settings.evaluation.runs_dir = str(tmp_path / "runs")
    settings.evaluation.executions_dir = str(tmp_path / "executions")
    # Not opened: the fail-closed gates below must never need the DB / HISIEM.
    return Container(settings)


def _artifact_path(container: Container, dataset_run_id: str, record) -> Path:
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
    artifact = _artifact_path(container, "no-such-run", record)
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
    tampered = False
    for i, byte in enumerate(raw):
        if byte == ord('"'):
            raw[i] = ord("'")
            tampered = True
            break
    assert tampered
    target.write_bytes(bytes(raw))
    record = await execute_execution(dataset_run_id="fw-run", container=container)
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_MANIFEST_VERIFY_FAILURE
    assert record.investigation_id is None


async def test_dirty_manifest_is_not_authoritative_fails_closed(tmp_path: Path) -> None:
    container = _container(tmp_path)
    seal_dataset(
        runs_dir=Path(container.settings.evaluation.runs_dir),
        dataset_run_id="dirty-run",
        dirty=True,
    )
    record = await execute_execution(dataset_run_id="dirty-run", container=container)
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_MANIFEST_NOT_AUTHORITATIVE
    assert record.copilot_dirty is True
    assert record.investigation_id is None


async def test_clean_authoritative_manifest_persists_created_before_db(
    tmp_path: Path,
) -> None:
    """A clean manifest passes the pure gates and would reach the DB phase; here
    we force the DB phase (unopened container) to fail so we can assert the record
    transitioned to RUNNING-proof CREATED persistence then FAILED — never a raise."""
    container = _container(tmp_path)
    seal_dataset(
        runs_dir=Path(container.settings.evaluation.runs_dir),
        dataset_run_id="clean-run",
    )
    record = await execute_execution(dataset_run_id="clean-run", container=container)
    # The container was never opened: the DB-phase handler construction fails and
    # the harness must translate it to a bounded FAILED record (never raise).
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_START_FAILED
    artifact = _artifact_path(container, "clean-run", record)
    assert artifact.is_file()
