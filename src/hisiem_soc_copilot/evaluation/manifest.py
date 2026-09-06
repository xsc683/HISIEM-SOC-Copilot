"""Deterministic GP-01 sealed-manifest builder + canonicalization (E1-B.4 §5-§16).

The sealer boundary accepts ONLY a :class:`contracts.VerifiedDataset`; sealing an
unverified :class:`contracts.MaterializationDraft` is structurally impossible
because every entry point here requires the verified types. Pure and
deterministic — no provider, model, LangGraph, or DB I/O.

Scenario identity (E1-B.4 §6) needs ``source_file_sha256`` + ``semantic_sha256``
digests, but the frozen :class:`contracts.ScenarioSpec` has no hash slot, so the
builder accepts them as explicit parameters and records them on the sealed
manifest under reserved ``scope`` keys. The canonical payload re-emits them on the
``scenario`` block, so ``manifest.json`` exposes the doc-mandated
``scenario.source_file_sha256`` / ``scenario.semantic_sha256`` fields and the
digests survive a JSON round trip for verification.

Canonical payload top-level order (E1-B.4 §5, §15)::

    schema_version, scenario, run, scope, entities, events, control_events,
    source_alert, oracle, code, integrity

``manifest_sha256 = SHA256(canonical_json(manifest with integrity.manifest_sha256
OMITTED))`` — the digest never includes itself, and ``integrity.canonicalization``
IS part of the hashed payload (E1-B.4 §16).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Final

from .contracts import (
    CANONICALIZATION_ID,
    GP01_SEMANTIC_ROLES,
    MANIFEST_SCHEMA_VERSION,
    CodeRevision,
    ResolvedAlert,
    ResolvedEvent,
    ScenarioOracle,
    ScenarioSpec,
    SealedManifest,
    VerifiedDataset,
    canonical_json,
    sha256_hex,
)
from .errors import (
    ManifestCanonicalizationError,
    ManifestIntegrityError,
    ManifestNotVerifiedError,
    ManifestSchemaError,
    OracleIsolationViolation,
)

# Reserved manifest.scope keys carrying scenario identity digests. They keep the
# two hashes on the typed SealedManifest (ScenarioSpec has no slot) so
# canonicalize/hash/verify stay a pure function of the sealed object.
SCOPE_KEY_SCENARIO_SOURCE_FILE_SHA256: Final = "scenario_source_file_sha256"
SCOPE_KEY_SCENARIO_SEMANTIC_SHA256: Final = "scenario_semantic_sha256"

# Canonical top-level key order (E1-B.4 §5). sort_keys still orders object keys,
# but top-level key order and array order are established by this builder.
_CANONICAL_TOP_KEYS: Final[tuple[str, ...]] = (
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
)

__all__ = [
    "ManifestBuilder",
    "SCOPE_KEY_SCENARIO_SEMANTIC_SHA256",
    "SCOPE_KEY_SCENARIO_SOURCE_FILE_SHA256",
    "build_manifest",
    "canonicalize_manifest",
    "compute_manifest_sha256",
    "manifest_payload_bytes",
    "to_json",
    "validate_manifest",
]


# ---------------------------------------------------------------------------
# Canonical JSON + timestamp helpers
# ---------------------------------------------------------------------------


def _canonical_json(payload: dict[str, Any] | list[Any]) -> str:
    """project canonical JSON, mapping NaN/TypeError onto the manifest taxonomy."""
    try:
        return canonical_json(payload)
    except (ValueError, TypeError) as exc:  # allow_nan=False rejects NaN/Infinity
        raise ManifestCanonicalizationError(
            f"payload cannot be canonicalized: {exc}",
        ) from exc


def _rfc3339_utc(value: str, where: str) -> str:
    """Validate + canonicalize ``value`` to a deterministic RFC3339 UTC string.

    Naive timestamps are rejected (E1-B.4 §15 requires canonical RFC3339 UTC).
    Any UTC offset is normalized to ``Z`` so the same instant always hashes the
    same way regardless of how the materializer rendered it.
    """
    if not isinstance(value, str) or not value:
        raise ManifestSchemaError(f"{where} must be a non-empty RFC3339 UTC string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ManifestSchemaError(f"{where} is not a valid RFC3339 instant: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ManifestSchemaError(f"{where} must be timezone-aware (RFC3339 UTC): {value!r}")
    rendered = parsed.astimezone(UTC).isoformat()
    return rendered.replace("+00:00", "Z")


def _now_utc_rfc3339() -> str:
    """The current instant as canonical RFC3339 UTC — the default ``sealed_at``."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _verify_finite(value: Any, where: str) -> None:
    """Reject NaN/Infinity at any depth of the payload (E1-B.4 §15)."""
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ManifestCanonicalizationError(
                f"non-finite number {value!r} cannot be canonicalized at {where}",
            )
    elif isinstance(value, dict):
        for key, item in value.items():
            _verify_finite(item, f"{where}.{key}")
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _verify_finite(item, f"{where}[{i}]")


