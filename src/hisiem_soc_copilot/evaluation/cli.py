"""GP-01 evaluation CLI — materialize / resume / seal / prepare (E1-B.3 §25).

Commands:
    python -m hisiem_soc_copilot.evaluation.cli materialize GP-01
    python -m hisiem_soc_copilot.evaluation.cli resume <run_id>
    python -m hisiem_soc_copilot.evaluation.cli seal <run_id>
    python -m hisiem_soc_copilot.evaluation.cli prepare GP-01
    python -m hisiem_soc_copilot.evaluation.cli verify-manifest <run_id>

``prepare GP-01`` is a convenience that runs materialize -> resolve -> verify ->
seal, but materialization and sealing remain separate internal contracts. Run
artifacts live under ``<runs_dir>/gp-01/<run_id>/materialization.json`` (mutable
recovery ledger) and ``manifest.json`` (immutable sealed evaluation artifact) —
never one file, never committed to Git.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from ..config import EvaluationSettings, HisiemSettings
from .contracts import (
    GP01_RULE_ID,
    MaterializationDraft,
    ScenarioSpec,
    VerifiedDataset,
)
from .hisiem_reader import HisiemEvaluationReader
from .identity import derive_run_identity
from .injector import TcpSyslogEventInjector
from .ledger import dump_draft, load_draft
from .manifest import build_manifest
from .materializer import Gp01Materializer
from .oracle import scenario_oracle
from .scenario_loader import semantic_sha256, source_file_sha256
from .sealer import seal_manifest, verify_sealed_manifest
from .time_plan import build_event_time_plan

_SCENARIO_KEY = "gp-01"
_GP01 = "GP-01"


def _run_dir(settings: EvaluationSettings, run_id: str) -> Path:
    return Path(settings.runs_dir) / "gp-01" / run_id


def _materialization_path(run_dir: Path) -> Path:
    return run_dir / "materialization.json"


def _manifest_path(run_dir: Path) -> Path:
    return run_dir / "manifest.json"


def _code_revision() -> tuple[str, bool]:
    """Best-effort git revision under which the tool runs (E1-B.4 §17)."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0:
            commit = out.stdout.strip()
            dirty = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip() != ""
            return commit, dirty
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown", True


def _reader(settings: EvaluationSettings, hisiem: HisiemSettings) -> HisiemEvaluationReader:
    return HisiemEvaluationReader(
        tenant_id=settings.tenant_id,
        settings=hisiem,
        base_url=hisiem.base_url,
        bearer_token=hisiem.bearer_token,
        timeout_seconds=hisiem.timeout_seconds,
    )


def _injector(settings: EvaluationSettings) -> TcpSyslogEventInjector:
    return TcpSyslogEventInjector(settings.ssh_tcp_host, settings.ssh_tcp_port)


def _scenario() -> ScenarioSpec:
    return ScenarioSpec()


def _checkpoint(ledger_path: Path, materializer: Gp01Materializer) -> None:
    """Persist the mutable draft ledger after every state transition so an
    INDETERMINATE/FAILED/partial run is durably recorded and a later ``resume``
    can reconcile instead of losing the ambiguous attempt (E1-B.3 §12, §17)."""
    ledger_path.write_text(dump_draft(materializer.draft), encoding="utf-8")


