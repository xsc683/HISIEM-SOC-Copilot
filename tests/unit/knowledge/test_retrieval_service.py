"""KnowledgeRetrievalService tests over hand-written in-memory doubles.

The doubles here are deliberately LOCAL to this module: they are knowledge
retrieval specific (candidate channels, embedding profile identity) and the
assertions read back the exact calls the service made, so a fake that drifts
from the port contract shows up as a failing expectation rather than a silent
pass.

Covers the tenant-scoped entry point, the fail-closed embedding profile gate,
per-mode retrieval profiles, candidate budgets, diversification, determinism,
and the citation handle returned with every hit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID

import pytest

from hisiem_soc_copilot.application.errors import (
    InvalidKnowledgeQueryError,
    KnowledgeRetrievalUnavailableError,
)
from hisiem_soc_copilot.application.ports.embedding import (
    EmbeddingBatch,
    EmbeddingProfileDescriptor,
    EmbeddingVector,
)
from hisiem_soc_copilot.application.ports.knowledge import (
    ChunkProjectionState,
    EmbeddingProfileRecord,
    KnowledgeChunkRecord,
    KnowledgeChunkView,
    KnowledgeQuery,
    LexicalCandidate,
    VectorCandidate,
)
from hisiem_soc_copilot.application.services.knowledge_retrieval import (
    HybridRetrievalConfig,
    KnowledgeRetrievalService,
    RetrievalMode,
)
from hisiem_soc_copilot.domain.knowledge.enums import SourceKind, Visibility
from hisiem_soc_copilot.domain.knowledge.value_objects import parse_citation_id

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"
T0 = datetime(2026, 9, 12, 10, 0, 0, tzinfo=UTC)

PROVIDER = "openai-compatible"
MODEL_ID = "text-embedding-test"
DIMENSION = 4
PROFILE_ID = UUID("33333333-3333-4333-8333-333333333333")

DOC_A = UUID(int=901)
DOC_B = UUID(int=902)
DOC_C = UUID(int=903)
VERSION_A = UUID(int=801)


# ---------------------------------------------------------------------------
# in-memory doubles
# ---------------------------------------------------------------------------


class FakeChunkRepository:
    """In-memory ``KnowledgeChunkRepository`` double.

    Candidate channels return whatever fixtures the test injected; the call log
    is the assertion surface (which tenant, which terms, which budget).
    """

    def __init__(
        self,
        *,
        lexical: Sequence[LexicalCandidate] = (),
        vectors: Sequence[VectorCandidate] = (),
        views: Mapping[UUID, KnowledgeChunkView] | None = None,
    ) -> None:
        self.lexical = tuple(lexical)
        self.vectors = tuple(vectors)
        self.views = dict(views or {})
        self.lexical_calls: list[tuple[str, tuple[str, ...], int]] = []
        self.vector_calls: list[tuple[str, UUID, tuple[float, ...], int]] = []

    async def lexical_candidates(
        self, *, tenant_id: str, search_terms: Sequence[str], limit: int
    ) -> tuple[LexicalCandidate, ...]:
        self.lexical_calls.append((tenant_id, tuple(search_terms), limit))
        return self.lexical

    async def vector_candidates(
        self,
        *,
        tenant_id: str,
        embedding_profile_id: UUID,
        query_vector: Sequence[float],
        limit: int,
    ) -> tuple[VectorCandidate, ...]:
        self.vector_calls.append((tenant_id, embedding_profile_id, tuple(query_vector), limit))
        return self.vectors

    async def get_chunk_view(
        self, *, tenant_id: str, chunk_id: UUID
    ) -> KnowledgeChunkView | None:
        return self.views.get(chunk_id)

    async def add_many(self, *, chunks: Sequence[KnowledgeChunkRecord]) -> None:
        raise NotImplementedError("retrieval never writes chunks")

    async def count_for_version(self, *, document_version_id: UUID) -> int:
        raise NotImplementedError("retrieval never counts chunks")

    async def projection_state(self, *, document_version_id: UUID) -> ChunkProjectionState:
        raise NotImplementedError("retrieval never inspects projection state")

    async def delete_for_version(self, *, document_version_id: UUID) -> None:
        raise NotImplementedError("retrieval never deletes chunks")


class FakeEmbeddingProfileRepository:
    """In-memory ``EmbeddingProfileRepository`` double with a call counter."""

    def __init__(self, *, active: EmbeddingProfileRecord | None = None) -> None:
        self.active = active
        self.get_active_calls = 0

    async def get_active(self) -> EmbeddingProfileRecord | None:
        self.get_active_calls += 1
        return self.active

    async def find_by_identity(
        self,
        *,
        provider: str,
        model_id: str,
        dimension: int,
        distance_metric: str,
        normalization: str,
        profile_version: int,
    ) -> EmbeddingProfileRecord | None:
        raise NotImplementedError("retrieval only reads the ACTIVE profile")

    async def add(self, *, profile: EmbeddingProfileRecord) -> None:
        raise NotImplementedError("retrieval never registers a profile")

    async def retire_active(self, *, retired_at: datetime) -> None:
        raise NotImplementedError("retrieval never retires a profile")


class FakeEmbeddingProvider:
    """A deterministic ``EmbeddingProvider`` double that records every call."""

    def __init__(
        self,
        *,
        descriptor: EmbeddingProfileDescriptor,
        vector: EmbeddingVector | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._vector = vector
        self.query_calls: list[str] = []
        self.document_calls: list[tuple[str, ...]] = []

    @property
    def descriptor(self) -> EmbeddingProfileDescriptor:
        return self._descriptor

    async def embed_query(self, text: str) -> EmbeddingVector:
        self.query_calls.append(text)
        if self._vector is not None:
            return self._vector
        return EmbeddingVector(
            values=_values(self._descriptor.dimension), descriptor=self._descriptor
        )

    async def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        self.document_calls.append(tuple(texts))
        return EmbeddingBatch(
            descriptor=self._descriptor,
            vectors=tuple(
                EmbeddingVector(
                    values=_values(self._descriptor.dimension),
                    descriptor=self._descriptor,
                    index=index,
                )
                for index, _ in enumerate(texts)
            ),
        )


class FakeClock:
    """A frozen clock: retrieval timestamps must be reproducible."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def utc_now(self) -> datetime:
        return self._now


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _values(dimension: int) -> tuple[float, ...]:
    return tuple(0.25 for _ in range(dimension))


