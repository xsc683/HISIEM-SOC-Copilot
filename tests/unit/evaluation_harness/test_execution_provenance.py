"""Execution-provenance anchoring tests (E1-C1 final §1 / §7-A, §7-B).

Proves execution provenance is resolved from the Git repository that CONTAINS the
executed Copilot source (derived from the package path), NEVER from the caller's
CWD — and that an unprovable or dirty checkout fails closed BEFORE any
Investigation side effect. The hermetic anchor tests drive the REAL resolver code
path; only the anchor directory is substituted for a disposable checkout so they
never depend on the cleanliness of the real worktree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hisiem_soc_copilot.bootstrap.container import Container
from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.evaluation_harness import harness as harness_mod
from hisiem_soc_copilot.evaluation_harness.harness import (
    CAT_EXECUTION_PROVENANCE_UNAVAILABLE,
    CAT_EXECUTION_WORKTREE_DIRTY,
    execute_execution,
    resolve_execution_provenance,
)
from hisiem_soc_copilot.evaluation_harness.record import ExecutionStatus, read_record
from tests.unit.evaluation_harness._seal_helpers import seal_dataset


def _run_git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ("git", *args), cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _init_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _run_git("init", "-b", "main", cwd=path)
    _run_git("config", "user.email", "eval@test", cwd=path)
    _run_git("config", "user.name", "eval-test", cwd=path)
    _run_git("commit", "--allow-empty", "-m", "seed", cwd=path)
    return _run_git("rev-parse", "HEAD", cwd=path)


def _container(tmp_path: Path) -> Container:
    settings = Settings()
    settings.evaluation.runs_dir = str(tmp_path / "runs")
    settings.evaluation.executions_dir = str(tmp_path / "executions")
    # Not opened: every fail-closed gate below must never need the DB / HISIEM.
    return Container(settings)


def _artifact(container: Container, dataset_run_id: str, record) -> Path:
    return (
        Path(container.settings.evaluation.executions_dir)
        / "gp-01"
        / dataset_run_id
        / record.execution_id
        / "execution.json"
    )


def test_execution_provenance_is_cwd_independent(tmp_path, monkeypatch) -> None:
    """§7-A: caller CWD (a DIFFERENT Git repo, HEAD A) must not influence the
    resolved Copilot execution provenance (HEAD B)."""
    copilot_head = _run_git("rev-parse", "HEAD")  # caller CWD is the Copilot repo
    baseline = resolve_execution_provenance()
    assert baseline is not None
    assert baseline[0] == copilot_head

    other = tmp_path / "unrelated-repo"
    other_head = _init_git_repo(other)
    assert other_head != copilot_head
    # The caller's unrelated repo is dirty — irrelevant to Copilot provenance.
    (other / "scratch.txt").write_text("dirty", encoding="utf-8")
    monkeypatch.chdir(other)

    from_other = resolve_execution_provenance()
    assert from_other is not None
    assert from_other == baseline  # identical regardless of caller CWD
    assert from_other[0] == copilot_head  # the checkout CONTAINING the code
    assert from_other[0] != other_head  # NOT the caller's repo HEAD


async def test_unprovable_execution_provenance_fails_closed(tmp_path, monkeypatch) -> None:
    """§7-A: the executed source is NOT inside a resolvable Git checkout →
    EXECUTION_PROVENANCE_UNAVAILABLE → FAILED → no Investigation."""
    container = _container(tmp_path)
    seal_dataset(
        runs_dir=Path(container.settings.evaluation.runs_dir),
        dataset_run_id="prov-run",
    )
    anchor = tmp_path / "not-a-git-dir"
    anchor.mkdir(parents=True, exist_ok=True)
    assert resolve_execution_provenance(anchor=anchor) is None
    monkeypatch.setattr(harness_mod, "_provenance_anchor", lambda: anchor)

    record = await execute_execution(dataset_run_id="prov-run", container=container)
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_EXECUTION_PROVENANCE_UNAVAILABLE
    assert record.execution_code_git_commit == "unknown"
    assert record.execution_code_dirty is True
    assert record.investigation_id is None
    artifact = _artifact(container, "prov-run", record)
    assert artifact.is_file()
    restored = read_record(artifact)
    assert restored.failure.category == CAT_EXECUTION_PROVENANCE_UNAVAILABLE
    assert restored.execution_code_git_commit == "unknown"


async def test_dirty_copilot_worktree_fails_closed_via_anchor(tmp_path, monkeypatch) -> None:
    """§7-B: the Copilot checkout (anchor) is DIRTY → EXECUTION_WORKTREE_DIRTY →
    FAILED → no Investigation. A clean/unrelated caller CWD must NOT mask it."""
    container = _container(tmp_path)
    seal_dataset(
        runs_dir=Path(container.settings.evaluation.runs_dir),
        dataset_run_id="dirty-anchor",
    )
    checkout = tmp_path / "copilot-checkout"
    head = _init_git_repo(checkout)
    (checkout / "uncommitted.txt").write_text("dirty", encoding="utf-8")
    monkeypatch.setattr(harness_mod, "_provenance_anchor", lambda: checkout)

    clean_other = tmp_path / "clean-unrelated"
    _init_git_repo(clean_other)
    monkeypatch.chdir(clean_other)

    record = await execute_execution(dataset_run_id="dirty-anchor", container=container)
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_EXECUTION_WORKTREE_DIRTY
    assert record.execution_code_git_commit == head
    assert record.execution_code_dirty is True
    assert record.investigation_id is None
    restored = read_record(_artifact(container, "dirty-anchor", record))
    assert restored.failure.category == CAT_EXECUTION_WORKTREE_DIRTY
