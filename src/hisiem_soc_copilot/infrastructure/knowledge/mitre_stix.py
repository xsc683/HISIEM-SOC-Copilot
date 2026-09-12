"""MITRE ATT&CK STIX 2.1 import (brief sections 33-38).

Why this module exists
----------------------
The knowledge subsystem needs an authoritative, reproducible map from a MITRE
technique id (``T1110``) to its canonical name, description, tactics and
platforms. That map is projected into knowledge documents which are later
chunked, embedded and hashed, so the projection must produce identical bytes on
every machine.

The rules that follow from that, and that this module enforces:

* Import reads a LOCAL, operator-supplied STIX 2.1 JSON file. There is NO
  network access, no URL download and no GitHub fetch at import time: an import
  that could silently change upstream content would break provenance.
* Only the stdlib ``json`` parser is used. A third-party STIX framework is
  deliberately NOT a dependency -- it would add a large, versioned parser whose
  own schema drift could change what a release contains.
* A payload that is malformed, wrongly typed or written against a non-2.x STIX
  spec is REJECTED wholesale (``InvalidMitreBundleError``). A bad bundle is
  never partially imported, because a half-imported release would look complete.
* Everything out of scope for Enterprise detection engineering (mobile/ics
  domains, revoked/deprecated techniques, ids that do not match the ATT&CK
  technique pattern, duplicates) is SKIPPED and RECORDED in ``skipped_ids`` --
  skipping is never silent, but it is also never fatal: the caller decides
  whether the resulting release is acceptable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ...domain.knowledge.errors import InvalidMitreBundleError

#: The STIX ``source_name`` / ``kill_chain_name`` that marks MITRE-authored
#: material. Anything else in ``external_references`` (CVE, CAPEC, ...) is not a
#: technique identifier.
FRAMEWORK = "mitre-attack"

#: The only ATT&CK domain this knowledge base imports (brief sections 33-38).
_ENTERPRISE_DOMAIN = "enterprise-attack"

#: ``T1110`` or a sub-technique ``T1110.001``. A reference that does not match
#: is not an ATT&CK technique id and must not become one.
_TECHNIQUE_ID = re.compile(r"\AT\d{4}(?:\.\d{3})?\Z")

#: STIX spec_version accepted by this importer; the ``2.`` prefix leaves room
#: for 2.0/2.1 while rejecting 1.x outright.
_SPEC_VERSION_PREFIX = "2."
_DEFAULT_SPEC_VERSION = "2.1"


@dataclass(frozen=True)
class ParsedTechnique:
    """One in-scope Enterprise technique, already normalized for projection.

    Instances are immutable and fully determined by the bundle: two parses of the
    same bundle produce equal values, which is what makes the derived content
    (and therefore its hash) reproducible (brief sections 22/23).
    """

    technique_id: str
    """ATT&CK id, e.g. ``T1110`` or ``T1110.001``."""

    stix_id: str
    """The STIX object id, e.g. ``attack-pattern--abc...`` (provenance)."""

    name: str
    description: str
    """Verbatim bundle description, or ``""`` when the object has none."""

    tactics: tuple[str, ...]
    """Kill-chain phase names, sorted and deduped."""

    platforms: tuple[str, ...]
    """``x_mitre_platforms`` values, sorted and deduped."""

    revoked: bool
    deprecated: bool

    @property
    def is_active(self) -> bool:
        """True only when the technique is neither revoked nor deprecated."""
        return not self.revoked and not self.deprecated


@dataclass(frozen=True)
class ParsedBundle:
    """The result of importing one local STIX bundle.

    ``techniques`` carries ONLY active Enterprise techniques. Everything else the
    bundle contained is reported through ``skipped_ids`` / ``total_attack_patterns``
    so an operator can see what an import dropped without re-reading the file.
    """

    spec_version: str
    techniques: tuple[ParsedTechnique, ...]
    skipped_ids: tuple[str, ...]
    total_attack_patterns: int


# ---------------------------------------------------------------------------
# Tolerant readers
#
# The bundle is untrusted input: a field may be missing, null or the wrong JSON
# type. Each reader below answers "what value did the bundle give, if any?" and
# never raises, so a single odd field cannot abort an otherwise good import.
# ---------------------------------------------------------------------------


def _as_mapping(value: object) -> dict[str, object]:
    """Return ``value`` as a str-keyed mapping, or ``{}`` when it is not one."""
    if not isinstance(value, dict):
        return {}
    mapping: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str):
            mapping[key] = item
    return mapping


def _as_sequence(value: object) -> list[object]:
    """Return ``value`` as a list, or ``[]`` when it is not a JSON array."""
    if not isinstance(value, list):
        return []
    items: list[object] = []
    for item in value:
        items.append(item)
    return items


def _as_str(value: object) -> str:
    """Return ``value`` when it is a string, else ``""``."""
    return value if isinstance(value, str) else ""


def _as_bool(value: object) -> bool:
    """Return True only for the JSON literal ``true``.

    Any other type (in particular the string ``"true"``) is treated as absent:
    STIX booleans are JSON booleans, and guessing at a string would let a
    malformed bundle silently retire a technique.
    """
    return value is True


def _attack_id_of(attack_pattern: dict[str, object]) -> str | None:
    """Return the MITRE technique id, or ``None`` when the object has none."""
    for reference in _as_sequence(attack_pattern.get("external_references")):
        entry = _as_mapping(reference)
        if entry.get("source_name") != FRAMEWORK:
            continue
        external_id = entry.get("external_id")
        if isinstance(external_id, str) and external_id:
            return external_id
    return None


def _tactics_of(attack_pattern: dict[str, object]) -> tuple[str, ...]:
    """Return the sorted, deduped MITRE kill-chain phase names (may be empty)."""
    names: set[str] = set()
    for phase in _as_sequence(attack_pattern.get("kill_chain_phases")):
        entry = _as_mapping(phase)
        if entry.get("kill_chain_name") != FRAMEWORK:
            continue
        phase_name = entry.get("phase_name")
        if isinstance(phase_name, str) and phase_name:
            names.add(phase_name)
    return tuple(sorted(names))


def _platforms_of(attack_pattern: dict[str, object]) -> tuple[str, ...]:
    """Return the sorted, deduped ``x_mitre_platforms`` values (may be empty)."""
    platforms: set[str] = set()
    for item in _as_sequence(attack_pattern.get("x_mitre_platforms")):
        if isinstance(item, str) and item:
            platforms.add(item)
    return tuple(sorted(platforms))


def _skip_reference(*, stix_id: str, attack_id: str | None) -> str:
    """Pick the id that identifies a skipped object in ``skipped_ids``.

    A well-formed ATT&CK id is the useful reference for an operator ("T1430 was
    dropped because it is mobile-attack"); when the object never had one, the
    STIX id is all we can report.
    """
    if attack_id is not None and _TECHNIQUE_ID.match(attack_id):
        return attack_id
    return stix_id


def _is_in_scope(attack_pattern: dict[str, object]) -> bool:
    """True when ``x_mitre_domains`` is absent/empty or covers Enterprise.

    A MISSING domain list keeps the technique: the field only became mandatory in
    later ATT&CK releases, and dropping older Enterprise techniques for a missing
    field would silently shrink a release. An explicit non-Enterprise domain
    (mobile-attack, ics-attack) is out of scope and skipped by the caller.
    """
    domains = _as_sequence(attack_pattern.get("x_mitre_domains"))
    if not domains:
        return True
    return any(domain == _ENTERPRISE_DOMAIN for domain in domains)


def _spec_version_of(root: dict[str, object]) -> str:
    """Read and validate the bundle ``spec_version``."""
    raw = root.get("spec_version", _DEFAULT_SPEC_VERSION)
    if raw is None:
        return _DEFAULT_SPEC_VERSION
    if not isinstance(raw, str):
        raise InvalidMitreBundleError("MITRE bundle spec_version must be a string")
    if not raw.startswith(_SPEC_VERSION_PREFIX):
        raise InvalidMitreBundleError(
            f"unsupported STIX spec_version: {raw!r} (expected 2.x)"
        )
    return raw


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def parse_attack_stix(payload: bytes | str) -> ParsedBundle:
    """Parse a local STIX 2.1 bundle into active Enterprise techniques.

    ``payload`` is the raw file content (``bytes`` are decoded as UTF-8; invalid
    UTF-8 is a rejection). Raises :class:`InvalidMitreBundleError` for anything
    that is not a STIX 2.1 bundle of ``attack-pattern`` objects -- no bundle is
    ever partially imported.

    Out-of-scope, malformed and duplicate objects are skipped rather than
    rejected, and are reported through ``ParsedBundle.skipped_ids``. An empty
    bundle is legal here and yields an empty technique tuple; whether an empty
    release is acceptable is the caller's decision, not the parser's.
    """
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidMitreBundleError(
                "MITRE bundle payload is not valid UTF-8"
            ) from exc
    elif isinstance(payload, str):
        text = payload
    else:
        raise InvalidMitreBundleError("MITRE bundle payload must be bytes or str")

    try:
        document: object = json.loads(text)
    except json.JSONDecodeError as exc:
        # ``exc.msg`` is a short parser message; the raw body never leaves here.
        raise InvalidMitreBundleError(f"MITRE bundle is not valid JSON: {exc.msg}") from exc

    if not isinstance(document, dict):
        raise InvalidMitreBundleError("MITRE bundle root must be a JSON object")
    root = _as_mapping(document)
    if root.get("type") != "bundle":
        raise InvalidMitreBundleError("MITRE bundle type must be 'bundle'")

    spec_version = _spec_version_of(root)

    raw_objects = root.get("objects")
    if not isinstance(raw_objects, list):
        raise InvalidMitreBundleError("MITRE bundle must contain an objects array")

    total_attack_patterns = 0
    active: list[ParsedTechnique] = []
    kept_ids: set[str] = set()
    skipped: list[str] = []

    for raw_object in raw_objects:
        attack_pattern = _as_mapping(raw_object)
        if attack_pattern.get("type") != "attack-pattern":
            continue
        total_attack_patterns += 1

        stix_id = _as_str(attack_pattern.get("id"))
        attack_id = _attack_id_of(attack_pattern)

        # Out-of-scope domain (mobile/ics) is never imported, but is reported.
        if not _is_in_scope(attack_pattern):
            skipped.append(_skip_reference(stix_id=stix_id, attack_id=attack_id))
            continue

        # No MITRE reference, or a reference that is not an ATT&CK technique id.
        if attack_id is None or not _TECHNIQUE_ID.match(attack_id):
            skipped.append(_skip_reference(stix_id=stix_id, attack_id=None))
            continue

        technique = ParsedTechnique(
            technique_id=attack_id,
            stix_id=stix_id,
            name=_as_str(attack_pattern.get("name")),
            description=_as_str(attack_pattern.get("description")),
            tactics=_tactics_of(attack_pattern),
            platforms=_platforms_of(attack_pattern),
            revoked=_as_bool(attack_pattern.get("revoked")),
            deprecated=_as_bool(attack_pattern.get("x_mitre_deprecated")),
        )

        # Revoked/deprecated techniques stay out of the active release.
        if not technique.is_active:
            skipped.append(_skip_reference(stix_id=stix_id, attack_id=attack_id))
            continue

        # Duplicate ids within the kept set: first in bundle order wins, so the
        # import is stable regardless of how the objects were ordered upstream.
        if attack_id in kept_ids:
            skipped.append(_skip_reference(stix_id=stix_id, attack_id=attack_id))
            continue
        kept_ids.add(attack_id)
        active.append(technique)

    techniques = tuple(sorted(active, key=lambda t: (t.technique_id, t.stix_id)))
    return ParsedBundle(
        spec_version=spec_version,
        techniques=techniques,
        skipped_ids=tuple(sorted(set(skipped))),
        total_attack_patterns=total_attack_patterns,
    )