def _profile_record(
    *, dimension: int = DIMENSION, model_id: str = MODEL_ID, normalization: str = "NONE"
) -> EmbeddingProfileRecord:
    return EmbeddingProfileRecord(
        id=PROFILE_ID,
        provider=PROVIDER,
        model_id=model_id,
        dimension=dimension,
        distance_metric="COSINE",
        normalization=normalization,
        profile_version=1,
        status="ACTIVE",
        created_at=T0,
        retired_at=None,
    )


def _descriptor(
    *, dimension: int = DIMENSION, model_id: str = MODEL_ID, normalization: str = "NONE"
) -> EmbeddingProfileDescriptor:
    return EmbeddingProfileDescriptor(
        provider=PROVIDER,
        model_id=model_id,
        dimension=dimension,
        distance_metric="COSINE",
        normalization=normalization,
        profile_version=1,
    )


def _provider(
    *,
    descriptor: EmbeddingProfileDescriptor | None = None,
    vector: EmbeddingVector | None = None,
) -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider(
        descriptor=descriptor if descriptor is not None else _descriptor(),
        vector=vector,
    )


def _view(
    *,
    index: int,
    document_id: UUID = DOC_A,
    document_version_id: UUID = VERSION_A,
    ordinal: int = 0,
    content: str = "T1110 brute force guidance on the sshd authentication_failure path.",
) -> KnowledgeChunkView:
    return KnowledgeChunkView(
        chunk_id=UUID(int=index),
        document_id=document_id,
        document_version_id=document_version_id,
        ordinal=ordinal,
        heading_path="Detection",
        content=content,
        content_hash=f"{index:064x}",
        title="Brute Force Guidance",
        source_kind=SourceKind.CURATED_GUIDANCE,
        language="en",
        source_version="2026.09",
        document_status="ACTIVE",
        visibility=Visibility.GLOBAL,
        tenant_id=None,
    )


def _views(document_id: UUID, count: int, *, start: int) -> tuple[KnowledgeChunkView, ...]:
    return tuple(
        _view(index=start + ordinal, document_id=document_id, ordinal=ordinal)
        for ordinal in range(count)
    )


def _lexical(view: KnowledgeChunkView, rank: float = 1.0) -> LexicalCandidate:
    return LexicalCandidate(view=view, rank=rank)


def _vector(view: KnowledgeChunkView, distance: float = 0.0) -> VectorCandidate:
    return VectorCandidate(view=view, distance=distance)


