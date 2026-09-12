"""Deterministic ATT&CK release fingerprint (brief section 2.2).

A pinned release must be IMMUTABLE: "v14.1" has to mean the same technique
collection forever, or a citation, a document version, and a canonical row can
silently describe different facts while all claiming the same release name. The
fingerprint is what makes that checkable -- it is a deterministic function of the
release's canonical technique collection, so a re-import of the same bytes
converges and a re-import of *different* content under the same release name
fails closed instead of overwriting history.

Two properties are load-bearing, and both come from where it is computed rather
than from discipline:

1. **Input-order independence.** Techniques are sorted by ``(technique_id,
   source_stix_id)`` and each technique's tactics/platforms are sorted and deduped
   before hashing, so reordering the objects in the STIX bundle (or the keys in a
   JSON object) cannot change the fingerprint. The technique key is total within a
   framework because ``(framework, technique_id, source_release)`` is unique.

2. **Computability from stored rows.** The fingerprint binds ONLY fields that are
   persisted on ``attack_technique`` -- including the technique's own content
   hash. That is what lets the migration's legacy-adoption path re-derive the
   fingerprint of an already-imported release from the database and compare it
   with the incoming bundle, using this one function, instead of trusting a
   remembered value or inventing a second definition.

The SHA-256 here is over the canonical JSON of the release, which is a different
fact from the per-content hash of ``domain.knowledge.value_objects`` (that one
hashes one normalized document body). They are deliberately not shared: sharing
them would mean one function whose meaning depends on its caller.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..ports.knowledge import AttackTechniqueRecord

#: Schema tag mixed into the hashed bytes. Bumping it is how a future, genuinely
#: different fingerprint definition is introduced without silently reinterpreting
#: fingerprints already persisted in ``attack_release.content_fingerprint``.
FINGERPRINT_SCHEMA = "attack-release-fingerprint/v1"

FINGERPRINT_HEX_LENGTH = 64


class FingerprintTechnique(Protocol):
    """The technique fields the fingerprint binds.

    Declared as READ-ONLY properties, which is what makes a persisted
    :class:`~...application.ports.knowledge.AttackTechniqueRecord` satisfy it: its
    ``tactics``/``platforms`` are immutable tuples, and an invariant mutable
    attribute would reject ``tuple[str, ...]`` where the protocol says
    ``Sequence[str]``. Nothing here is ever written through this view.

    Every field is one that ``attack_technique`` actually stores, so the
    fingerprint of a release can be re-derived from the database alone -- which is
    what the legacy-adoption path relies on.
    """

    @property
    def technique_id(self) -> str: ...
    @property
    def source_stix_id(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def tactics(self) -> Sequence[str]: ...
    @property
    def platforms(self) -> Sequence[str]: ...
    @property
    def content_hash(self) -> str: ...


@dataclass(frozen=True)
class _BoundTechnique:
    """A technique reduced to exactly the facts the fingerprint binds."""

    technique_id: str
    source_stix_id: str
    name: str
    description: str
    tactics: tuple[str, ...]
    platforms: tuple[str, ...]
    content_hash: str


def _canonical_technique(technique: FingerprintTechnique) -> _BoundTechnique:
    return _BoundTechnique(
        technique_id=technique.technique_id,
        source_stix_id=technique.source_stix_id,
        name=technique.name,
        description=technique.description,
        # Sets, not sequences: ATT&CK tactic/platform collections are unordered
        # attributes, so a bundle that lists them differently is the same release.
        tactics=tuple(sorted(set(technique.tactics))),
        platforms=tuple(sorted(set(technique.platforms))),
        content_hash=technique.content_hash,
    )


def release_fingerprint(
    *,
    framework: str,
    source_release: str,
    techniques: Iterable[FingerprintTechnique],
) -> str:
    """Return the lowercase hex SHA-256 fingerprint of one release.

    ``techniques`` may arrive in any order and from any source (a parsed bundle or
    stored rows); the result depends only on the canonical content of the
    collection.
    """
    bound = sorted(
        (_canonical_technique(technique) for technique in techniques),
        key=lambda item: (item.technique_id, item.source_stix_id),
    )
    document: dict[str, Any] = {
        "schema": FINGERPRINT_SCHEMA,
        "framework": framework,
        "source_release": source_release,
        "techniques": [
            {
                "technique_id": item.technique_id,
                "source_stix_id": item.source_stix_id,
                "name": item.name,
                "description": item.description,
                "tactics": list(item.tactics),
                "platforms": list(item.platforms),
                "content_hash": item.content_hash,
            }
            for item in bound
        ],
    }
    payload = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fingerprint_from_records(
    *,
    framework: str,
    source_release: str,
    techniques: Sequence[AttackTechniqueRecord],
) -> str:
    """Fingerprint the technique collection actually STORED for a release.

    Used by legacy adoption: the incoming bundle's fingerprint is compared against
    what the database holds, so adopting a pre-fingerprint release records a
    verified fact.
    """
    return release_fingerprint(
        framework=framework, source_release=source_release, techniques=techniques
    )


def is_valid_fingerprint(value: str) -> bool:
    return len(value) == FINGERPRINT_HEX_LENGTH and all(
        character in "0123456789abcdef" for character in value
    )
