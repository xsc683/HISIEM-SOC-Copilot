"""ATT&CK bundle port (brief sections 33-38).

The importer reads a LOCAL, pinned MITRE ATT&CK STIX 2.1 bundle. Parsing that
file format is an infrastructure concern (it is a specific external wire format
with a specific spec version), so the application states what it needs -- the
in-scope Enterprise techniques of one bundle, plus what the parse dropped -- and
infrastructure supplies it.

Mirrors ``application/ports/chunking.py``: the returned dataclasses are the
application's own types, not the STIX reader's, so the reader can be replaced
without an application-layer edit.

There is deliberately NO url/fetch capability on this port. The brief forbids
runtime network access for ATT&CK import, and the surest way to keep a promise
like that is to not have the interface for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

#: The only ATT&CK framework key this knowledge base knows.
FRAMEWORK = "mitre-attack"


@dataclass(frozen=True)
class AttackTechnique:
    """One in-scope Enterprise technique, normalized for projection.

    Fully determined by the bundle: two parses of the same bytes produce equal
    values, which is what makes the derived document content (and therefore its
    hash) reproducible (sections 22/23).
    """

    technique_id: str
    """ATT&CK id, e.g. ``T1110`` or ``T1110.001``."""

    stix_id: str
    """The STIX object id, e.g. ``attack-pattern--abc...`` (provenance)."""

    name: str
    description: str
    tactics: tuple[str, ...]
    platforms: tuple[str, ...]


@dataclass(frozen=True)
class AttackBundle:
    """One parsed bundle: what is in scope, and what the parse skipped."""

    spec_version: str
    techniques: tuple[AttackTechnique, ...]
    skipped_ids: tuple[str, ...]
    total_attack_patterns: int


class AttackBundleParser(Protocol):
    """Parse one local ATT&CK STIX bundle from bytes."""

    def parse(self, payload: bytes) -> AttackBundle:
        """Return the active Enterprise techniques in ``payload``.

        Must fail closed with ``InvalidMitreBundleError`` on malformed JSON or an
        unsupported STIX spec version rather than returning a partial bundle: an
        import that silently read half a file would produce a corpus that looks
        complete and is not (section 68).
        """
        ...
