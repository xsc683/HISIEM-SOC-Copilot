"""Manifest sealer typed error taxonomy (E1-B.4 §23).

Bounded diagnostic context only — never secrets, tokens, Authorization material,
or environment dumps. Subclasses :class:`contracts.EvaluationError` so callers may
catch the whole evaluation package with one base class.
"""

from __future__ import annotations

from .contracts import EvaluationError

__all__ = [
    "ManifestCanonicalizationError",
    "ManifestError",
    "ManifestIntegrityError",
    "ManifestNotVerifiedError",
    "ManifestPersistenceError",
    "ManifestSchemaError",
    "ManifestSealConflict",
    "OracleIsolationViolation",
]


class ManifestError(EvaluationError):
    """Base for all manifest-sealer typed failures (E1-B.4 §23)."""

    code = "MANIFEST_ERROR"


class ManifestNotVerifiedError(ManifestError):
    """The input is not a verified dataset (E1-B.4 §2): sealing requires a
    :class:`contracts.VerifiedDataset`; an unverified
    :class:`contracts.MaterializationDraft` is rejected by construction."""

    code = "MANIFEST_NOT_VERIFIED"


class ManifestSchemaError(ManifestError):
    """A schema/type/shape invariant of the manifest payload is violated."""

    code = "MANIFEST_SCHEMA_ERROR"


class ManifestCanonicalizationError(ManifestError):
    """The payload cannot be canonicalized (e.g. NaN/Infinity, bad timestamps)."""

    code = "MANIFEST_CANONICALIZATION_ERROR"


class ManifestIntegrityError(ManifestError):
    """Recomputed manifest_sha256 does not match the sealed digest (E1-B.4 §16)."""

    code = "MANIFEST_INTEGRITY_ERROR"


class ManifestSealConflict(ManifestError):
    """The target already holds a DIFFERENT sealed manifest (E1-B.4 §21).

    Idempotent re-seal of identical bytes succeeds; a differing manifest must
    never be silently overwritten.
    """

    code = "MANIFEST_SEAL_CONFLICT"


class ManifestPersistenceError(ManifestError):
    """Filesystem persistence failed while sealing or reading a manifest."""

    code = "MANIFEST_PERSISTENCE_ERROR"


class OracleIsolationViolation(ManifestError):
    """Oracle/watermark content leaked into evidence or launch context (E1-B.4 §13).

    Raised when a control (W1) role would satisfy semantic evidence requirements
    or an oracle datum would reach a production launch projection.
    """

    code = "ORACLE_ISOLATION_VIOLATION"
