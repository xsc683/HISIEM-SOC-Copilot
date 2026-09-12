"""Embedding provider boundary (brief section 17).

An embedding provider turns text into vectors, and every vector carries the
identity of the embedding SPACE it was produced in. The profile travels WITH the
vector so a distance can never be computed between a query vector and a document
vector from two different spaces.

No vendor SDK is imported here: Application talks to this Protocol, and
``infrastructure/embedding`` adapts a concrete provider to it. The Application
layer owns the fail-closed validation (count, dimension, finiteness, ordering,
profile identity); the adapter only has to be honest.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ...domain.knowledge.errors import InvalidEmbeddingVectorError

# A generous but bounded ceiling: big enough for every current embedding model,
# small enough that a malformed provider response cannot allocate a huge vector.
MAX_EMBEDDING_DIMENSION = 8192

SUPPORTED_DISTANCE_METRICS = ("COSINE",)
SUPPORTED_NORMALIZATIONS = ("NONE", "L2")


@dataclass(frozen=True)
class EmbeddingProfileDescriptor:
    """Identity of the embedding space a vector lives in.

    The ``embedding_profile`` table persists exactly this tuple, and retrieval
    filters chunks by the ACTIVE profile id, so ``identity`` is what makes
    "compare within one space only" checkable rather than aspirational.
    """

    provider: str
    model_id: str
    dimension: int
    distance_metric: str = "COSINE"
    normalization: str = "NONE"
    profile_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise InvalidEmbeddingVectorError("embedding provider must be non-empty")
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise InvalidEmbeddingVectorError("embedding model_id must be non-empty")
        if not 1 <= self.dimension <= MAX_EMBEDDING_DIMENSION:
            raise InvalidEmbeddingVectorError(
                f"embedding dimension must be between 1 and {MAX_EMBEDDING_DIMENSION}"
            )
        if self.distance_metric not in SUPPORTED_DISTANCE_METRICS:
            raise InvalidEmbeddingVectorError(
                f"unsupported distance metric {self.distance_metric!r}"
            )
        if self.normalization not in SUPPORTED_NORMALIZATIONS:
            raise InvalidEmbeddingVectorError(
                f"unsupported normalization {self.normalization!r}"
            )
        if self.profile_version < 1:
            raise InvalidEmbeddingVectorError("profile_version must be >= 1")

    @property
    def identity(self) -> tuple[str, str, int, str, str, int]:
        """The full comparable identity: equal tuples are the same vector space."""
        return (
            self.provider,
            self.model_id,
            self.dimension,
            self.distance_metric,
            self.normalization,
            self.profile_version,
        )


@dataclass(frozen=True)
class EmbeddingVector:
    """One vector plus the space it belongs to.

    ``index`` is the position of the source text in the request. Document
    batches must return it (contiguously from 0) so a provider that reorders its
    output is rejected instead of silently pairing the wrong vector with the
    wrong chunk; ``embed_query`` leaves it None.
    """

    values: tuple[float, ...]
    descriptor: EmbeddingProfileDescriptor
    index: int | None = None

    def __post_init__(self) -> None:
        if not self.values:
            raise InvalidEmbeddingVectorError("embedding vector must not be empty")
        if len(self.values) != self.descriptor.dimension:
            raise InvalidEmbeddingVectorError(
                "embedding vector dimension does not match its profile "
                f"({len(self.values)} != {self.descriptor.dimension})"
            )
        for value in self.values:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise InvalidEmbeddingVectorError("embedding values must be numeric")
            if not math.isfinite(value):
                raise InvalidEmbeddingVectorError(
                    "embedding values must be finite (no NaN/Infinity)"
                )
        if self.index is not None and self.index < 0:
            raise InvalidEmbeddingVectorError("embedding index must be >= 0")

    @property
    def dimension(self) -> int:
        return len(self.values)


@dataclass(frozen=True)
class EmbeddingBatch:
    """The result of embedding several documents at once."""

    descriptor: EmbeddingProfileDescriptor
    vectors: tuple[EmbeddingVector, ...]

    def __post_init__(self) -> None:
        if not self.vectors:
            raise InvalidEmbeddingVectorError("embedding batch must not be empty")
        for vector in self.vectors:
            if vector.descriptor.identity != self.descriptor.identity:
                raise InvalidEmbeddingVectorError(
                    "every vector in a batch must share the batch's profile"
                )
        indices = [vector.index for vector in self.vectors]
        if all(index is not None for index in indices) and indices != list(
            range(len(self.vectors))
        ):
            raise InvalidEmbeddingVectorError(
                "document embeddings must be returned in input order "
                "(index must run 0..n-1)"
            )

    @property
    def dimension(self) -> int:
        return self.descriptor.dimension


class EmbeddingProvider(Protocol):
    """Structural port implemented by every embedding adapter.

    Implementations must not be given secrets they do not need, and must never
    log request bodies, credentials, or raw provider responses.
    """

    @property
    def descriptor(self) -> EmbeddingProfileDescriptor: ...

    async def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch: ...

    async def embed_query(self, text: str) -> EmbeddingVector: ...
