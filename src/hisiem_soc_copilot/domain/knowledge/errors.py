"""Knowledge domain errors.

Every error carries a stable machine ``code``. Messages are operator/analyst
facing and never contain secrets, credentials, or raw document bodies.
"""

from __future__ import annotations

from typing import Any

from ..shared.errors import DomainError, StateTransitionError


class KnowledgeError(DomainError):
    """Base for knowledge-domain errors."""

    code = "KNOWLEDGE_ERROR"


class InvalidKnowledgeDocumentError(KnowledgeError):
    """Raised when a document would be constructed with invalid identity/scope."""

    code = "INVALID_KNOWLEDGE_DOCUMENT"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)


class InvalidKnowledgeScopeError(KnowledgeError):
    """Raised when visibility and tenant_id disagree (GLOBAL<->None, TENANT<->id)."""

    code = "INVALID_KNOWLEDGE_SCOPE"


class InvalidKnowledgeVersionError(KnowledgeError):
    """Raised when a document version violates its immutability invariants."""

    code = "INVALID_KNOWLEDGE_VERSION"


class KnowledgeDocumentStateError(StateTransitionError):
    """Raised when a command would perform an illegal document transition."""

    def __init__(self, *, document_id: Any, current_status: str, command: str) -> None:
        super().__init__(
            aggregate_type="knowledge_document",
            current_status=current_status,
            command=command,
            message=(
                f"knowledge document {document_id} cannot transition from "
                f"{current_status} via {command}"
            ),
        )


class InvalidMitreBundleError(KnowledgeError):
    """Raised when a local MITRE ATT&CK STIX bundle cannot be trusted.

    Import reads a LOCAL, operator-supplied file only; a bundle that is not a
    STIX 2.1 JSON bundle of Enterprise ``attack-pattern`` objects is rejected
    outright rather than partially imported (brief sections 33-38).
    """

    code = "INVALID_MITRE_BUNDLE"


class InvalidContentHashError(KnowledgeError):
    """Raised when a content hash is not a valid lowercase SHA-256 hex digest."""

    code = "INVALID_CONTENT_HASH"


class InvalidEmbeddingVectorError(KnowledgeError):
    """Raised when an embedding vector is empty, non-finite, or the wrong size."""

    code = "INVALID_EMBEDDING_VECTOR"


class InvalidCitationError(KnowledgeError):
    """Raised when a citation string is malformed.

    A malformed citation is never repaired and never trusted: it carries no
    authority (brief section 51).
    """

    code = "INVALID_CITATION"


class KnowledgeBoundsExceededError(KnowledgeError):
    """Raised when content exceeds an explicit, configured bound.

    Bounds are REJECTIONS, never truncations: silently truncating a document
    would break provenance (brief section 25).
    """

    code = "KNOWLEDGE_BOUNDS_EXCEEDED"

    def __init__(self, *, bound: str, limit: int, actual: int) -> None:
        super().__init__(
            f"knowledge content exceeds {bound} (limit={limit}, actual={actual})",
            details={"bound": bound, "limit": limit, "actual": actual},
        )
