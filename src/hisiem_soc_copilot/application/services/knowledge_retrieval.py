"""Deterministic hybrid retrieval over the knowledge corpus (brief sections 39-53).

Everything in this module is pure and deterministic except the two repository
calls and the embedding call it orchestrates. That is deliberate: ranking is the
part of retrieval that must be reproducible, so ranking is plain functions of
plain data, testable without a database.

The shape of the pipeline:

1. **Normalize** the caller's query into bounded terms. Rejection, never repair.
2. **Generate candidates** independently -- PostgreSQL FTS and exact pgvector
   nearest-neighbour -- each with its own budget (sections 44/45).
3. **Fuse** the two ranked lists with Reciprocal Rank Fusion (section 47).
4. **Diversify** so one document cannot monopolize the result set (section 49).

Position on authority: a hit is RANKING EVIDENCE plus a validated handle. The
retrieval score is a ranking signal, not a judgement; the excerpt is data, not
instruction; the citation is a reference the resolver re-validates, not a grant
(section 92).
"""

from __future__ import annotations

import enum
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from ...domain.knowledge.entities import require_valid_scope
from ...domain.knowledge.enums import Visibility
from ...domain.knowledge.value_objects import (
    CHUNKER_VERSION,
    format_citation_id,
    parse_citation_id,
)
from ..errors import InvalidKnowledgeQueryError, KnowledgeRetrievalUnavailableError
from ..ports.clock import ClockPort
from ..ports.embedding import EmbeddingProvider
from ..ports.knowledge import (
    LEXICAL_PROFILE_ID,
    MAX_RESULT_LIMIT,
    RETRIEVAL_PROFILE_ID,
    VECTOR_PROFILE_ID,
    CitationResolution,
    EmbeddingProfileRecord,
    EmbeddingProfileRepository,
    KnowledgeChunkRepository,
    KnowledgeChunkView,
    KnowledgeHit,
    KnowledgeQuery,
    KnowledgeSearchResult,
    LexicalCandidate,
    RetrievalProfile,
    VectorCandidate,
)

#: Reciprocal Rank Fusion constant (brief section 47). 60 is the value from the
#: original RRF paper and is frozen here: changing it changes every ranking.
RRF_K = 60

#: Independent candidate budgets per channel. Two channels of 20 fused down to a
#: final result set of at most 5 (sections 44/45/49).
LEXICAL_CANDIDATE_LIMIT = 20
VECTOR_CANDIDATE_LIMIT = 20

#: A single document may contribute at most this many chunks before the
#: diversification pass falls back to the remaining candidates.
MAX_CHUNKS_PER_DOCUMENT = 2

#: Query bounds (section 41). These are REJECTIONS: a query that does not fit is
#: refused rather than quietly reshaped into a different question.
MAX_TOPIC_CHARS = 256
MAX_CONTEXT_TERMS = 12
MAX_CONTEXT_TERM_CHARS = 64
#: Backstop on the derived token list, unreachable for a query that satisfies the
#: bounds above; it exists so a pathological input cannot become a 10k-term
#: tsquery.
MAX_SEARCH_TERMS = 96

#: Retrieved excerpts are truncated to this many characters. Bounded so a hit is
#: a pointer to the content, never a way to smuggle a whole document into a
#: future model context.
MAX_EXCERPT_CHARS = 480

#: The six fields that together identify an embedding SPACE. Two vectors are only
#: comparable when these all match (brief section 16).
EmbeddingProfileIdentity = tuple[str, str, int, str, str, int]


def _identity_of(record: EmbeddingProfileRecord) -> EmbeddingProfileIdentity:
    """The comparable identity of a persisted embedding profile."""
    return (
        record.provider,
        record.model_id,
        record.dimension,
        record.distance_metric,
        record.normalization,
        record.profile_version,
    )


