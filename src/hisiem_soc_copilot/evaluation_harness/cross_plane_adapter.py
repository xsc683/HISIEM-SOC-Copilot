"""Sanctioned XP-01 evaluation-to-runtime bridge (Stage E / E1).

This is the ONLY place XP-01 touches the filesystem. It lives under
``evaluation_harness`` on purpose: that package is already the sanctioned bridge
between sealed evaluation input and the real runtime, and it is already an allowed
importer of ``hisiem_soc_copilot.evaluation`` (pinned by
``tests/architecture/test_knowledge_boundary.py``). Keeping the adapter here means
E1 needs **no** change to the evaluation-importer allowlist.

Direction of dependency, unchanged by E1:

```text
evaluation_harness  ->  evaluation          (allowed, existing)
evaluation          ->  evaluation_harness  (NOT introduced)
production layers   ->  either              (still forbidden)
```

E1 stops at the boundary: this adapter loads a scenario, accepts already-collected
measurements, evaluates the gates, and writes the artifact. It does NOT start HISIEM,
a browser, an MCP server, a model provider, an Investigation, or a Collector — those
belong to E2-E6. Its collectors can therefore be exercised with deterministic inputs.

The atomic write reuses ``record.atomic_write_json`` rather than a second
implementation, and the secret scan runs BEFORE any bytes reach the disk, so an
unsafe payload never produces an artifact at all.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..evaluation.cross_plane import (
    GATE_RESULTS_FILENAME,
    XP_PACK_ID,
    ArtifactSecretViolation,
    CrossPlaneContractError,
    CrossPlaneScenarioResult,
    CrossPlaneScenarioSpec,
    assert_artifact_safe,
    build_gate_results_payload,
    evaluate_scenario,
    gate_results_from_payload,
    validate_catalog,
)
from ..evaluation.cross_plane import (
    scenario as load_catalog_scenario,
)
from .record import atomic_write_json

#: Subdirectory the XP-01 family owns beneath the shared executions dir. A distinct
#: subtree guarantees an XP-01 artifact can never be confused with a GP-01 one.
XP01_SUBDIR = "xp-01"

_UNSAFE_PATH_CHARS = ("/", "\\", "..", "\x00")


class CrossPlanePathError(ValueError):
    """An artifact path component was not a safe, bounded identifier."""


def _safe_segment(value: str, *, what: str) -> str:
    """Reject anything that could escape the XP-01 subtree.

    Path ownership is a security property here: the executions dir is shared with
    other evaluation families, so a scenario id or run id must never be able to
    address a path outside ``<executions_dir>/xp-01``.
    """
    text = str(value).strip()
    if not text:
        raise CrossPlanePathError(f"{what} must be non-empty")
    if any(token in text for token in _UNSAFE_PATH_CHARS):
        raise CrossPlanePathError(f"{what} contains an unsafe path fragment: {text!r}")
    if text.startswith("."):
        raise CrossPlanePathError(f"{what} must not start with '.': {text!r}")
    if len(text) > 120:
        raise CrossPlanePathError(f"{what} exceeds 120 characters")
    return text


def gate_results_path(
    executions_dir: str | Path, scenario_id: str, run_id: str | None = None
) -> Path:
    """The gate-results artifact path for one scenario (and optionally one run).

    Layout mirrors the existing family convention:
    ``<executions_dir>/xp-01/<scenario_id>[/<run_id>]/gate-results.json``.
    """
    base = Path(executions_dir) / XP01_SUBDIR
    scenario_segment = _safe_segment(scenario_id, what="scenario_id")
    target = base / scenario_segment
    if run_id is not None:
        target = target / _safe_segment(run_id, what="run_id")
    return target / GATE_RESULTS_FILENAME


def load_scenario(scenario_id: str) -> CrossPlaneScenarioSpec:
    """Load one audited XP-01 scenario from the frozen catalog."""
    return load_catalog_scenario(scenario_id)


def evaluate_and_build(
    scenario_id: str, measurements: Mapping[str, object]
) -> tuple[CrossPlaneScenarioResult, dict[str, Any]]:
    """Evaluate a scenario's hard gates and build its artifact payload.

    Pure apart from reading the in-process catalog: no IO, no model, no runtime.
    """
    spec = load_catalog_scenario(scenario_id)
    result = evaluate_scenario(spec, measurements)
    return result, build_gate_results_payload(result)


def write_gate_results(path: str | Path, payload: Mapping[str, Any]) -> Path:
    """Secret-scan then atomically write a gate-results payload.

    The scan runs first: an unsafe payload raises and nothing is written.
    """
    assert_artifact_safe(payload)
    target = Path(path)
    atomic_write_json(target, dict(payload))
    return target


def read_gate_results(path: str | Path) -> CrossPlaneScenarioResult:
    """Read and validate a persisted gate-results artifact."""
    raw = Path(path).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, Mapping):
        raise CrossPlaneContractError("gate-results artifact must be a JSON object")
    return gate_results_from_payload(payload)


def run_scenario_to_artifact(
    *,
    scenario_id: str,
    measurements: Mapping[str, object],
    executions_dir: str | Path,
    run_id: str | None = None,
) -> tuple[CrossPlaneScenarioResult, Path]:
    """Evaluate one scenario and persist its gate-results artifact.

    This is the E1 end-to-end entry point later stages build on: E2-E6 supply real
    measurements, this function evaluates and records. It deliberately performs no
    runtime work of its own.
    """
    validate_catalog()
    result, payload = evaluate_and_build(scenario_id, measurements)
    if payload.get("pack_id") != XP_PACK_ID:
        raise CrossPlaneContractError("built payload is not an XP-01 artifact")
    path = write_gate_results(
        gate_results_path(executions_dir, scenario_id, run_id), payload
    )
    return result, path


def artifact_ref(path: str | Path, *, executions_dir: str | Path) -> str:
    """Return a bounded, repo-relative-ish reference for an artifact path.

    Used by reporting so an artifact reference never embeds an absolute host path.
    """
    target = Path(path)
    try:
        return target.relative_to(Path(executions_dir)).as_posix()
    except ValueError:
        return target.name


__all__ = [
    "ArtifactSecretViolation",
    "CrossPlanePathError",
    "XP01_SUBDIR",
    "artifact_ref",
    "evaluate_and_build",
    "gate_results_path",
    "load_scenario",
    "read_gate_results",
    "run_scenario_to_artifact",
    "write_gate_results",
]