# ---------------------------------------------------------------------------
# ManifestBuilder
# ---------------------------------------------------------------------------


class ManifestBuilder:
    """Assemble an immutable :class:`contracts.SealedManifest` from a verified
    dataset. Deterministic for the same explicit inputs (E1-B.4 §3)."""

    def build(
        self,
        verified: VerifiedDataset,
        oracle: ScenarioOracle,
        code: CodeRevision,
        *,
        scenario_source_file_sha256: str,
        scenario_semantic_sha256: str,
        sealed_at: str | None = None,
    ) -> SealedManifest:
        """Build the sealed manifest for ``verified``.

        ``oracle`` must be derived from ``verified.scenario`` (see
        :func:`.oracle.scenario_oracle`); a control (W1) role can never satisfy
        evidence. Semantic events are F1..F5,S1 in committed order; W1 is isolated
        into ``control_events``. ``integrity.manifest_sha256`` digests the
        canonical payload with itself omitted (E1-B.4 §16).

        ``sealed_at`` is the SEALING instant (when this builder runs) and is
        deliberately decoupled from ``verified.materialized_at`` (the instant the
        DatasetVerifier produced the VerifiedDataset). It defaults to now-UTC;
        callers may inject a fixed value for deterministic byte-identical output
        given identical explicit inputs (E1-B.4 correctness round)."""
        _validate_inputs(
            verified,
            oracle,
            code,
            scenario_source_file_sha256,
            scenario_semantic_sha256,
        )
        manifest = SealedManifest(
            schema_version=MANIFEST_SCHEMA_VERSION,
            scenario=verified.scenario,
            run=verified.run,
            scope=self._scope(verified, scenario_source_file_sha256, scenario_semantic_sha256),
            entities=self._entities(verified.run),
            events=self._semantic_events(verified),
            control_events=self._control_events(verified),
            source_alert=verified.source_alert,
            oracle=oracle,
            code=code,
            integrity={"canonicalization": CANONICALIZATION_ID},
            sealed_at=sealed_at or _now_utc_rfc3339(),
            materialized_at=verified.materialized_at,
        )
        digest = compute_manifest_sha256(manifest)
        return replace(
            manifest,
            integrity={"canonicalization": CANONICALIZATION_ID, "manifest_sha256": digest},
        )

    @staticmethod
    def _scope(
        verified: VerifiedDataset, source_sha256: str, semantic_sha256: str
    ) -> dict[str, str]:
        return {
            "provider": "hisiem",
            "tenant_id": verified.tenant_id,
            SCOPE_KEY_SCENARIO_SOURCE_FILE_SHA256: source_sha256,
            SCOPE_KEY_SCENARIO_SEMANTIC_SHA256: semantic_sha256,
        }

    @staticmethod
    def _entities(run: Any) -> dict[str, str]:
        """Normalized attack entity block (E1-B.4 §8)."""
        return {
            "source_ip": run.attack_source_ip,
            "user_name": run.user_name,
            "host_name": run.host_name,
        }

    @staticmethod
    def _semantic_events(verified: VerifiedDataset) -> list[ResolvedEvent]:
        """Ground-truth semantic events in committed role order F1..F5,S1."""
        events = list(verified.events)
        roles = [event.logical_role for event in events]
        if roles != list(GP01_SEMANTIC_ROLES):
            raise ManifestSchemaError(
                f"verified semantic events not in committed order "
                f"{list(GP01_SEMANTIC_ROLES)}; got {roles}",
            )
        return events

    @staticmethod
    def _control_events(verified: VerifiedDataset) -> list[ResolvedEvent]:
        """W1 watermark control events, separate from semantic ground truth."""
        controls = list(verified.control_events)
        for event in controls:
            if event.logical_role == verified.scenario.control_role:
                continue
            raise ManifestSchemaError(
                f"control_events contains {event.logical_role!r}; only "
                f"{verified.scenario.control_role!r} may be a control event",
            )
        if any(event.logical_role in GP01_SEMANTIC_ROLES for event in controls):
            raise OracleIsolationViolation(
                "a semantic GROUND_TRUTH role appears among watermark control events",
            )
        return sorted(controls, key=lambda event: event.logical_role)


