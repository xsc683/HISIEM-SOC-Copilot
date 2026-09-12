"""Unit tests for the ATT&CK -> knowledge document projection (brief sections 38).

The projection is application-layer policy: it decides what bytes become a
knowledge document, and therefore what the immutable version's content hash is
computed over. These tests pin the rendering, because changing it silently would
re-key every technique document in the corpus.
"""

from __future__ import annotations

from hisiem_soc_copilot.application.ports.attack import AttackTechnique
from hisiem_soc_copilot.application.services.attack_projection import (
    EXTERNAL_KEY_PREFIX,
    technique_document_body,
    technique_document_title,
    technique_external_key,
)
from hisiem_soc_copilot.domain.knowledge.value_objects import normalize_and_hash


def _brute_force_technique() -> AttackTechnique:
    return AttackTechnique(
        technique_id="T1110",
        stix_id="attack-pattern--1111",
        name="Brute Force",
        description="Adversaries may use brute force to obtain credentials.",
        tactics=("credential-access", "defense-evasion"),
        platforms=("Linux", "Windows", "macOS"),
    )


def test_body_matches_the_frozen_rendering() -> None:
    expected = (
        "# T1110: Brute Force\n"
        "\n"
        "- Technique ID: T1110\n"
        "- Tactics: credential-access, defense-evasion\n"
        "- Platforms: Linux, Windows, macOS\n"
        "\n"
        "Adversaries may use brute force to obtain credentials.\n"
    )
    assert technique_document_body(_brute_force_technique()) == expected


def test_body_omits_empty_bullets() -> None:
    technique = AttackTechnique(
        technique_id="T1110",
        stix_id="attack-pattern--1111",
        name="Brute Force",
        description="Body only.",
        tactics=(),
        platforms=(),
    )
    content = technique_document_body(technique)

    assert "- Tactics:" not in content
    assert "- Platforms:" not in content
    assert content == (
        "# T1110: Brute Force\n\n- Technique ID: T1110\n\nBody only.\n"
    )


def test_body_preserves_description_verbatim() -> None:
    description = "Line one.\n\n- a markdown bullet\n\n  indented tail  "
    technique = AttackTechnique(
        technique_id="T1110",
        stix_id="attack-pattern--1111",
        name="Brute Force",
        description=description,
        tactics=(),
        platforms=(),
    )
    content = technique_document_body(technique)

    # Verbatim except for trimmed per-line trailing whitespace; exactly one
    # trailing newline overall.
    assert content.endswith("- a markdown bullet\n\n  indented tail\n")
    assert not content.endswith("\n\n")
    assert all(line == line.rstrip() for line in content.split("\n"))


def test_body_is_deterministic() -> None:
    technique = _brute_force_technique()
    assert technique_document_body(technique) == technique_document_body(technique)


def test_body_is_already_a_normalization_fixed_point() -> None:
    """The importer renders; the DOMAIN hashes -- and the two must agree.

    This is the reason the projection does not carry its own hash: if it did, a
    description containing an NFD sequence would be hashed over different bytes
    than ingestion later hashes, and one technique would have two identities.
    """
    body = technique_document_body(_brute_force_technique())
    normalized, content_hash = normalize_and_hash(body)

    assert normalized == body
    assert len(content_hash) == 64


def test_title_and_external_key_are_derived_from_the_technique() -> None:
    technique = _brute_force_technique()

    assert technique_document_title(technique) == "T1110: Brute Force"
    assert technique_external_key(technique) == f"{EXTERNAL_KEY_PREFIX}T1110"
    assert technique_external_key(technique) == "mitre-attack:T1110"
