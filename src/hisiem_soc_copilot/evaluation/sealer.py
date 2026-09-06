"""Atomic, immutable filesystem persistence for sealed GP-01 manifests (E1-B.4).

The sealer persists only already-built :class:`contracts.SealedManifest` objects
(produced from a :class:`contracts.VerifiedDataset` by :mod:`.manifest`). There is
deliberately NO ``seal(draft)`` / ``seal(VerifiedDataset)`` API and no code path
accepting a :class:`contracts.MaterializationDraft`, so sealing an unverified
dataset is structurally impossible (E1-B.4 §2).

Immutability + idempotency (E1-B.4 §21): an existing target must byte-match the
newly computed bytes for idempotent success; any difference is a
:class:`ManifestSealConflict`. Pure filesystem work — no network, provider, or
model I/O.

Publication is CROSS-PROCESS first-writer-wins AND atomically visible. The final
``manifest.json`` is ONLY ever created by an atomic ``os.replace`` of a fully
written + fsynced temp file in the same directory — it can never be observed as a
torn/partial JSON document (E1-B.4 §20). A separate ``<name>.seal.lock`` claim
file gates the publication:

- final absent → claim the lock file with ``O_CREAT | O_EXCL`` (owner token +
  pid + created-at), then DOUBLE-CHECK the final path (another writer may have
  completed while we waited for the lock), then atomically publish via temp +
  fsync + ``os.replace``, then release the lock.
- loser/contender → bounded wait for the lock with a stale-lock timeout; once the
  final appears, resolve idempotent-success (identical bytes) vs
  :class:`ManifestSealConflict` (different bytes).
- a crash that leaves a stale lock is recovered after a bounded stale threshold
  WITHOUT ever overwriting an already-published final manifest.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import time
from pathlib import Path
from typing import Any, Final

from .contracts import (
    CodeRevision,
    RelatedEventRef,
    ResolvedAlert,
    ResolvedEvent,
    RunIdentity,
    ScenarioOracle,
    ScenarioSpec,
    SealedManifest,
)
from .errors import (
    ManifestPersistenceError,
    ManifestSchemaError,
    ManifestSealConflict,
)
from .manifest import (
    SCOPE_KEY_SCENARIO_SEMANTIC_SHA256,
    SCOPE_KEY_SCENARIO_SOURCE_FILE_SHA256,
    to_json,
    validate_manifest,
)

# Temp-file suffix; kept in the SAME directory so os.replace stays atomic.
_TMP_SUFFIX: Final = ".seal.tmp"

# Lock-file suffix for the separate publication-claim file.
_LOCK_SUFFIX: Final = ".seal.lock"

# Bounded wait/poll/stale constants for cross-process first-writer-wins. These are
# module-level (not Final) so tests may shrink the timeout/poll to keep adversarial
# cases fast; production uses the documented defaults.
SEAL_LOCK_WAIT_TIMEOUT_SECONDS: float = 10.0
SEAL_LOCK_POLL_INTERVAL_SECONDS: float = 0.05
SEAL_LOCK_STALE_AFTER_SECONDS: Final = 30.0

# Backfill for the RunIdentity.watermark_source_ip on file verification. The
# sealed schema intentionally does not persist the watermark control source (it is
# a materializer-internal identity, never a scored semantic), so a verified
# manifest reconstructs it as this documented placeholder. It is a non-address
# literal, so the ``attack_source_ip != watermark_source_ip`` invariant cannot be
# violated by the rebuild.
_WATERMARK_NOT_SEALED: Final = "watermark-not-sealed"

# Test seam: invoked after the temp manifest has been fully written + fsynced but
# BEFORE the atomic ``os.replace`` onto the final path. Defaults to a no-op; tests
# that prove the final path is never observable as partial JSON (E1-B.4 §20 / §25)
# install a hook that blocks here while a concurrent reader polls the final path.
_AFTER_TEMP_WRITE_HOOK = None  # callable[[Path, Path], None] | None


__all__ = [
    "ManifestSealer",
    "seal_manifest",
    "validate_sealed_json",
    "verify_sealed_manifest",
]


def _lock_path(target: Path) -> Path:
    return target.with_name(target.name + _LOCK_SUFFIX)


def _tmp_path(target: Path) -> Path:
    return target.with_name(target.name + _TMP_SUFFIX)


def _owner_token() -> str:
    """A short, collision-resistant writer identity for the lock claim file."""
    return f"{socket.gethostname()}:{os.getpid()}:{time.monotonic_ns():x}"


def _read_lock_record(lock: Path) -> dict[str, Any] | None:
    """Parse the lock claim record (owner + created_at), or None if absent/bad."""
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _lock_is_stale(lock: Path) -> bool:
    """A lock is stale when it has outlived the bounded stale threshold.

    The PRIMARY signal is the ``created_at`` recorded inside the lock (written by
    the owner right after the exclusive-create). A lock with NO parseable record
    (e.g. a crash between ``os.open`` and the record write) falls back to the file
    mtime so a wedged target is still recoverable after the threshold — mtime is
    never the sole signal for a well-formed lock.
    """
    record = _read_lock_record(lock)
    if record is not None:
        created = record.get("created_at")
        if isinstance(created, (int, float)):
            return (time.time() - created) > SEAL_LOCK_STALE_AFTER_SECONDS
    try:
        mtime = lock.stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) > SEAL_LOCK_STALE_AFTER_SECONDS


def _recover_stale_lock(lock: Path) -> None:
    """Remove a STALE lock file, but NEVER if a final manifest already appeared.

    Recovery only applies to a lock that has exceeded the stale threshold AND
    whose final manifest is still absent. It never removes a lock whose owner is
    actually mid-publication of a manifest (that would let two writers race the
    final rename) — a lock younger than the stale threshold is left untouched.
    """
    if not lock.exists():
        return
    if not _lock_is_stale(lock):
        return
    # The final manifest must still be absent — never unlink a claim whose owner
    # already published (the manifest is authoritative; the lock is just residue).
    if not lock.name.endswith(_LOCK_SUFFIX):
        return
    final_name = lock.name[: -len(_LOCK_SUFFIX)]
    if lock.with_name(final_name).exists():
        return
    with contextlib.suppress(OSError):
        lock.unlink(missing_ok=True)


def _acquire_lock(lock: Path, token: str) -> bool:
    """Try to claim ``lock`` with ``O_CREAT | O_EXCL``; True if this caller won.

    The record (owner token + created_at) is written through the owning descriptor
    immediately after the exclusive-create. A crash between create and record
    leaves an EMPTY lock file that reads as ``None`` and is therefore never judged
    stale on its own — it will only be cleared once it passes the stale threshold
    (created_at unreadable -> not stale -> a later real writer with the same target
    will keep waiting; an operator may clean it, but the protocol stays safe).
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(lock, flags)
    except FileExistsError:
        return False
    except OSError as exc:
        raise ManifestPersistenceError(
            f"failed to claim seal lock {lock}: {exc}",
        ) from exc
    try:
        record = json.dumps(
            {"owner": token, "created_at": time.time()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        _write_all(fd, record)
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def _release_lock(lock: Path, token: str) -> None:
    """Remove ``lock`` ONLY if it still belongs to ``token`` (ownership check).

    A stale-lock recovery may have removed this caller's claim and handed it to a
    newer writer; releasing unconditionally would unlink the NEW holder's lock and
    let a third writer race it. Compare the recorded owner before unlinking.
    """
    record = _read_lock_record(lock)
    if record is None or record.get("owner") != token:
        return  # not ours (anymore): never release another holder's claim
    with contextlib.suppress(OSError):
        lock.unlink(missing_ok=True)


def _write_atomic(target: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``target`` (E1-B.4 §20) via temp+rename.

    Temp file in target's directory → write → flush → fsync → os.replace, so the
    final path is never observed as a partially written JSON document. Directory
    fsync is attempted best-effort where supported. This is the SOLE publication
    primitive for the final manifest path — the final path only ever appears via an
    atomic rename of a complete temp file.
    """
    tmp = _tmp_path(target)
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        hook = _AFTER_TEMP_WRITE_HOOK
        if hook is not None:
            hook(target, tmp)
        os.replace(tmp, target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise ManifestPersistenceError(
            f"failed to atomically seal manifest to {target}: {exc}",
        ) from exc
    _fsync_directory(target.parent)


def _seal_absent(target: Path, data: bytes) -> None:
    """First-writer-wins, atomic-visible publication for an absent final path.

    Bounded contention loop (J §25):

    1. try to claim ``<name>.seal.lock`` (O_CREAT|O_EXCL, owner token + created_at);
    2. won the claim → double-check the final is STILL absent, then atomically
       publish via temp+fsync+rename and release our own lock;
    3. lost the claim → bounded-wait, recovering a STALE lock only when the final
       is still absent; re-try the claim so the recovered holder publishes;
    4. if the final ever appears (published by the winner) → compare bytes and
       resolve idempotent-success vs :class:`ManifestSealConflict`.

    On bounded timeout with no final and no acquirable lock, raise a typed
    :class:`ManifestPersistenceError` (never a bare TimeoutError).
    """
    lock = _lock_path(target)
    token = _owner_token()
    deadline = time.time() + SEAL_LOCK_WAIT_TIMEOUT_SECONDS
    while True:
        if target.exists():
            _compare_with_target(target, data)
            return
        if _acquire_lock(lock, token):
            try:
                # Double-check AFTER acquiring the lock (J §25): a writer may have
                # published while we waited for the claim — never overwrite it.
                if target.exists():
                    _compare_with_target(target, data)
                    return
                _write_atomic(target, data)
                return
            finally:
                _release_lock(lock, token)
        if time.time() >= deadline:
            raise ManifestPersistenceError(
                f"timed out after {SEAL_LOCK_WAIT_TIMEOUT_SECONDS}s waiting to "
                f"publish sealed manifest {target} (concurrent sealer never "
                "completed; lock not recoverable within the bounded window)"
            )
        # Bounded wait, recovering a stale lock only while the final is still absent.
        _recover_stale_lock(lock)
        time.sleep(SEAL_LOCK_POLL_INTERVAL_SECONDS)


def _write_all(fd: int, data: bytes) -> None:
    """Write ``data`` to ``fd`` in full (os.write may return a short count)."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory fsync (unsupported on some platforms, e.g. Windows)."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _compare_with_target(target: Path, data: bytes) -> None:
    """Resolve an observed existing target against ``data``.

    Reads the current bytes of ``target`` (which may now belong to a concurrent
    winner) and decides idempotent-success (identical bytes) vs
    :class:`ManifestSealConflict` (different bytes). Never overwrites.
    """
    try:
        existing = target.read_bytes()
    except OSError as exc:
        raise ManifestPersistenceError(
            f"failed to read existing sealed manifest {target}: {exc}",
        ) from exc
    if existing != data:
        raise ManifestSealConflict(
            f"target {target} already holds a DIFFERENT sealed manifest "
            "(byte mismatch); refusing to overwrite an immutable evaluation record",
        )


class ManifestSealer:
    """Atomic, immutable persistence boundary for sealed manifests.

    Deterministic and pure-filesystem (E1-B.4 §3, §20). Only a
    :class:`contracts.SealedManifest` may be passed; there is no seal(draft).
    """

    def seal(self, manifest: SealedManifest, path: str | Path) -> SealedManifest:
        """Seal ``manifest`` to ``path``; see :func:`seal_manifest`."""
        return seal_manifest(manifest, path)

    def verify(self, path: str | Path) -> SealedManifest:
        """Verify a persisted manifest; see :func:`verify_sealed_manifest`."""
        return verify_sealed_manifest(path)


def seal_manifest(manifest: SealedManifest, path: str | Path) -> SealedManifest:
    """Seal ``manifest`` to ``path`` immutably, atomically, cross-process.

    Publication is first-writer-wins AND atomically visible (E1-B.4 §20, §21):

    - ``path`` absent → the ``<name>.seal.lock`` claim file is acquired
      (``O_CREAT | O_EXCL``); the final path is DOUBLE-CHECKED still absent; the
      bytes are written to a same-directory temp file, flushed + fsynced, then
      atomically ``os.replace``d onto the final path — so the final manifest can
      never be observed as a torn/partial JSON document.
    - ``path`` exists with IDENTICAL bytes → idempotent success, whether it was
      present before this call or published by a concurrent winner.
    - ``path`` exists with DIFFERENT bytes (present before, or published by a
      concurrent sealer of another manifest) → :class:`ManifestSealConflict`; a
      different immutable record is never silently overwritten.
    - a crashed winner leaves a STALE lock which is recovered after a bounded
      stale threshold, but NEVER when a final manifest is already present (I/J).

    Returns the sealed ``manifest``.
    """
    if not isinstance(manifest, SealedManifest):
        raise ManifestSchemaError(
            f"seal_manifest requires a SealedManifest; got {type(manifest).__name__} — "
            "only a VerifiedDataset-derived manifest may be sealed",
        )
    validate_manifest(manifest)  # schema + W1 isolation + integrity digest first
    target = Path(path)
    data = to_json(manifest).encode("utf-8")
    if not target.parent.is_dir():
        raise ManifestPersistenceError(
            f"seal target directory does not exist: {target.parent}",
        )
    if target.exists():
        _compare_with_target(target, data)
        return manifest  # idempotent success (no lock needed)
    # Absent: first-writer-wins + atomic-visible publication under a claim lock.
    # _seal_absent resolves idempotent-success vs ManifestSealConflict internally
    # and raises a typed ManifestPersistenceError on a bounded-timeout with no final.
    _seal_absent(target, data)
    return manifest


def verify_sealed_manifest(path: str | Path) -> SealedManifest:
    """Verify a persisted manifest at ``path`` and return a trusted
    :class:`SealedManifest` (E1-B.4 §22). Validates schema invariants and the
    integrity digest before returning. Filesystem failures raise
    :class:`ManifestPersistenceError`; schema/integrity failures raise the
    manifest error taxonomy."""
    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise ManifestPersistenceError(
            f"failed to read sealed manifest {target}: {exc}",
        ) from exc
    return validate_sealed_json(raw, source=str(target))


def validate_sealed_json(raw: bytes, *, source: str) -> SealedManifest:
    """Deserialize + validate raw sealed-manifest JSON into a trusted
    :class:`SealedManifest` (schema + integrity). Shared by the filesystem
    verifier and any in-memory caller."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestSchemaError(f"{source} is not valid UTF-8 JSON: {exc}") from exc
    return _manifest_from_json(_require_object(data, source, "manifest"), source=source)


# ---------------------------------------------------------------------------
# JSON rebuild (bounded, typed)
# ---------------------------------------------------------------------------


def _manifest_from_json(obj: dict[str, Any], *, source: str) -> SealedManifest:
    """Rebuild + validate a SealedManifest from a parsed manifest object.

    All fields are read from the canonical JSON; nothing is fabricated except the
    RunIdentity watermark control source, which the schema intentionally does not
    persist (see :data:`_WATERMARK_NOT_SEALED`). Provider references (event
    provider_ref, source_alert.address_id) are copied verbatim as persisted.
    ``integrity.manifest_sha256`` is validated by re-deriving the digest over the
    rebuilt object (E1-B.4 §22).
    """
    scenario_obj = _require_object(obj.get("scenario"), source, "scenario")
    run_obj = _require_object(obj.get("run"), source, "run")
    scope_obj = _require_object(obj.get("scope"), source, "scope")
    entities_obj = _require_object(obj.get("entities"), source, "entities")
    source_alert_obj = _require_object(obj.get("source_alert"), source, "source_alert")
    oracle_obj = _require_object(obj.get("oracle"), source, "oracle")
    code_obj = _require_object(obj.get("code"), source, "code")
    integrity_obj = _require_object(obj.get("integrity"), source, "integrity")

    scope = {key: _string(scope_obj, key, source) for key in scope_obj}
    entities = {key: _string(entities_obj, key, source) for key in entities_obj}

    scenario = ScenarioSpec(
        id=_string(scenario_obj, "id", source),
        version=_string(scenario_obj, "version", source),
        rule_id=_string(scenario_obj, "rule_id", source),
        semantic_roles=_string_tuple(
            scenario_obj.get("semantic_roles"), source, "scenario.semantic_roles"
        ),
        failure_roles=_string_tuple(
            scenario_obj.get("failure_roles"), source, "scenario.failure_roles"
        ),
        control_role=_string(scenario_obj, "control_role", source),
    )
    run = RunIdentity(
        run_id=_string(run_obj, "run_id", source),
        run_tag=_string(run_obj, "run_tag", source),
        attack_source_ip=_string(entities_obj, "source_ip", source),
        user_name=_string(entities_obj, "user_name", source),
        host_name=_string(entities_obj, "host_name", source),
        watermark_source_ip=_WATERMARK_NOT_SEALED,
    )

    events = [
        _event_from_json(item, source)
        for item in _obj_list(obj.get("events"), source, "events")
    ]
    control_events = [
        _event_from_json(item, source)
        for item in _obj_list(obj.get("control_events"), source, "control_events")
    ]

    facts = tuple(
        _fact_pair(item, source)
        for item in _obj_list(oracle_obj.get("facts"), source, "oracle.facts")
    )
    oracle = ScenarioOracle(
        expected_verdict=_string(oracle_obj, "expected_verdict", source),
        facts=facts,
        required_evidence_roles=_string_tuple(
            oracle_obj.get("required_evidence_roles"),
            source,
            "oracle.required_evidence_roles",
        ),
    )

    integrity = {key: _string(integrity_obj, key, source) for key in integrity_obj}

    manifest = SealedManifest(
        schema_version=_string(obj, "schema_version", source),
        scenario=scenario,
        run=run,
        scope=scope,
        entities=entities,
        events=events,
        control_events=control_events,
        source_alert=_alert_from_json(source_alert_obj, source),
        oracle=oracle,
        code=CodeRevision(
            git_commit=_string(code_obj, "git_commit", source),
            dirty=_bool(code_obj.get("dirty"), source, "code.dirty"),
        ),
        integrity=integrity,
        sealed_at=_string(run_obj, "sealed_at", source),
        materialized_at=_string(run_obj, "materialized_at", source),
    )
    # Rebuild is faithful for every persisted field; scenario identity digests
    # were persisted on reserved scope keys and must be present to re-canonicalize.
    if not scope.get(SCOPE_KEY_SCENARIO_SOURCE_FILE_SHA256) or not scope.get(
        SCOPE_KEY_SCENARIO_SEMANTIC_SHA256
    ):
        raise ManifestSchemaError(
            f"{source} manifest.scope is missing the reserved scenario identity "
            "digests required for re-canonicalization",
        )
    validate_manifest(manifest)  # re-derives digest; raises ManifestIntegrityError on mismatch
    return manifest


def _require_object(value: object, source: str, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestSchemaError(f"{source}.{name} must be a JSON object")
    return value


def _obj_list(value: object, source: str, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ManifestSchemaError(f"{source}.{name} must be a list")
    result: list[dict[str, Any]] = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise ManifestSchemaError(f"{source}.{name}[{i}] must be an object")
        result.append(item)
    return result


def _string_tuple(value: object, source: str, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManifestSchemaError(f"{source}.{name} must be a list of strings")
    return tuple(value)


def _string(obj: dict[str, Any], key: str, source: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise ManifestSchemaError(f"{source}.{key} must be a string")
    return value


def _optional_string(obj: dict[str, Any], key: str, source: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestSchemaError(f"{source}.{key} must be a string or null")
    return value


def _bool(value: object, source: str, key: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestSchemaError(f"{source}.{key} must be a boolean")
    return value


def _fact_pair(item: dict[str, Any], source: str) -> tuple[str, str]:
    return _string(item, "id", source), _string(item, "description", source)


def _event_from_json(obj: dict[str, Any], source: str) -> ResolvedEvent:
    """Rebuild a :class:`ResolvedEvent` from a canonical event block. provider_ref
    index/document_id are copied verbatim (E1-B.4 §9) — never derived."""
    ref = _require_object(obj.get("provider_ref"), source, "event.provider_ref")
    return ResolvedEvent(
        logical_role=_string(obj, "role", source),
        provider=_string(ref, "provider", source),
        index=_string(ref, "index", source),
        document_id=_string(ref, "document_id", source),
        timestamp=_string(obj, "timestamp", source),
        event_category="",  # not part of the canonical manifest schema
        event_action=_string(obj, "event_action", source),
        event_outcome=_optional_string(obj, "event_outcome", source),
        source_ip=_string(obj, "source_ip", source),
        user_name=_string(obj, "user_name", source),
        host_name=_string(obj, "host_name", source),
        log_source_id=_optional_string(obj, "log_source_id", source),
        message_fingerprint=_optional_string(obj, "message_fingerprint", source),
    )


def _alert_from_json(obj: dict[str, Any], source: str) -> ResolvedAlert:
    """Rebuild a :class:`ResolvedAlert` from the canonical source_alert block.
    ``address_id`` is copied verbatim — it is the real HISIEM alert ES ``_id`` and
    is never derived from ``business_id`` or any id/hash (E1-B.4 §11)."""
    refs = [
        RelatedEventRef(
            index=_string(item, "index", source),
            document_id=_string(item, "document_id", source),
        )
        for item in _obj_list(
            obj.get("related_event_refs", []), source, "source_alert.related_event_refs"
        )
    ]
    return ResolvedAlert(
        provider=_string(obj, "provider", source),
        address_id=_string(obj, "address_id", source),
        business_id=_optional_string(obj, "business_id", source),
        rule_id=_string(obj, "rule_id", source),
        rule_name=_optional_string(obj, "rule_name", source),
        entity=_optional_string(obj, "entity", source),
        created_at=_optional_string(obj, "created_at", source) or "",
        timestamp=_optional_string(obj, "timestamp", source) or "",
        event_count=_int(obj.get("event_count", 0), source, "source_alert.event_count"),
        status=_optional_string(obj, "status", source) or "",
        related_event_refs=refs,
    )


def _int(value: object, source: str, key: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ManifestSchemaError(f"{source}.{key} must be an integer")
    return value
