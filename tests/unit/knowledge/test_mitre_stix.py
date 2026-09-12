"""Unit tests for the local MITRE ATT&CK STIX import (brief sections 33-38).

Every fixture is built INLINE as a small Python dict and serialized with
``json.dumps``: the import must be provable against a pinned, human-readable
bundle, never against the 100MB+ upstream dataset (which would also put a
network dependency into the test suite).
"""

from __future__ import annotations

import json
import random

import pytest

from hisiem_soc_copilot.domain.knowledge.errors import InvalidMitreBundleError
from hisiem_soc_copilot.infrastructure.knowledge.mitre_stix import (
    FRAMEWORK,
    ParsedBundle,
    ParsedTechnique,
    parse_attack_stix,
)

ENTERPRISE = ["enterprise-attack"]


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _attack_pattern(
    *,
    stix_id: str,
    attack_id: str | None,
    name: str = "Technique",
    description: str = "Description.",
    tactics: list[str] | None = None,
    platforms: list[str] | None = None,
    domains: list[str] | None = None,
    revoked: object = False,
    deprecated: object = False,
) -> dict[str, object]:
    obj: dict[str, object] = {
        "type": "attack-pattern",
        "id": stix_id,
        "name": name,
        "description": description,
    }
    if attack_id is not None:
        obj["external_references"] = [
            {"source_name": FRAMEWORK, "external_id": attack_id, "url": "https://x/"}
        ]
    if tactics is not None:
        obj["kill_chain_phases"] = [
            {"kill_chain_name": FRAMEWORK, "phase_name": tactic} for tactic in tactics
        ]
    if platforms is not None:
        obj["x_mitre_platforms"] = platforms
    if domains is not None:
        obj["x_mitre_domains"] = domains
    obj["revoked"] = revoked
    obj["x_mitre_deprecated"] = deprecated
    return obj


def _bundle(objects: list[object], spec_version: str | None = "2.1") -> str:
    document: dict[str, object] = {"type": "bundle", "objects": objects}
    if spec_version is not None:
        document["spec_version"] = spec_version
    return json.dumps(document)


def _parse(payload: bytes | str) -> ParsedBundle:
    return parse_attack_stix(payload)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_enterprise_technique_parses() -> None:
    raw = _bundle(
        [
            _attack_pattern(
                stix_id="attack-pattern--1111",
                attack_id="T1110",
                name="Brute Force",
                description="Adversaries may use brute force...",
                tactics=["credential-access", "defense-evasion"],
                platforms=["Windows", "Linux"],
                domains=ENTERPRISE,
            )
        ]
    )
    bundle = _parse(raw)

    assert bundle.spec_version == "2.1"
    assert bundle.total_attack_patterns == 1
    assert bundle.skipped_ids == ()
    assert len(bundle.techniques) == 1

    technique = bundle.techniques[0]
    assert technique.technique_id == "T1110"
    assert technique.stix_id == "attack-pattern--1111"
    assert technique.name == "Brute Force"
    assert technique.description == "Adversaries may use brute force..."
    assert technique.tactics == ("credential-access", "defense-evasion")
    assert technique.platforms == ("Linux", "Windows")
    assert technique.revoked is False
    assert technique.deprecated is False
    assert technique.is_active is True


def test_framework_constant_is_stable() -> None:
    # Other modules import FRAMEWORK; changing it silently re-keys every import.
    assert FRAMEWORK == "mitre-attack"


def test_missing_and_empty_domains_keep_the_technique() -> None:
    raw = _bundle(
        [
            _attack_pattern(stix_id="attack-pattern--a", attack_id="T1001"),
            _attack_pattern(stix_id="attack-pattern--b", attack_id="T1002", domains=[]),
        ]
    )
    bundle = _parse(raw)
    assert [t.technique_id for t in bundle.techniques] == ["T1001", "T1002"]
    assert bundle.skipped_ids == ()


