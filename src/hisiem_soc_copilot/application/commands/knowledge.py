"""Knowledge ingestion commands (brief sections 26, 32, 38).

Commands carry CALLER INTENT only. There is deliberately no field for
``content_hash``, ``normalized_content``, ``version``, or ``active_version_id``:
provenance is derived server-side from the content the caller actually supplied,
so a caller cannot claim a version identity it did not produce. Nothing here is
model-supplied on any path -- the CLI and the ATT&CK importer are the only
callers in P3-A, and neither is reachable from an Agent (sections 4/88).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from ...domain.knowledge.entities import KnowledgeDocument, KnowledgeDocumentVersion
from ...domain.knowledge.enums import SourceKind, Visibility
from ..ports.attack import FRAMEWORK


@dataclass(frozen=True)
class IngestKnowledgeDocument:
    """Ingest one document body as a new immutable version.

    ``tenant_id`` is required for ``TENANT`` visibility and forbidden for
    ``GLOBAL``; the domain validates the pairing rather than trusting the caller
    to send a coherent pair (section 6).
    """

    source_kind: SourceKind
    external_key: str
    visibility: Visibility
    title: str
    content: str
    tenant_id: str | None = None
    language: str = "en"
    source_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Legacy opt-in for changing the ACTIVE embedding space. KEPT so an existing
    #: caller is told WHY it is refused instead of failing on an unknown keyword,
    #: but P3-A rejects it outright with
    #: ``EMBEDDING_PROFILE_SWITCH_REQUIRES_CORPUS_REINDEX``: a different ACTIVE
    #: profile means a corpus-wide reindex, never a side effect of one document's
    #: ingest (brief section 4.1). No value of this flag switches anything.
    allow_embedding_profile_switch: bool = False
    #: Whether this ingest may move the document's ACTIVE version pointer.
    #:
    #: ``True`` -- the default, and the behaviour of every ordinary caller -- is
    #: "record this version and make it the one retrieval serves". ``False``
    #: records the immutable version, its chunks and its embeddings WITHOUT
    #: touching ``active_version_id``, which is what lets a staged ATT&CK release
    #: be fully projected while the framework's current authority keeps serving
    #: its own content (brief sections 2.4/2.5).
    #:
    #: This is deliberately NOT the generic rollback the ingestion handler
    #: refuses: a staged ingest never moves the pointer, in either direction. The
    #: ordinary "re-ingesting old content is not a rollback" rule is untouched.
    activate_version: bool = True
    command_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True)
class RetireKnowledgeDocument:
    """Retire a document so it stops appearing in retrieval.

    Retirement is terminal (section 6): a RETIRED document is excluded from
    normal search, while citations captured earlier still resolve (section 53).
    """

    tenant_id: str
    document_id: UUID
    reason: str | None = None
    command_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True)
class IngestKnowledgeOutcome:
    """What one ingestion actually did.

    The three booleans exist so idempotency is observable rather than asserted:
    re-ingesting identical content must report ``created_version=False`` and
    ``projection_rebuilt=False`` instead of quietly "succeeding".
    """

    document: KnowledgeDocument
    version: KnowledgeDocumentVersion
    chunk_count: int
    document_created: bool
    version_created: bool
    projection_rebuilt: bool


@dataclass(frozen=True)
class RetireKnowledgeOutcome:
    """The retired document plus whether this call performed the transition."""

    document: KnowledgeDocument
    already_retired: bool


@dataclass(frozen=True)
class ImportAttackRelease:
    """Import one LOCAL, pinned MITRE ATT&CK STIX bundle (section 33).

    ``payload`` is the already-read file content. Reading it is the CLI's job (a
    filesystem concern); the use case owns everything that follows, so the CLI
    can never be the thing that decides what a technique document says.

    ``activate`` is the explicit state switch section 38 requires: importing a
    release for backfill must be possible WITHOUT changing which release the
    canonical projection serves, so the default is to leave the switch alone.
    """

    release: str
    payload: bytes
    framework: str = FRAMEWORK
    activate: bool = False
    command_id: UUID = field(default_factory=uuid4)