def _query(*, limit: int = 5) -> KnowledgeQuery:
    return KnowledgeQuery(topic="T1110 brute force", context_terms=("sshd",), limit=limit)


def _service(
    *,
    chunks: FakeChunkRepository | None = None,
    profiles: FakeEmbeddingProfileRepository | None = None,
    provider: FakeEmbeddingProvider | None = None,
    config: HybridRetrievalConfig | None = None,
) -> KnowledgeRetrievalService:
    return KnowledgeRetrievalService(
        chunks=chunks if chunks is not None else FakeChunkRepository(),
        embedding_profiles=(
            profiles if profiles is not None else FakeEmbeddingProfileRepository()
        ),
        embedding_provider=provider,
        clock=FakeClock(T0),
        config=config,
    )


def _active_profile_service(
    *,
    chunks: FakeChunkRepository | None = None,
    provider: FakeEmbeddingProvider | None = None,
    config: HybridRetrievalConfig | None = None,
) -> KnowledgeRetrievalService:
    return _service(
        chunks=chunks,
        profiles=FakeEmbeddingProfileRepository(active=_profile_record()),
        provider=provider if provider is not None else _provider(),
        config=config,
    )


# ---------------------------------------------------------------------------
# tenant scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tenant_id", ["", "   ", "\t\n "])
async def test_retrieve_rejects_an_empty_tenant_id(tenant_id: str) -> None:
    chunks = FakeChunkRepository()
    service = _service(chunks=chunks)

    with pytest.raises(InvalidKnowledgeQueryError) as excinfo:
        await service.retrieve(tenant_id=tenant_id, query=_query())

    assert excinfo.value.code == "INVALID_KNOWLEDGE_QUERY"
    assert chunks.lexical_calls == []
    assert chunks.vector_calls == []


async def test_lexical_candidates_are_requested_for_the_callers_tenant() -> None:
    chunks = FakeChunkRepository(lexical=(_lexical(_view(index=1)),))
    service = _service(chunks=chunks)

    await service.retrieve(
        tenant_id=OTHER_TENANT, query=_query(), mode=RetrievalMode.LEXICAL_ONLY
    )

    assert chunks.lexical_calls[0][0] == OTHER_TENANT


# ---------------------------------------------------------------------------
# LEXICAL_ONLY -- no embedding dependency at all
# ---------------------------------------------------------------------------


async def test_lexical_only_never_touches_the_embedding_provider() -> None:
    chunks = FakeChunkRepository(lexical=(_lexical(_view(index=1)),))
    profiles = FakeEmbeddingProfileRepository(active=None)
    provider = _provider()
    service = _service(chunks=chunks, profiles=profiles, provider=provider)

    result = await service.retrieve(
        tenant_id=TENANT, query=_query(), mode=RetrievalMode.LEXICAL_ONLY
    )

    assert provider.query_calls == []
    assert provider.document_calls == []
    # Not even the profile table is consulted: LEXICAL_ONLY has no embedding
    # dependency, so it works on a deployment with no ACTIVE profile.
    assert profiles.get_active_calls == 0
    assert len(result.hits) == 1


async def test_lexical_only_profile_carries_no_embedding_identity() -> None:
    service = _service(chunks=FakeChunkRepository())

    result = await service.retrieve(
        tenant_id=TENANT, query=_query(), mode=RetrievalMode.LEXICAL_ONLY
    )

    profile = result.retrieval_profile
    assert profile is not None
    assert profile.profile_id == "lexical-v1"
    assert profile.embedding_profile_id == ""
    assert profile.embedding_model_id == ""
    assert profile.is_hybrid is False


# ---------------------------------------------------------------------------
# the embedding profile gate -- fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [RetrievalMode.VECTOR_ONLY, RetrievalMode.HYBRID])
async def test_vector_channels_require_an_active_embedding_profile(mode: RetrievalMode) -> None:
    chunks = FakeChunkRepository()
    provider = _provider()
    service = _service(
        chunks=chunks, profiles=FakeEmbeddingProfileRepository(active=None), provider=provider
    )

    with pytest.raises(KnowledgeRetrievalUnavailableError) as excinfo:
        await service.retrieve(tenant_id=TENANT, query=_query(), mode=mode)

    assert excinfo.value.code == "KNOWLEDGE_RETRIEVAL_UNAVAILABLE"
    assert "ACTIVE embedding profile" in str(excinfo.value)
    assert provider.query_calls == []
    assert chunks.lexical_calls == []
    assert chunks.vector_calls == []


