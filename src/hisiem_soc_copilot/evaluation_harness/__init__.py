"""Evaluation Execution Harness package (E1-C1).

Bridges the sealed evaluation dataset to the REAL production investigation
pipeline. This package is intentionally NOT part of ``evaluation`` (the oracle
side) and NOT a production layer: it is the only component allowed to hold a full
:class:`SealedManifest` and drive the production ``Container``. Production code
never imports this package; the graph never sees the manifest — only the typed
launch projection crosses into ``StartAlertInvestigation``.
"""

from __future__ import annotations

from .harness import (
    EvaluationProfile,
    build_start_command,
    execute_cli,
    execute_execution,
    execute_real_model_cli,
    launch_projection,
    record_from_manifest,
    report,
    resolve_execution_provenance,
    verify_dataset_manifest,
)
from .record import EvaluationExecutionRecord, ExecutionStatus
from .telemetry import (
    REQUIRED_OPERATIONS,
    ModelTelemetry,
    model_telemetry_path,
    read_model_telemetry,
)

__all__ = [
    "EvaluationProfile",
    "ExecutionStatus",
    "EvaluationExecutionRecord",
    "ModelTelemetry",
    "REQUIRED_OPERATIONS",
    "build_start_command",
    "execute_cli",
    "execute_execution",
    "execute_real_model_cli",
    "launch_projection",
    "model_telemetry_path",
    "read_model_telemetry",
    "record_from_manifest",
    "report",
    "resolve_execution_provenance",
    "verify_dataset_manifest",
]
