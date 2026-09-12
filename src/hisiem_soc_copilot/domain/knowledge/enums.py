"""Knowledge domain enums (brief P3-A sections 6/9/13/46).

Values are exactly the frozen design values. There is deliberately no
``PUBLIC``/``PRIVATE``/``ORG``/``GROUP``/``USER``/``CONFIDENTIAL`` visibility:
those would be an authorization model, and knowledge carries none (visibility is
a SCOPE, not a permission).
"""

from __future__ import annotations

import enum


class SourceKind(enum.StrEnum):
    """Where a document came from. Immutable for the life of the document."""

    MITRE_ATTACK = "MITRE_ATTACK"
    CURATED_GUIDANCE = "CURATED_GUIDANCE"
    TENANT_RUNBOOK = "TENANT_RUNBOOK"


class Visibility(enum.StrEnum):
    """The document's scope. GLOBAL is tenant-less; TENANT belongs to exactly one."""

    GLOBAL = "GLOBAL"
    TENANT = "TENANT"


class DocumentStatus(enum.StrEnum):
    """Document lifecycle. RETIRED is terminal: RETIRED -> ACTIVE is not supported."""

    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class EmbeddingProfileStatus(enum.StrEnum):
    """Embedding profile lifecycle. At most one profile may be ACTIVE."""

    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class DistanceMetric(enum.StrEnum):
    """Distance metric of an embedding profile (brief section 46).

    P3-A freezes exactly ONE metric: cosine. A second metric would be a ranking
    semantics change and requires its own design round -- it is not added here
    speculatively, and retrieval rejects any other value explicitly.
    """

    COSINE = "COSINE"