@pytest.mark.parametrize("mode", [RetrievalMode.VECTOR_ONLY, RetrievalMode.HYBRID])
async def test_vector_channels_require_an_embedding_provider(mode: RetrievalMode) -> None:
    chunks = FakeChunkRepository()
    service = _service(
        chunks=chunks,
        profiles=FakeEmbeddingProfileRepository(active=_profile_record()),
        provider=None,
    )

    with pytest.raises(KnowledgeRetrievalUnavailableError) as excinfo:
        await service.retrieve(tenant_id=TENANT, query=_query(), mode=mode)

    assert excinfo.value.code == "KNOWLEDGE_RETRIEVAL_UNAVAILABLE"
    assert "no embedding provider" in str(excinfo.value)
    assert chunks.lexical_calls == []


@pytest.mark.parametrize(
    ("dimension", "model_id", "normalization"),
    [
        (DIMENSION * 2, MODEL_ID, "NONE"),  # different dimension
        (DIMENSION, "some-other-model", "NONE"),  # different model
        (DIMENSION, MODEL_ID, "L2"),  # different normalization
    ],
)
async def test_hybrid_rejects_a_provider_from_a_different_space(
    dimension: int, model_id: str, normalization: str
) -> None:
    chunks = FakeChunkRepository(lexical=(_lexical(_view(index=1)),))
    provider = _provider(
        descriptor=_descriptor(
            dimension=dimension, model_id=model_id, normalization=normalization
        )
    )
    service = _service(
        chunks=chunks,
        profiles=FakeEmbeddingProfileRepository(active=_profile_record()),
        provider=provider,
    )

    with pytest.raises(KnowledgeRetrievalUnavailableError) as excinfo:
        await service.retrieve(tenant_id=TENANT, query=_query(), mode=RetrievalMode.HYBRID)

    assert excinfo.value.code == "KNOWLEDGE_RETRIEVAL_UNAVAILABLE"
    assert "different spaces" in str(excinfo.value)
    # Nothing was generated: a mismatched space is refused before any candidate
    # is fetched, let alone compared.
    assert chunks.lexical_calls == []
    assert chunks.vector_calls == []
    assert provider.query_calls == []


async def test_hybrid_rejects_a_returned_vector_from_another_space() -> None:
    """A wrong-dimension response is caught by the profile-identity guard.

    ``_vector_candidates`` compares the returned vector's descriptor identity
    before the explicit dimension comparison, and that identity includes the
    dimension -- so the identity guard is what fires here and the dimension check
    behind it is unreachable. Either way the call fails closed instead of
    comparing vectors from two spaces.
    """
    chunks = FakeChunkRepository(lexical=(_lexical(_view(index=1)),))
    provider = _provider(
        vector=EmbeddingVector(
            values=_values(DIMENSION * 2), descriptor=_descriptor(dimension=DIMENSION * 2)
        )
    )
    service = _active_profile_service(chunks=chunks, provider=provider)

    with pytest.raises(KnowledgeRetrievalUnavailableError) as excinfo:
        await service.retrieve(tenant_id=TENANT, query=_query(), mode=RetrievalMode.HYBRID)

    assert excinfo.value.code == "KNOWLEDGE_RETRIEVAL_UNAVAILABLE"
    assert "different profile" in str(excinfo.value)
    assert chunks.vector_calls == []


# ---------------------------------------------------------------------------
# retrieval profile echo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected_profile_id"),
    [
        (RetrievalMode.LEXICAL_ONLY, "lexical-v1"),
        (RetrievalMode.VECTOR_ONLY, "vector-v1"),
        (RetrievalMode.HYBRID, "hybrid-v1"),
    ],
)
async def test_each_mode_reports_its_own_frozen_profile_id(
    mode: RetrievalMode, expected_profile_id: str
) -> None:
    service = _active_profile_service()

    result = await service.retrieve(tenant_id=TENANT, query=_query(), mode=mode)

    assert result.profile_id == expected_profile_id
    assert result.retrieval_profile is not None


async def test_retrieve_defaults_to_hybrid() -> None:
    service = _active_profile_service()

    result = await service.retrieve(tenant_id=TENANT, query=_query())

    assert result.profile_id == "hybrid-v1"


