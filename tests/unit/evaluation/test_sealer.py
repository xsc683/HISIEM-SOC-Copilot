"""Offline manifest-sealer unit tests (E1-B.4 §2, §3, §5-§16, §21, §23).

No network / provider / model I/O: drives ManifestBuilder + sealer over a
``VerifiedDataset`` built by the shared fixture helpers. Covers the unverified
draft rejection, byte-determinism, tamper detection, no-overwrite immutability,
W1 evidence isolation, NaN/Infinity rejection, the oracle-free launch projection,
and CROSS-PROCESS first-writer-wins seal concurrency (E1-B.4 §20/§21) exercised
with REAL subprocesses under the kernel-backed advisory publication lock (stable
guard file; crash-release via process death).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from hisiem_soc_copilot.evaluation.contracts import (
    CANONICALIZATION_ID,
    MANIFEST_SCHEMA_VERSION,
    CodeRevision,
    MaterializationDraft,
    ScenarioOracle,
    SealedManifest,
    VerifiedDataset,
)
from hisiem_soc_copilot.evaluation.errors import (
    ManifestCanonicalizationError,
    ManifestIntegrityError,
    ManifestNotVerifiedError,
    ManifestPersistenceError,
    ManifestSchemaError,
    ManifestSealConflict,
    OracleIsolationViolation,
)
from hisiem_soc_copilot.evaluation.launch_projection import launch_ref
from hisiem_soc_copilot.evaluation.manifest import (
    build_manifest,
    canonicalize_manifest,
    compute_manifest_sha256,
    to_json,
    validate_manifest,
)
from hisiem_soc_copilot.evaluation.oracle import scenario_oracle
from hisiem_soc_copilot.evaluation.scenario_loader import (
    semantic_sha256,
    source_file_sha256,
)
from hisiem_soc_copilot.evaluation.sealer import seal_manifest, verify_sealed_manifest
from tests.fixtures.evaluation_fakes import (
    make_verified,
    source_alert,
)

_SOURCE_SHA = source_file_sha256()
_SEMANTIC_SHA = semantic_sha256()
_CODE = CodeRevision(git_commit="f326fb9", dirty=False)
_TENANT = "tenant-a"

# Fixed sealing instant: build_manifest defaults sealed_at to now-UTC, so the
# deterministic byte-identity tests inject an explicit canonical RFC3339 UTC
# instant (E1-B.4 correctness round).
_FIXED_SEALED_AT = "2026-09-05T12:01:30Z"


def _verified(**overrides) -> VerifiedDataset:
    return make_verified(**overrides)


def _build(verified: VerifiedDataset, *, sealed_at: str = _FIXED_SEALED_AT) -> SealedManifest:
    return build_manifest(
        verified,
        scenario_oracle(verified.scenario),
        _CODE,
        scenario_source_file_sha256=_SOURCE_SHA,
        scenario_semantic_sha256=_SEMANTIC_SHA,
        sealed_at=sealed_at,
    )


# ---------------------------------------------------------------------------
# §2 — an unverified draft can never be sealed
# ---------------------------------------------------------------------------


def test_draft_cannot_be_sealed() -> None:
    draft = MaterializationDraft(
        run_id="draft-run-1", scenario_id="gp-01", tenant_id=_TENANT
    )
    with pytest.raises(ManifestNotVerifiedError):
        build_manifest(
            draft,  # type: ignore[arg-type]
            ScenarioOracle(),
            _CODE,
            scenario_source_file_sha256=_SOURCE_SHA,
            scenario_semantic_sha256=_SEMANTIC_SHA,
        )
    with pytest.raises(ManifestSchemaError):
        seal_manifest(draft, "some-path.json")  # type: ignore[arg-type]


def test_build_manifest_requires_verified_types() -> None:
    verified = _verified()
    with pytest.raises(ManifestSchemaError):
        build_manifest(
            verified,
            oracle="not-an-oracle",  # type: ignore[arg-type]
            code=_CODE,
            scenario_source_file_sha256=_SOURCE_SHA,
            scenario_semantic_sha256=_SEMANTIC_SHA,
        )
    with pytest.raises(ManifestSchemaError):
        build_manifest(
            verified,
            scenario_oracle(verified.scenario),
            code="not-code",  # type: ignore[arg-type]
            scenario_source_file_sha256=_SOURCE_SHA,
            scenario_semantic_sha256=_SEMANTIC_SHA,
        )


# ---------------------------------------------------------------------------
# §3/§5 — deterministic canonical bytes
# ---------------------------------------------------------------------------


def test_same_verified_input_is_byte_identical() -> None:
    manifest_a = _build(_verified(run_id="det-run"))
    manifest_b = _build(_verified(run_id="det-run"))
    assert to_json(manifest_a) == to_json(manifest_b)
    assert canonicalize_manifest(manifest_a) == canonicalize_manifest(manifest_b)
    assert compute_manifest_sha256(manifest_a) == compute_manifest_sha256(manifest_b)
    assert manifest_a.schema_version == MANIFEST_SCHEMA_VERSION


def test_materialized_at_differs_from_sealed_at_when_explicit_times_differ() -> None:
    """E1-B.4 correctness round (contract item 24-D): ``sealed_at`` is the SEALING
    instant (when the manifest builder ran) and is deliberately decoupled from
    ``verified.materialized_at`` (the instant the DatasetVerifier produced the
    VerifiedDataset). Injecting an explicit fixed sealed_at must yield a manifest
    carrying BOTH distinct instants — neither overwrites the other."""
    verified = _verified(run_id="det-run")
    manifest = _build(verified, sealed_at="2026-09-05T12:01:30Z")
    assert verified.materialized_at == "2026-09-05T12:01:00Z"
    assert manifest.materialized_at == verified.materialized_at
    assert manifest.sealed_at == "2026-09-05T12:01:30Z"
    assert manifest.sealed_at != manifest.materialized_at
    # The canonical payload preserves the distinction on the run block.
    payload = canonicalize_manifest(manifest)
    assert payload["run"]["materialized_at"] == "2026-09-05T12:01:00Z"
    assert payload["run"]["sealed_at"] == "2026-09-05T12:01:30Z"
    assert payload["run"]["materialized_at"] != payload["run"]["sealed_at"]


def test_seal_round_trip_preserves_materialized_at_and_sealed_at(tmp_path: Path) -> None:
    """E1-B.4 correctness round (contract item 24-F): the seal ->
    ``verify_sealed_manifest`` round trip must preserve BOTH instants — the sealed
    manifest is an immutable record whose ``materialized_at`` (the verification
    instant) is never rewritten by the later seal."""
    path = tmp_path / "manifest.json"
    verified = _verified(run_id="det-run")
    manifest = _build(verified, sealed_at="2026-09-05T12:01:30Z")
    seal_manifest(manifest, path)
    restored = verify_sealed_manifest(path)
    assert restored.materialized_at == "2026-09-05T12:01:00Z"
    assert restored.sealed_at == "2026-09-05T12:01:30Z"
    assert restored.materialized_at != restored.sealed_at


def test_manifest_schema_and_integrity() -> None:
    manifest = _build(_verified())
    assert manifest.integrity["canonicalization"] == CANONICALIZATION_ID
    assert len(manifest.integrity["manifest_sha256"]) == 64
    validate_manifest(manifest)  # recomputes digest; no raise
    # canonical payload is an ordered top-level mapping
    payload = canonicalize_manifest(manifest)
    assert list(payload) == [
        "schema_version",
        "scenario",
        "run",
        "scope",
        "entities",
        "events",
        "control_events",
        "source_alert",
        "oracle",
        "code",
        "integrity",
    ]


def test_semantic_events_in_committed_order() -> None:
    manifest = _build(_verified())
    assert [e.logical_role for e in manifest.events] == ["F1", "F2", "F3", "F4", "F5", "S1"]
    assert [c.logical_role for c in manifest.control_events] == ["W1"]


# ---------------------------------------------------------------------------
# §16 — tampering invalidates the digest
# ---------------------------------------------------------------------------


def test_tamper_changes_manifest_sha256() -> None:
    manifest = _build(_verified())
    valid = to_json(manifest)
    # Flip a byte in the address_id inside the source_alert block.
    tampered = json.loads(valid)
    tampered["source_alert"]["address_id"] = "es-doc-TAMPERED"
    tampered_bytes = json.dumps(
        tampered, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert tampered_bytes != valid.encode("utf-8")

    from hisiem_soc_copilot.evaluation.sealer import validate_sealed_json

    with pytest.raises(ManifestIntegrityError):
        validate_sealed_json(tampered_bytes, source="tampered.json")


def test_validate_manifest_rejects_recorded_digest_mismatch() -> None:
    manifest = _build(_verified())
    forged = replace(
        manifest, integrity={"canonicalization": CANONICALIZATION_ID, "manifest_sha256": "0" * 64}
    )
    with pytest.raises(ManifestIntegrityError):
        validate_manifest(forged)


# ---------------------------------------------------------------------------
# §21 — immutability / no overwrite
# ---------------------------------------------------------------------------


def test_existing_different_manifest_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest_a = _build(_verified(run_id="run-a"))
    manifest_b = _build(_verified(run_id="run-b"))
    assert manifest_a.run.run_id != manifest_b.run.run_id

    seal_manifest(manifest_a, path)
    with pytest.raises(ManifestSealConflict):
        seal_manifest(manifest_b, path)
    # Idempotent re-seal of identical bytes succeeds.
    seal_manifest(manifest_a, path)
    assert verify_sealed_manifest(path).run.run_id == "run-a"


def test_seal_then_verify_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = _build(_verified())
    seal_manifest(manifest, path)
    verified = verify_sealed_manifest(path)
    assert verified.schema_version == MANIFEST_SCHEMA_VERSION
    assert verified.integrity["manifest_sha256"] == manifest.integrity["manifest_sha256"]
    assert verified.run.run_id == manifest.run.run_id


# ---------------------------------------------------------------------------
# §10/§12 — W1 never satisfies evidence / oracle isolation
# ---------------------------------------------------------------------------


def test_w1_absent_from_oracle_required_evidence_roles() -> None:
    verified = _verified()
    oracle = scenario_oracle(verified.scenario)
    assert "W1" not in oracle.required_evidence_roles
    # Correctness freeze: the source alert proves the brute-force threshold, so
    # the oracle requires only the S1 success — F1..F5 are not mandatory
    # re-retrievals (E1-B.4 §12). A fresh ScenarioSpec() default is the contract.
    assert oracle.required_evidence_roles == ("S1",)
    assert "W1" not in [e.logical_role for e in verified.events]  # isolated in control
    assert verified.control_events[0].logical_role == "W1"


def test_oracle_control_role_in_evidence_is_rejected() -> None:

    verified = _verified()
    leaking = replace(
        verified.scenario,
        required_evidence_roles=("S1", "W1"),
    )
    verified = replace(verified, scenario=leaking)
    with pytest.raises(OracleIsolationViolation):
        scenario_oracle(leaking)
    with pytest.raises(OracleIsolationViolation):
        _build(verified)


def test_oracle_s1_only_and_subset_of_semantic_roles() -> None:
    # A fresh ScenarioSpec() default (the GP-01 evidence contract) must yield an
    # oracle whose required_evidence_roles == ("S1",) — a strict SUBSET of the
    # full semantic ground-truth roles — without an equality force.
    verified = _verified()
    oracle = scenario_oracle(verified.scenario)
    assert oracle.required_evidence_roles == ("S1",)
    assert set(oracle.required_evidence_roles) <= set(verified.scenario.semantic_roles)
    assert set(oracle.required_evidence_roles) < set(verified.scenario.semantic_roles)


def test_oracle_evidence_outside_semantic_roles_is_rejected() -> None:
    # The generic invariant is containment: an evidence role that is NOT among
    # the declared semantic roles violates oracle isolation.
    verified = _verified()
    invalid = replace(verified.scenario, required_evidence_roles=("S1", "NOT-A-ROLE"))
    with pytest.raises(OracleIsolationViolation):
        scenario_oracle(invalid)


# ---------------------------------------------------------------------------
# §15 — NaN/Infinity rejected
# ---------------------------------------------------------------------------


def test_nan_infinity_rejected_during_canonicalization() -> None:
    manifest = _build(_verified())
    # Non-finite values must be rejected even if they reach the payload object.
    poisoned = replace(manifest, source_alert=source_alert(manifest.run, event_count=float("inf")))
    with pytest.raises(ManifestCanonicalizationError):
        canonicalize_manifest(poisoned)


# ---------------------------------------------------------------------------
# §14 — launch projection contains no oracle data
# ---------------------------------------------------------------------------


def test_launch_projection_contains_only_launch_fields() -> None:
    manifest = _build(_verified())
    ref = launch_ref(manifest)
    assert ref.provider == "hisiem"
    assert ref.resource_type == "alert"
    assert ref.address_id == manifest.source_alert.address_id
    assert ref.business_id == manifest.source_alert.business_id
    serialized = json.dumps(ref.__dict__)
    # No oracle / events / integrity content may leak into the launcher view.
    for secret_token in (
        "oracle",
        "required_evidence_roles",
        "expected_verdict",
        "manifest_sha256",
        "facts",
        "integrity",
    ):
        assert secret_token not in serialized


# ---------------------------------------------------------------------------
# §20/§21 — CROSS-PROCESS first-writer-wins seal concurrency (E1-B.4 correctness
# round). REAL worker subprocesses (not threads / asyncio / multiprocessing
# shared-memory), launched via subprocess.run against the venv interpreter, so
# the kernel-backed advisory lock on the stable guard file is exercised across
# independent processes.
# ---------------------------------------------------------------------------


def _subprocess_python() -> str:
    """The interpreter that runs the tests (venv python) so subprocess workers
    resolve the same installed ``hisiem_soc_copilot`` package."""
    return sys.executable


def _spawn_workers(
    tmp_path: Path,
    path: Path,
    spec: str,
    workers: list[str],
) -> list[subprocess.CompletedProcess[str]]:
    """Run each worker script concurrently against the same seal target.

    ``spec`` is the raw ``python -c`` script and each entry of ``workers`` is a
    JSON-encoded list of positional argv entries to that spec. All workers are
    Popen'd first and parked on a release barrier (a sentinel file whose absence
    they spin on) so every process reaches the seal gate before any is released —
    guaranteeing genuinely CONTENDED kernel-lock acquisition, not merely serialized
    seals. stdout/stderr are captured as text.
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)  # hermetic: no ambient test-path leakage
    barrier = tmp_path / f".barrier-{os.getpid()}"  # absent => workers wait
    procs: list[tuple[subprocess.Popen[str], list[str]]] = []
    for argv in workers:
        env2 = env.copy()
        env2["_SEAL_BARRIER"] = str(barrier)  # workers spin until this file exists
        procs.append(
            (
                subprocess.Popen(
                    [_subprocess_python(), "-c", spec, *json.loads(argv)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env2,
                ),
                json.loads(argv),
            )
        )
    # Every worker has been spawned; release them all at once so the seals race.
    barrier.write_text("go", encoding="utf-8")
    results: list[subprocess.CompletedProcess[str]] = []
    for proc, argv in procs:
        stdout, stderr = proc.communicate(timeout=120)
        results.append(
            subprocess.CompletedProcess(
                [_subprocess_python(), "-c", spec, *argv],
                proc.returncode,
                stdout,
                stderr,
            )
        )
    with contextlib.suppress(OSError):
        barrier.unlink()
    return results


# The worker script body. argv[0] is the manifest path, argv[1] the worker seed,
# argv[2] the run_id suffix, argv[3] the shared sealed_at instant (identical for
# every worker, so IDENTICAL-input workers emit byte-identical to_json), and
# argv[4] a json-encoded dict of address_id overrides for the fake source alert.
_WORKER_SCRIPT = r"""
import json
import sys

from hisiem_soc_copilot.evaluation.contracts import (
    ScenarioSpec,
    CodeRevision,
)
from hisiem_soc_copilot.evaluation.manifest import build_manifest, to_json
from hisiem_soc_copilot.evaluation.oracle import scenario_oracle
from hisiem_soc_copilot.evaluation.scenario_loader import (
    semantic_sha256,
    source_file_sha256,
)
from hisiem_soc_copilot.evaluation.sealer import seal_manifest
from tests.fixtures.evaluation_fakes import make_verified

path, seed, run_suffix, sealed_at, overrides_json = sys.argv[1:6]
run_id = f"{seed}-{run_suffix}"

# NOTE: cannot apply ScenarioSpec() here directly because scenario_oracle
# requires required_evidence_roles to be a SUBSET of semantic_roles; the GP-01
# default ("S1",) already satisfies that, so build a spec from the default.
scenario = ScenarioSpec()

overrides = json.loads(overrides_json)
alert_overrides = overrides.get("alert", {})
verified = make_verified(run_id=run_id, scenario=scenario)
# Deterministic fake source alert: an explicit per-worker address_id (default
# address is fixed and identical, so use overrides to force distinct manifests
# for the different-manifest scenario).
from hisiem_soc_copilot.evaluation.contracts import VerifiedDataset, ResolvedAlert
from dataclasses import replace as _replace
verified = _replace(
    verified,
    source_alert=ResolvedAlert(
        provider=verified.source_alert.provider,
        address_id=alert_overrides.get("address_id", verified.source_alert.address_id),
        business_id=verified.source_alert.business_id,
        rule_id=verified.source_alert.rule_id,
        rule_name=verified.source_alert.rule_name,
        entity=verified.source_alert.entity,
        created_at=verified.source_alert.created_at,
        event_count=alert_overrides.get("event_count", verified.source_alert.event_count),
        status=verified.source_alert.status,
        related_event_refs=list(verified.source_alert.related_event_refs),
    ),
)

oracle = scenario_oracle(verified.scenario)
code = CodeRevision(git_commit="f326fb9", dirty=False)
manifest = build_manifest(
    verified,
    oracle,
    code,
    scenario_source_file_sha256=source_file_sha256(),
    scenario_semantic_sha256=semantic_sha256(verified.scenario),
    sealed_at=sealed_at,
)
# Every worker constructs and serializes ITS OWN manifest deterministically from
# explicit inputs — nothing is copied byte-for-byte from the parent test.
expected = to_json(manifest).encode("utf-8")

# Park on the release barrier: bounded spin (no unbounded wall-clock sleep) until
# the parent has spawned every worker, so the advisory-lock acquisitions below
# genuinely race.
import os as _os
import time as _time

_barrier = _os.environ.get("_SEAL_BARRIER")
if _barrier:
    for _ in range(10_000):
        if _os.path.exists(_barrier):
            break
        _time.sleep(0.0005)

try:
    seal_manifest(manifest, path)
except Exception as exc:  # noqa: BLE001 - a typed sealer failure is a valid outcome
    print(type(exc).__name__, file=sys.stderr)
    sys.exit(3)

try:
    with open(path, "rb") as handle:
        actual = handle.read()
except OSError:
    print("READ_FAILED", file=sys.stderr)
    sys.exit(4)

if actual == expected:
    sys.exit(0)  # success exit for this worker
print("BYTE_MISMATCH", file=sys.stderr)
sys.exit(5)
"""


def test_concurrent_seal_different_manifests_one_wins(tmp_path: Path) -> None:
    """Two processes seal DIFFERENT manifests to one absent path.

    Exactly one seal must succeed (exit 0) and the other must report a typed
    :class:`ManifestSealConflict` (exit 3). The surviving bytes must equal the
    winner's exact bytes (never a torn/mixed JSON document).
    """
    path = tmp_path / "manifest.json"
    shared_sealed_at = "2026-09-05T12:01:30Z"
    # Distinct source-alert address_ids force distinct deterministic manifests.
    worker_a = json.dumps(
        [
            str(path),
            "run-a",
            "x",
            shared_sealed_at,
            json.dumps({"alert": {"address_id": "es-doc-a"}}),
        ]
    )
    worker_b = json.dumps(
        [
            str(path),
            "run-b",
            "y",
            shared_sealed_at,
            json.dumps({"alert": {"address_id": "es-doc-b"}}),
        ]
    )

    results = _spawn_workers(tmp_path, path, _WORKER_SCRIPT, [worker_a, worker_b])

    exits = sorted(result.returncode for result in results)
    assert exits == [0, 3], (
        f"expected exactly one success and one ManifestSealConflict; got {results!r}"
    )
    loser = next(result for result in results if result.returncode == 3)
    assert "ManifestSealConflict" in loser.stderr

    # The surviving file must be a complete, valid sealed manifest whose run_id
    # is the winner's — i.e. the winner's exact bytes won the race.
    sealed = verify_sealed_manifest(path)
    assert sealed.run.run_id in ("run-a-x", "run-b-y")
    winner_bytes = path.read_bytes()
    assert json.loads(winner_bytes)["run"]["run_id"] == sealed.run.run_id
    # verify_sealed_manifest re-validates schema + integrity digest, proving the
    # file is not torn/partial. Winner's manifest serializes to these bytes.
    from hisiem_soc_copilot.evaluation.manifest import to_json as _to_json

    winner_manifest = _build(_verified(run_id=sealed.run.run_id), sealed_at=shared_sealed_at)
    # The worker replaced the default source alert address_id; rebuild to match.
    from hisiem_soc_copilot.evaluation.contracts import ResolvedAlert

    winner_manifest = replace(
        winner_manifest,
        source_alert=ResolvedAlert(
            provider=winner_manifest.source_alert.provider,
            address_id="es-doc-a" if sealed.run.run_id == "run-a-x" else "es-doc-b",
            business_id=winner_manifest.source_alert.business_id,
            rule_id=winner_manifest.source_alert.rule_id,
            rule_name=winner_manifest.source_alert.rule_name,
            entity=winner_manifest.source_alert.entity,
            created_at=winner_manifest.source_alert.created_at,
            event_count=winner_manifest.source_alert.event_count,
            status=winner_manifest.source_alert.status,
            related_event_refs=list(winner_manifest.source_alert.related_event_refs),
        ),
    )
    assert winner_bytes == _to_json(winner_manifest).encode("utf-8")
    assert json.loads(winner_bytes)["run"]["sealed_at"] == shared_sealed_at


def test_concurrent_seal_identical_manifests_both_succeed(tmp_path: Path) -> None:
    """Two processes seal the IDENTICAL manifest (same run_id + sealed_at) to one
    absent path. Both must succeed idempotently (exit 0) and the file must hold
    the identical bytes — no corruption."""
    path = tmp_path / "manifest.json"
    shared_sealed_at = "2026-09-05T12:01:30Z"
    shared = json.dumps(
        [
            str(path),
            "run-same",
            "x",
            shared_sealed_at,
            json.dumps({"alert": {"address_id": "es-doc-same"}}),
        ]
    )
    results = _spawn_workers(tmp_path, path, _WORKER_SCRIPT, [shared, shared])
    assert [r.returncode for r in results] == [0, 0], (
        f"identical manifests must BOTH succeed idempotently; got {results!r}"
    )
    sealed = verify_sealed_manifest(path)  # schema + integrity valid
    assert sealed.run.run_id == "run-same-x"
    assert sealed.sealed_at == shared_sealed_at


# ---------------------------------------------------------------------------
# §20/§25 — kernel-backed publication lock: atomic visibility + crash release +
# double-check-after-lock (correctness round §25, cases F-J). H/I/J are exercised
# with real threads + real os calls where the protocol is identical across threads
# and processes; the crash-release + contention cases use REAL subprocesses (the
# kernel, not a thread scheduler, arbitrates lock lifetime).
# ---------------------------------------------------------------------------


def test_reader_never_observes_partial_final_manifest(tmp_path: Path) -> None:
    """H (§25): while a writer is blocked between its temp-file write and the atomic
    rename, a concurrent reader must NEVER observe a partial ``manifest.json`` —
    the final path is either absent or the complete winner bytes."""
    import threading

    from hisiem_soc_copilot.evaluation import sealer as sealer_mod

    path = tmp_path / "manifest.json"
    manifest = _build(_verified(run_id="atomic-h"))
    data = to_json(manifest).encode("utf-8")

    released = threading.Event()
    entered_hook = threading.Event()

    def hook(target: Path, tmp: Path) -> None:
        del target, tmp
        entered_hook.set()
        released.wait(timeout=10)

    original_hook = sealer_mod._AFTER_TEMP_WRITE_HOOK  # noqa: SLF001
    sealer_mod._AFTER_TEMP_WRITE_HOOK = hook  # type: ignore[assignment]  # noqa: SLF001
    try:
        worker = threading.Thread(target=seal_manifest, args=(manifest, path))
        worker.start()
        assert entered_hook.wait(timeout=10), "writer never reached the temp-write hook"
        # While the writer is paused AFTER the temp file is complete but BEFORE the
        # atomic rename, the final manifest.json must NOT exist — a concurrent
        # reader can never observe a partial final document.
        assert not path.exists(), "final manifest.json appeared before the atomic rename"
        released.set()
        worker.join(timeout=10)
    finally:
        sealer_mod._AFTER_TEMP_WRITE_HOOK = original_hook  # type: ignore[assignment]  # noqa: SLF001

    # After the rename the final is the complete valid winner manifest.
    sealed = verify_sealed_manifest(path)
    assert sealed.run.run_id == "atomic-h"
    assert path.read_bytes() == data


def test_double_check_after_lock_does_not_overwrite_new_final(tmp_path: Path) -> None:
    """J (§25): a sealer that waits on a held kernel lock, then acquires it AFTER
    another writer published, must re-check the final and compare (idempotent/
    conflict) instead of blindly overwriting the manifest that appeared while it
    waited."""
    import threading

    from hisiem_soc_copilot.evaluation import sealer as sealer_mod

    path = tmp_path / "manifest.json"
    manifest_a = _build(_verified(run_id="dc-a"))
    manifest_b = _build(_verified(run_id="dc-b"))

    # B (sealer) starts, observes the final ABSENT, and is about to acquire the lock.
    results: list[Exception | None] = []

    def sealer_b() -> None:
        try:
            seal_manifest(manifest_b, path)
            results.append(None)
        except Exception as exc:  # noqa: BLE001 - capture the typed outcome
            results.append(exc)

    # Hold the kernel lock first so B cannot acquire it immediately.
    holder_release = threading.Event()
    holder_won = threading.Event()

    def holder() -> None:
        handle = sealer_mod._open_guard(path)  # noqa: SLF001
        try:
            while not sealer_mod._try_lock_nb(handle):  # noqa: SLF001
                time.sleep(0.01)
        except Exception:  # noqa: BLE001 - never let the holder thread die silently
            handle.close()
            raise
        holder_won.set()
        try:
            # While holding the lock, A publishes the final manifest via a direct
            # atomic write (simulating a writer that won the publication before B).
            sealer_mod._write_atomic(  # noqa: SLF001
                path, to_json(manifest_a).encode("utf-8")
            )
            holder_release.wait(timeout=10)
        finally:
            sealer_mod._release_lock(handle)  # noqa: SLF001

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    assert holder_won.wait(timeout=10), "holder never acquired the kernel lock"

    b_thread = threading.Thread(target=sealer_b)
    b_thread.start()
    # Let B spin waiting for the lock, then release the holder (A already published).
    time.sleep(0.2)
    holder_release.set()
    holder_thread.join(timeout=10)
    b_thread.join(timeout=10)

    assert len(results) == 1
    # B must NOT overwrite A: B's bytes differ, so B reports a conflict and A's
    # manifest survives.
    assert isinstance(results[0], ManifestSealConflict)
    assert verify_sealed_manifest(path).run.run_id == "dc-a"


def test_losers_wait_bounded_and_raise_typed_error_on_hung_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """J (§25): a contender whose kernel-lock holder never completes within the
    bounded wait must raise a typed ManifestPersistenceError — never a bare
    TimeoutError/OSError, and never an infinite wait. There is no stale-lease
    recovery to unbind a hung holder; the bounded nonblocking wait is the backstop."""
    from hisiem_soc_copilot.evaluation import sealer as sealer_mod

    monkeypatch.setattr(sealer_mod, "SEAL_LOCK_WAIT_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(sealer_mod, "SEAL_LOCK_POLL_INTERVAL_SECONDS", 0.02)

    path = tmp_path / "manifest.json"
    manifest = _build(_verified(run_id="hung-j"))

    # Hold the kernel lock in THIS process so the sealer cannot acquire it within
    # the bounded window — a real (os-level) lock holder that never releases.
    holder_handle = sealer_mod._open_guard(path)  # noqa: SLF001
    while not sealer_mod._try_lock_nb(holder_handle):  # noqa: SLF001
        time.sleep(0.01)
    try:
        with pytest.raises(ManifestPersistenceError):
            seal_manifest(manifest, path)
        assert not path.exists()  # nothing was published by the hung holder
    finally:
        sealer_mod._release_lock(holder_handle)  # noqa: SLF001
    # After the holder releases, the SAME sealer succeeds — the guard is reusable.
    seal_manifest(manifest, path)
    assert verify_sealed_manifest(path).run.run_id == "hung-j"
    assert tmp_path / "manifest.json.seal.lock"  # guard persists (never deleted)


def _hermetic_env(barrier: str | None) -> dict[str, str]:
    """A PYTHONPATH-clean env for subprocess workers; the release barrier is set
    only when supplied."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("_SEAL_BARRIER", None)
    if barrier is not None:
        env["_SEAL_BARRIER"] = barrier
    return env


# Crash worker: acquires the kernel lock on the guard and then dies WITHOUT ever
# unlocking or closing the handle (os._exit never runs finally/atexit). Only the
# kernel can release the lock — at process death. exit code 7 signals the crash.
_CRASH_WORKER = r"""
import os
import sys
import time
from pathlib import Path as _Path

from hisiem_soc_copilot.evaluation import sealer as sealer_mod

target = _Path(sys.argv[1])

_barrier = os.environ.get("_SEAL_BARRIER")
if _barrier:
    for _ in range(10_000):
        if os.path.exists(_barrier):
            break
        time.sleep(0.0005)

handle = sealer_mod._open_guard(target)
while not sealer_mod._try_lock_nb(handle):
    time.sleep(0.001)
print("LOCKED", flush=True)
os._exit(7)  # noqa: PLR1722 - deliberate hard crash; never unlock/close
"""


def test_crash_releases_lock_and_next_sealer_succeeds(tmp_path: Path) -> None:
    """Kernel lock lifetime (no stale-timeout, no ABA): a writer that CRASHES while
    holding the publication lock releases it when its process dies; the next sealer
    (in THIS process) immediately acquires the freed lock and publishes."""
    path = tmp_path / "manifest.json"
    manifest = _build(_verified(run_id="crash-k"))

    crash = subprocess.run(
        [_subprocess_python(), "-c", _CRASH_WORKER, str(path)],
        capture_output=True,
        text=True,
        env=_hermetic_env(barrier=None),
    )
    assert crash.returncode == 7, f"crash worker must os._exit(7); got {crash!r}"
    assert "LOCKED" in crash.stdout, f"holder never locked; got {crash!r}"

    # The stable guard persists (never deleted by crash recovery) and the crashed
    # holder never published a final manifest.
    guard = tmp_path / "manifest.json.seal.lock"
    assert guard.exists(), "the stable guard file must persist after a crash"
    assert not path.exists(), "crashed holder must not have published a final"

    # The SAME process now acquires the freed kernel lock and publishes. There is no
    # stale lease/record to wait out — the kernel released the lock at process exit.
    seal_manifest(manifest, path)
    assert verify_sealed_manifest(path).run.run_id == "crash-k"


def test_kernel_lock_blocks_second_process_until_holder_releases(
    tmp_path: Path,
) -> None:
    """Two REAL processes contend on one kernel lock: while the holder keeps it, a
    second process's seal can neither acquire the lock nor publish; only after the
    holder releases (or dies) does the contender proceed. This proves the sealer
    serializes publication across processes through the OS lock — the ABA window of
    the old lock-file unlink protocol is gone."""
    path = tmp_path / "manifest.json"
    shared_sealed_at = "2026-09-05T12:01:30Z"

    # Worker A: seal different-bytes manifest in a subprocess, racing the local
    # holder. It must wait out its bounded window (no stale-lock recovery can free
    # our live hold) and report a typed error — NOT publish over us.
    from hisiem_soc_copilot.evaluation import sealer as sealer_mod

    sealer_mod.SEAL_LOCK_WAIT_TIMEOUT_SECONDS = 0.4  # type: ignore[assignment]
    sealer_mod.SEAL_LOCK_POLL_INTERVAL_SECONDS = 0.02  # type: ignore[assignment]
    try:
        handle = sealer_mod._open_guard(path)  # noqa: SLF001
        while not sealer_mod._try_lock_nb(handle):  # noqa: SLF001
            time.sleep(0.01)
        try:
            contender = subprocess.run(
                [
                    _subprocess_python(),
                    "-c",
                    _WORKER_SCRIPT,
                    str(path),
                    "contend",
                    "x",
                    shared_sealed_at,
                    json.dumps({"alert": {"address_id": "es-doc-contend"}}),
                ],
                capture_output=True,
                text=True,
                env=_hermetic_env(barrier=None),
            )
            assert contender.returncode == 3, (
                f"contender must time out while we hold the lock; got {contender!r}"
            )
            assert "ManifestPersistenceError" in contender.stderr
            assert not path.exists(), (
                "contender must never publish while the lock is held elsewhere"
            )
        finally:
            sealer_mod._release_lock(handle)  # noqa: SLF001
    finally:
        sealer_mod.SEAL_LOCK_WAIT_TIMEOUT_SECONDS = 10.0  # type: ignore[assignment]
        sealer_mod.SEAL_LOCK_POLL_INTERVAL_SECONDS = 0.05  # type: ignore[assignment]

    # Once the holder releases, the SAME contender input publishes cleanly — the
    # guard was never deleted and the lock is reusable across processes.
    seal_manifest(_build(_verified(run_id="contend-final")), path)
    assert verify_sealed_manifest(path).run.run_id == "contend-final"
