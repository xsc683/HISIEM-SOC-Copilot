"""!! TEST FIXTURE ONLY — NOT A PRODUCTION EMBEDDING PROVIDER !!

``DeterministicEmbeddingProvider`` produces SEMANTICALLY MEANINGLESS vectors. It is
a plumbing fixture, and nothing more:

- it exists ONLY so that unit and integration tests (ranking stability,
  tenant isolation, citation provenance, database round-trips, batch ordering) can
  run with a provider that is byte-for-byte reproducible across runs and processes;
- it is NEVER a production default, NEVER wired from configuration, and its output
  must NEVER be presented as evidence of retrieval quality, semantic similarity, or
  any product capability. A test that "ranks well" against this provider has proven
  only that plumbing works — no more.

Implementation: each vector is a deterministic function of the text alone. The text's
SHA-256 digest seeds a stream of 8-byte chunks that are mapped linearly into
``[-1, 1)``, so no ``random``, clock, ``hash()`` (salted per process) or dict-iteration
order can influence the result. Same text ⇒ same bytes, in this process and in any
other.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

from ...application.ports.embedding import (
    EmbeddingBatch,
    EmbeddingProfileDescriptor,
    EmbeddingProvider,
    EmbeddingVector,
)
from ...domain.knowledge.errors import InvalidEmbeddingVectorError

_PROVIDER = "deterministic"

# One 8-byte chunk maps exactly onto the 64 bits of the [0, 1) interval.
_CHUNK_BYTES = 8
_TWO_POW_64 = float(2**64)


class DeterministicEmbeddingProvider:
    """Reproducible, semantically meaningless embeddings — for tests only.

    NEVER use this outside a test suite, and never present its output as evidence of
    retrieval or semantic quality. The descriptor carries ``provider="deterministic"``
    so a run that accidentally used it is auditable rather than invisible.
    """

    def __init__(
        self,
        *,
        dimension: int = 64,
        model_id: str = "deterministic-test-v1",
        normalization: str = "L2",
        distance_metric: str = "COSINE",
        profile_version: int = 1,
    ) -> None:
        # The descriptor validates dimension/normalization/distance_metric and raises
        # InvalidEmbeddingVectorError, so there is one definition of a legal space.
        self._descriptor = EmbeddingProfileDescriptor(
            provider=_PROVIDER,
            model_id=model_id,
            dimension=dimension,
            distance_metric=distance_metric,
            normalization=normalization,
            profile_version=profile_version,
        )

    @property
    def descriptor(self) -> EmbeddingProfileDescriptor:
        return self._descriptor

    # ------------------------------------------------------------------
    # EmbeddingProvider
    # ------------------------------------------------------------------
    async def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        """Embed a batch, assigning contiguous indices so order is provable."""
        if not texts:
            raise InvalidEmbeddingVectorError(
                "embed_documents requires at least one text"
            )
        vectors = tuple(
            EmbeddingVector(
                values=self._values_for(text),
                descriptor=self._descriptor,
                index=index,
            )
            for index, text in enumerate(texts)
        )
        return EmbeddingBatch(descriptor=self._descriptor, vectors=vectors)

    async def embed_query(self, text: str) -> EmbeddingVector:
        """Embed a query; a blank query is rejected rather than silently hashed."""
        if not text or not text.strip():
            raise InvalidEmbeddingVectorError(
                "embed_query requires a non-empty query text"
            )
        return EmbeddingVector(
            values=self._values_for(text), descriptor=self._descriptor, index=None
        )

    # ------------------------------------------------------------------
    # determinism core
    # ------------------------------------------------------------------
    def _values_for(self, text: str) -> tuple[float, ...]:
        """Derive a stable ``dimension``-length vector from the text alone."""
        dimension = self._descriptor.dimension
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values: list[float] = []
        counter = 0
        # 32 digest bytes give 4 chunks per counter value; re-hash the digest with a
        # big-endian counter until enough chunks exist for the requested dimension.
        while len(values) < dimension:
            block = hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
            for offset in range(0, len(block), _CHUNK_BYTES):
                if len(values) >= dimension:
                    break
                chunk = block[offset : offset + _CHUNK_BYTES]
                values.append(int.from_bytes(chunk, "big") / _TWO_POW_64 * 2.0 - 1.0)
            counter += 1
        if self._descriptor.normalization == "L2":
            values = _l2_normalize(values)
        return tuple(values)


def _l2_normalize(values: list[float]) -> list[float]:
    """Scale to unit length; a zero vector stays zero (never divide by zero)."""
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:
        return values
    return [value / norm for value in values]


def _implements_embedding_provider(
    provider: DeterministicEmbeddingProvider,
) -> EmbeddingProvider:
    """Static (mypy-only) proof that the fixture satisfies the port's structure."""
    return provider