async def test_result_echoes_the_configured_profile_knobs() -> None:
    config = HybridRetrievalConfig(
        lexical_candidate_limit=7,
        vector_candidate_limit=3,
        rrf_k=13,
        max_chunks_per_document=1,
        chunker_version="test-chunker-v1",
    )
    chunks = FakeChunkRepository(
        lexical=(_lexical(_view(index=1)),),
        vectors=(_vector(_view(index=1)),),
    )
    service = _active_profile_service(chunks=chunks, config=config)

    result = await service.retrieve(tenant_id=TENANT, query=_query(), mode=RetrievalMode.HYBRID)

    profile = result.retrieval_profile
    assert profile is not None
    assert profile.profile_id == "hybrid-v1"
    assert profile.lexical_candidate_limit == 7
    assert profile.vector_candidate_limit == 3
    assert profile.rrf_k == 13
    assert profile.max_chunks_per_document == 1
    assert profile.chunker_version == "test-chunker-v1"
    assert profile.embedding_profile_id == str(PROFILE_ID)
    assert profile.embedding_model_id == MODEL_ID
    assert profile.is_hybrid is True
    # The budgets are not merely echoed: they were the limits actually passed on.
    assert chunks.lexical_calls[0][2] == 7
    assert chunks.vector_calls[0][3] == 3
    assert chunks.vector_calls[0][1] == PROFILE_ID


async def test_service_exposes_its_configuration() -> None:
    config = HybridRetrievalConfig(rrf_k=11)

    assert _active_profile_service(config=config).config is config


# ---------------------------------------------------------------------------
# candidate channels
# ---------------------------------------------------------------------------


async def test_lexical_channel_receives_the_derived_search_terms() -> None:
    chunks = FakeChunkRepository()
    service = _service(chunks=chunks, config=HybridRetrievalConfig(lexical_candidate_limit=17))
    query = KnowledgeQuery(
        topic="T1110  brute   force", context_terms=("sshd  logs", "T1110"), limit=5
    )

    await service.retrieve(tenant_id=TENANT, query=query, mode=RetrievalMode.LEXICAL_ONLY)

    assert chunks.lexical_calls == [(TENANT, ("T1110", "brute", "force", "sshd", "logs"), 17)]


async def test_vector_channel_embeds_the_joined_search_text() -> None:
    chunks = FakeChunkRepository(vectors=(_vector(_view(index=1)),))
    provider = _provider()
    service = _active_profile_service(chunks=chunks, provider=provider)

    await service.retrieve(tenant_id=TENANT, query=_query(), mode=RetrievalMode.VECTOR_ONLY)

    assert provider.query_calls == ["T1110 brute force sshd"]
    assert chunks.vector_calls[0][2] == _values(DIMENSION)


async def test_hybrid_runs_both_channels_and_fuses_them() -> None:
    lexical_view = _view(index=1)
    shared_view = _view(index=2)
    vector_view = _view(index=3)
    chunks = FakeChunkRepository(
        lexical=(_lexical(lexical_view, 0.9), _lexical(shared_view, 0.4)),
        vectors=(_vector(shared_view, 0.1), _vector(vector_view, 0.2)),
    )
    service = _active_profile_service(chunks=chunks)

    result = await service.retrieve(tenant_id=TENANT, query=_query(), mode=RetrievalMode.HYBRID)

    assert len(chunks.lexical_calls) == 1
    assert len(chunks.vector_calls) == 1
    # The chunk found by both channels wins the fusion.
    assert result.hits[0].chunk_id == shared_view.chunk_id
    assert {hit.chunk_id for hit in result.hits} == {
        lexical_view.chunk_id,
        shared_view.chunk_id,
        vector_view.chunk_id,
    }


# ---------------------------------------------------------------------------
# caps, truncation, determinism
# ---------------------------------------------------------------------------


async def test_results_are_capped_by_the_query_limit_and_the_per_document_cap() -> None:
    views = (
        _views(DOC_A, 4, start=1) + _views(DOC_B, 4, start=11) + _views(DOC_C, 4, start=21)
    )
    service = _service(
        chunks=FakeChunkRepository(lexical=tuple(_lexical(view) for view in views))
    )

    result = await service.retrieve(
        tenant_id=TENANT, query=_query(limit=3), mode=RetrievalMode.LEXICAL_ONLY
    )

    assert len(result.hits) == 3
    assert [hit.document_id for hit in result.hits] == [DOC_A, DOC_A, DOC_B]
    assert result.truncated is True


async def test_a_small_corpus_relaxes_the_per_document_cap_to_fill_the_page() -> None:
    views = _views(DOC_A, 3, start=1)
    service = _service(
        chunks=FakeChunkRepository(lexical=tuple(_lexical(view) for view in views))
    )

    result = await service.retrieve(
        tenant_id=TENANT, query=_query(limit=5), mode=RetrievalMode.LEXICAL_ONLY
    )

    assert [hit.chunk_id for hit in result.hits] == [view.chunk_id for view in views]
    assert result.truncated is False


