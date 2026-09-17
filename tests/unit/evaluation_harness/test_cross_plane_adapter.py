"""XP-01 harness-adapter tests (Stage E / E1 §34, §35, §42).

The adapter is the sanctioned bridge. Its artifact IO is exercised end-to-end in
``tests/unit/evaluation/cross_plane/test_artifacts.py``; these tests cover the
adapter-specific concerns that do not belong to the artifact contract: scenario
loading, bounded references, the guaranteed absence of runtime work, and the fact
that evaluating does not write anything by itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from hisiem_soc_copilot.evaluation.cross_plane import (
    GATE_CROSS_TENANT_LEAK,
    CrossPlaneContractError,
    CrossTenantLeakMeasurement,
    MeasurementSource,
)
from hisiem_soc_copilot.evaluation_harness import (
    XP01_SUBDIR,
    artifact_ref,
    evaluate_and_build,
    gate_results_path,
    load_scenario,
    run_scenario_to_artifact,
)

_ADAPTER = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "src"
    / "hisiem_soc_copilot"
    / "evaluation_harness"
    / "cross_plane_adapter.py"
)

_MEASUREMENTS = {
    GATE_CROSS_TENANT_LEAK: CrossTenantLeakMeasurement(
        source=MeasurementSource.EVIDENCE_GRAPH_FACT
    )
}


def test_adapter_loads_a_catalog_scenario() -> None:
    spec = load_scenario("XP-TEN-001")
    assert spec.scenario_id == "XP-TEN-001"
    assert spec.pack_id == "XP-01"


def test_adapter_rejects_an_unknown_scenario() -> None:
    with pytest.raises(CrossPlaneContractError, match="unknown XP-01 scenario"):
        load_scenario("XP-NOPE-001")


def test_evaluate_and_build_is_pure(tmp_path: Path) -> None:
    """Evaluating must not write: only the explicit write step touches the disk."""
    before = sorted(p.name for p in tmp_path.iterdir())
    result, payload = evaluate_and_build("XP-TEN-001", _MEASUREMENTS)
    assert result.scenario_id == "XP-TEN-001"
    assert payload["pack_id"] == "XP-01"
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_artifact_ref_is_bounded_and_relative(tmp_path: Path) -> None:
    path = gate_results_path(tmp_path, "XP-TEN-001", "run-1")
    ref = artifact_ref(path, executions_dir=tmp_path)
    assert ref == "xp-01/XP-TEN-001/run-1/gate-results.json"
    assert not ref.startswith("/")
    assert ":" not in ref


def test_artifact_ref_falls_back_to_the_file_name_outside_the_tree(tmp_path: Path) -> None:
    ref = artifact_ref(Path("elsewhere/gate-results.json"), executions_dir=tmp_path)
    assert ref == "gate-results.json"


def test_run_scenario_to_artifact_creates_the_family_subtree(tmp_path: Path) -> None:
    _result, path = run_scenario_to_artifact(
        scenario_id="XP-TEN-001",
        measurements=_MEASUREMENTS,
        executions_dir=tmp_path,
        run_id="run-1",
    )
    assert path == tmp_path / XP01_SUBDIR / "XP-TEN-001" / "run-1" / "gate-results.json"
    assert path.is_file()


def test_adapter_performs_no_runtime_work() -> None:
    """E1 §34/§51: the E1 adapter must not start HISIEM, a browser, MCP, a model,
    a Collector, or any process. Those belong to E2-E6."""
    source = _ADAPTER.read_text(encoding="utf-8")
    for forbidden in (
        "subprocess",
        "requests",
        "httpx",
        "socket",
        "uvicorn",
        "asyncio.create_task",
        "Container(",
        "playwright",
    ):
        assert forbidden not in source, f"adapter reaches {forbidden!r}"


def test_adapter_imports_only_the_evaluation_plane_and_its_sibling() -> None:
    tree = ast.parse(_ADAPTER.read_text(encoding="utf-8"))
    production = {"domain", "application", "agent", "api", "infrastructure", "bootstrap"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            top = node.module.split(".")[0]
            assert top not in production, f"adapter imports {node.module}"