class RetrievalMode(enum.StrEnum):
    """Which candidate generators run. Production uses ``HYBRID``.

    The single-channel modes exist so retrieval quality can be ATTRIBUTED: an
    evaluation can tell whether hybrid actually added value over the better of
    the two baselines instead of asserting it (brief sections 57/61).
    """

    LEXICAL_ONLY = "LEXICAL_ONLY"
    VECTOR_ONLY = "VECTOR_ONLY"
    HYBRID = "HYBRID"


_MODE_PROFILE_ID: dict[RetrievalMode, str] = {
    RetrievalMode.LEXICAL_ONLY: LEXICAL_PROFILE_ID,
    RetrievalMode.VECTOR_ONLY: VECTOR_PROFILE_ID,
    RetrievalMode.HYBRID: RETRIEVAL_PROFILE_ID,
}


@dataclass(frozen=True)
class HybridRetrievalConfig:
    """The frozen retrieval knobs (brief section 50)."""

    lexical_candidate_limit: int = LEXICAL_CANDIDATE_LIMIT
    vector_candidate_limit: int = VECTOR_CANDIDATE_LIMIT
    rrf_k: int = RRF_K
    max_chunks_per_document: int = MAX_CHUNKS_PER_DOCUMENT
    chunker_version: str = CHUNKER_VERSION

    def __post_init__(self) -> None:
        if self.lexical_candidate_limit < 1 or self.vector_candidate_limit < 1:
            raise ValueError("candidate limits must be >= 1")
        if self.rrf_k < 1:
            raise ValueError("rrf_k must be >= 1")
        if self.max_chunks_per_document < 1:
            raise ValueError("max_chunks_per_document must be >= 1")
        if not self.chunker_version.strip():
            raise ValueError("chunker_version must not be empty")


# ---------------------------------------------------------------------------
# Query normalization (brief section 41)
# ---------------------------------------------------------------------------


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def normalize_topic(topic: str) -> str:
    """Unicode-normalize, trim, and collapse a topic, rejecting empty/oversized.

    Deliberately does NOT lower-case, stem, or strip punctuation: security
    identifiers such as ``T1110``, ``sshd``, and ``authentication_failure`` must
    survive normalization byte-identical (section 42).
    """
    if not isinstance(topic, str):
        raise InvalidKnowledgeQueryError("topic must be a string")
    normalized = _collapse_whitespace(unicodedata.normalize("NFC", topic))
    if not normalized:
        raise InvalidKnowledgeQueryError("topic must not be empty")
    if len(normalized) > MAX_TOPIC_CHARS:
        raise InvalidKnowledgeQueryError(
            f"topic must be <= {MAX_TOPIC_CHARS} characters (actual={len(normalized)})"
        )
    return normalized


def normalize_context_terms(context_terms: Iterable[str]) -> tuple[str, ...]:
    """Normalize, bound, and DEDUPE context terms while preserving first order.

    Stable ordering matters: the lexical query and the vector query text are both
    derived from this tuple, so an unstable order would make the same question
    produce two different searches (section 41/48).
    """
    terms: list[str] = []
    if context_terms is None:
        return ()
    for raw in context_terms:
        if not isinstance(raw, str):
            raise InvalidKnowledgeQueryError("context terms must be strings")
        term = _collapse_whitespace(unicodedata.normalize("NFC", raw))
        if not term:
            continue
        if len(term) > MAX_CONTEXT_TERM_CHARS:
            raise InvalidKnowledgeQueryError(
                f"context term must be <= {MAX_CONTEXT_TERM_CHARS} characters "
                f"(actual={len(term)})"
            )
        if term not in terms:
            terms.append(term)
    if len(terms) > MAX_CONTEXT_TERMS:
        raise InvalidKnowledgeQueryError(
            f"at most {MAX_CONTEXT_TERMS} context terms are allowed (actual={len(terms)})"
        )
    return tuple(terms)


