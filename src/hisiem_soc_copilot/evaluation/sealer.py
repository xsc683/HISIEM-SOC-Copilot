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
written + fsynced same-directory temp file — it can never be observed as a torn/
partial JSON document (E1-B.4 §20).

Mutual exclusion uses a STABLE, kernel-backed advisory file lock on a guard file
``<name>.seal.lock`` that is NEVER deleted and holds no ownership record:

- ``seal_manifest`` opens the guard file (creating it if needed) and takes an
  OS-level EXCLUSIVE advisory lock (``fcntl.flock`` on POSIX, ``msvcrt.locking``
  on Windows) with a bounded, nonblocking retry loop.
- Inside the critical section the final path is DOUBLE-CHECKED (another writer may
  have published while we waited), then atomically published.
- On process crash the kernel closes the descriptor and RELEASES the advisory lock
  automatically — there is no stale lease, no stale unlink, no ABA, no
  PID/created_at recovery protocol. Crash recovery is provided by kernel lock
  lifetime, not by deleting stale ownership records.

The guard file is not a sealed artifact; it may persist. It is only a stable
lock anchor and is never removed.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

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

# Stable guard file suffix — the kernel-lock anchor. It is never deleted.
_LOCK_SUFFIX: Final = ".seal.lock"

# Temp-file name is owner-unique so a crash residue never collides with a later run.
_TMP_SUFFIX: Final = ".seal.tmp"

# Bounded wait/poll constants for the nonblocking advisory-lock retry loop. These
# are module-level (not Final) so tests may shrink the timeout/poll to keep
# adversarial cases fast; production uses the documented defaults.
SEAL_LOCK_WAIT_TIMEOUT_SECONDS: float = 10.0
SEAL_LOCK_POLL_INTERVAL_SECONDS: float = 0.05

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


# ---------------------------------------------------------------------------
# OS-backed advisory publication lock (stable guard file, never deleted)
# ---------------------------------------------------------------------------
# Windows: msvcrt.locking over a byte-range of an always-open guard file.
# POSIX:   fcntl.flock exclusive, nonblocking. The module never imports fcntl on
#          Windows (it does not exist there); the platform branch resolves lazily.
# ---------------------------------------------------------------------------


if sys.platform == "win32":
    import msvcrt  # noqa: PLC0415

    def _try_lock_nb(handle: Any) -> bool:
        """Best-effort nonblocking exclusive lock on one guard byte (Windows).

        msvcrt.locking requires the file position at the locked byte; we keep the
        guard file at >= 1 byte and always lock byte 0. Raises OSError on
        contention — that is the retry signal.
        """
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(handle: Any) -> None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    try:
        import fcntl  # noqa: PLC0415

        def _try_lock_nb(handle: Any) -> bool:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                return False

        def _unlock(handle: Any) -> None:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    except ImportError:  # pragma: no cover - unsupported platform guard
        def _try_lock_nb(handle: Any) -> bool:
            raise ManifestPersistenceError(
                "cross-process publication lock unsupported on platform "
                f"{sys.platform!r} (no fcntl / msvcrt advisory locking available)"
            )

        def _unlock(handle: Any) -> None:
            return


def _open_guard(target: Path) -> Any:
    """Open (creating if needed) the stable guard file for advisory locking.

    Returns a binary handle positioned for locking. The guard file persists across
    runs and is never deleted. Filesystem failure is reported as a typed
    :class:`ManifestPersistenceError` (never a bare OSError).
    """
    lock = _lock_path(target)
    try:
        handle = open(lock, "a+b")  # noqa: SIM115 - the handle lives until release
    except OSError as exc:
        raise ManifestPersistenceError(
            f"failed to open seal lock guard {lock}: {exc}",
        ) from exc
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
    except OSError as exc:
        handle.close()
        raise ManifestPersistenceError(
            f"failed to initialize seal lock guard {lock}: {exc}",
        ) from exc
    return handle


def _acquire_lock_guarded(target: Path) -> Any:
    """Acquire the kernel-backed exclusive advisory lock with a bounded retry.

    Returns an OPEN file handle for the guard, or ``None`` when the bounded wait
    expired without acquiring the lock. The caller decides how to treat the two
    conditions. Before each retry the target's final path is re-checked so a
    contender waiting behind a holder that just PUBLISHED can resolve idempotent/
    conflict promptly instead of exhausting its deadline. On a bounded timeout with
    the lock still held by another live process, raises a typed
    :class:`ManifestPersistenceError` (never a bare TimeoutError/OSError).
    """
    handle = _open_guard(target)
    deadline = time.monotonic() + SEAL_LOCK_WAIT_TIMEOUT_SECONDS
    while True:
        if _try_lock_nb(handle):
            return handle
        if target.exists():  # the holder published while we waited — resolve outside
            handle.close()
            return None
        if time.monotonic() >= deadline:
            handle.close()
            raise ManifestPersistenceError(
                f"timed out after {SEAL_LOCK_WAIT_TIMEOUT_SECONDS}s waiting for the "
                f"cross-process publication lock on {_lock_path(target)} "
                "(another sealer holds it); refusing to proceed"
            )
        time.sleep(SEAL_LOCK_POLL_INTERVAL_SECONDS)


def _release_lock(handle: Any) -> None:
    """Release the advisory lock and close the guard handle."""
    _unlock(handle)
    with contextlib.suppress(OSError):
        handle.close()


def _owner_temp_path(target: Path) -> Path:
    """An owner-unique temp path for one publication (pid + random suffix)."""
    return target.with_name(
        f"{target.name}{_TMP_SUFFIX}.{os.getpid()}.{uuid4().hex}"
    )


def _write_atomic(target: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``target`` (E1-B.4 §20) via temp+rename.

    Owner-unique temp file in target's directory → write → flush → fsync →
    ``os.replace``, so the final path is never observed as a partially written JSON
    document. Directory fsync is attempted best-effort where supported. This is the
    SOLE publication primitive for the final manifest path.
    """
    tmp = _owner_temp_path(target)
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
            tmp.unlink(missing_ok=True)  # best-effort cleanup of our own temp
        raise ManifestPersistenceError(
            f"failed to atomically seal manifest to {target}: {exc}",
        ) from exc
    _fsync_directory(target.parent)


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

    - fast-path: ``path`` already exists with IDENTICAL bytes → idempotent success;
      with DIFFERENT bytes → :class:`ManifestSealConflict`.
    - ``path`` absent → acquire the kernel-backed advisory publication lock on the
      stable guard file ``<name>.seal.lock``; DOUBLE-CHECK the final is still
      absent; write a same-directory owner-unique temp file, flush + fsync, then
      atomically ``os.replace`` onto the final path. The guard file is never
      deleted; a concurrent loser waits on the kernel lock and then re-checks the
      final (compare, never overwrite a different manifest).
    - process crash → the kernel releases the advisory lock when the descriptor
      closes; no stale-lock cleanup is required.

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
    # Absent: serialize publication under the kernel-backed advisory lock.
    while True:
        handle = _acquire_lock_guarded(target)
        if handle is None:
            # The final appeared while we waited on the lock — resolve it exactly
            # as the fast path does (idempotent success vs conflict), never overwrite.
            if target.exists():
                _compare_with_target(target, data)
            return manifest
        try:
            # DOUBLE-CHECK after acquiring the lock: a writer may have published
            # while we waited — never overwrite a different immutable manifest.
            if target.exists():
                _compare_with_target(target, data)
                return manifest
            _write_atomic(target, data)
            return manifest
        finally:
            _release_lock(handle)


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
