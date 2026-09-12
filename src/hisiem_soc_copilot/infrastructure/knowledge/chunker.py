"""Structure-aware knowledge chunker (brief sections 21/22/23/24/25).

Chunking decides retrieval provenance, so it is deliberately boring: no tokenizer
dependency, no randomness, no clock, no locale, no set/dict iteration. The same
normalized document plus the same ``ChunkerProfile`` must produce exactly the same
chunk contents and hashes on every machine and every run -- a chunk's
``content_hash`` is what a citation resolves against, so a non-deterministic
chunker would silently invalidate stored citations.

Three properties are load-bearing, and they are why this walks the document
structure instead of slicing a character window:

1. **Boundaries follow the document, not a character count.** A markdown heading
   opens a section, a fenced code block stays whole, a list run stays whole, and
   the chain of enclosing headings travels with the chunk as ``heading_path``.
   Slicing mid-sentence would separate an indicator (``T1110``) from the log line
   it describes.
2. **Content is DATA, never instructions (brief section 24).** Chunk content is
   copied byte-for-byte: identifiers, CVE ids, markdown and code survive verbatim.
   This module never executes, never evaluates, never rewrites the text.
3. **Bounds are rejections, never truncations (brief section 25).** Exceeding
   ``max_chunks`` or ``max_chunk_chars`` raises ``KnowledgeBoundsExceededError``
   BEFORE any partial result is returned; a silently truncated document would
   break the provenance chain everything downstream depends on.

``estimate_tokens`` is an explicitly APPROXIMATE, dependency-free length proxy. It
is used only to make packing decisions and is NOT upstream-model-accurate; it is
deterministic and integer-valued so packing is reproducible.

``CHUNKER_VERSION`` is re-exported here so callers that reach for the chunker also
reach for the version that identifies its output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...domain.knowledge.errors import KnowledgeBoundsExceededError
from ...domain.knowledge.value_objects import (
    CHUNKER_VERSION,
    ChunkerProfile,
    compute_content_hash,
)

__all__ = ("CHUNKER_VERSION", "Chunk", "chunk_document", "estimate_tokens")

#: Cost divisor for the token approximation: ~6 characters per token.
_CHARS_PER_TOKEN = 6
#: Blank line joining blocks that end up inside one chunk. Markdown-safe and
#: stable; it never appears *inside* a block, so block bytes stay verbatim.
_BLOCK_SEPARATOR = "\n\n"

_WORD_RE = re.compile(r"\S+")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$")
_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[^\n]*$")
_LIST_ITEM_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+\.)[ \t]+")


@dataclass(frozen=True)
class Chunk:
    """One persisted, addressable unit of a knowledge document version.

    The chunk is the unit a citation resolves against, so every field is derived
    from the content and the frozen profile -- never from the environment:

    - ``ordinal`` is 0-based and contiguous over the document, which is what makes
      "chunk 3 of 9" a stable, replayable statement;
    - ``heading_path`` is the ``" > "``-joined chain of enclosing markdown
      headings, or ``""`` when the chunk sits outside every heading. The heading
      lines themselves are not part of ``content``: the path carries them, so the
      same body text under different headings hashes the same;
    - ``content`` is exactly what is persisted and hashed (rule: content is DATA);
    - ``token_count`` is the ``estimate_tokens`` approximation, persisted so the
      retrieval profile can report an honest, reproducible size.
    """

    ordinal: int
    heading_path: str
    content: str
    content_hash: str
    token_count: int


@dataclass(frozen=True)
class _Block:
    """A parsed structural unit of the document, before it is packed into chunks."""

    heading_path: str
    text: str
    is_fence: bool = False


def estimate_tokens(text: str) -> int:
    """Return an approximate token count for ``text``.

    Approximation: split on whitespace and charge ``max(1, ceil(len(word) / 6))``
    per word, summing to a floor of 0 for empty/whitespace-only text. This is
    dependency-free, pure, integer-valued and deterministic -- deliberately NOT a
    real tokenizer and NOT upstream-model-accurate. It only has to be
    *reproducible* so that packing decisions (and therefore chunk boundaries)
    cannot drift between runs.
    """
    total = 0
    for word in text.split():
        total += _word_token_cost(word)
    return total


def chunk_document(
    text: str,
    *,
    profile: ChunkerProfile | None = None,
    max_chunks: int | None = None,
    max_chunk_chars: int | None = None,
) -> tuple[Chunk, ...]:
    """Split ``text`` into deterministic, structure-aware :class:`Chunk` values.

    ``profile=None`` means the frozen defaults (``ChunkerProfile()``). ``text`` is
    expected to be already normalized upstream (LF, NFC, no trailing whitespace,
    one final newline), but line endings are normalized again here so a CRLF
    checkout cannot change the result.

    Empty or whitespace-only input returns ``()``: rejecting an empty document is
    the caller's job, not the chunker's. ``max_chunks`` and ``max_chunk_chars`` are
    bounds, not budgets -- exceeding either raises
    :class:`KnowledgeBoundsExceededError` before anything is returned. A single
    over-long LINE is unbreakable without lying about its bytes, so it is rejected
    rather than silently cut (brief section 25). Note that a block which is not a
    fenced code block is never split either: it becomes its own chunk even if it
    exceeds ``max_tokens``, because cutting it would reflow content.
    """
    chunker = ChunkerProfile() if profile is None else profile
    lines = _split_lines(text)

    # Cheap and unbreakable, so it is checked before any parsing work.
    if max_chunk_chars is not None:
        for line in lines:
            if len(line) > max_chunk_chars:
                raise KnowledgeBoundsExceededError(
                    bound="max_chunk_chars", limit=max_chunk_chars, actual=len(line)
                )

    contents = _assemble_contents(_parse_blocks(lines), chunker)

    if max_chunks is not None and len(contents) > max_chunks:
        raise KnowledgeBoundsExceededError(
            bound="max_chunks_per_document", limit=max_chunks, actual=len(contents)
        )

    return tuple(
        Chunk(
            ordinal=ordinal,
            heading_path=heading_path,
            content=content,
            content_hash=compute_content_hash(content),
            token_count=estimate_tokens(content),
        )
        for ordinal, (heading_path, content) in enumerate(contents)
    )


# ---------------------------------------------------------------------------
# Line/block parsing
# ---------------------------------------------------------------------------


def _split_lines(text: str) -> list[str]:
    """Split ``text`` into lines after collapsing every line ending to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _word_token_cost(word: str) -> int:
    return max(1, (len(word) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def _heading_level_and_text(line: str) -> tuple[int, str] | None:
    """Return ``(level, title)`` for an ATX heading line, else ``None``."""
    match = _HEADING_RE.match(line)
    if match is None:
        return None
    return len(match.group(1)), match.group(2).strip()


def _fence_marker(line: str) -> tuple[str, int] | None:
    """Return ``(fence_char, fence_length)`` for a fence opener line, else ``None``."""
    match = _FENCE_OPEN_RE.match(line)
    if match is None:
        return None
    marker = match.group(1)
    return marker[0], len(marker)


def _is_closing_fence(line: str, fence_char: str, fence_len: int) -> bool:
    stripped = line.strip()
    if len(stripped) < fence_len:
        return False
    return all(char == fence_char for char in stripped)


def _is_indented(line: str) -> bool:
    return bool(line.strip()) and line[0] in (" ", "\t")


def _render_heading_path(stack: list[tuple[int, str]]) -> str:
    return " > ".join(title for _, title in stack)


def _trimmed_text(lines: list[str]) -> str:
    """Return ``lines`` joined by LF with surrounding blank lines dropped.

    Interior bytes are untouched: no reflow, no line-level rstrip. Blank-line
    trimming only removes separation that the doc structure already encodes
    elsewhere (a heading change, a block boundary).
    """
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


def _fence_block_end(lines: list[str], start: int, marker: tuple[str, int]) -> int:
    """Return the index just past a fenced code block (closer included).

    An unterminated fence swallows the rest of the document rather than being
    guessed at: a partial fence is still content, and guessing where it "should"
    have ended would fabricate structure.
    """
    fence_char, fence_len = marker
    index = start + 1
    while index < len(lines):
        if _is_closing_fence(lines[index], fence_char, fence_len):
            return index + 1
        index += 1
    return len(lines)


def _list_block_end(lines: list[str], start: int) -> int:
    """Return the index just past a list run (trailing blank lines excluded).

    A blank line continues the run only when the next non-blank line is another
    item of the same list; otherwise the list ended and the blank is separation.
    """
    total = len(lines)
    index = start
    last_item = start
    while index < total:
        line = lines[index]
        if _LIST_ITEM_RE.match(line) is not None:
            last_item = index + 1
            index += 1
            while index < total and _is_indented(lines[index]):
                last_item = index + 1
                index += 1
            continue
        if not line.strip():
            look = index
            while look < total and not lines[look].strip():
                look += 1
            if look < total and _LIST_ITEM_RE.match(lines[look]) is not None:
                index = look
                continue
            break
        break
    return last_item


def _paragraph_block_end(lines: list[str], start: int) -> int:
    total = len(lines)
    index = start
    while index < total:
        line = lines[index]
        if not line.strip():
            break
        if _heading_level_and_text(line) is not None:
            break
        if _fence_marker(line) is not None:
            break
        if _LIST_ITEM_RE.match(line) is not None:
            break
        index += 1
    return index


def _parse_blocks(lines: list[str]) -> list[_Block]:
    """Walk the document once, emitting ordered blocks that each carry a heading path.

    Heading lines are structural only: they update the enclosing path but are not
    emitted as content, so a chunk's bytes never depend on which section it is in.
    """
    blocks: list[_Block] = []
    heading_stack: list[tuple[int, str]] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        heading = _heading_level_and_text(line)
        if heading is not None:
            level, title = heading
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            index += 1
            continue
        heading_path = _render_heading_path(heading_stack)
        fence = _fence_marker(line)
        if fence is not None:
            end = _fence_block_end(lines, index, fence)
            blocks.append(_Block(heading_path, _trimmed_text(lines[index:end]), is_fence=True))
            index = end
            continue
        if _LIST_ITEM_RE.match(line) is not None:
            end = _list_block_end(lines, index)
            blocks.append(_Block(heading_path, _trimmed_text(lines[index:end])))
            index = end
            continue
        end = _paragraph_block_end(lines, index)
        blocks.append(_Block(heading_path, _trimmed_text(lines[index:end])))
        index = end
    return blocks


# ---------------------------------------------------------------------------
# Packing + overlap
# ---------------------------------------------------------------------------


def _join_block_texts(block_texts: list[str]) -> str:
    return _BLOCK_SEPARATOR.join(block_texts).rstrip()


def _assemble_contents(blocks: list[_Block], profile: ChunkerProfile) -> list[tuple[str, str]]:
    """Pack blocks into ``(heading_path, content)`` pairs, then apply overlap."""
    raw: list[tuple[str, str]] = []
    buffer: list[str] = []
    buffer_path = ""
    buffer_tokens = 0

    def flush() -> None:
        nonlocal buffer, buffer_tokens
        if buffer:
            raw.append((buffer_path, _join_block_texts(buffer)))
            buffer = []
            buffer_tokens = 0

    for block in blocks:
        if buffer and block.heading_path != buffer_path:
            flush()

        if block.is_fence and estimate_tokens(block.text) > profile.max_tokens:
            # The one legal split: an oversized fence, cut at line boundaries so
            # every piece is still valid Markdown. Pieces are emitted directly --
            # they are already at the size ceiling and must not absorb neighbours.
            flush()
            buffer_path = block.heading_path
            for piece in _split_fence_block(block.text, profile.max_tokens):
                raw.append((block.heading_path, piece))
            continue

        block_tokens = estimate_tokens(block.text)
        if not buffer:
            buffer_path = block.heading_path
            buffer.append(block.text)
            buffer_tokens = block_tokens
        elif buffer_tokens + block_tokens <= profile.target_tokens:
            buffer.append(block.text)
            buffer_tokens += block_tokens
        else:
            flush()
            buffer_path = block.heading_path
            buffer.append(block.text)
            buffer_tokens = block_tokens

    flush()
    return _apply_overlap(raw, profile)


def _split_fence_block(text: str, max_tokens: int) -> list[str]:
    """Split an oversized fenced code block at line boundaries.

    Every piece repeats the opening fence and, when the block was terminated, the
    closing fence, so each piece parses as a code block on its own. A single body
    line that cannot fit under ``max_tokens`` is still emitted whole: splitting
    mid-line would change the bytes of code, which is worse than a large chunk.
    """
    lines = text.split("\n")
    opener = lines[0]
    body = lines[1:]
    closer: str | None = None
    marker = _fence_marker(opener)
    if marker is not None and body:
        fence_char, fence_len = marker
        if _is_closing_fence(body[-1], fence_char, fence_len):
            closer = body[-1]
            body = body[:-1]
    if not body:
        return [text]

    overhead = estimate_tokens(opener)
    if closer is not None:
        overhead += estimate_tokens(closer)

    pieces: list[str] = []
    index = 0
    while index < len(body):
        piece_lines = [opener]
        used = overhead
        first = True
        while index < len(body) and (first or used + estimate_tokens(body[index]) <= max_tokens):
            used += estimate_tokens(body[index])
            piece_lines.append(body[index])
            index += 1
            first = False
        if closer is not None:
            piece_lines.append(closer)
        pieces.append("\n".join(piece_lines))
    return pieces


def _overlap_prefix(previous: str, content: str, profile: ChunkerProfile) -> str:
    """Return the tail of ``previous`` to prepend to ``content``, or ``""``.

    The window is measured in estimated tokens and cut at a whitespace boundary, so
    a sentence spanning the two chunks is not lost. Three guards apply (brief
    section 22): a whole previous chunk is never copied, the prefix is dropped word
    by word until it fits under ``max_tokens``, and nothing is returned when no word
    fits the window at all.
    """
    matches = list(_WORD_RE.finditer(previous))
    if not matches:
        return ""

    cursor = len(matches)
    budget = 0
    for position in range(len(matches) - 1, -1, -1):
        cost = _word_token_cost(matches[position].group(0))
        if budget + cost > profile.overlap_tokens:
            break
        budget += cost
        cursor = position
    if cursor >= len(matches):
        return ""
    if cursor == 0:
        # The entire previous chunk fits in the overlap window; copying it would
        # duplicate a whole chunk, which is forbidden.
        return ""

    content_tokens = estimate_tokens(content)
    while cursor < len(matches):
        prefix = previous[matches[cursor].start() :]
        if estimate_tokens(prefix) + content_tokens <= profile.max_tokens:
            return prefix
        cursor += 1
    return ""


def _apply_overlap(raw: list[tuple[str, str]], profile: ChunkerProfile) -> list[tuple[str, str]]:
    """Prepend a tail of the previous chunk to same-heading-path successors.

    Overlap exists so a fact split across a chunk boundary is still retrievable
    from the second chunk. It never crosses a heading change: two different
    sections are different claims, and mixing them would misattribute evidence.
    """
    if profile.overlap_tokens <= 0:
        return raw
    result: list[tuple[str, str]] = []
    for index, (heading_path, content) in enumerate(raw):
        if index == 0:
            result.append((heading_path, content))
            continue
        previous_path, previous_content = raw[index - 1]
        if previous_path != heading_path:
            result.append((heading_path, content))
            continue
        prefix = _overlap_prefix(previous_content, content, profile)
        if prefix:
            result.append((heading_path, f"{prefix}{_BLOCK_SEPARATOR}{content}"))
        else:
            result.append((heading_path, content))
    return result