def normalize_query(query: KnowledgeQuery) -> KnowledgeQuery:
    """Return a bounded, normalized copy of ``query`` (rejecting anything invalid)."""
    if not 1 <= query.limit <= MAX_RESULT_LIMIT:
        raise InvalidKnowledgeQueryError(
            f"limit must be between 1 and {MAX_RESULT_LIMIT} (actual={query.limit})"
        )
    return KnowledgeQuery(
        topic=normalize_topic(query.topic),
        context_terms=normalize_context_terms(query.context_terms),
        limit=query.limit,
    )


def build_search_text(query: KnowledgeQuery) -> str:
    """The single query string used for the VECTOR channel.

    A fixed, deterministic formatting of topic then context terms -- no
    separators, weights, or prompt scaffolding that a future model could
    influence.
    """
    parts = [query.topic, *query.context_terms]
    return _collapse_whitespace(" ".join(parts))


def build_search_terms(query: KnowledgeQuery) -> tuple[str, ...]:
    """The deduped term list used for the LEXICAL channel.

    Terms are split on whitespace and combined with OR by the repository, because
    ``plainto_tsquery`` ANDs its input: requiring one chunk to contain every word
    of a question would suppress precisely the semantic matches hybrid retrieval
    is supposed to add.
    """
    terms: list[str] = []
    for part in (query.topic, *query.context_terms):
        for token in part.split():
            if token and token not in terms:
                terms.append(token)
    if len(terms) > MAX_SEARCH_TERMS:
        raise InvalidKnowledgeQueryError(
            f"query expands to more than {MAX_SEARCH_TERMS} search terms"
        )
    return tuple(terms)


# ---------------------------------------------------------------------------
# Ranking (brief sections 47-49)
# ---------------------------------------------------------------------------


def _stable_key(view: KnowledgeChunkView) -> tuple[str, str, int]:
    """The documented tie-break order: document, version, ordinal (section 48)."""
    return (str(view.document_id), str(view.document_version_id), view.ordinal)