def _validate_inputs(
    verified: VerifiedDataset,
    oracle: ScenarioOracle,
    code: CodeRevision,
    scenario_source_file_sha256: str,
    scenario_semantic_sha256: str,
    sealed_at: str | None = None,
) -> None:
    if not isinstance(verified, VerifiedDataset):
        raise ManifestNotVerifiedError(
            f"ManifestBuilder requires a VerifiedDataset; got {type(verified).__name__} — "
            "an unverified MaterializationDraft can never be sealed",
        )
    if not isinstance(oracle, ScenarioOracle):
        raise ManifestSchemaError(f"oracle must be a ScenarioOracle; got {type(oracle).__name__}")
    if not isinstance(code, CodeRevision):
        raise ManifestSchemaError(f"code must be a CodeRevision; got {type(code).__name__}")
    if not isinstance(verified.scenario, ScenarioSpec):
        raise ManifestSchemaError("verified.scenario must be a ScenarioSpec")
    for name, digest in (
        ("source_file_sha256", scenario_source_file_sha256),
        ("semantic_sha256", scenario_semantic_sha256),
    ):
        invalid_digest = (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        )
        if invalid_digest:
            raise ManifestSchemaError(f"scenario {name} must be a 64-char lowercase hex digest")
    if verified.scenario.control_role in oracle.required_evidence_roles:
        raise OracleIsolationViolation(
            f"scenario {verified.scenario.id} control role "
            f"{verified.scenario.control_role!r} appears in oracle.required_evidence_roles",
        )
    if sealed_at is not None:
        _rfc3339_utc(sealed_at, "sealed_at")  # must be a canonical RFC3339 UTC instant


def build_manifest(
    verified: VerifiedDataset,
    oracle: ScenarioOracle,
    code: CodeRevision,
    *,
    scenario_source_file_sha256: str,
    scenario_semantic_sha256: str,
    sealed_at: str | None = None,
) -> SealedManifest:
    """Module-level convenience for E1-B.4 §22 ``build_manifest``."""
    return ManifestBuilder().build(
        verified,
        oracle,
        code,
        scenario_source_file_sha256=scenario_source_file_sha256,
        scenario_semantic_sha256=scenario_semantic_sha256,
        sealed_at=sealed_at,
    )


# ---------------------------------------------------------------------------
# Canonical payload model
# ---------------------------------------------------------------------------


def _scenario_digests(manifest: SealedManifest) -> tuple[str, str]:
    """Read the reserved scenario-identity digests off ``manifest.scope``."""
    source = manifest.scope.get(SCOPE_KEY_SCENARIO_SOURCE_FILE_SHA256, "")
    semantic = manifest.scope.get(SCOPE_KEY_SCENARIO_SEMANTIC_SHA256, "")
    if not source or not semantic:
        raise ManifestSchemaError(
            "manifest.scope is missing reserved scenario identity digests "
            f"({SCOPE_KEY_SCENARIO_SOURCE_FILE_SHA256!r}, {SCOPE_KEY_SCENARIO_SEMANTIC_SHA256!r})",
        )
    return source, semantic


def _scenario_payload(manifest: SealedManifest) -> dict[str, Any]:
    scenario = manifest.scenario
    source_sha256, semantic_sha256 = _scenario_digests(manifest)
    return {
        "id": scenario.id,
        "version": scenario.version,
        "rule_id": scenario.rule_id,
        "semantic_roles": list(scenario.semantic_roles),
        "failure_roles": list(scenario.failure_roles),
        "control_role": scenario.control_role,
        "source_file_sha256": source_sha256,
        "semantic_sha256": semantic_sha256,
    }


def _run_payload(manifest: SealedManifest) -> dict[str, str]:
    return {
        "run_id": manifest.run.run_id,
        "run_tag": manifest.run.run_tag,
        "materialized_at": _rfc3339_utc(manifest.materialized_at, "run.materialized_at"),
        "sealed_at": _rfc3339_utc(manifest.sealed_at, "run.sealed_at"),
    }


