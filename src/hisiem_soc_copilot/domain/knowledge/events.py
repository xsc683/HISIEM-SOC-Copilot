"""Knowledge domain events (append-only audit facts).

These are AUDIT FACTS, not async side-effect triggers: no outbox destination is
wired for them (brief section 29). They are recorded inside the same transaction
as the document/version rows so the ledger can never disagree with the state.

Event factories are named exactly after their ``event_type`` and stringify every
UUID in their payload (the JSONB column holds strings, never UUID objects).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from ..shared.identifiers import new_uuid, utc_now


@dataclass(frozen=True)
class KnowledgeEvent:
    """A single append-only knowledge business event."""

    event_type: str
    aggregate_id: UUID
    aggregate_type: str = "knowledge_document"
    version: int = 1
    tenant_id: str | None = None
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    actor_subject_id: str | None = None
    occurred_at: datetime = field(default_factory=utc_now)
    event_id: UUID = field(default_factory=new_uuid)
    payload: dict[str, Any] = field(default_factory=dict)


def _ctx(
    *,
    tenant_id: str | None = None,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
    actor_subject_id: str | None = None,
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "actor_subject_id": actor_subject_id,
    }


def knowledge_document_created(
    *,
    aggregate_id: UUID,
    source_kind: str,
    external_key: str,
    visibility: str,
    tenant_id: str | None = None,
    **ctx: Any,
) -> KnowledgeEvent:
    return KnowledgeEvent(
        event_type="knowledge_document_created",
        aggregate_id=aggregate_id,
        payload={
            "source_kind": source_kind,
            "external_key": external_key,
            "visibility": visibility,
        },
        **_ctx(**{**ctx, "tenant_id": tenant_id}),
    )


def knowledge_document_version_ingested(
    *,
    aggregate_id: UUID,
    document_version_id: UUID,
    version: int,
    content_hash: str,
    tenant_id: str | None = None,
    **ctx: Any,
) -> KnowledgeEvent:
    return KnowledgeEvent(
        event_type="knowledge_document_version_ingested",
        aggregate_id=aggregate_id,
        payload={
            "document_version_id": str(document_version_id),
            "version": version,
            "content_hash": content_hash,
        },
        **_ctx(**{**ctx, "tenant_id": tenant_id}),
    )


def knowledge_document_retired(
    *,
    aggregate_id: UUID,
    tenant_id: str | None = None,
    **ctx: Any,
) -> KnowledgeEvent:
    return KnowledgeEvent(
        event_type="knowledge_document_retired",
        aggregate_id=aggregate_id,
        payload={},
        **_ctx(**{**ctx, "tenant_id": tenant_id}),
    )
