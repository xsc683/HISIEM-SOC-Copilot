from __future__ import annotations

from pathlib import Path

from hisiem_soc_copilot.evaluation.contracts import CodeRevision, SealedManifest, VerifiedDataset
from hisiem_soc_copilot.evaluation.manifest import build_manifest
from hisiem_soc_copilot.evaluation.oracle import scenario_oracle
from hisiem_soc_copilot.evaluation.scenario_loader import (
    semantic_sha256,
    source_file_sha256,
)
from hisiem_soc_copilot.evaluation.sealer import seal_manifest
from tests.fixtures.evaluation_fakes import make_verified

_SOURCE_SHA = source_file_sha256()
_SEMANTIC_SHA = semantic_sha256()


def build_manifest_for(
    verified: VerifiedDataset,
    *,
    dirty: bool = False,
    git_commit: str = "test-commit",
) -> SealedManifest:
    """Seal a real manifest for ``verified`` (E1-B.4 recipe used by the sealer
    tests), returning the trusted in-memory manifest (also on disk at ``path``)."""
    return build_manifest(
        verified,
        scenario_oracle(verified.scenario),
        CodeRevision(git_commit=git_commit, dirty=dirty),
        scenario_source_file_sha256=_SOURCE_SHA,
        scenario_semantic_sha256=_SEMANTIC_SHA,
    )


def seal_dataset(
    *,
    runs_dir: Path,
    dataset_run_id: str,
    tenant_id: str = "tenant-a",
    dirty: bool = False,
) -> SealedManifest:
    """Seal ``make_verified`` into ``<runs_dir>/gp-01/<dataset_run_id>/manifest.json``."""
    target = runs_dir / "gp-01" / dataset_run_id / "manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    verified = make_verified(run_id=dataset_run_id, tenant_id=tenant_id)
    manifest = build_manifest_for(verified, dirty=dirty)
    seal_manifest(manifest, target)
    return manifest
