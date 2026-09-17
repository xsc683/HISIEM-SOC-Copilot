"""E4 runtime-integrated slice (Stage E / E4 §24, §16, §18, §25).

``XP-OBS-001`` declares the ``runtime-integrated`` profile, and only this module
executes it: a REAL Copilot runtime process, a REAL OpenTelemetry Collector running
the repository's own configuration, and a REAL representative cross-plane lifecycle
whose spans are captured at the export boundary.

The same lifecycle runs twice — once with the collector reachable and once with it
stopped — so E4 can also measure §15/§16's invariant on ITS OWN slice: a telemetry
backend outage must not change the persisted business outcome. That invariant is
decided by the FROZEN ``TELEMETRY_CHANGED_BUSINESS_STATE`` gate; E4 does not invent
a second outage gate for it.

Everything is skipped, with an explicit reason, when the required runtime is absent.
No credential is printed; the child process receives it through its environment.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from hisiem_soc_copilot.evaluation.cross_plane import GateStatus
from hisiem_soc_copilot.evaluation_harness.cross_plane_e4_scenarios import (
    REPRESENTATIVE_OPERATIONS,
    E4Fixture,
    evaluate_e4_scenario,
    observability_regression_gate,
    run_e4_scenario_to_artifact,
)
from hisiem_soc_copilot.evaluation_harness.cross_plane_observability import (
    SpanFact,
    business_outcome_equivalent,
    trace_continuation_facts,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env.local"
HISIEM_BASE_URL = "http://127.0.0.1:8080"
HISIEM_TENANT = "default"
COLLECTOR_NAME = "e4-otel-collector"
COLLECTOR_IMAGE = "otel/opentelemetry-collector-contrib:0.115.1"
COLLECTOR_CONFIG = REPO_ROOT / "infra" / "otel-collector" / "collector.yaml"
OTLP_ENDPOINT = "http://127.0.0.1:4317"
TOKEN_ENV = "HISIEM_COPILOT_SERVICE_TOKEN"
DRIVER = "tests.support.e4_observability_driver"

_ENV_CACHE: dict[str, str] | None = None


def _env_values() -> dict[str, str]:
    """Read the gitignored dev env WITHOUT printing it or touching os.environ."""
    global _ENV_CACHE
    if _ENV_CACHE is None:
        values: dict[str, str] = {}
        if ENV_FILE.is_file():
            for raw in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip():
                    values[key.strip()] = value.strip().strip('"').strip("'")
        _ENV_CACHE = values
    return _ENV_CACHE


def _service_token() -> str:
    return os.environ.get(TOKEN_ENV, "").strip() or _env_values().get(TOKEN_ENV, "")


def _hisiem_soar_ready() -> bool:
    import httpx

    token = _service_token()
    if not token:
        return False
    try:
        probe = httpx.post(
            f"{HISIEM_BASE_URL}/api/internal/soar/executions",
            headers={"X-Tenant-ID": HISIEM_TENANT, "Idempotency-Key": "e4-readiness"},
            json={},
            timeout=10.0,
        )
    except Exception:
        return False
    return probe.status_code in {400, 401, 403}


@pytest.fixture(scope="module")
def hisiem_soar() -> Iterator[None]:
    """The real HISIEM internal SOAR boundary must be answering."""
    if not _service_token():
        pytest.skip("E4 runtime slice: no HISIEM service token is configured")
    if not _hisiem_soar_ready():
        pytest.skip("E4 runtime slice: the HISIEM internal SOAR boundary is not answering")
    yield


@contextmanager
def _collector_running() -> Iterator[str]:
    """Run the real OTel Collector with the repository's own configuration.

    Each caller owns the container's lifecycle, so the outage test cannot disturb
    the trace test (and vice versa) regardless of execution order.
    """
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("E4 runtime slice: the docker CLI is not available")
    if not COLLECTOR_CONFIG.is_file():
        pytest.skip("E4 runtime slice: the repository collector configuration is missing")

    subprocess.run([docker, "rm", "-f", COLLECTOR_NAME], capture_output=True, check=False)
    started = subprocess.run(
        [
            docker,
            "run",
            "-d",
            "--name",
            COLLECTOR_NAME,
            "-p",
            "4317:4317",
            "-p",
            "4318:4318",
            "-v",
            f"{COLLECTOR_CONFIG}:/etc/otelcol-contrib/config.yaml:ro",
            "-e",
            "OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4319",
            COLLECTOR_IMAGE,
            "--config=/etc/otelcol-contrib/config.yaml",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        pytest.skip(
            f"E4 runtime slice: collector did not start ({started.stderr.strip()[:120]})"
        )
    try:
        yield COLLECTOR_NAME
    finally:
        subprocess.run(
            [docker, "rm", "-f", COLLECTOR_NAME], capture_output=True, check=False
        )


def _run_driver(database_url: str, otlp_endpoint: str, out_path: Path) -> dict[str, Any]:
    """One real Copilot runtime process; returns its bounded telemetry document."""
    child_env = {**os.environ, TOKEN_ENV: _service_token()}
    completed = subprocess.run(
        [sys.executable, "-m", DRIVER, database_url, otlp_endpoint, str(out_path)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=child_env,
        timeout=600,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return dict(json.loads(out_path.read_text(encoding="utf-8")))


def _spans(document: dict[str, Any]) -> tuple[SpanFact, ...]:
    return tuple(
        SpanFact(
            operation=str(item["operation"]),
            status_category=str(item.get("status_category", "unset")),
            parent_present=bool(item.get("parent_present")),
            link_count=int(item.get("link_count", 0)),
            trace_id=str(item.get("trace_id", "")),
            attribute_keys=tuple(item.get("attribute_keys", ())),
        )
        for item in document.get("spans", ())
    )


def _fixture(document: dict[str, Any]) -> E4Fixture:
    spans = _spans(document)
    outcome = dict(document.get("business_outcome", {}))
    return E4Fixture(
        spans=spans,
        required_operations=REPRESENTATIVE_OPERATIONS,
        metric_label_sets=({"operation": "response.submit"}, {"response_state": "SUCCEEDED"}),
        surfaces={"span:representative": " ".join(sorted({s.operation for s in spans}))},
        oracle_tokens=("XP-OBS-001", "XP-OBS-002"),
        persisted_traceparents=tuple(document.get("traceparents", ())),
        logical_command_ids=tuple(document.get("logical_command_ids", ())),
        business_outcome_with_telemetry=outcome,
        business_outcome_without_telemetry=outcome,
        representative_trace_id=_representative_trace_id(spans),
    )


def _representative_trace_id(spans: tuple[SpanFact, ...]) -> str:
    """The trace the representative lifecycle started in (bounded correlation ref)."""
    for span in spans:
        if span.operation == "investigation.run" and span.trace_id:
            return span.trace_id
    return next((span.trace_id for span in spans if span.trace_id), "")


def test_xp_obs_001_representative_cross_plane_trace(
    hisiem_soar: None, scratch_db_url: str, tmp_path: Path
) -> None:
    with _collector_running():
        document = _run_driver(scratch_db_url, OTLP_ENDPOINT, tmp_path / "run-up.json")

    spans = _spans(document)
    operations = {span.operation for span in spans}
    assert {"durable.dispatch", "investigation.run"} <= operations, sorted(operations)

    fixture = _fixture(document)
    result, path = run_e4_scenario_to_artifact(
        "XP-OBS-001", fixture, executions_dir=tmp_path
    )
    assert result.overall_gate is GateStatus.PASS, result.gate_failures
    assert path.is_file()

    # Durable continuation and business/trace independence, measured on the real run.
    continuation = trace_continuation_facts(
        spans=spans,
        persisted_traceparents=tuple(document.get("traceparents", ())),
        logical_command_ids=tuple(document.get("logical_command_ids", ())),
    )
    assert continuation.persisted_traceparent_count > 0
    assert continuation.all_traceparents_are_valid_w3c is True
    assert continuation.durable_link_present is True
    assert continuation.crosses_a_durable_boundary is True
    assert continuation.logical_command_count == 1
    assert continuation.business_identity_independent_of_trace is True


def test_collector_outage_does_not_change_the_business_outcome(
    hisiem_soar: None, scratch_db_url: str, tmp_path: Path
) -> None:
    docker = shutil.which("docker")
    assert docker is not None

    with _collector_running() as collector:
        with_collector = _run_driver(
            scratch_db_url, OTLP_ENDPOINT, tmp_path / "run-1.json"
        )
        # The telemetry backend is genuinely gone before the second process starts.
        subprocess.run([docker, "stop", collector], capture_output=True, check=False)
        without_collector = _run_driver(
            scratch_db_url, OTLP_ENDPOINT, tmp_path / "run-2.json"
        )

    up = dict(with_collector["business_outcome"])
    down = dict(without_collector["business_outcome"])
    assert business_outcome_equivalent(
        business_outcome_with_telemetry=up, business_outcome_without_telemetry=down
    ), f"telemetry availability changed the business outcome: {up} != {down}"
    assert up["investigation_status"] == "COMPLETED"
    assert up["verdict_disposition"] == "MALICIOUS"

    # The same invariant is decided by the frozen gate — one gate, two callers.
    facts = observability_regression_gate(
        business_outcome_with_telemetry=up, business_outcome_without_telemetry=down
    )
    assert "TELEMETRY_OUTAGE_ISOLATED" in facts.observed_facts

    # Telemetry being unavailable does not stop the runtime from producing its
    # semantic spans either: only the EXPORT target was gone. (Span sets are not
    # compared wholesale — SQL/HTTP instrumentation differs run to run and is not a
    # business invariant.)
    down_ops = {item["operation"] for item in without_collector.get("spans", ())}
    up_ops = {item["operation"] for item in with_collector.get("spans", ())}
    assert set(REPRESENTATIVE_OPERATIONS) <= up_ops
    assert set(REPRESENTATIVE_OPERATIONS) <= down_ops


def test_an_outage_that_changed_the_outcome_would_fail() -> None:
    """The negative half: an outcome that depended on telemetry fails the gate."""
    healthy = {"investigation_status": "COMPLETED", "execution_status": "SUCCEEDED"}
    degraded = dict(healthy, execution_status="FAILED")
    facts = observability_regression_gate(
        business_outcome_with_telemetry=healthy,
        business_outcome_without_telemetry=degraded,
    )
    assert facts.observed_facts == ("TELEMETRY_ALTERED_BUSINESS_STATE",)
    incomplete_trace = evaluate_e4_scenario(
        "XP-OBS-001",
        E4Fixture(
            spans=(SpanFact(operation="investigation.run"),),
            required_operations=("investigation.run", "durable.dispatch"),
        ),
    )
    assert incomplete_trace.overall_gate is GateStatus.FAIL