@pytest.mark.parametrize("domain", ["mobile-attack", "ics-attack"])
def test_non_enterprise_domain_is_skipped(domain: str) -> None:
    raw = _bundle(
        [
            _attack_pattern(
                stix_id="attack-pattern--m",
                attack_id="T1430",
                name="Location Tracking",
                domains=[domain],
            ),
            _attack_pattern(stix_id="attack-pattern--e", attack_id="T1110"),
        ]
    )
    bundle = _parse(raw)

    assert [t.technique_id for t in bundle.techniques] == ["T1110"]
    assert bundle.skipped_ids == ("T1430",)
    # The out-of-scope object is still an attack-pattern in the file.
    assert bundle.total_attack_patterns == 2


@pytest.mark.parametrize(
    ("revoked", "deprecated"),
    [(True, False), (False, True)],
)
def test_revoked_or_deprecated_technique_is_excluded(
    revoked: bool, deprecated: bool
) -> None:
    raw = _bundle(
        [
            _attack_pattern(
                stix_id="attack-pattern--gone",
                attack_id="T9999",
                revoked=revoked,
                deprecated=deprecated,
            )
        ]
    )
    bundle = _parse(raw)

    assert bundle.techniques == ()
    assert bundle.skipped_ids == ("T9999",)
    # A directly constructed technique reports the same inactivity.
    assert (
        ParsedTechnique(
            technique_id="T9999",
            stix_id="attack-pattern--gone",
            name="",
            description="",
            tactics=(),
            platforms=(),
            revoked=revoked,
            deprecated=deprecated,
        ).is_active
        is False
    )


def test_is_active_is_true_only_when_neither_flag_is_set() -> None:
    def build(**flags: bool) -> ParsedTechnique:
        return ParsedTechnique(
            technique_id="T1110",
            stix_id="attack-pattern--x",
            name="Brute Force",
            description="",
            tactics=(),
            platforms=(),
            revoked=flags.get("revoked", False),
            deprecated=flags.get("deprecated", False),
        )

    assert build().is_active is True
    assert build(revoked=True).is_active is False
    assert build(deprecated=True).is_active is False
    assert build(revoked=True, deprecated=True).is_active is False


@pytest.mark.parametrize("bad_flag", [1, "true", None])
def test_non_boolean_flags_are_treated_as_false(bad_flag: object) -> None:
    raw = _bundle(
        [
            _attack_pattern(
                stix_id="attack-pattern--odd",
                attack_id="T1110",
                revoked=bad_flag,
                deprecated=bad_flag,
            )
        ]
    )
    bundle = _parse(raw)
    # Only the JSON literal ``true`` retires a technique; anything else is absent.
    assert len(bundle.techniques) == 1
    assert bundle.techniques[0].is_active is True


# ---------------------------------------------------------------------------
# Skipping
# ---------------------------------------------------------------------------


def test_attack_pattern_without_mitre_reference_is_skipped() -> None:
    no_references = {
        "type": "attack-pattern",
        "id": "attack-pattern--no-ref",
        "name": "Mystery",
        "description": "",
    }
    other_source = {
        "type": "attack-pattern",
        "id": "attack-pattern--cve-ref",
        "name": "Also Mystery",
        "external_references": [{"source_name": "cve", "external_id": "CVE-2020-1"}],
    }
    bundle = _parse(_bundle([no_references, other_source]))

    assert bundle.techniques == ()
    assert bundle.skipped_ids == ("attack-pattern--cve-ref", "attack-pattern--no-ref")
    assert bundle.total_attack_patterns == 2


def test_mitre_reference_without_external_id_is_skipped() -> None:
    obj = {
        "type": "attack-pattern",
        "id": "attack-pattern--empty-ref",
        "name": "No Id",
        "external_references": [{"source_name": FRAMEWORK}],
    }
    bundle = _parse(_bundle([obj]))
    assert bundle.techniques == ()
    assert bundle.skipped_ids == ("attack-pattern--empty-ref",)


