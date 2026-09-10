"""E1-C2 evaluation-profile gates (offline, no Postgres).

The typed profile selects harness policy ONLY:
- ``execute`` (E1_C1_SCRIPTED) must stay scripted-only — never silently switch to
  a real provider based on the environment;
- ``execute-real-model`` (E1_C2_REAL_MODEL) requires ``openai_compatible`` +
  dispatcher disabled BEFORE Container.open, and a missing API-key env fails
  closed BEFORE any Investigation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hisiem_soc_copilot.bootstrap.container import Container
from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.evaluation_harness.harness import (
    CAT_DISPATCHER_ENABLED,
    CAT_MODEL_CONFIGURATION,
    CAT_NON_SCRIPTED_PROVIDER,
    CAT_REAL_MODEL_PROVIDER_REQUIRED,
    CAT_UNEXPECTED_API_KEY_ENV,
    CAT_UNEXPECTED_BASE_URL,
    CAT_UNEXPECTED_MODEL,
    CAT_UNEXPECTED_STRUCTURED_OUTPUT_CONFIG,
    CAT_ZDR_DISABLED,
    E1_C2_BASE_URL,
    EvaluationProfile,
    execute_execution,
    execution_isolation_violation,
)
from hisiem_soc_copilot.evaluation_harness.record import (
    ExecutionStatus,
    read_record,
)


def _container(tmp_path: Path, **mutate) -> Container:
    settings = Settings()
    settings.evaluation.runs_dir = str(tmp_path / "runs")
    settings.evaluation.executions_dir = str(tmp_path / "executions")
    for section, attr, value in mutate.get("settings", []):
        getattr(settings, section).__setattr__(attr, value)
    # Not opened: every gate below must never need the DB / HISIEM.
    return Container(settings)


def _artifact(container: Container, dataset_run_id: str, record) -> Path:
    return (
        Path(container.settings.evaluation.executions_dir)
        / "gp-01"
        / dataset_run_id
        / record.execution_id
        / "execution.json"
    )


async def test_e1c1_execute_rejects_openai_compatible_provider(tmp_path: Path) -> None:
    """``execute`` (E1_C1_SCRIPTED) must NOT silently switch to a real provider."""
    container = _container(tmp_path, settings=[("llm", "provider", "openai_compatible")])
    record = await execute_execution(dataset_run_id="any", container=container)
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_NON_SCRIPTED_PROVIDER
    assert record.model_provider == "openai_compatible"
    assert record.investigation_id is None


async def test_e1c2_rejects_scripted_config(tmp_path: Path) -> None:
    """E1_C2_REAL_MODEL with the default scripted provider fails closed."""
    container = _container(tmp_path)
    assert container.settings.llm.provider == "scripted"
    assert (
        execution_isolation_violation(
            container.settings, EvaluationProfile.E1_C2_REAL_MODEL
        )
        == CAT_REAL_MODEL_PROVIDER_REQUIRED
    )
    record = await execute_execution(
        dataset_run_id="any",
        container=container,
        profile=EvaluationProfile.E1_C2_REAL_MODEL,
    )
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_REAL_MODEL_PROVIDER_REQUIRED
    assert record.investigation_id is None


async def test_e1c2_rejects_dispatcher_true_before_open(tmp_path: Path) -> None:
    """E1_C2_REAL_MODEL with the background dispatcher enabled fails closed."""
    container = _container(
        tmp_path,
        settings=[
            ("llm", "provider", "openai_compatible"),
            ("app", "enable_dispatcher", True),
        ],
    )
    assert (
        execution_isolation_violation(
            container.settings, EvaluationProfile.E1_C2_REAL_MODEL
        )
        == CAT_DISPATCHER_ENABLED
    )
    record = await execute_execution(
        dataset_run_id="any",
        container=container,
        profile=EvaluationProfile.E1_C2_REAL_MODEL,
    )
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_DISPATCHER_ENABLED
    assert record.investigation_id is None


async def test_e1c2_missing_api_key_fails_closed_before_investigation(
    tmp_path: Path, monkeypatch
) -> None:
    """openai_compatible configured but CMD_API_KEY absent → the provider cannot be
    constructed → MODEL_CONFIGURATION, FAILED, NO investigation (never a guess)."""
    monkeypatch.delenv("CMD_API_KEY", raising=False)
    container = _container(
        tmp_path, settings=[("llm", "provider", "openai_compatible")]
    )
    # No sealed manifest is needed: the config gate fires before the manifest phase.
    record = await execute_execution(
        dataset_run_id="no-manifest-needed",
        container=container,
        profile=EvaluationProfile.E1_C2_REAL_MODEL,
    )
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_MODEL_CONFIGURATION
    assert record.failure.failure_type == "ModelConfigurationError"
    assert record.investigation_id is None
    artifact = _artifact(container, "no-manifest-needed", record)
    assert artifact.is_file()
    restored = read_record(artifact)
    assert restored.failure.category == CAT_MODEL_CONFIGURATION
    payload = artifact.read_text(encoding="utf-8")
    assert "CMD_API_KEY" not in payload
    assert "secret" not in payload


async def test_e1c2_profile_accepts_openai_compatible_when_key_present(
    tmp_path: Path, monkeypatch
) -> None:
    """Isolation passes (openai_compatible + dispatcher off + key present); the run
    then fails downstream at a NON-config gate (manifest missing) — proving the
    profile gate itself is not the blocker."""
    monkeypatch.setenv("CMD_API_KEY", "test-key-not-a-secret")
    container = _container(
        tmp_path, settings=[("llm", "provider", "openai_compatible")]
    )
    assert (
        execution_isolation_violation(
            container.settings, EvaluationProfile.E1_C2_REAL_MODEL
        )
        is None
    )
    record = await execute_execution(
        dataset_run_id="no-such-run",
        container=container,
        profile=EvaluationProfile.E1_C2_REAL_MODEL,
    )
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == "MANIFEST_NOT_FOUND"
    assert record.investigation_id is None


@pytest.mark.parametrize(
    ("section", "attr", "value", "category"),
    [
        ("llm", "base_url", "https://evil.example/v1", CAT_UNEXPECTED_BASE_URL),
        ("llm", "model", "gpt-4o", CAT_UNEXPECTED_MODEL),
        ("llm", "api_key_env", "SOME_OTHER_KEY", CAT_UNEXPECTED_API_KEY_ENV),
        ("llm", "zdr", False, CAT_ZDR_DISABLED),
        (
            "llm",
            "structured_output_mode",
            "json_object",
            CAT_UNEXPECTED_STRUCTURED_OUTPUT_CONFIG,
        ),
    ],
)
def test_e1c2_rejects_wrong_provider_baseline(
    tmp_path: Path, section: str, attr: str, value: object, category: str
) -> None:
    """Each deviation from the exact Command Code baseline fails closed BEFORE open."""
    container = _container(
        tmp_path,
        settings=[("llm", "provider", "openai_compatible"), (section, attr, value)],
    )
    assert (
        execution_isolation_violation(
            container.settings, EvaluationProfile.E1_C2_REAL_MODEL
        )
        == category
    )


async def test_e1c2_wrong_baseline_fails_before_investigation(tmp_path: Path) -> None:
    """A wrong baseline model → FAILED with NO investigation (never a Container open)."""
    container = _container(
        tmp_path,
        settings=[("llm", "provider", "openai_compatible"), ("llm", "model", "gpt-4o")],
    )
    record = await execute_execution(
        dataset_run_id="any",
        container=container,
        profile=EvaluationProfile.E1_C2_REAL_MODEL,
    )
    assert record.execution_status == ExecutionStatus.FAILED
    assert record.failure.category == CAT_UNEXPECTED_MODEL
    assert record.investigation_id is None


def test_e1c2_base_url_trailing_slash_is_normalized(tmp_path: Path) -> None:
    """A harmless trailing slash on the Command Code base URL still passes."""
    container = _container(
        tmp_path,
        settings=[
            ("llm", "provider", "openai_compatible"),
            ("llm", "base_url", E1_C2_BASE_URL + "/"),
        ],
    )
    assert (
        execution_isolation_violation(
            container.settings, EvaluationProfile.E1_C2_REAL_MODEL
        )
        is None
    )
