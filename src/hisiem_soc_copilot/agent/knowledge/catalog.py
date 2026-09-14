"""Knowledge catalog adapter for retrieval and ATT&CK authority."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from ...application.errors import (
    InvalidKnowledgeQueryError,
    KnowledgeAuthorityIntegrityError,
    KnowledgeCitationIntegrityError,
)
from ...application.ports.knowledge import (
    LEXICAL_PROFILE_ID,
    RETRIEVAL_PROFILE_ID,
    VECTOR_PROFILE_ID,
    AttackTechniqueRecord,
    KnowledgeQuery,
    KnowledgeRetrievalCatalogPort,
    KnowledgeSearchResult,
)
from ...application.ports.unit_of_work import UnitOfWork
from ...application.services.attack_projection import technique_external_key_for_id
from ...application.services.knowledge_retrieval import (
    KnowledgeCitationResolver,
    KnowledgeRetrievalService,
    RetrievalMode,
)
from ...contracts.tools.types import ToolResult, ToolResultStatus
from ...domain.knowledge.enums import DocumentStatus, SourceKind, Visibility


@dataclass(frozen=True)
class CitationValidatedSearchResult:
    """Agent-layer sidecar for resolver-verified citation hashes.

    Hashes stay outside the frozen application KnowledgeHit contract and are
    carried only to Knowledge Evidence serialization.
    """

    result: KnowledgeSearchResult
    verified_content_hashes: tuple[tuple[str, str], ...]


class KnowledgeRetrievalCatalogAdapter(KnowledgeRetrievalCatalogPort):
    """Production adapter behind the two knowledge tools.

    Each operation owns one short-lived UnitOfWork. The graph never leaves a read
    session checked out while later nodes run, and citation validation uses the
    same scope and snapshot as the retrieval that produced the handle.
    """

    def __init__(
        self,
        *,
        retrieval_service_factory: Callable[[UnitOfWork], KnowledgeRetrievalService],
        unit_of_work_factory: Callable[[], UnitOfWork],
    ) -> None:
        self._service_factory = retrieval_service_factory
        self._uow_factory = unit_of_work_factory

    async def retrieve_security_guidance(
        self, *, tenant_id: str, query: KnowledgeQuery
    ) -> KnowledgeSearchResult:
        """Run HYBRID retrieval and validate every returned citation."""
        return (
            await self.retrieve_security_guidance_validated(
                tenant_id=tenant_id, query=query
            )
        ).result

    async def retrieve_security_guidance_validated(
        self, *, tenant_id: str, query: KnowledgeQuery
    ) -> CitationValidatedSearchResult:
        """Return retrieval data plus Agent-only verified-hash sidecar."""
        return await self._retrieve(
            tenant_id=tenant_id, query=query, mode=RetrievalMode.HYBRID
        )

    async def retrieve_lexical_guidance(
        self, *, tenant_id: str, query: KnowledgeQuery
    ) -> KnowledgeSearchResult:
        """Run lexical fallback over the same corpus."""
        return (
            await self.retrieve_lexical_guidance_validated(
                tenant_id=tenant_id, query=query
            )
        ).result

    async def retrieve_lexical_guidance_validated(
        self, *, tenant_id: str, query: KnowledgeQuery
    ) -> CitationValidatedSearchResult:
        """Return lexical retrieval plus Agent-only verified-hash sidecar."""
        return await self._retrieve(
            tenant_id=tenant_id, query=query, mode=RetrievalMode.LEXICAL_ONLY
        )

    async def _retrieve(
        self, *, tenant_id: str, query: KnowledgeQuery, mode: RetrievalMode
    ) -> CitationValidatedSearchResult:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise InvalidKnowledgeQueryError("tenant_id is required for retrieval")
        async with self._uow_factory() as uow:
            result = await self._service_factory(uow).retrieve(
                tenant_id=tenant_id, query=query, mode=mode
            )
            resolver = KnowledgeCitationResolver(chunks=uow.knowledge_chunks)
            validated_hits = []
            verified_content_hashes = []
            for hit in result.hits:
                resolution = await resolver.resolve(
                    tenant_id=tenant_id, citation_id=hit.citation_id
                )
                if (
                    not resolution.resolved
                    or resolution.document_id != hit.document_id
                    or resolution.document_version_id != hit.document_version_id
                    or resolution.chunk_id != hit.chunk_id
                    or resolution.source_kind != hit.source_kind
                    or not resolution.content_hash
                ):
                    raise KnowledgeCitationIntegrityError(
                        "retrieved citation failed scoped content re-validation"
                    )
                validated_hits.append(
                    replace(
                        hit,
                        title=resolution.title or hit.title,
                        excerpt=resolution.excerpt or hit.excerpt,
                        source_kind=resolution.source_kind or hit.source_kind,
                        source_version=resolution.source_version,
                    )
                )
                verified_content_hashes.append(
                    (hit.citation_id, resolution.content_hash)
                )
            return CitationValidatedSearchResult(
                result=replace(result, hits=tuple(validated_hits)),
                verified_content_hashes=tuple(verified_content_hashes),
            )

    async def resolve_attack_technique(
        self, *, tenant_id: str, technique_id: str, framework: str = "mitre-attack"
    ) -> AttackTechniqueRecord | None:
        """Resolve an exact id through ACTIVE release and projection authority."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise InvalidKnowledgeQueryError("tenant_id is required for resolution")
        normalized = technique_id.strip().upper()
        async with self._uow_factory() as uow:
            release = await uow.attack_releases.get_active(framework=framework)
            if release is None:
                return None
            technique = await uow.attack_techniques.find(
                framework=framework,
                technique_id=normalized,
                source_release=release.source_release,
            )
            if technique is None:
                return None
            bindings = await uow.attack_release_projections.list_for_release(
                framework=framework, source_release=release.source_release
            )
            matches = [item for item in bindings if item.technique_id == normalized]
            if len(matches) != 1:
                raise KnowledgeAuthorityIntegrityError(
                    "ACTIVE ATT&CK release has no unique technique projection binding"
                )
            binding = matches[0]
            document = await uow.knowledge_documents.get(
                tenant_id=None, document_id=binding.document_id
            )
            version = await uow.knowledge_documents.get_version(
                tenant_id=None, document_version_id=binding.document_version_id
            )
            if (
                not technique.active
                or technique.framework != framework
                or technique.technique_id != normalized
                or technique.source_release != release.source_release
                or document is None
                or version is None
                or document.id != binding.document_id
                or document.source_kind is not SourceKind.MITRE_ATTACK
                or document.visibility is not Visibility.GLOBAL
                or document.tenant_id is not None
                or document.status is not DocumentStatus.ACTIVE
                or document.external_key != technique_external_key_for_id(normalized)
                or document.active_version_id != binding.document_version_id
                or version.document_id != binding.document_id
                or technique.content_hash != binding.content_hash
                or version.content_hash != binding.content_hash
            ):
                raise KnowledgeAuthorityIntegrityError(
                    "ACTIVE ATT&CK release projection failed identity or hash-chain validation"
                )
            return technique


