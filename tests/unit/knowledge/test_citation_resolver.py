"""KnowledgeCitationResolver tests with an in-memory chunk repository double.

A citation is a LOOKUP KEY, never a claim. These tests pin exactly that: RETIRED
documents and historical versions still resolve (a past investigation must stay
explainable), while a malformed, unknown, hash-mismatched, or foreign-tenant
citation resolves to ``False`` with a stable reason and never raises.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import pytest

from hisiem_soc_copilot.application.errors import InvalidKnowledgeQueryError
from hisiem_soc_copilot.application.ports.knowledge import (
    ChunkProjectionState,
    KnowledgeChunkRecord,
    KnowledgeChunkView,
    LexicalCandidate,
    VectorCandidate,
)
from hisiem_soc_copilot.application.services.knowledge_retrieval import (
    KnowledgeCitationResolver,
)
from hisiem_soc_copilot.domain.knowledge.enums import SourceKind, Visibility
from hisiem_soc_copilot.domain.knowledge.errors import InvalidKnowledgeScopeError
from hisiem_soc_copilot.domain.knowledge.value_objects import (
    compute_content_hash,
    format_citation_id,
)

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"

DOCUMENT_ID = UUID("0d1c4a10-0000-4000-8000-000000000001")
VERSION_ID = UUID("0d1c4a10-0000-4000-8000-000000000002")
CHUNK_ID = UUID("3f2504e0-4f89-41d3-9a0c-0305e82c3301")

CONTENT = "T1110 brute force guidance for the sshd authentication_failure path."


# ---------------------------------------------------------------------------
# in-memory double
# ---------------------------------------------------------------------------


class FakeChunkRepository:
    """In-memory ``KnowledgeChunkRepository`` double for citation resolution.

    ``get_chunk_view`` deliberately IGNORES the tenant scope: it returns whatever
    view is stored for any requesting tenant. That is what makes SCOPE_MISMATCH
    meaningful -- the resolver's own re-check, not the repository, has to reject
    a foreign-tenant chunk (in production the SQL filter would have omitted the
    row entirely, so this double is strictly more permissive than production).
    """

    def __init__(self, *, views: Sequence[KnowledgeChunkView] = ()) -> None:
        self.views = {view.chunk_id: view for view in views}
        self.calls: list[tuple[str, UUID]] = []

    async def get_chunk_view(
        self, *, tenant_id: str, chunk_id: UUID
    ) -> KnowledgeChunkView | None:
        self.calls.append((tenant_id, chunk_id))
        return self.views.get(chunk_id)

    async def add_many(self, *, chunks: Sequence[KnowledgeChunkRecord]) -> None:
        raise NotImplementedError("resolution never writes chunks")

    async def count_for_version(self, *, document_version_id: UUID) -> int:
        raise NotImplementedError("resolution never counts chunks")

    async def projection_state(self, *, document_version_id: UUID) -> ChunkProjectionState:
        raise NotImplementedError("resolution never inspects projection state")

    async def delete_for_version(self, *, document_version_id: UUID) -> None:
        raise NotImplementedError("resolution never deletes chunks")

    async def lexical_candidates(
        self, *, tenant_id: str, search_terms: Sequence[str], limit: int
    ) -> tuple[LexicalCandidate, ...]:
        raise NotImplementedError("resolution never generates lexical candidates")

    async def vector_candidates(
        self,
        *,
        tenant_id: str,
        embedding_profile_id: UUID,
        query_vector: Sequence[float],
        limit: int,
    ) -> tuple[VectorCandidate, ...]:
        raise NotImplementedError("resolution never generates vector candidates")


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _view(
    *,
    chunk_id: UUID = CHUNK_ID,
    document_version_id: UUID = VERSION_ID,
    content: str = CONTENT,
    document_status: str = "ACTIVE",
    visibility: Visibility = Visibility.GLOBAL,
    tenant_id: str | None = None,
) -> KnowledgeChunkView:
    return KnowledgeChunkView(
        chunk_id=chunk_id,
        document_id=DOCUMENT_ID,
        document_version_id=document_version_id,
        ordinal=0,
        heading_path="Detection",
        content=content,
        content_hash=compute_content_hash(content),
        title="Brute Force Guidance",
        source_kind=SourceKind.CURATED_GUIDANCE,
        language="en",
        source_version="2026.09",
        document_status=document_status,
        visibility=visibility,
        tenant_id=tenant_id,
    )


def _resolver(*views: KnowledgeChunkView) -> tuple[KnowledgeCitationResolver, FakeChunkRepository]:
    repository = FakeChunkRepository(views=views)
    return KnowledgeCitationResolver(chunks=repository), repository


def _citation(view: KnowledgeChunkView) -> str:
    return format_citation_id(chunk_id=view.chunk_id, content_hash=view.content_hash)


# ---------------------------------------------------------------------------
# historical citations stay explainable
# ---------------------------------------------------------------------------


async def test_a_citation_in_a_retired_document_still_resolves() -> None:
    view = _view(document_status="RETIRED")
    resolver, repository = _resolver(view)

    resolution = await resolver.resolve(tenant_id=TENANT, citation_id=_citation(view))

    assert resolution.resolved is True
    assert resolution.reason is None
    assert resolution.document_status == "RETIRED"
    assert resolution.citation_id == _citation(view)
    assert resolution.document_id == view.document_id
    assert resolution.document_version_id == view.document_version_id
    assert resolution.chunk_id == view.chunk_id
    assert resolution.source_kind is SourceKind.CURATED_GUIDANCE
    assert resolution.title == view.title
    assert resolution.language == view.language
    assert resolution.source_version == view.source_version
    assert resolution.excerpt == view.content
    assert repository.calls == [(TENANT, view.chunk_id)]


async def test_a_citation_for_a_superseded_version_still_resolves() -> None:
    superseded = UUID("0d1c4a10-0000-4000-8000-00000000dead")
    view = _view(document_version_id=superseded)
    resolver, _ = _resolver(view)

    resolution = await resolver.resolve(tenant_id=TENANT, citation_id=_citation(view))

    assert resolution.resolved is True
    assert resolution.document_version_id == superseded
    assert resolution.document_status == "ACTIVE"


# ---------------------------------------------------------------------------
# malformed citations
# ---------------------------------------------------------------------------

MALFORMED_CITATIONS = [
    "",
    "   ",
    "not-a-citation",
    "kcit:only-two-parts",
    f"kcit:{CHUNK_ID}:{'0f' * 6}:one-part-too-many",
    f"kref:{CHUNK_ID}:{'0f' * 6}",  # wrong prefix
    f"kcit:not-a-uuid:{'0f' * 6}",  # chunk id is not a UUID
    f"kcit:{str(CHUNK_ID).upper()}:{'0f' * 6}",  # non-canonical UUID form
    f"kcit:{CHUNK_ID}:{'0f'}",  # hash prefix below 8 characters
    f"kcit:{CHUNK_ID}:{'0f' * 9}",  # hash prefix above 16 characters
    f"kcit:{CHUNK_ID}:{'0F' * 6}",  # uppercase hash prefix
    f"kcit:{CHUNK_ID}:{'zz' * 6}",  # non-hex hash prefix
    f"kcit:{CHUNK_ID}:",  # missing hash prefix
]


@pytest.mark.parametrize("citation_id", MALFORMED_CITATIONS)
async def test_a_malformed_citation_never_raises(citation_id: str) -> None:
    resolver, repository = _resolver(_view())

    resolution = await resolver.resolve(tenant_id=TENANT, citation_id=citation_id)

    assert resolution.resolved is False
    assert resolution.reason == "MALFORMED_CITATION"
    assert resolution.citation_id == citation_id
    assert resolution.document_id is None
    assert resolution.chunk_id is None
    # A string that does not even parse is never looked up.
    assert repository.calls == []


# ---------------------------------------------------------------------------
# unknown chunks
# ---------------------------------------------------------------------------


async def test_a_well_formed_citation_for_an_unknown_chunk_does_not_resolve() -> None:
    citation = _citation(_view())
    resolver, repository = _resolver()

    resolution = await resolver.resolve(tenant_id=TENANT, citation_id=citation)

    assert resolution.resolved is False
    assert resolution.reason == "CHUNK_NOT_FOUND"
    assert resolution.citation_id == citation
    assert repository.calls == [(TENANT, CHUNK_ID)]


# ---------------------------------------------------------------------------
# content hash re-validation
# ---------------------------------------------------------------------------


async def test_a_citation_whose_hash_prefix_is_wrong_does_not_resolve() -> None:
    view = _view()
    mismatched = format_citation_id(
        chunk_id=view.chunk_id, content_hash=compute_content_hash("a different body")
    )
    resolver, _ = _resolver(view)

    resolution = await resolver.resolve(tenant_id=TENANT, citation_id=mismatched)

    assert resolution.resolved is False
    assert resolution.reason == "CONTENT_HASH_MISMATCH"
    assert resolution.chunk_id is None


async def test_a_fabricated_hash_prefix_confers_nothing() -> None:
    view = _view()
    resolver, _ = _resolver(view)

    resolution = await resolver.resolve(
        tenant_id=TENANT, citation_id=f"kcit:{view.chunk_id}:{'ab' * 6}"
    )

    assert resolution.resolved is False
    assert resolution.reason == "CONTENT_HASH_MISMATCH"


@pytest.mark.parametrize("length", [8, 12, 16])
async def test_any_valid_prefix_length_of_the_true_hash_resolves(length: int) -> None:
    view = _view()
    resolver, _ = _resolver(view)

    resolution = await resolver.resolve(
        tenant_id=TENANT, citation_id=f"kcit:{view.chunk_id}:{view.content_hash[:length]}"
    )

    assert resolution.resolved is True
    assert resolution.chunk_id == view.chunk_id


# ---------------------------------------------------------------------------
# scope re-validation
# ---------------------------------------------------------------------------


async def test_a_tenant_chunk_owned_by_another_tenant_does_not_resolve() -> None:
    view = _view(visibility=Visibility.TENANT, tenant_id=OTHER_TENANT)
    resolver, repository = _resolver(view)

    resolution = await resolver.resolve(tenant_id=TENANT, citation_id=_citation(view))

    assert resolution.resolved is False
    assert resolution.reason == "SCOPE_MISMATCH"
    # The repository handed the foreign row back (see the double's docstring) and
    # the hash prefix matched, so the resolver's own scope re-check is the only
    # thing that rejected it.
    assert repository.calls == [(TENANT, view.chunk_id)]


async def test_a_tenant_chunk_resolves_for_its_owning_tenant() -> None:
    view = _view(visibility=Visibility.TENANT, tenant_id=TENANT)
    resolver, _ = _resolver(view)

    resolution = await resolver.resolve(tenant_id=TENANT, citation_id=_citation(view))

    assert resolution.resolved is True


@pytest.mark.parametrize("tenant_id", [TENANT, OTHER_TENANT])
async def test_a_global_chunk_resolves_for_any_tenant(tenant_id: str) -> None:
    view = _view(visibility=Visibility.GLOBAL, tenant_id=None)
    resolver, _ = _resolver(view)

    resolution = await resolver.resolve(tenant_id=tenant_id, citation_id=_citation(view))

    assert resolution.resolved is True


async def test_an_impossible_scope_row_is_refused() -> None:
    view = _view(visibility=Visibility.TENANT, tenant_id=None)
    resolver, _ = _resolver(view)

    with pytest.raises(InvalidKnowledgeScopeError) as excinfo:
        await resolver.resolve(tenant_id=TENANT, citation_id=_citation(view))

    assert excinfo.value.code == "INVALID_KNOWLEDGE_SCOPE"


# ---------------------------------------------------------------------------
# entry-point contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tenant_id", ["", "   ", "\t\n"])
async def test_resolve_rejects_an_empty_tenant_id(tenant_id: str) -> None:
    view = _view()
    resolver, repository = _resolver(view)

    with pytest.raises(InvalidKnowledgeQueryError) as excinfo:
        await resolver.resolve(tenant_id=tenant_id, citation_id=_citation(view))

    assert excinfo.value.code == "INVALID_KNOWLEDGE_QUERY"
    assert repository.calls == []


async def test_the_resolved_excerpt_is_bounded() -> None:
    view = _view(content="word " * 400)
    resolver, _ = _resolver(view)

    resolution = await resolver.resolve(tenant_id=TENANT, citation_id=_citation(view))

    assert resolution.resolved is True
    assert resolution.excerpt is not None
    assert len(resolution.excerpt) <= 481
    assert resolution.excerpt.endswith("…")