@pytest.mark.parametrize("bad_id", ["T11", "t1110", "T1110001", "M1013", "TA0001"])
def test_malformed_technique_id_is_skipped(bad_id: str) -> None:
    bundle = _parse(
        _bundle([_attack_pattern(stix_id="attack-pattern--bad", attack_id=bad_id)])
    )
    assert bundle.techniques == ()
    # The unusable id is not a reportable reference: the STIX id identifies it.
    assert bundle.skipped_ids == ("attack-pattern--bad",)


def test_subtechnique_id_is_accepted() -> None:
    bundle = _parse(
        _bundle([_attack_pattern(stix_id="attack-pattern--sub", attack_id="T1110.001")])
    )
    assert [t.technique_id for t in bundle.techniques] == ["T1110.001"]


def test_non_attack_pattern_objects_are_ignored() -> None:
    raw = _bundle(
        [
            {"type": "malware", "id": "malware--1", "name": "Emotet"},
            {"type": "identity", "id": "identity--1"},
            _attack_pattern(stix_id="attack-pattern--only", attack_id="T1110"),
        ]
    )
    bundle = _parse(raw)

    assert [t.technique_id for t in bundle.techniques] == ["T1110"]
    assert bundle.total_attack_patterns == 1
    assert bundle.skipped_ids == ()


def test_duplicate_external_ids_keep_the_first_in_bundle_order() -> None:
    raw = _bundle(
        [
            _attack_pattern(
                stix_id="attack-pattern--first", attack_id="T1059", name="First"
            ),
            _attack_pattern(
                stix_id="attack-pattern--second", attack_id="T1059", name="Second"
            ),
        ]
    )
    bundle = _parse(raw)

    assert len(bundle.techniques) == 1
    assert bundle.techniques[0].stix_id == "attack-pattern--first"
    assert bundle.techniques[0].name == "First"
    assert bundle.skipped_ids == ("T1059",)


def test_wrongly_typed_optional_fields_yield_empty_tuples() -> None:
    obj = {
        "type": "attack-pattern",
        "id": "attack-pattern--wrong-types",
        "name": "Wrong Types",
        "external_references": [
            {"source_name": FRAMEWORK, "external_id": "T1110"}
        ],
        "kill_chain_phases": {"kill_chain_name": FRAMEWORK},
        "x_mitre_platforms": "Windows",
    }
    bundle = _parse(_bundle([obj]))

    assert len(bundle.techniques) == 1
    assert bundle.techniques[0].tactics == ()
    assert bundle.techniques[0].platforms == ()


def test_missing_optional_fields_yield_empty_strings_and_tuples() -> None:
    obj = {"type": "attack-pattern", "id": "attack-pattern--bare"}
    bundle = _parse(_bundle([obj]))
    # No MITRE reference at all: skipped, not crashed, and no name invented.
    assert bundle.techniques == ()
    assert bundle.total_attack_patterns == 1


def test_kill_chain_phases_from_other_frameworks_are_ignored() -> None:
    obj = {
        "type": "attack-pattern",
        "id": "attack-pattern--phases",
        "name": "Phases",
        "external_references": [
            {"source_name": FRAMEWORK, "external_id": "T1110"}
        ],
        "kill_chain_phases": [
            {"kill_chain_name": "lockheed-martin-cyber-kill-chain", "phase_name": "actions"},
            {"kill_chain_name": FRAMEWORK, "phase_name": "credential-access"},
        ],
    }
    bundle = _parse(_bundle([obj]))
    assert bundle.techniques[0].tactics == ("credential-access",)


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        '{"type": "bundle", "objects": [}',
        '{"type": "bundle",',
    ],
)
def test_malformed_json_raises(payload: str) -> None:
    with pytest.raises(InvalidMitreBundleError):
        _parse(payload)


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps([1, 2, 3]),
        json.dumps("a string"),
        json.dumps(42),
        json.dumps(None),
    ],
)
def test_non_object_root_raises(payload: str) -> None:
    with pytest.raises(InvalidMitreBundleError):
        _parse(payload)


