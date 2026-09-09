"""Execution-record unit tests (E1-C1 §21): typed record, atomic persistence,
bounded failure payloads, split dataset/execution provenance, and the guarantee
that a serialized record never contains oracle / dataset-event / secret content."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from hisiem_soc_copilot.evaluation_harness.harness import record_from_manifest
from hisiem_soc_copilot.evaluation_harness.record import (
    EXECUTION_SCHEMA_VERSION,
    EvaluationExecutionRecord,
    ExecutionSchemaError,
    ExecutionStatus,
    LaunchProjection,
    read_record,
    rfc3339_utc,
    write_record,
)
from tests.unit.evaluation_harness._seal_helpers import seal_dataset

# Keys that would violate the oracle firewall / secrets rule if they ever appear.
_FORBIDDEN_TOKENS = (
    "oracle",
    "expected_verdict",
    "control_events",
    "required_evidence_roles",
    "FAILURE_SEQUENCE",
    "POST_FAILURE_SUCCESS",
    "bearer",
    "api_key",
    "password",
    "secret",
    "authorization",
    "token",
)

_PAYLOAD_KEYS = {
    "schema_version",
    "execution_id",
    "scenario_id",
    "dataset_run_id",
    "dataset_manifest_sha256",
    "dataset_code_git_commit",
    "dataset_code_dirty",
    "execution_code_git_commit",
    "execution_code_dirty",
    "model_provider",
    "tenant_id",
    "started_at",
    "finished_at",
    "launch_ref",
    "investigation_id",
    "thread_id",
    "investigation_status",
    "investigation_phase",
    "termination_reason",
    "result",
    "evidence_count",
    "finding_count",
    "hypothesis_count",
    "execution_status",
    "failure",
}


def _full_record() -> EvaluationExecutionRecord:
    return EvaluationExecutionRecord(
        execution_id=uuid4().hex,
        scenario_id="gp-01",
        dataset_run_id="run-123",
        dataset_manifest_sha256="c0ffee" * 8,
        dataset_code_git_commit="dataset-head",
        dataset_code_dirty=False,
        execution_code_git_commit="exec-head",
        execution_code_dirty=False,
        model_provider="scripted",
        tenant_id="tenant-a",
        started_at=rfc3339_utc(),
        finished_at=rfc3339_utc(),
        launch=LaunchProjection(
            provider="hisiem",
            resource_type="alert",
            address_id="es-doc-0001",
            business_id="biz-0001",
        ),
        investigation_id="inv-1",
        thread_id="inv:inv-1",
        investigation_status="COMPLETED",
        investigation_phase="FINALIZING",
        termination_reason="COMPLETED_WITHOUT_RESPONSE",
        result_disposition="INCONCLUSIVE",
        result_summary="Insufficient evidence",
        result_confidence=0.3,
        evidence_count=0,
        finding_count=0,
        hypothesis_count=0,
        execution_status=ExecutionStatus.COMPLETED,
    )


def test_payload_round_trip_preserves_bounded_fields() -> None:
    original = _full_record()
    restored = EvaluationExecutionRecord.from_payload(original.to_payload())
    assert restored == original
    assert restored.execution_status == ExecutionStatus.COMPLETED
    assert restored.launch.address_id == "es-doc-0001"
    assert restored.result_disposition == "INCONCLUSIVE"
    assert restored.thread_id == "inv:inv-1"
    assert restored.dataset_code_git_commit == "dataset-head"
    assert restored.execution_code_git_commit == "exec-head"


def test_schema_version_is_stable() -> None:
    assert EXECUTION_SCHEMA_VERSION == "evaluation-execution/v2"
    assert EvaluationExecutionRecord().schema_version == EXECUTION_SCHEMA_VERSION


def test_new_records_serialize_as_v2() -> None:
    payload = _full_record().to_payload()
    assert payload["schema_version"] == "evaluation-execution/v2"


def test_v1_payload_is_explicitly_rejected() -> None:
    payload = _full_record().to_payload()
    payload["schema_version"] = "evaluation-execution/v1"
    with pytest.raises(ExecutionSchemaError):
        EvaluationExecutionRecord.from_payload(payload)


def test_unknown_schema_is_explicitly_rejected() -> None:
    payload = _full_record().to_payload()
    payload["schema_version"] = "evaluation-execution/v99"
    with pytest.raises(ExecutionSchemaError):
        EvaluationExecutionRecord.from_payload(payload)


def test_missing_schema_is_explicitly_rejected() -> None:
    payload = _full_record().to_payload()
    del payload["schema_version"]
    with pytest.raises(ExecutionSchemaError):
        EvaluationExecutionRecord.from_payload(payload)


def test_dataset_and_execution_provenance_are_distinct_fields() -> None:
    """Dataset provenance is NOT execution provenance: they are separate fields."""
    record = _full_record()
    assert record.dataset_code_git_commit != record.execution_code_git_commit
    payload = record.to_payload()
    assert payload["dataset_code_git_commit"] == "dataset-head"
    assert payload["execution_code_git_commit"] == "exec-head"
    # Neither legacy "copilot_*" field is allowed to resurface.
    assert "copilot_git_commit" not in payload
    assert "copilot_dirty" not in payload


def test_record_from_real_manifest_never_contains_oracle_or_events(tmp_path: Path) -> None:
    dataset_run_id = "firewall-run"
    manifest = seal_dataset(runs_dir=tmp_path, dataset_run_id=dataset_run_id)
    record = record_from_manifest(
        manifest, execution_id=uuid4().hex, dataset_run_id=dataset_run_id
    )
    assert record.dataset_code_dirty is False
    # Drive the record through every lifecycle the harness persists.
    record.investigation_id = "inv-1"
    record.thread_id = "inv:inv-1"
    record.execution_status = ExecutionStatus.RUNNING
    record.fail(
        category="INVESTIGATION_NOT_COMPLETED",
        failure_type="TerminalNotCompleted",
        message="investigation reached terminal status CANCELLED",
    )

    payload = record.to_payload()
    text = json.dumps(payload)
    for token in _FORBIDDEN_TOKENS:
        assert token not in text, f"execution payload leaked {token!r}"
    assert payload["launch_ref"]["address_id"] == manifest.source_alert.address_id
    assert payload["execution_status"] == "FAILED"
    # The record must be a strict allow-list payload: no unexpected top-level keys.
    assert set(payload) == _PAYLOAD_KEYS


def test_serialized_payload_has_no_secret_shaped_fields() -> None:
    """The record schema has no field that could hold a secret (structure, not
    just content): no bearer/api_key/password/token/secret names anywhere."""
    import dataclasses

    record = _full_record()
    fields = {f.name for f in dataclasses.fields(record)}
    for secret in ("bearer_token", "api_key", "password", "secret", "authorization"):
        assert secret not in fields
    payload = json.dumps(record.to_payload())
    assert "Bearer " not in payload


def test_fail_is_bounded_and_collapses_control_characters() -> None:
    record = EvaluationExecutionRecord(execution_id="exec-1", started_at=rfc3339_utc())
    record.fail(
        category="MANIFEST_VERIFY_FAILURE",
        failure_type="ManifestSchemaError",
        message="line one\n\tline two\r\n  spaced    text " + ("x" * 700),
    )
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.finished_at != ""
    assert record.failure.message.startswith("line one line two spaced text")
    assert len(record.failure.message) <= 600
    assert "\n" not in record.failure.message
    assert "\t" not in record.failure.message


def test_atomic_write_read_round_trip_and_no_temp_leak(tmp_path: Path) -> None:
    target = tmp_path / "exec.json"
    record = _full_record()
    write_record(target, record)
    assert target.exists()
    restored = read_record(target)
    assert restored == record
    # Atomic replace leaves no temp artifacts behind.
    assert [p.name for p in tmp_path.iterdir()] == ["exec.json"]


def test_default_execution_status_is_created() -> None:
    record = EvaluationExecutionRecord()
    assert record.execution_status == ExecutionStatus.CREATED
    assert record.to_payload()["execution_status"] == "CREATED"
