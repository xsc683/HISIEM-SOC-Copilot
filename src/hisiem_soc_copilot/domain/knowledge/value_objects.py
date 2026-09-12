"""Knowledge value objects: content normalization, hashing, chunking config, citation.

Everything here is pure and stdlib-only, so the SAME normalized content and the
SAME chunker version always produce the same bytes and therefore the same hash on
any platform (brief sections 8/22/23/51).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from uuid import UUID

from .errors import (
    InvalidCitationError,
    InvalidContentHashError,
    InvalidKnowledgeDocumentError,
)

# ---------------------------------------------------------------------------
# Content normalization + hashing (brief section 8)
# ---------------------------------------------------------------------------

_BOM = "﻿"
_SHA256_HEX = re.compile(r"\A[0-9a-f]{64}\Z")
_CITATION_PREFIX = "kcit"
_CITATION_PREFIX_LEN = 12
_CITATION_HASH_PREFIX = re.compile(r"\A[0-9a-f]{8,16}\Z")


def normalize_knowledge_content(raw: str) -> str:
    """Return the canonical form of a knowledge document body.

    The normalization is deliberately MINIMAL -- it may only remove differences
    that carry no security meaning:

    1. a leading BOM is dropped (an editor artefact, not content);
    2. every line ending becomes LF (CRLF and bare CR both collapse);
    3. Unicode NFC normalization is applied;
    4. trailing whitespace is trimmed on every line;
    5. meaningless trailing blank space is removed and the text ends with exactly
       one final newline (empty content normalizes to the empty string).

    It must NOT lowercase, strip punctuation, or stem: ``T1110``, ``sshd``,
    ``authentication_failure`` and CVE-like tokens are security identifiers and
    changing them would change what the document means.
    """
    text = raw
    if text.startswith(_BOM):
        text = text[len(_BOM) :]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = text.rstrip("\n")
    if not text:
        return ""
    return text + "\n"


def compute_content_hash(normalized_content: str) -> str:
    """Return ``SHA-256(normalized_content UTF-8)`` as lowercase hex.

    Callers must pass content that already went through
    :func:`normalize_knowledge_content`; that is what makes the same document
    ingested from a Windows checkout and a Linux checkout hash identically.
    """
    return hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()


def normalize_and_hash(raw: str) -> tuple[str, str]:
    """Normalize ``raw`` and return ``(normalized_content, content_hash)``."""
    normalized = normalize_knowledge_content(raw)
    return normalized, compute_content_hash(normalized)


def require_valid_content_hash(content_hash: str) -> str:
    """Return ``content_hash`` when it is a lowercase SHA-256 hex digest."""
    if not _SHA256_HEX.match(content_hash):
        raise InvalidContentHashError(
            "content hash must be a lowercase SHA-256 hex digest"
        )
    return content_hash


def content_hash_prefix(content_hash: str, *, length: int = _CITATION_PREFIX_LEN) -> str:
    """Return the short, non-authoritative hash prefix used inside a citation id."""
    require_valid_content_hash(content_hash)
    return content_hash[:length]


def is_valid_content_hash(value: str) -> bool:
    return bool(_SHA256_HEX.match(value))


# ---------------------------------------------------------------------------
# Chunker configuration (brief sections 21/22/23)
# ---------------------------------------------------------------------------

CHUNKER_VERSION = "structure-aware-v1"
DEFAULT_TARGET_TOKENS = 600
DEFAULT_MAX_TOKENS = 800
DEFAULT_OVERLAP_TOKENS = 80
#: Hard ceiling on ``max_tokens`` (brief section 22: ``max_tokens`` <= 800).
MAX_TOKENS_CEILING = 800


@dataclass(frozen=True)
class ChunkerProfile:
    """The FROZEN chunker configuration.

    ``chunker_version`` is part of the identity of every chunk: changing the
    algorithm without changing the version would silently rewrite the retrieval
    projection for content that did not change, so the version is persisted with
    the chunks and echoed by the retrieval profile.
    """

    chunker_version: str = CHUNKER_VERSION
    target_tokens: int = DEFAULT_TARGET_TOKENS
    max_tokens: int = DEFAULT_MAX_TOKENS
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS

    def __post_init__(self) -> None:
        if not self.chunker_version.strip():
            raise InvalidKnowledgeDocumentError("chunker_version must not be empty")
        if self.target_tokens < 1:
            raise InvalidKnowledgeDocumentError("target_tokens must be >= 1")
        if self.max_tokens < self.target_tokens:
            raise InvalidKnowledgeDocumentError("max_tokens must be >= target_tokens")
        if self.max_tokens > MAX_TOKENS_CEILING:
            raise InvalidKnowledgeDocumentError(
                f"max_tokens must be <= {MAX_TOKENS_CEILING}"
            )
        if self.overlap_tokens < 0:
            raise InvalidKnowledgeDocumentError("overlap_tokens must be >= 0")
        if self.overlap_tokens >= self.target_tokens:
            raise InvalidKnowledgeDocumentError("overlap_tokens must be < target_tokens")


# ---------------------------------------------------------------------------
# Citation (brief section 51)
# ---------------------------------------------------------------------------

CITATION_PREFIX = _CITATION_PREFIX


@dataclass(frozen=True)
class Citation:
    """A citation HANDLE: ``kcit:<chunk_uuid>:<content_hash_prefix>``.

    A citation is NOT authority. It is a validated retrieval reference: the
    resolver re-reads the chunk, document, and version from the database and
    re-checks the hash and the scope. Fabricating a citation string therefore
    confers nothing -- the string is never trusted as data about the world, only
    used as a lookup key whose result is independently validated.
    """

    chunk_id: UUID
    content_hash_prefix: str = field(default="")

    def __post_init__(self) -> None:
        if self.content_hash_prefix and not _CITATION_HASH_PREFIX.match(
            self.content_hash_prefix
        ):
            raise InvalidCitationError(
                "citation hash prefix must be 8-16 lowercase hex digits"
            )

    def render(self) -> str:
        return f"{_CITATION_PREFIX}:{self.chunk_id}:{self.content_hash_prefix}"


def format_citation_id(*, chunk_id: UUID, content_hash: str) -> str:
    """Render the citation id for a chunk plus its persisted content hash."""
    return Citation(
        chunk_id=chunk_id,
        content_hash_prefix=content_hash_prefix(content_hash),
    ).render()


def parse_citation_id(text: str) -> Citation | None:
    """Parse a citation id, returning ``None`` for anything malformed.

    Never raises and never repairs: an unparseable citation simply fails to
    resolve, and every field it claims is re-validated against the database
    before the citation is reported as resolvable.
    """
    if not isinstance(text, str):
        return None
    parts = text.split(":")
    if len(parts) != 3:
        return None
    prefix, raw_chunk_id, raw_hash_prefix = parts
    if prefix != _CITATION_PREFIX:
        return None
    try:
        chunk_id = UUID(raw_chunk_id)
    except (ValueError, AttributeError, TypeError):
        return None
    if str(chunk_id) != raw_chunk_id:
        return None
    if not _CITATION_HASH_PREFIX.match(raw_hash_prefix):
        return None
    return Citation(chunk_id=chunk_id, content_hash_prefix=raw_hash_prefix)
