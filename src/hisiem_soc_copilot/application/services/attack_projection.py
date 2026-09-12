"""Projection of an ATT&CK technique into ingestable knowledge content.

This lives in the APPLICATION because it is a policy, not a wire concern: it
decides what bytes become a knowledge document, and therefore what the content
hash -- the immutable version identity -- is computed over. If rendering a
technique differently produced a different hash somewhere else, one technique
would silently have two identities.

The rendering is deterministic plain Markdown. The ingestion chunker parses
structure, so the heading and the bullets are load-bearing rather than cosmetic.
The description is included VERBATIM: a technique document that paraphrased
MITRE would be a different, unauthoritative document (section 24's DATA_ONLY
rule -- nothing here interprets the text, it only carries it).
"""

from __future__ import annotations

from ..ports.attack import AttackTechnique

#: Natural key prefix for a technique's knowledge document, so a MITRE document
#: is addressable as ``mitre-attack:T1110`` (section 38).
EXTERNAL_KEY_PREFIX = "mitre-attack:"


def technique_external_key(technique: AttackTechnique) -> str:
    """Return the knowledge ``external_key`` for ``technique``."""
    return f"{EXTERNAL_KEY_PREFIX}{technique.technique_id}"


def technique_document_title(technique: AttackTechnique) -> str:
    """Return the document title, which mirrors the rendered heading."""
    return f"{technique.technique_id}: {technique.name}"


def technique_document_body(technique: AttackTechnique) -> str:
    """Render one technique as the Markdown body ingested into knowledge.

    Only per-line trailing whitespace is trimmed and the text ends with exactly
    one newline, so that the body is already a fixed point of the domain's
    ``normalize_knowledge_content`` step -- the content the importer hashes and
    the content the ingestion handler hashes are the same bytes, because there is
    one hashing rule (``domain.knowledge.value_objects``) applied in one place.
    """
    lines: list[str] = [
        f"# {technique.technique_id}: {technique.name}",
        "",
        f"- Technique ID: {technique.technique_id}",
    ]
    if technique.tactics:
        lines.append(f"- Tactics: {', '.join(technique.tactics)}")
    if technique.platforms:
        lines.append(f"- Platforms: {', '.join(technique.platforms)}")
    lines.append("")
    lines.append(technique.description)

    text = "\n".join(line.rstrip() for line in "\n".join(lines).split("\n"))
    # The heading always exists, so ``text`` is never empty; it ends with exactly
    # one newline so the content hash is stable regardless of trailing spacing.
    return text.rstrip("\n") + "\n"