def reciprocal_rank_fusion(
    *ranked_lists: Sequence[LexicalCandidate | VectorCandidate],
    k: int = RRF_K,
) -> tuple[KnowledgeChunkView, ...]:
    """Fuse ranked candidate lists by Reciprocal Rank Fusion.

    ``score(d) = sum over lists of 1 / (k + rank_i(d))`` with 1-based ranks.

    RRF is used instead of a weighted sum of raw scores because the two channels
    produce numbers on incomparable scales: a ``ts_rank`` and a cosine distance
    have no shared unit, so any mixing coefficient would be a magic number
    dressed up as a parameter (section 47).

    With a single list this degenerates to "keep that list's order", since RRF is
    strictly monotone in rank and ranks within one list are distinct. That is why
    the single-channel modes need no separate code path.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    scores: dict[UUID, float] = {}
    views: dict[UUID, KnowledgeChunkView] = {}
    for candidates in ranked_lists:
        for rank, candidate in enumerate(candidates, start=1):
            key = candidate.view.chunk_id
            views.setdefault(key, candidate.view)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores.items(), key=lambda item: (-item[1], _stable_key(views[item[0]])))
    return tuple(views[chunk_id] for chunk_id, _ in ordered)


@dataclass(frozen=True)
class DiversificationOutcome:
    """The selected views plus whether candidates were dropped."""

    views: tuple[KnowledgeChunkView, ...]
    truncated: bool


def diversify(
    ranked: Sequence[KnowledgeChunkView],
    *,
    limit: int,
    max_per_document: int = MAX_CHUNKS_PER_DOCUMENT,
) -> DiversificationOutcome:
    """Cap how many chunks one document may contribute, then relax if needed.

    The first pass takes candidates in rank order while each document is under
    its cap. If that leaves the result set short, a second pass fills the
    remaining slots from the deferred candidates in rank order -- still
    deterministic, just less diversified, so a small corpus still returns a full
    page instead of an artificially empty one (section 49).
    """
    selected: list[KnowledgeChunkView] = []
    deferred: list[KnowledgeChunkView] = []
    per_document: dict[UUID, int] = {}
    for view in ranked:
        if len(selected) >= limit:
            break
        used = per_document.get(view.document_id, 0)
        if used < max_per_document:
            selected.append(view)
            per_document[view.document_id] = used + 1
        else:
            deferred.append(view)
    if len(selected) < limit:
        for view in deferred:
            if len(selected) >= limit:
                break
            selected.append(view)
    return DiversificationOutcome(
        views=tuple(selected),
        truncated=len(ranked) > len(selected),
    )


def build_excerpt(content: str, *, limit: int = MAX_EXCERPT_CHARS) -> str:
    """Collapse and truncate chunk content into a bounded, readable excerpt.

    The result is DATA. Nothing in this function interprets its input, so a chunk
    containing ``rm -rf`` or "ignore previous instructions" produces exactly the
    same kind of string as any other text (section 24).
    """
    collapsed = _collapse_whitespace(content)
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "…"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class KnowledgeRetrievalService:
    """Retrieval over the ACTIVE corpus, scoped to exactly one tenant.

    ``tenant_id`` is a required keyword on every entry point (section 40). There
    is no scope-less variant, so no future caller -- tool, CLI, or evaluation --
    can accidentally search the whole corpus.
    """

    def __init__(
        self,
        *,
        chunks: KnowledgeChunkRepository,
        embedding_profiles: EmbeddingProfileRepository,
        embedding_provider: EmbeddingProvider | None,
        clock: ClockPort,
        config: HybridRetrievalConfig | None = None,
    ) -> None:
        self._chunks = chunks
        self._embedding_profiles = embedding_profiles
        self._embedding_provider = embedding_provider
        self._clock = clock
        self._config = config or HybridRetrievalConfig()

    @property
    def config(self) -> HybridRetrievalConfig:
        return self._config

    async def retrieve(
        self,
        *,
        tenant_id: str,
        query: KnowledgeQuery,
        mode: RetrievalMode = RetrievalMode.HYBRID,
    ) -> KnowledgeSearchResult:
        """Run one retrieval and return ranked hits with resolvable citations."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise InvalidKnowledgeQueryError("tenant_id is required for retrieval")
        normalized = normalize_query(query)

        if mode is RetrievalMode.LEXICAL_ONLY:
            return await self._retrieve_lexical_only(tenant_id=tenant_id, query=normalized)
        if mode is RetrievalMode.VECTOR_ONLY:
            return await self._retrieve_vector_only(tenant_id=tenant_id, query=normalized)
        return await self._retrieve_hybrid(tenant_id=tenant_id, query=normalized)

    # -- single-channel retrievals ------------------------------------------

    async def _retrieve_lexical_only(
        self, *, tenant_id: str, query: KnowledgeQuery
    ) -> KnowledgeSearchResult:
        lexical = await self._lexical_candidates(tenant_id=tenant_id, query=query)
        profile = await self._describe_profile(
            mode=RetrievalMode.LEXICAL_ONLY, embedding_profile_id="", embedding_model_id=""
        )
        return self._finalize(
            tenant_id=tenant_id,
            query=query,
            ranked=reciprocal_rank_fusion(lexical, k=self._config.rrf_k),
            profile=profile,
        )

    async def _retrieve_vector_only(
        self, *, tenant_id: str, query: KnowledgeQuery
    ) -> KnowledgeSearchResult:
        profile_record, descriptor_identity = await self._require_active_profile()
        vector = await self._vector_candidates(
            tenant_id=tenant_id,
            query=query,
            profile=profile_record,
            descriptor_identity=descriptor_identity,
        )
        profile = await self._describe_profile(
            mode=RetrievalMode.VECTOR_ONLY,
            embedding_profile_id=str(profile_record.id),
            embedding_model_id=profile_record.model_id,
        )
        return self._finalize(
            tenant_id=tenant_id,
            query=query,
            ranked=reciprocal_rank_fusion(vector, k=self._config.rrf_k),
            profile=profile,
        )

    async def _retrieve_hybrid(
        self, *, tenant_id: str, query: KnowledgeQuery
    ) -> KnowledgeSearchResult:
        profile_record, descriptor_identity = await self._require_active_profile()
        lexical = await self._lexical_candidates(tenant_id=tenant_id, query=query)
        vector = await self._vector_candidates(
            tenant_id=tenant_id,
            query=query,
            profile=profile_record,
            descriptor_identity=descriptor_identity,
        )
        profile = await self._describe_profile(
            mode=RetrievalMode.HYBRID,
            embedding_profile_id=str(profile_record.id),
            embedding_model_id=profile_record.model_id,
        )
        return self._finalize(
            tenant_id=tenant_id,
            query=query,
            ranked=reciprocal_rank_fusion(lexical, vector, k=self._config.rrf_k),
            profile=profile,
        )

    # -- channel helpers ---------------------------------------------------

    async def _lexical_candidates(
        self, *, tenant_id: str, query: KnowledgeQuery
    ) -> tuple[LexicalCandidate, ...]:
        terms = build_search_terms(query)
        if not terms:
            return ()
        return await self._chunks.lexical_candidates(
            tenant_id=tenant_id,
            search_terms=terms,
            limit=self._config.lexical_candidate_limit,
        )

    async def _require_active_profile(
        self,
    ) -> tuple[EmbeddingProfileRecord, EmbeddingProfileIdentity]:
        """Resolve the one ACTIVE embedding profile, or fail loudly.

        Vector channels may only ever compare vectors produced in the SAME
        embedding space (section 16). There is exactly one ACTIVE profile, so
        "which space" is never a per-query decision.
        """
        record = await self._embedding_profiles.get_active()
        if record is None:
            raise KnowledgeRetrievalUnavailableError(
                "no ACTIVE embedding profile is configured; vector and hybrid "
                "retrieval are unavailable until one is registered"
            )
        provider = self._embedding_provider
        if provider is None:
            raise KnowledgeRetrievalUnavailableError(
                "no embedding provider is configured; vector and hybrid retrieval "
                "are unavailable"
            )
        expected = _identity_of(record)
        actual = provider.descriptor.identity
        if actual != expected:
            # Fail closed: embedding the query in a different space than the one
            # the chunks were indexed in would produce plausible-looking nonsense.
            raise KnowledgeRetrievalUnavailableError(
                "the configured embedding provider does not match the ACTIVE "
                "embedding profile; refusing to compare vectors from different spaces"
            )
        return record, expected

    async def _vector_candidates(
        self,
        *,
        tenant_id: str,
        query: KnowledgeQuery,
        profile: EmbeddingProfileRecord,
        descriptor_identity: EmbeddingProfileIdentity,
    ) -> tuple[VectorCandidate, ...]:
        provider = self._embedding_provider
        if provider is None:  # pragma: no cover - guarded by _require_active_profile
            raise KnowledgeRetrievalUnavailableError("no embedding provider is configured")
        embedded = await provider.embed_query(build_search_text(query))
        if embedded.descriptor.identity != descriptor_identity:
            raise KnowledgeRetrievalUnavailableError(
                "the embedding provider returned a vector from a different profile "
                "than the ACTIVE one"
            )
        if embedded.dimension != profile.dimension:
            raise KnowledgeRetrievalUnavailableError(
                f"embedding dimension mismatch: profile declares {profile.dimension}, "
                f"provider returned {embedded.dimension}"
            )
        return await self._chunks.vector_candidates(
            tenant_id=tenant_id,
            embedding_profile_id=profile.id,
            query_vector=list(embedded.values),
            limit=self._config.vector_candidate_limit,
        )

    # -- assembly ----------------------------------------------------------

    async def _describe_profile(
        self, *, mode: RetrievalMode, embedding_profile_id: str, embedding_model_id: str
    ) -> RetrievalProfile:
        return RetrievalProfile(
            profile_id=_MODE_PROFILE_ID[mode],
            lexical_candidate_limit=self._config.lexical_candidate_limit,
            vector_candidate_limit=self._config.vector_candidate_limit,
            rrf_k=self._config.rrf_k,
            max_chunks_per_document=self._config.max_chunks_per_document,
            chunker_version=self._config.chunker_version,
            embedding_profile_id=embedding_profile_id,
            embedding_model_id=embedding_model_id,
        )

    def _finalize(
        self,
        *,
        tenant_id: str,
        query: KnowledgeQuery,
        ranked: Sequence[KnowledgeChunkView],
        profile: RetrievalProfile,
    ) -> KnowledgeSearchResult:
        outcome = diversify(
            ranked,
            limit=query.limit,
            max_per_document=self._config.max_chunks_per_document,
        )
        retrieved_at: datetime = self._clock.utc_now()
        hits = tuple(
            self._to_hit(view, retrieved_at=retrieved_at) for view in outcome.views
        )
        return KnowledgeSearchResult(
            hits=hits,
            truncated=outcome.truncated,
            retrieval_profile=profile,
        )

    def _to_hit(self, view: KnowledgeChunkView, *, retrieved_at: datetime) -> KnowledgeHit:
        return KnowledgeHit(
            citation_id=format_citation_id(
                chunk_id=view.chunk_id, content_hash=view.content_hash
            ),
            document_id=view.document_id,
            document_version_id=view.document_version_id,
            chunk_id=view.chunk_id,
            source_kind=view.source_kind,
            title=view.title,
            excerpt=build_excerpt(view.content),
            language=view.language,
            source_version=view.source_version,
            retrieved_at=retrieved_at,
        )