@pytest.mark.parametrize(
    "document",
    [
        {"objects": []},  # missing type
        {"type": "collection", "objects": []},  # wrong type
        {"type": "bundle"},  # missing objects
        {"type": "bundle", "objects": {}},  # wrong objects type
    ],
)
def test_bad_bundle_shape_raises(document: dict[str, object]) -> None:
    with pytest.raises(InvalidMitreBundleError):
        _parse(json.dumps(document))


def test_spec_version_1_2_raises() -> None:
    with pytest.raises(InvalidMitreBundleError):
        _parse(_bundle([], spec_version="1.2"))


def test_non_string_spec_version_raises() -> None:
    document: dict[str, object] = {"type": "bundle", "spec_version": 2.1, "objects": []}
    with pytest.raises(InvalidMitreBundleError):
        _parse(json.dumps(document))


def test_missing_spec_version_defaults_to_2_1() -> None:
    bundle = _parse(_bundle([], spec_version=None))
    assert bundle.spec_version == "2.1"


def test_spec_version_2_0_is_accepted() -> None:
    bundle = _parse(_bundle([], spec_version="2.0"))
    assert bundle.spec_version == "2.0"


def test_invalid_utf8_bytes_raise() -> None:
    with pytest.raises(InvalidMitreBundleError):
        _parse(b"\xff\xfe\x00\x01")


def test_bytes_payload_is_decoded() -> None:
    raw = _bundle([_attack_pattern(stix_id="attack-pattern--b", attack_id="T1110")])
    assert _parse(raw.encode("utf-8")).techniques == _parse(raw).techniques


def test_empty_objects_returns_empty_techniques() -> None:
    bundle = _parse(_bundle([]))

    assert bundle.techniques == ()
    assert bundle.skipped_ids == ()
    assert bundle.total_attack_patterns == 0
    assert bundle.spec_version == "2.1"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_parsing_twice_is_reproducible() -> None:
    raw = _bundle(
        [
            _attack_pattern(
                stix_id="attack-pattern--3",
                attack_id="T1548",
                tactics=["privilege-escalation"],
                platforms=["Windows"],
            ),
            _attack_pattern(stix_id="attack-pattern--1", attack_id="T1110"),
            _attack_pattern(stix_id="attack-pattern--2", attack_id="T1059"),
        ]
    )
    first = _parse(raw)
    second = _parse(raw)

    assert first == second
    assert first.techniques == second.techniques


def test_technique_order_depends_on_id_not_bundle_order() -> None:
    objects = [
        _attack_pattern(stix_id="attack-pattern--3", attack_id="T1548"),
        _attack_pattern(stix_id="attack-pattern--1", attack_id="T1110"),
        _attack_pattern(stix_id="attack-pattern--2", attack_id="T1059"),
    ]
    expected = ["T1059", "T1110", "T1548"]
    assert [t.technique_id for t in _parse(_bundle(objects)).techniques] == expected

    shuffled = list(objects)
    random.Random(1234).shuffle(shuffled)
    assert [t.technique_id for t in _parse(_bundle(shuffled)).techniques] == expected


def test_skipped_ids_are_sorted_and_deduped() -> None:
    raw = _bundle(
        [
            _attack_pattern(
                stix_id="attack-pattern--z", attack_id="T1430", domains=["mobile-attack"]
            ),
            _attack_pattern(
                stix_id="attack-pattern--a", attack_id="T1430", domains=["mobile-attack"]
            ),
            _attack_pattern(stix_id="attack-pattern--x", attack_id="T1110", revoked=True),
        ]
    )
    bundle = _parse(raw)
    assert bundle.skipped_ids == ("T1110", "T1430")
