"""Oracle firewall + launch projection unit tests (E1-C1 §14 / E1-B.4 §13).

The production investigation receives ONLY the four-field launch projection —
never the oracle, dataset events, or the sealed object. These tests assert that
boundary at the pure seam helpers (no container / DB required).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from hisiem_soc_copilot.domain.investigation.value_objects import ExternalResourceRef
from hisiem_soc_copilot.evaluation.contracts import EvaluationLaunchRef
from hisiem_soc_copilot.evaluation_harness.harness import (
    build_start_command,
    launch_projection,
    record_from_manifest,
    to_external_resource_ref,
)
from hisiem_soc_copilot.evaluation_harness.record import LaunchProjection
from tests.unit.evaluation_harness._seal_helpers import seal_dataset


def test_launch_projection_is_exactly_the_four_bounded_fields(tmp_path: Path) -> None:
    dataset_run_id = "firewall-run"
    manifest = seal_dataset(runs_dir=tmp_path, dataset_run_id=dataset_run_id)

    projected = launch_projection(manifest)
    assert projected.provider == "hisiem"
    assert projected.resource_type == "alert"
    assert projected.address_id == manifest.source_alert.address_id
    assert projected.business_id == manifest.source_alert.business_id

    # Identical to the manifest's own launch_projection / EvaluationLaunchRef.
    assert manifest.launch_projection == EvaluationLaunchRef(
        provider=projected.provider,
        resource_type=projected.resource_type,
        address_id=projected.address_id,
        business_id=projected.business_id,
    )


def test_external_ref_is_an_alert_ref() -> None:
    ref = to_external_resource_ref(
        LaunchProjection(
            provider="hisiem",
            resource_type="alert",
            address_id="es-doc-0001",
            business_id=None,
        )
    )
    assert isinstance(ref, ExternalResourceRef)
    assert ref.is_alert
    assert ref.address_id == "es-doc-0001"


def test_start_command_is_the_only_production_message(tmp_path: Path) -> None:
    dataset_run_id = "firewall-run"
    manifest = seal_dataset(runs_dir=tmp_path, dataset_run_id=dataset_run_id)
    launch = launch_projection(manifest)
    execution_id = uuid4().hex

    command = build_start_command(
        launch=launch,
        tenant_id=manifest.scope["tenant_id"],
        dataset_run_id=dataset_run_id,
        execution_id=execution_id,
    )
    assert command.source_alert_ref.is_alert
    assert command.source_alert_ref.provider == "hisiem"
    assert command.source_alert_ref.address_id == manifest.source_alert.address_id
    assert command.initiated_by_subject == "evaluation-runner"
    assert command.initiated_by_display_name == "Evaluation Runner"
    assert command.idempotency_key == f"eval:{dataset_run_id}:{execution_id}:start"
    assert command.tenant_id == manifest.scope["tenant_id"]


def test_record_from_manifest_stamps_bounded_identity(tmp_path: Path) -> None:
    dataset_run_id = "firewall-run"
    manifest = seal_dataset(runs_dir=tmp_path, dataset_run_id=dataset_run_id)
    execution_id = uuid4().hex

    record = record_from_manifest(
        manifest, execution_id=execution_id, dataset_run_id=dataset_run_id
    )
    assert record.scenario_id == "gp-01"
    assert record.dataset_run_id == dataset_run_id
    assert record.dataset_manifest_sha256 == manifest.integrity["manifest_sha256"]
    assert record.copilot_git_commit == manifest.code.git_commit
    assert record.copilot_dirty is False
    assert record.model_provider == "scripted"
    assert record.tenant_id == manifest.scope["tenant_id"]
    assert record.launch.address_id == manifest.source_alert.address_id
    assert record.execution_status.value == "CREATED"
