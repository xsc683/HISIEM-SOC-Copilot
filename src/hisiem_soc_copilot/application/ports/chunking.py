"""Chunking port (brief sections 21-24).

The structure-aware chunker is an INFRASTRUCTURE concern -- it is a text
algorithm with a frozen version, and the application layer must not import
infrastructure. So the application states what it needs (deterministically split
normalized content into addressable pieces, and tell me which chunker version
produced them) and infrastructure provides it.

The returned :class:`DocumentChunk` is deliberately NOT the infrastructure
chunker's own ``Chunk`` type: application code owns the shape it depends on, so
the chunker can be replaced or version-bumped without an application-layer edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DocumentChunk:
    """One addressable unit of a document version, as the application sees it.

    ``content`` is DATA. Nothing in the retrieval or ingestion path interprets it
    as an instruction, so a chunk containing shell commands, credentials-looking
    strings, or "ignore previous instructions" is handled exactly like any other
    text (section 24).
    """

    ordinal: int
    heading_path: str
    content: str
    content_hash: str
    token_count: int


class ChunkingPort(Protocol):
    """Deterministic, structure-aware document splitting."""

    @property
    def chunker_version(self) -> str:
        """The frozen version of the algorithm that produced the chunks.

        Persisted with every chunk so a later configuration change is detectable
        instead of silently serving a projection built by a different chunker
        (section 22).
        """
        ...

    def chunk_document(self, text: str) -> tuple[DocumentChunk, ...]:
        """Split already-normalized content into contiguous, ordered chunks.

        Must be deterministic: the same input and version yield the same chunk
        count, ordinals, and content, independent of file path, checkout, or line
        endings (section 23). Must raise ``KnowledgeBoundsExceededError`` rather
        than truncate when a configured bound is exceeded (section 25).
        """
        ...
