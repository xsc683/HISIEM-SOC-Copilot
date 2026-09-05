"""Offline manifest-sealer unit tests (E1-B.4 §2, §3, §5-§16, §21, §23).

No network / provider / model I/O: drives ManifestBuilder + sealer over a
``VerifiedDataset`` built by the shared fixture helpers. Covers the unverified
draft rejection, byte-determinism, tamper detection, no-overwrite immutability,
W1 evidence isolation, NaN/Infinity rejection, and the oracle-free launch
projection.
"""

from __future__ import annotations

import json
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


def _verified(**overrides) -> VerifiedDataset:
    return make_verified(**overrides)


def _build(verified: VerifiedDataset) -> SealedManifest:
    return build_manifest(
        verified,
        scenario_oracle(verified.scenario),
        _CODE,
        scenario_source_file_sha256=_SOURCE_SHA,
        scenario_semantic_sha256=_SEMANTIC_SHA,
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
    assert oracle.required_evidence_roles == ("F1", "F2", "F3", "F4", "F5", "S1")
    assert "W1" not in [e.logical_role for e in verified.events]  # isolated in control
    assert verified.control_events[0].logical_role == "W1"


def test_oracle_control_role_in_evidence_is_rejected() -> None:

    verified = _verified()
    leaking = replace(
        verified.scenario,
        required_evidence_roles=("F1", "F2", "F3", "F4", "F5", "S1", "W1"),
    )
    verified = replace(verified, scenario=leaking)
    with pytest.raises(OracleIsolationViolation):
        scenario_oracle(leaking)
    with pytest.raises(OracleIsolationViolation):
        _build(verified)


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
