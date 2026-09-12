"""Adapter exposing the structure-aware chunker through the application port.

The chunker itself (``chunker.py``) is a pure function over text with a frozen
version. This adapter is the only place that binds it to the ingestion bounds and
translates its ``Chunk`` values into the application's ``DocumentChunk``, so the
dependency direction stays ``infrastructure -> application`` (section 80).
"""

from __future__ import annotations

from ...application.ports.chunking import ChunkingPort, DocumentChunk
from ...domain.knowledge.value_objects import CHUNKER_VERSION, ChunkerProfile
from .chunker import chunk_document

__all__ = ("StructureAwareChunker",)


class StructureAwareChunker:
    """``ChunkingPort`` implementation backed by the frozen chunker."""

    def __init__(
        self,
        *,
        profile: ChunkerProfile | None = None,
        max_chunks: int | None = None,
        max_chunk_chars: int | None = None,
    ) -> None:
        self._profile = profile or ChunkerProfile()
        self._max_chunks = max_chunks
        self._max_chunk_chars = max_chunk_chars

    @property
    def chunker_version(self) -> str:
        return str(self._profile.chunker_version or CHUNKER_VERSION)

    def chunk_document(self, text: str) -> tuple[DocumentChunk, ...]:
        return tuple(
            DocumentChunk(
                ordinal=chunk.ordinal,
                heading_path=chunk.heading_path,
                content=chunk.content,
                content_hash=chunk.content_hash,
                token_count=chunk.token_count,
            )
            for chunk in chunk_document(
                text,
                profile=self._profile,
                max_chunks=self._max_chunks,
                max_chunk_chars=self._max_chunk_chars,
            )
        )


def _satisfies_port(chunker: StructureAwareChunker) -> ChunkingPort:
    """Static proof that the adapter structurally satisfies the port."""
    return chunker