def _event_payload(event: ResolvedEvent, *, classification: str, excluded: bool) -> dict[str, Any]:
    """Bounded canonical event entry (E1-B.4 §9, §10).

    ``provider_ref.index``/``document_id`` are the EXACT real HISIEM ``_index`` /
    ``_id`` copied from the resolved event — never derived from logical roles.
    """
    payload: dict[str, Any] = {
        "role": event.logical_role,
        "classification": classification,
        "provider_ref": {
            "provider": event.provider,
            "index": event.index,
            "document_id": event.document_id,
        },
        "timestamp": _rfc3339_utc(event.timestamp, "event.timestamp"),
        "event_action": event.event_action,
        "source_ip": event.source_ip,
        "user_name": event.user_name,
        "host_name": event.host_name,
    }
    if event.event_outcome is not None:
        payload["event_outcome"] = event.event_outcome
    if event.log_source_id is not None:
        payload["log_source_id"] = event.log_source_id
    if event.message_fingerprint is not None:
        payload["message_fingerprint"] = event.message_fingerprint
    if classification == "WATERMARK_CONTROL":
        payload["excluded_from_ground_truth"] = excluded
    return payload


def _source_alert_payload(alert: ResolvedAlert) -> dict[str, Any]:
    """Canonical source-alert binding (E1-B.4 §11). ``address_id`` is the REAL
    provider address (HISIEM alert ES ``_id``) copied verbatim — never derived
    from ``business_id`` or any id/hash."""
    payload: dict[str, Any] = {
        "provider": alert.provider,
        "resource_type": "alert",
        "address_id": alert.address_id,
        "rule_id": alert.rule_id,
        "event_count": alert.event_count,
        "related_event_refs": [
            {"index": ref.index, "document_id": ref.document_id}
            for ref in sorted(alert.related_event_refs, key=lambda r: (r.index, r.document_id))
        ],
    }
    if alert.business_id is not None:
        payload["business_id"] = alert.business_id
    if alert.rule_name is not None:
        payload["rule_name"] = alert.rule_name
    if alert.entity is not None:
        payload["entity"] = alert.entity
    if alert.created_at:
        payload["created_at"] = _rfc3339_utc(alert.created_at, "source_alert.created_at")
    if alert.timestamp:
        payload["timestamp"] = _rfc3339_utc(alert.timestamp, "source_alert.timestamp")
    if alert.status:
        payload["status"] = alert.status
    return payload


def _oracle_payload(manifest: SealedManifest) -> dict[str, Any]:
    """Private oracle block (E1-B.4 §12): facts + evidence roles in declared
    ScenarioSpec order, never fixed Finding wording."""
    return {
        "expected_verdict": manifest.oracle.expected_verdict,
        "facts": [
            {"id": fact_id, "description": description}
            for fact_id, description in manifest.oracle.facts  # declared order
        ],
        "required_evidence_roles": list(manifest.oracle.required_evidence_roles),
    }


def canonicalize_manifest(manifest: SealedManifest) -> dict[str, Any]:
    """Return the canonical payload dict for ``manifest``.

    Deterministic: events follow committed role order F1..F5,S1, control_events
    are ordered by role, oracle.facts / required_evidence_roles keep declared
    ScenarioSpec order, and timestamps are canonical RFC3339 UTC. Numbers are
    finite (NaN/Infinity rejected). ``integrity.manifest_sha256`` is OMITTED — the
    caller hashes this payload to obtain the digest (E1-B.4 §16).
    """
    if not isinstance(manifest, SealedManifest):
        raise ManifestSchemaError(
            f"canonicalize_manifest requires a SealedManifest; got {type(manifest).__name__}",
        )
    if manifest.schema_version != MANIFEST_SCHEMA_VERSION:
        raise ManifestSchemaError(
            f"unsupported schema_version {manifest.schema_version!r}; "
            f"expected {MANIFEST_SCHEMA_VERSION}",
        )
    events = _ordered_semantic_events(manifest)
    controls = sorted(manifest.control_events, key=lambda event: event.logical_role)
    _check_watermark_isolation(manifest, events, controls)
    control_in_evidence = (
        manifest.oracle.required_evidence_roles
        and manifest.scenario.control_role in manifest.oracle.required_evidence_roles
    )
    if control_in_evidence:
        raise OracleIsolationViolation(
            f"oracle requires control role {manifest.scenario.control_role!r} as evidence",
        )

    payload: dict[str, Any] = {
        "schema_version": manifest.schema_version,
        "scenario": _scenario_payload(manifest),
        "run": _run_payload(manifest),
        "scope": dict(manifest.scope),
        "entities": dict(manifest.entities),
        "events": [
            _event_payload(event, classification="GROUND_TRUTH", excluded=False)
            for event in events
        ],
        "control_events": [
            _event_payload(event, classification="WATERMARK_CONTROL", excluded=True)
            for event in controls
        ],
        "source_alert": _source_alert_payload(manifest.source_alert),
        "oracle": _oracle_payload(manifest),
        "code": {"git_commit": manifest.code.git_commit, "dirty": manifest.code.dirty},
        "integrity": {"canonicalization": CANONICALIZATION_ID},
    }
    _verify_finite(payload, "manifest")
    return {key: payload[key] for key in _CANONICAL_TOP_KEYS}


