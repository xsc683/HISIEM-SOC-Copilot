"""Adapter exposing the local STIX reader through the application port.

The reader (``mitre_stix.py``) knows the STIX 2.1 file format; the application
knows what a technique MEANS to the knowledge subsystem. This adapter is the only
place the two meet, translating the reader's ``ParsedTechnique`` values into the
application's ``AttackTechnique`` so the dependency direction stays
``infrastructure -> application`` (section 80).

It also drops the reader's own ``revoked``/``deprecated`` flags on the way out.
That is not information loss: the reader has already excluded revoked and
deprecated techniques from ``techniques``, and carrying the flags would invite a
caller to re-derive "is this active?" from two places instead of one.
"""

from __future__ import annotations

from ...application.ports.attack import AttackBundle, AttackBundleParser, AttackTechnique
from .mitre_stix import parse_attack_stix

__all__ = ("MitreStixAttackSource",)


class MitreStixAttackSource:
    """``AttackBundleParser`` implementation backed by the local STIX reader."""

    def parse(self, payload: bytes) -> AttackBundle:
        parsed = parse_attack_stix(payload)
        return AttackBundle(
            spec_version=parsed.spec_version,
            techniques=tuple(
                AttackTechnique(
                    technique_id=technique.technique_id,
                    stix_id=technique.stix_id,
                    name=technique.name,
                    description=technique.description,
                    tactics=technique.tactics,
                    platforms=technique.platforms,
                )
                for technique in parsed.techniques
            ),
            skipped_ids=parsed.skipped_ids,
            total_attack_patterns=parsed.total_attack_patterns,
        )


def _satisfies_port(source: MitreStixAttackSource) -> AttackBundleParser:
    """Static proof that the adapter structurally satisfies the port."""
    return source
