"""Opt-in REAL HISIEM GP-01 materialization + sealing live gate (E1-B.3 §21, §22).

NOT part of the default suite: only runs when ``RUN_HISIEM_DATASET_EVAL=1``. It
drives the REAL SSH TCP syslog input + REAL HISIEM control API and proves:

1. F1..F5 resolve to real HISIEM event documents;
2. S1 resolves to a real success event;
3. W1 resolves as an independent control event;
4. a real SSH brute-force alert appears for the run;
5. the alert reference uses the actual HISIEM addressing ``_id``;
6. the DatasetVerifier returns ``VerifiedDataset``;
7. the manifest seals and its SHA-256 re-validates.

In every other environment these tests SKIP — they never touch the network or
fabricate a PASS.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hisiem_soc_copilot.config import HisiemSettings, get_settings
from hisiem_soc_copilot.evaluation.contracts import CodeRevision, MaterializationState
from hisiem_soc_copilot.evaluation.hisiem_reader import HisiemEvaluationReader
from hisiem_soc_copilot.evaluation.identity import derive_run_identity
from hisiem_soc_copilot.evaluation.injector import TcpSyslogEventInjector
from hisiem_soc_copilot.evaluation.ledger import dump_draft
from hisiem_soc_copilot.evaluation.manifest import build_manifest
from hisiem_soc_copilot.evaluation.materializer import Gp01Materializer
from hisiem_soc_copilot.evaluation.oracle import scenario_oracle
from hisiem_soc_copilot.evaluation.scenario_loader import (
    load_scenario,
    semantic_sha256,
    source_file_sha256,
)
from hisiem_soc_copilot.evaluation.sealer import seal_manifest, verify_sealed_manifest
from hisiem_soc_copilot.evaluation.time_plan import build_event_time_plan

_LIVE = os.environ.get("RUN_HISIEM_DATASET_EVAL") == "1"

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason="set RUN_HISIEM_DATASET_EVAL=1 to run the real HISIEM GP-01 materialization gate",
)


def _reader() -> HisiemEvaluationReader:
    settings = get_settings()
    hisiem: HisiemSettings = settings.hisiem
    return HisiemEvaluationReader(
        tenant_id=settings.evaluation.tenant_id,
        settings=hisiem,
        base_url=hisiem.base_url,
        bearer_token=hisiem.bearer_token,
        timeout_seconds=hisiem.timeout_seconds,
    )


@pytest.fixture(scope="module")
def hisiem_reachable() -> bool:
    """True only when a real HISIEM control surface answers the reachability probe."""
    reader = _reader()
    try:
        return asyncio.run(reader.ping())
    finally:
        asyncio.run(reader.close())


async def test_live_gp01_materialize_resolve_verify_seal(hisiem_reachable: bool) -> None:
    """Prove the full real materialization path (E1-B.3 §21 / E1-B.4 §26)."""
    if not hisiem_reachable:
        pytest.skip("no reachable HISIEM control surface; not fabricating a PASS")

    settings = get_settings()
    ev = settings.evaluation
    hisiem = settings.hisiem

    run_id = f"live-gp01-{os.getpid()}"
    scenario = load_scenario()
    identity = derive_run_identity(run_id)
    now = datetime.now(UTC)
    time_plan = build_event_time_plan(now=now)

    reader = HisiemEvaluationReader(
        tenant_id=ev.tenant_id,
        settings=hisiem,
        base_url=hisiem.base_url,
        bearer_token=hisiem.bearer_token,
        timeout_seconds=hisiem.timeout_seconds,
    )
    injector = TcpSyslogEventInjector(ev.ssh_tcp_host, ev.ssh_tcp_port)
    try:
        materializer = Gp01Materializer(
            run_id=run_id,
            tenant_id=ev.tenant_id,
            scenario=scenario,
            identity=identity,
            time_plan=time_plan,
            injector=injector,
            reader=reader,
        )
        rule = await reader.get_rule_contract(scenario.rule_id)
        reachable = await reader.ping()
        await materializer.preflight(rule=rule, reachable=reachable)
        materializer.render_events()
        deadline = now + timedelta(seconds=ev.resolve_deadline_seconds)
        await materializer.inject_events()
        await materializer.resolve_events(deadline=deadline, interval=ev.poll_interval)
        await materializer.resolve_alert(deadline=deadline, interval=ev.poll_interval)
        dataset = materializer.verify()
        materializer.mark_materialized()
    finally:
        await reader.close()

    # Every semantic role resolved to a real HISIEM document.
    for role in ("F1", "F2", "F3", "F4", "F5", "S1"):
        resolved = dataset.resolved_events[role]
        assert resolved.document_id, f"{role} did not resolve to a real _id"
        assert resolved.index.startswith("siem-events-"), (
            f"{role} index {resolved.index!r} unexpected"
        )
    w1 = dataset.resolved_events["W1"]
    assert w1.source_ip == identity.watermark_source_ip

    # The real brute-force alert + its addressing id.
    alert = dataset.source_alert
    assert alert.rule_id == scenario.rule_id
    assert alert.address_id  # real HISIEM alert ES _id
    assert materializer.draft.state == MaterializationState.MATERIALIZED.value

    # Seal + integrity re-validation.
    oracle = scenario_oracle(dataset.scenario)
    code = CodeRevision(git_commit="live", dirty=True)
    manifest = build_manifest(
        dataset,
        oracle,
        code,
        scenario_source_file_sha256=source_file_sha256(),
        scenario_semantic_sha256=semantic_sha256(dataset.scenario),
    )
    run_dir = Path(ev.runs_dir) / "gp-01" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "materialization.json").write_text(dump_draft(materializer.draft), encoding="utf-8")
    manifest_path = run_dir / "manifest.json"
    seal_manifest(manifest, manifest_path)
    verified = verify_sealed_manifest(manifest_path)
    assert verified.source_alert.address_id == alert.address_id
    # launch projection == exact real address_id, no oracle data.
    proj = verified.launch_projection
    assert proj.address_id == alert.address_id
    assert proj.provider == "hisiem" and proj.resource_type == "alert"
