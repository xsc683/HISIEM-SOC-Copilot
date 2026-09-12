"""Knowledge aggregate + immutable document version.

``KnowledgeDocument`` is the aggregate root for one externally-identified piece of
knowledge. ``KnowledgeDocumentVersion`` is an immutable entity: a version is
never UPDATEd, a change always appends a new one (brief sections 6/7/92).

Identity is ``(source_kind, external_key)`` inside a scope (GLOBAL) or a tenant
(TENANT). ``source_kind``, ``external_key`` and ``visibility`` never change; the
only lifecycle move is ACTIVE -> RETIRED (RETIRED -> ACTIVE is not supported).
``active_version_id`` is a POINTER, so activating a version is a one-row switch --
but only in the same transaction that persists that version and its chunks, so a
pointer can never reference a version whose chunks are missing (brief section 27).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from ..shared.identifiers import utc_now
from . import events as ev
from .enums import DocumentStatus, SourceKind, Visibility
from .errors import (
    InvalidKnowledgeDocumentError,
    InvalidKnowledgeScopeError,
    InvalidKnowledgeVersionError,
    KnowledgeDocumentStateError,
)
from .value_objects import is_valid_content_hash, normalize_knowledge_content

MAX_TITLE_CHARS = 512
MAX_EXTERNAL_KEY_CHARS = 512
MAX_LANGUAGE_CHARS = 16
MAX_SOURCE_VERSION_CHARS = 128


def require_valid_scope(*, visibility: Visibility, tenant_id: str | None) -> None:
    """Enforce GLOBAL <=> no tenant and TENANT <=> exactly one tenant.

    This is the domain half of the DB CHECK constraint: the database is the final
    arbiter, and this makes the injective scope impossible to construct in memory.
    """
    if visibility is Visibility.GLOBAL and tenant_id is not None:
        raise InvalidKnowledgeScopeError("a GLOBAL knowledge document has no tenant")
    if visibility is Visibility.TENANT and not (tenant_id or "").strip():
        raise InvalidKnowledgeScopeError(
            "a TENANT knowledge document requires a tenant_id"
        )


@dataclass(frozen=True)
class KnowledgeDocumentVersion:
    """One immutable version of a document's content.

    Immutability is what makes provenance checkable: a citation that names
    ``(document_version_id, content_hash)`` stays verifiable, because neither the
    bytes nor the row can be rewritten after the fact.
    """

    id: UUID
    document_id: UUID
    version: int
    content_hash: str
    title: str
    normalized_content: str
    language: str = "en"
    source_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    ingested_at: datetime = field(default_factory=utc_now)
    effective_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.version < 1:
            raise InvalidKnowledgeVersionError("document version must be >= 1")
        if not is_valid_content_hash(self.content_hash):
            raise InvalidKnowledgeVersionError(
                "content_hash must be a lowercase SHA-256 hex digest"
            )
        if not self.normalized_content:
            raise InvalidKnowledgeVersionError("normalized content must not be empty")
        # The invariant that makes ``content_hash`` MEAN anything. It is checked
        # here rather than only in ``create()`` because this dataclass is also
        # constructed directly from persisted rows and by tests: a version whose
        # "normalized" content is not a fixed point of normalization would carry a
        # hash that a later re-normalization of the same bytes could not
        # reproduce, and provenance would be quietly broken. Re-normalizing
        # instead of rejecting would be a second definition of the hash.
        if normalize_knowledge_content(self.normalized_content) != self.normalized_content:
            raise InvalidKnowledgeVersionError(
                "normalized_content must already be normalized"
            )
        _require_bounded_text(
            self.title,
            field_name="title",
            limit=MAX_TITLE_CHARS,
            error=InvalidKnowledgeVersionError,
        )
        _require_bounded_text(
            self.language,
            field_name="language",
            limit=MAX_LANGUAGE_CHARS,
            error=InvalidKnowledgeVersionError,
        )
        if self.source_version is not None:
            _require_bounded_text(
                self.source_version,
                field_name="source_version",
                limit=MAX_SOURCE_VERSION_CHARS,
                error=InvalidKnowledgeVersionError,
            )

    @classmethod
    def create(
        cls,
        *,
        id: UUID,
        document_id: UUID,
        version: int,
        normalized_content: str,
        content_hash: str,
        title: str,
        language: str = "en",
        source_version: str | None = None,
        metadata: dict[str, Any] | None = None,
        ingested_at: datetime | None = None,
        effective_at: datetime | None = None,
    ) -> KnowledgeDocumentVersion:
        """Build a version.

        Rejects content that is not already normalized -- see ``__post_init__``,
        which enforces that for every construction path, not just this one. A
        caller that hands over un-normalized bytes has a bug to fix, not a
        document to repair.
        """
        return cls(
            id=id,
            document_id=document_id,
            version=version,
            content_hash=content_hash,
            title=title,
            normalized_content=normalized_content,
            language=language,
            source_version=source_version,
            metadata=dict(metadata or {}),
            ingested_at=ingested_at or utc_now(),
            effective_at=effective_at,
        )


@dataclass
class KnowledgeDocument:
    """Aggregate root for one externally-identified knowledge document.

    Invariants (brief section 6):
    - identity (source_kind, external_key) + visibility are immutable;
    - GLOBAL has no tenant, TENANT has exactly one;
    - ACTIVE -> RETIRED is legal, RETIRED -> ACTIVE is not;
    - every state change goes through an aggregate method below.
    """

    id: UUID
    source_kind: SourceKind
    external_key: str
    visibility: Visibility
    tenant_id: str | None
    title: str
    status: DocumentStatus = DocumentStatus.ACTIVE
    active_version_id: UUID | None = None
    lock_version: int = 0
    revision: int = 0
    created_at: datetime = field(default_factory=utc_now)
    retired_at: datetime | None = None
    _pending_events: list[ev.KnowledgeEvent] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        require_valid_scope(visibility=self.visibility, tenant_id=self.tenant_id)
        _require_bounded_text(
            self.external_key,
            field_name="external_key",
            limit=MAX_EXTERNAL_KEY_CHARS,
            error=InvalidKnowledgeDocumentError,
        )
        _require_bounded_text(
            self.title,
            field_name="title",
            limit=MAX_TITLE_CHARS,
            error=InvalidKnowledgeDocumentError,
        )

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        id: UUID,
        source_kind: SourceKind,
        external_key: str,
        visibility: Visibility,
        tenant_id: str | None,
        title: str,
        now: datetime | None = None,
    ) -> KnowledgeDocument:
        document = cls(
            id=id,
            source_kind=source_kind,
            external_key=external_key,
            visibility=visibility,
            tenant_id=tenant_id,
            title=title,
            created_at=now or utc_now(),
        )
        document._pending_events.append(
            ev.knowledge_document_created(
                aggregate_id=id,
                source_kind=source_kind.value,
                external_key=external_key,
                visibility=visibility.value,
                tenant_id=tenant_id,
            )
        )
        return document

    # ------------------------------------------------------------------
    # domain API
    # ------------------------------------------------------------------
    def activate_version(
        self,
        *,
        version_id: UUID,
        version: int,
        content_hash: str,
        title: str,
    ) -> None:
        """Point the document at a newly persisted immutable version.

        The caller must have already persisted ``version_id`` (and its chunks) in
        the SAME transaction. Re-activating the already-active version is a legal
        no-op for idempotent replay; it still emits the audit event so the ledger
        records that the version was re-confirmed.
        """
        if self.status is not DocumentStatus.ACTIVE:
            raise KnowledgeDocumentStateError(
                document_id=self.id,
                current_status=self.status.value,
                command="activate_version",
            )
        self.active_version_id = version_id
        self.title = title
        self._bump()
        self._pending_events.append(
            ev.knowledge_document_version_ingested(
                aggregate_id=self.id,
                document_version_id=version_id,
                version=version,
                content_hash=content_hash,
                tenant_id=self.tenant_id,
            )
        )

    def retire(self, *, now: datetime | None = None) -> None:
        """Retire the document. Terminal: RETIRED never returns to ACTIVE."""
        if self.status is not DocumentStatus.ACTIVE:
            raise KnowledgeDocumentStateError(
                document_id=self.id,
                current_status=self.status.value,
                command="retire",
            )
        self.status = DocumentStatus.RETIRED
        self.retired_at = now or utc_now()
        self._bump()
        self._pending_events.append(
            ev.knowledge_document_retired(
                aggregate_id=self.id, tenant_id=self.tenant_id
            )
        )

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def is_visible_to(self, tenant_id: str) -> bool:
        """True when ``tenant_id`` may read this document at all.

        Visibility is a SCOPE: GLOBAL knowledge belongs to every tenant, TENANT
        knowledge to exactly one. This is not a permission model and confers no
        authority -- it only decides whose retrieval may read the row.
        """
        if self.visibility is Visibility.GLOBAL:
            return True
        return self.tenant_id == tenant_id

    def is_active(self) -> bool:
        return self.status is DocumentStatus.ACTIVE

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _bump(self) -> None:
        # ``revision`` is the domain change counter emitted with events;
        # ``lock_version`` is the persistence CAS token owned by the repository.
        self.revision += 1

    def clear_events(self) -> None:
        self._pending_events = []

    @property
    def pending_events(self) -> list[ev.KnowledgeEvent]:
        return list(self._pending_events)


def _require_bounded_text(
    value: str,
    *,
    field_name: str,
    limit: int,
    error: Callable[[str], Exception],
) -> None:
    if not isinstance(value, str) or not value.strip():
        raise error(f"{field_name} must be a non-empty string")
    if len(value) > limit:
        raise error(f"{field_name} must be at most {limit} characters")
