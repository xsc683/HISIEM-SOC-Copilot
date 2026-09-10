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
    RealModelRunResult,
    build_start_command,
    execute_cli,
    execute_execution,
    execute_real_model_cli,
    execute_real_model_run,
    launch_projection,
    record_from_manifest,
    report,
    resolve_execution_provenance,
    verify_dataset_manifest,
)
from .quality import (
    QUALITY_SCHEMA_VERSION,
    EvidenceFact,
    FindingFact,
    ToolEvidenceQuality,
    ToolInvocationFact,
    build_tool_evidence_quality,
    read_tool_evidence_quality,
    tool_evidence_quality_path,
    write_tool_evidence_quality,
)
from .quality_harness import (
    ToolEvidenceRunResult,
    execute_tool_evidence_cli,
    execute_tool_evidence_run,
    report_tool_evidence,
    resolve_scenario_identities,
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
    "EvidenceFact",
    "FindingFact",
    "ModelTelemetry",
    "QUALITY_SCHEMA_VERSION",
    "REQUIRED_OPERATIONS",
    "RealModelRunResult",
    "ToolEvidenceQuality",
    "ToolEvidenceRunResult",
    "ToolInvocationFact",
    "build_start_command",
    "build_tool_evidence_quality",
    "execute_cli",
    "execute_execution",
    "execute_real_model_cli",
    "execute_real_model_run",
    "execute_tool_evidence_cli",
    "execute_tool_evidence_run",
    "launch_projection",
    "model_telemetry_path",
    "read_model_telemetry",
    "read_tool_evidence_quality",
    "record_from_manifest",
    "report",
    "report_tool_evidence",
    "resolve_execution_provenance",
    "resolve_scenario_identities",
    "tool_evidence_quality_path",
    "verify_dataset_manifest",
    "write_tool_evidence_quality",
]