_PROFILE_MODE = {
    RETRIEVAL_PROFILE_ID: "HYBRID",
    LEXICAL_PROFILE_ID: "LEXICAL_ONLY",
    VECTOR_PROFILE_ID: "VECTOR_ONLY",
}


def guidance_tool_result(
    *,
    tool_call_id: str,
    result: KnowledgeSearchResult,
    verified_content_hashes: tuple[tuple[str, str], ...] = (),
) -> ToolResult:
    """Build a typed result with validated citation and retrieval provenance."""
    verified_hash_by_citation = dict(verified_content_hashes)
    profile_id = result.profile_id
    mode = _PROFILE_MODE.get(profile_id or "", "UNKNOWN")
    hits = [
        {
            "document_id": str(hit.document_id),
            "document_version_id": str(hit.document_version_id),
            "chunk_id": str(hit.chunk_id),
            "source_kind": hit.source_kind.value,
            "title": hit.title,
            "excerpt": hit.excerpt,
            "citation_id": hit.citation_id,
            "source_version": hit.source_version,
            "verified_content_hash": verified_hash_by_citation.get(hit.citation_id),
            "retrieval_provenance": {
                "mode": mode,
                "profile_id": profile_id,
                "retrieved_at": hit.retrieved_at.isoformat(),
            },
        }
        for hit in result.hits
    ]
    status: ToolResultStatus = "SUCCESS" if hits else "NO_DATA"
    return ToolResult(
        tool_call_id=tool_call_id,
        tool_name="knowledge.retrieve_security_guidance",
        status=status,
        fetched_at=datetime.now(UTC).isoformat(),
        data={
            "hits": hits,
            "returned": len(hits),
            "truncated": result.truncated,
            "retrieval_mode": mode,
            "retrieval_profile_id": profile_id,
        },
    )