# ---------------------------------------------------------------------------
# Citation resolution (brief sections 51-53)
# ---------------------------------------------------------------------------


class KnowledgeCitationResolver:
    """Re-validates a citation handle against the database.

    A citation is a LOOKUP KEY, never a claim. Nothing inside the citation string
    is trusted: the chunk is re-read, its content hash re-checked against the hash
    recorded in the string, and the scope re-verified as GLOBAL or the caller's
    own tenant.

    Unlike normal retrieval, resolution deliberately still works for RETIRED
    documents and for HISTORICAL versions (sections 52/53): a citation captured in
    a past investigation must remain explainable after the document it points at
    has been superseded or withdrawn.
    """

    def __init__(self, *, chunks: KnowledgeChunkRepository) -> None:
        self._chunks = chunks

    async def resolve(self, *, tenant_id: str, citation_id: str) -> CitationResolution:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise InvalidKnowledgeQueryError("tenant_id is required for resolution")
        parsed = parse_citation_id(citation_id)
        if parsed is None:
            return CitationResolution(
                citation_id=citation_id,
                resolved=False,
                reason="MALFORMED_CITATION",
            )
        view = await self._chunks.get_chunk_view(tenant_id=tenant_id, chunk_id=parsed.chunk_id)
        if view is None:
            return CitationResolution(
                citation_id=citation_id,
                resolved=False,
                reason="CHUNK_NOT_FOUND",
            )
        # Re-validate the scope even though the repository is already
        # tenant-scoped: a resolver must not depend on a single layer being right
        # about visibility (section 51).
        require_valid_scope(visibility=view.visibility, tenant_id=view.tenant_id)
        if view.visibility is Visibility.TENANT and view.tenant_id != tenant_id:
            return CitationResolution(
                citation_id=citation_id,
                resolved=False,
                reason="SCOPE_MISMATCH",
            )
        if not view.content_hash.startswith(parsed.content_hash_prefix):
            return CitationResolution(
                citation_id=citation_id,
                resolved=False,
                reason="CONTENT_HASH_MISMATCH",
            )
        return CitationResolution(
            citation_id=citation_id,
            resolved=True,
            document_id=view.document_id,
            document_version_id=view.document_version_id,
            chunk_id=view.chunk_id,
            source_kind=view.source_kind,
            title=view.title,
            language=view.language,
            source_version=view.source_version,
            excerpt=build_excerpt(view.content),
            document_status=view.document_status,
        )