async def _materialize_run(
    settings: EvaluationSettings,
    hisiem: HisiemSettings,
    *,
    run_id: str | None = None,
) -> tuple[Path, MaterializationDraft, VerifiedDataset]:
    """Bind identity/time, preflight, inject, resolve, verify. Pure parts are
    deterministic; IO (injector + reader) is real HISIEM. Returns (run_dir,
    draft, dataset). No manifest is sealed here (E1-B.4 §2). The ledger is
    checkpointed after every transition so an interrupted run is resumable."""
    run_id = run_id or uuid4().hex
    scenario = _scenario()
    identity = derive_run_identity(run_id)
    now = datetime.now(UTC)
    time_plan = build_event_time_plan(now=now)

    reader = _reader(settings, hisiem)
    injector = _injector(settings)
    run_dir = _run_dir(settings, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = _materialization_path(run_dir)

    try:
        materializer = Gp01Materializer(
            run_id=run_id,
            tenant_id=settings.tenant_id,
            scenario=scenario,
            identity=identity,
            time_plan=time_plan,
            injector=injector,
            reader=reader,
        )
        rule = await reader.get_rule_contract(GP01_RULE_ID)
        # Typed readiness (200 + body.status==UP) surfaces UNAVAILABLE/AUTH_ERROR/
        # CONTRACT_MISMATCH/NOT_READY precisely instead of a generic bool.
        await reader.readiness()
        await materializer.preflight(rule=rule, reachable=True)
        _checkpoint(ledger_path, materializer)
        materializer.render_events()
        _checkpoint(ledger_path, materializer)
        deadline = now + timedelta(seconds=settings.resolve_deadline_seconds)
        await materializer.inject_events()
        _checkpoint(ledger_path, materializer)
        await materializer.resolve_events(deadline=deadline, interval=settings.poll_interval)
        _checkpoint(ledger_path, materializer)
        await materializer.resolve_alert(deadline=deadline, interval=settings.poll_interval)
        _checkpoint(ledger_path, materializer)
        dataset = materializer.verify()
        materializer.mark_materialized()
        _checkpoint(ledger_path, materializer)
        return run_dir, materializer.draft, dataset
    finally:
        await reader.close()


def _resume_injection_state(state: str) -> bool:
    """E1-B.3 §12 resume dispatch: only a genuine PRE-injection window may call
    inject_events(). Every post-injection state and the TERMINAL INDETERMINATE/
    FAILED states are excluded — an ambiguous or completed window is never
    re-injected from (zero writes) and must instead be abandoned for a NEW run_id
    when it cannot be reconciled."""
    return state in (
        "NEW",
        "PREFLIGHTED",
        "EVENTS_RENDERED",
    )


async def _resume_run(
    settings: EvaluationSettings,
    hisiem: HisiemSettings,
    *,
    run_id: str,
) -> tuple[Path, MaterializationDraft, VerifiedDataset]:
    """Reconcile a prior run: reuse the persisted draft, NEVER re-inject already
    attempted events, complete resolution/alert/verification (E1-B.3 §12, §18)."""
    scenario = _scenario()
    run_dir = _run_dir(settings, run_id)
    if not run_dir.exists():
        raise FileNotFoundError(f"no run directory for run_id={run_id}: {run_dir}")
    ledger_path = _materialization_path(run_dir)
    draft = load_draft(
        ledger_path,
        run_id=run_id,
        scenario_id=scenario.id,
        tenant_id=settings.tenant_id,
    )
    if draft is None:
        raise FileNotFoundError(f"no materialization ledger at {ledger_path}")
    if draft.identity is None or draft.time_plan is None:
        raise RuntimeError(f"run {run_id} ledger is missing identity/time plan; cannot resume")

    reader = _reader(settings, hisiem)
    injector = _injector(settings)
    try:
        materializer = Gp01Materializer(
            run_id=run_id,
            tenant_id=settings.tenant_id,
            scenario=scenario,
            identity=draft.identity,
            time_plan=draft.time_plan,
            injector=injector,
            reader=reader,
            draft=draft,
        )
        # §12: an INDETERMINATE/FAILED injection window is NEVER re-injected from.
        # The dispatch below mirrors the state machine's legal-from rules so resume
        # calls inject_events() ONLY for a fresh/partial pre-injection window; all
        # other states (INDETERMINATE/FAILED and EVENTS_INJECTED and later) are
        # reconciled/resolved with ZERO new injection attempts.
        if _resume_injection_state(materializer.draft.state):
            await materializer.inject_events()
            _checkpoint(ledger_path, materializer)
        deadline = datetime.now(UTC) + timedelta(seconds=settings.resolve_deadline_seconds)
        await materializer.resolve_events(deadline=deadline, interval=settings.poll_interval)
        _checkpoint(ledger_path, materializer)
        await materializer.resolve_alert(deadline=deadline, interval=settings.poll_interval)
        _checkpoint(ledger_path, materializer)
        dataset = materializer.verify()
        materializer.mark_materialized()
        _checkpoint(ledger_path, materializer)
        return run_dir, materializer.draft, dataset
    finally:
        await reader.close()


def _seal(dataset: VerifiedDataset, run_dir: Path) -> None:
    """Seal the verified dataset into an immutable manifest (E1-B.4)."""
    oracle = scenario_oracle(dataset.scenario)
    commit, dirty = _code_revision()
    from .contracts import CodeRevision

    code = CodeRevision(git_commit=commit, dirty=dirty)
    manifest = build_manifest(
        dataset,
        oracle,
        code,
        scenario_source_file_sha256=source_file_sha256(),
        scenario_semantic_sha256=semantic_sha256(dataset.scenario),
    )
    # E1-B.4 §17: a dirty worktree means this record is not reproducible from a
    # clean committed revision — flag it NON_AUTHORITATIVE (not a hard block).
    if dirty:
        print(
            "NON_AUTHORITATIVE: worktree has uncommitted changes; this sealed "
            "manifest is not reproducible from a clean git revision"
        )
    manifest_path = _manifest_path(run_dir)
    seal_manifest(manifest, manifest_path)
    verified = verify_sealed_manifest(manifest_path)
    print(f"sealed {manifest_path} integrity={verified.integrity.get('manifest_sha256')}")


async def _prepare(settings: EvaluationSettings, hisiem: HisiemSettings) -> None:
    """materialize -> resolve -> verify -> seal (internal stages stay separate)."""
    run_dir, draft, dataset = await _materialize_run(settings, hisiem)
    _seal(dataset, run_dir)
    print(f"prepared run {draft.run_id} (dir={run_dir})")


def _build_settings() -> tuple[EvaluationSettings, HisiemSettings]:
    from ..config import get_settings

    root = get_settings()
    return root.evaluation, root.hisiem


def _print_materialized(run_dir: Path, draft: MaterializationDraft) -> None:
    print(f"run_id={draft.run_id} state={draft.state} dir={run_dir}")
    print(f"  injected={len(draft.injected)} rendered={len(draft.rendered)}")
    print(f"  resolved_events={sorted(draft.resolved_events)}")
    if draft.resolved_alert is not None:
        print(f"  alert.address_id={draft.resolved_alert.address_id}")


async def _dispatch(argv: list[str]) -> int:
    settings, hisiem = _build_settings()
    if len(argv) < 2:
        print(__doc__)
        return 2
    command, target = argv[0], argv[1]

    if command == "materialize":
        if target.lower() != _SCENARIO_KEY and target.upper() != _GP01:
            print(f"unknown scenario {target!r}; expected GP-01")
            return 2
        run_dir, draft, dataset = await _materialize_run(settings, hisiem)
        _print_materialized(run_dir, draft)
        return 0

    if command == "resume":
        run_dir, draft, dataset = await _resume_run(settings, hisiem, run_id=target)
        _print_materialized(run_dir, draft)
        return 0

    if command == "seal":
        run_dir = _run_dir(settings, target)
        dataset = _load_verified_from_draft(settings, target)
        _seal(dataset, run_dir)
        return 0

    if command == "verify-manifest":
        manifest = verify_sealed_manifest(_manifest_path(_run_dir(settings, target)))
        print(
            f"manifest ok schema={manifest.schema_version} "
            f"address_id={manifest.source_alert.address_id}"
        )
        return 0

    if command == "prepare":
        if target.lower() != _SCENARIO_KEY and target.upper() != _GP01:
            print(f"unknown scenario {target!r}; expected GP-01")
            return 2
        await _prepare(settings, hisiem)
        return 0

    print(f"unknown command {command!r}")
    return 2


def _load_verified_from_draft(
    settings: EvaluationSettings, run_id: str
) -> VerifiedDataset:
    """Rehydrate a VerifiedDataset from the mutable draft so the Sealer can accept
    it WITHOUT re-running materialization (the draft recorded VERIFIED state)."""
    scenario = _scenario()
    run_dir = _run_dir(settings, run_id)
    draft = load_draft(
        _materialization_path(run_dir),
        run_id=run_id,
        scenario_id=scenario.id,
        tenant_id=settings.tenant_id,
    )
    if draft is None:
        raise FileNotFoundError(f"no materialization ledger for run_id={run_id}")
    if draft.state not in ("VERIFIED", "MATERIALIZED"):
        raise RuntimeError(
            f"run {run_id} is in state {draft.state!r}; only a VERIFIED/MATERIALIZED "
            "dataset may be sealed"
        )
    if draft.identity is None or draft.time_plan is None or draft.resolved_alert is None:
        raise RuntimeError(f"run {run_id} ledger is incomplete; cannot seal")
    from .verifier import DatasetVerifier

    # Recover the ORIGINAL verification instant from the frozen draft.verified_at
    # (set when the DatasetVerifier produced the VerifiedDataset) — NEVER from
    # draft.updated_at, which later ledger checkpoints rewrite, and NEVER a fresh
    # materialization time at seal (E1-B.4 correctness round).
    verified_at = draft.verified_at or draft.updated_at
    return DatasetVerifier(
        scenario=scenario,
        run=draft.identity,
        tenant_id=settings.tenant_id,
        time_plan=draft.time_plan,
    ).verify(
        resolved_events=draft.resolved_events,
        source_alert=draft.resolved_alert,
        materialized_at=verified_at,
    )


def main() -> int:
    try:
        return asyncio.run(_dispatch(sys.argv[1:]))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # bounded CLI failure (never leaks secrets)
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
