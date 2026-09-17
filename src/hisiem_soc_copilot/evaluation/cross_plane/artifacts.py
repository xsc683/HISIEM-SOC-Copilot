"""XP-01 gate-results artifact: bounded, versioned, secret-scanned, pure.

This module is **pure**: it builds and validates the artifact payload and scans it
for secret markers. It deliberately performs no IO, so the atomic write stays where
the repository already put it (``evaluation_harness.record.atomic_write_json``) and
is reached through the sanctioned harness adapter rather than re-implemented here.

Artifact discipline (E1 §30), all of it inherited from the existing Evaluation
Plane rather than invented:

* explicit schema version, and an unknown version is **rejected** on read;
* field allowlist — the reader never passes an unknown field through;
* bounded strings and bounded collections, as rejection thresholds, never as
  truncation budgets;
* deterministic serialization for unchanged inputs (no clock, no randomness);
* an explicit secret scan before the payload may be written.

What the artifact deliberately does NOT contain: raw prompts, raw completions,
full ToolResult, full Alert/Event bodies, Authorization/Bearer material, passwords,
credential-bearing DSNs, MCP secrets, session tokens, embedding vectors, chain of
thought, or a sealed natural-language oracle. Expected facts are machine tokens.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import (
    GATE_RESULTS_SCHEMA_VERSION,
    SCENARIO_RESULT_SCHEMA_VERSION,
    XP_PACK_ID,
    XP_PACK_VERSION,
    BoundsViolation,
    CrossPlaneScenarioResult,
    ScenarioSchemaError,
    canonical_json,
    identity_hash,
)
from .gates import normalize_marker_hits

#: The artifact file name inside a scenario run directory.
GATE_RESULTS_FILENAME = "gate-results.json"

#: Hard ceiling on the serialized artifact. A larger payload is a contract error —
#: failing closed beats silently dropping the evidence that made it large.
MAX_ARTIFACT_BYTES = 256_000

#: Surfaces an XP-01 secret scan reports on. Logical names, never file paths.
SURFACE_GATE_PAYLOAD = "gate-results-payload"
SURFACE_MANIFEST = "xp-pack-manifest"
SURFACE_TELEMETRY_SNAPSHOT = "telemetry-snapshot"
SURFACE_WORKSPACE_SNAPSHOT = "workspace-snapshot"
SURFACE_EVIDENCE_SUMMARY = "evidence-summary"

#: Marker spellings that must never appear in an XP-01 artifact (E1 §28).
#:
#: A deliberate SUPERSET of the GP-01 suites' marker tuples (which cover api_key /
#: Authorization / Bearer / password / postgresql:// / sk-). XP-01 publishes more
#: surfaces — telemetry snapshots, workspace snapshots, evidence summaries — so its
#: scan covers the credential shapes those can carry. Only the *spelling* is stored
#: in a scan result; a matched value is never copied into an artifact.
SECRET_MARKERS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "CMD_API_KEY",
    "Authorization",
    "Bearer",
    "bearer ",
    "password",
    "passwd",
    "secret",
    "client_secret",
    "postgresql://",
    "postgres://",
    "sk-",
    "-----BEGIN",
    "private_key",
    "session_token",
    "access_token",
    "refresh_token",
)

#: The EXACT allowlisted top-level keys of a persisted gate-results payload.
_PAYLOAD_KEYS: tuple[str, ...] = (
    "schema_version",
    "pack_id",
    "pack_version",
    "scenario_id",
    "scenario_version",
    "gate_family",
    "execution_profile",
    "gate_results",
    "overall_gate",
    "gate_failures",
    "artifact_identity",
)


class ArtifactBoundsViolation(BoundsViolation):
    """A gate-results artifact exceeded a declared bound."""


class ArtifactSecretViolation(ValueError):
    """A gate-results artifact carries a secret marker and must not be written."""


def build_gate_results_payload(result: CrossPlaneScenarioResult) -> dict[str, Any]:
    """Build the bounded, allowlisted artifact payload for one scenario result.

    Deterministic: the same ``result`` always yields byte-identical canonical JSON,
    because nothing here reads a clock, a random source, or ambient state.
    """
    body = result.to_payload()
    payload: dict[str, Any] = {key: body[key] for key in _PAYLOAD_KEYS if key in body}
    payload["schema_version"] = GATE_RESULTS_SCHEMA_VERSION
    payload["pack_id"] = XP_PACK_ID
    payload["pack_version"] = XP_PACK_VERSION
    payload["artifact_identity"] = artifact_identity(payload)
    _enforce_bounds(payload)
    return payload


def artifact_identity(payload: Mapping[str, Any]) -> str:
    """Deterministic identity of an artifact payload, excluding the identity field."""
    content = {key: value for key, value in payload.items() if key != "artifact_identity"}
    return identity_hash(content)


def _enforce_bounds(payload: Mapping[str, Any]) -> None:
    encoded = canonical_json(payload)
    if len(encoded.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise ArtifactBoundsViolation(
            f"gate-results artifact exceeds {MAX_ARTIFACT_BYTES} bytes"
        )


def gate_results_from_payload(payload: Mapping[str, Any]) -> CrossPlaneScenarioResult:
    """Validate and read a persisted gate-results payload.

    Unknown schema versions are rejected, unknown fields are dropped by the
    allowlist rather than passed through, and the recorded artifact identity is
    re-derived and compared so a tampered payload cannot be read as valid.
    """
    schema = payload.get("schema_version")
    if schema != GATE_RESULTS_SCHEMA_VERSION:
        raise ScenarioSchemaError(
            f"unsupported gate-results schema {schema!r}; this reader only accepts "
            f"{GATE_RESULTS_SCHEMA_VERSION}"
        )
    safe = {key: payload[key] for key in _PAYLOAD_KEYS if key in payload}

    recorded_identity = safe.get("artifact_identity")
    if recorded_identity is not None:
        derived = artifact_identity(safe)
        if str(recorded_identity) != derived:
            raise ArtifactBoundsViolation(
                "gate-results artifact identity does not match its content"
            )

    # The artifact and the scenario result are two versioned documents: the artifact's
    # `schema_version` describes the ARTIFACT, and the embedded scenario result has its
    # own schema. Supply the latter explicitly rather than pretending the artifact
    # version identifies it.
    result_payload = dict(safe)
    result_payload["schema_version"] = SCENARIO_RESULT_SCHEMA_VERSION
    result = CrossPlaneScenarioResult.from_payload(result_payload)
    if result.pack_id != XP_PACK_ID:
        raise ArtifactBoundsViolation(
            f"gate-results artifact pack_id must be {XP_PACK_ID!r}, got "
            f"{result.pack_id!r}"
        )
    return result


def matches_of_secret_markers(payload: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """Return bounded ``(surface, marker)`` hits for the artifact payload.

    Only the marker spelling is returned. The matched value is never read out of the
    payload and never stored, so running the scan cannot itself leak the secret.
    """
    text = canonical_json(payload)
    hits = [
        (SURFACE_GATE_PAYLOAD, marker) for marker in SECRET_MARKERS if marker in text
    ]
    return normalize_marker_hits(hits)


def secret_scan_pass(payload: Mapping[str, Any]) -> bool:
    """True when the artifact payload carries no known secret marker."""
    return not matches_of_secret_markers(payload)


def assert_artifact_safe(payload: Mapping[str, Any]) -> None:
    """Raise :class:`ArtifactSecretViolation` when the payload is not safe to write.

    Called by the writer BEFORE any bytes reach the disk, so an unsafe payload never
    produces an artifact at all (rather than producing one and failing validation
    later).
    """
    hits = matches_of_secret_markers(payload)
    if hits:
        surfaces = sorted({surface for surface, _ in hits})
        raise ArtifactSecretViolation(
            "gate-results artifact carries secret markers on surface(s): "
            + ", ".join(surfaces)
        )


def bounded_scan_surfaces(surfaces: Sequence[str]) -> tuple[str, ...]:
    """Bound and de-duplicate the surface names a scan reports on."""
    if len(surfaces) > 64:
        raise ArtifactBoundsViolation("scan surfaces exceed 64 entries")
    seen: set[str] = set()
    out: list[str] = []
    for surface in surfaces:
        text = str(surface).strip()
        if not text or len(text) > 120:
            raise ArtifactBoundsViolation("scan surface names must be 1-120 characters")
        if text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)