async def test_truncated_is_false_when_every_candidate_is_returned() -> None:
    views = _views(DOC_A, 2, start=1)
    service = _service(
        chunks=FakeChunkRepository(lexical=tuple(_lexical(view) for view in views))
    )

    result = await service.retrieve(
        tenant_id=TENANT, query=_query(limit=5), mode=RetrievalMode.LEXICAL_ONLY
    )

    assert len(result.hits) == 2
    assert result.truncated is False


async def test_an_empty_corpus_returns_an_empty_result_with_a_profile() -> None:
    service = _service(chunks=FakeChunkRepository())

    result = await service.retrieve(
        tenant_id=TENANT, query=_query(), mode=RetrievalMode.LEXICAL_ONLY
    )

    assert result.hits == ()
    assert result.truncated is False
    assert result.profile_id == "lexical-v1"


async def test_repeated_lexical_calls_return_the_same_ordering() -> None:
    views = _views(DOC_A, 3, start=1) + _views(DOC_B, 3, start=11)
    service = _service(
        chunks=FakeChunkRepository(lexical=tuple(_lexical(view) for view in views))
    )

    first = await service.retrieve(
        tenant_id=TENANT, query=_query(limit=5), mode=RetrievalMode.LEXICAL_ONLY
    )
    second = await service.retrieve(
        tenant_id=TENANT, query=_query(limit=5), mode=RetrievalMode.LEXICAL_ONLY
    )

    assert [hit.chunk_id for hit in first.hits] == [hit.chunk_id for hit in second.hits]
    assert first == second


async def test_repeated_hybrid_calls_return_the_same_ordering() -> None:
    views = _views(DOC_A, 3, start=1) + _views(DOC_B, 3, start=11)
    chunks = FakeChunkRepository(
        lexical=tuple(_lexical(view) for view in views),
        vectors=tuple(_vector(view) for view in reversed(views)),
    )
    service = _active_profile_service(chunks=chunks)

    first = await service.retrieve(tenant_id=TENANT, query=_query(limit=5))
    second = await service.retrieve(tenant_id=TENANT, query=_query(limit=5))

    assert [hit.chunk_id for hit in first.hits] == [hit.chunk_id for hit in second.hits]
    assert first == second


# ---------------------------------------------------------------------------
# hits
# ---------------------------------------------------------------------------


async def test_hit_citation_id_round_trips_to_the_chunk_id() -> None:
    view = _view(index=1)
    service = _service(chunks=FakeChunkRepository(lexical=(_lexical(view),)))

    result = await service.retrieve(
        tenant_id=TENANT, query=_query(), mode=RetrievalMode.LEXICAL_ONLY
    )

    hit = result.hits[0]
    parsed = parse_citation_id(hit.citation_id)
    assert parsed is not None
    assert parsed.chunk_id == hit.chunk_id == view.chunk_id
    assert parsed.content_hash_prefix == view.content_hash[:12]
    assert hit.citation_id.startswith("kcit:")


async def test_hit_carries_the_version_facts_and_a_bounded_excerpt() -> None:
    view = _view(index=1, content="T1110   brute\n\nforce   guidance.")
    service = _service(chunks=FakeChunkRepository(lexical=(_lexical(view),)))

    result = await service.retrieve(
        tenant_id=TENANT, query=_query(), mode=RetrievalMode.LEXICAL_ONLY
    )

    hit = result.hits[0]
    assert hit.document_id == view.document_id
    assert hit.document_version_id == view.document_version_id
    assert hit.title == view.title
    assert hit.language == view.language
    assert hit.source_version == view.source_version
    assert hit.source_kind is SourceKind.CURATED_GUIDANCE
    assert hit.excerpt == "T1110 brute force guidance."
    assert hit.retrieved_at == T0


async def test_hits_are_truncated_excerpts_never_full_documents() -> None:
    view = _view(index=1, content="word " * 400)
    service = _service(chunks=FakeChunkRepository(lexical=(_lexical(view),)))

    result = await service.retrieve(
        tenant_id=TENANT, query=_query(), mode=RetrievalMode.LEXICAL_ONLY
    )

    excerpt = result.hits[0].excerpt
    assert len(excerpt) <= 481
    assert excerpt.endswith("…")