def _ordered_semantic_events(manifest: SealedManifest) -> list[ResolvedEvent]:
    """Validate + order semantic events by the committed role order."""
    by_role = {event.logical_role: event for event in manifest.events}
    if set(by_role) != set(GP01_SEMANTIC_ROLES):
        raise ManifestSchemaError(
            f"semantic event roles {sorted(by_role)} do not equal committed "
            f"{sorted(GP01_SEMANTIC_ROLES)}",
        )
    return [by_role[role] for role in GP01_SEMANTIC_ROLES]


def _check_watermark_isolation(
    manifest: SealedManifest,
    events: list[ResolvedEvent],
    controls: list[ResolvedEvent],
) -> None:
    """Enforce W1 isolation (E1-B.4 §10): control roles never count as evidence."""
    semantic_roles = {event.logical_role for event in events}
    control_roles = {event.logical_role for event in controls}
    overlap = semantic_roles & control_roles
    if overlap:
        raise OracleIsolationViolation(
            f"roles {sorted(overlap)} appear in both semantic and control events",
        )
    if control_roles and control_roles != {manifest.scenario.control_role}:
        raise ManifestSchemaError(
            f"control_events roles {sorted(control_roles)} must be exactly "
            f"{manifest.scenario.control_role!r}",
        )


def _canonical_payload_str(manifest: SealedManifest) -> str:
    """Canonical JSON of :func:`canonicalize_manifest` (digest-omitted payload)."""
    return _canonical_json(canonicalize_manifest(manifest))


def manifest_payload_bytes(manifest: SealedManifest) -> bytes:
    """UTF-8 bytes of the canonical payload with ``manifest_sha256`` omitted.

    This is the exact byte string hashed by :func:`compute_manifest_sha256`.
    """
    return _canonical_payload_str(manifest).encode("utf-8")


def compute_manifest_sha256(manifest: SealedManifest) -> str:
    """SHA-256 hex of the canonical manifest with ``integrity.manifest_sha256``
    omitted (E1-B.4 §16). ``integrity.canonicalization`` is inside the digest."""
    return sha256_hex(_canonical_payload_str(manifest))


def to_json(manifest: SealedManifest) -> str:
    """Final sealed JSON string with ``integrity.manifest_sha256`` filled.

    The digest is recomputed over the digest-omitted payload; the returned text is
    the exact artifact bytes the sealer persists.
    """
    payload = canonicalize_manifest(manifest)
    payload["integrity"] = {
        "canonicalization": CANONICALIZATION_ID,
        "manifest_sha256": compute_manifest_sha256(manifest),
    }
    return _canonical_json(payload)


# ---------------------------------------------------------------------------
# In-memory verification
# ---------------------------------------------------------------------------


def validate_manifest(manifest: SealedManifest) -> None:
    """Re-derive the digest; raise on schema/type or integrity violations.

    Raises :class:`ManifestSchemaError` for shape violations,
    :class:`OracleIsolationViolation` for W1 leaking into evidence, and
    :class:`ManifestIntegrityError` when the recorded ``integrity.manifest_sha256``
    differs from the recomputed digest.
    """
    canonicalize_manifest(manifest)  # full schema + isolation validation
    recorded = manifest.integrity.get("manifest_sha256", "")
    recomputed = compute_manifest_sha256(manifest)
    if recorded != recomputed:
        raise ManifestIntegrityError(
            "manifest_sha256 mismatch: recomputed "
            f"{recomputed} != recorded {recorded or '<absent>'}",
        )
    if manifest.integrity.get("canonicalization") != CANONICALIZATION_ID:
        raise ManifestSchemaError(
            f"integrity.canonicalization {manifest.integrity.get('canonicalization')!r} "
            f"!= {CANONICALIZATION_ID}",
        )
