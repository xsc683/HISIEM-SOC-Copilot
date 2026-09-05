"""Load the committed GP-01 scenario and expose its source bytes + hashes.

Scenario identity has two layers (E1-B.4 §15/§16): ``source_file_sha256`` binds
the exact committed source bytes (formatting churn would change it), while
``semantic_sha256`` hashes a canonical JSON of the parsed :class:`ScenarioSpec`
so it changes only when scenario MEANING changes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

from .contracts import ScenarioSpec, canonical_json, sha256_hex

# Canonical committed GP-01 scenario source. Constructing the ScenarioSpec from
# committed constants (contracts already freezes the rule + semantic roles + the
# logical dataset) means no separate data file is warranted.
GP01_SCENARIO_SOURCE_NAME: Final = "gp01"
GP01_SCENARIO_SOURCE: Final = """\
scenario:
  id: gp-01
  version: "1"
  rule:
    id: rule-ssh-brute-force-001
    key_field: source.ip
    condition: authentication_failure
    threshold: 5
    window_minutes: 5
  semantic_roles: [F1, F2, F3, F4, F5, S1]
  control_role: W1
  expected_verdict: MALICIOUS
  facts:
    - FAILURE_SEQUENCE
    - POST_FAILURE_SUCCESS
  required_evidence_roles: [F1, F2, F3, F4, F5, S1]
"""

_CANONICAL_SCENARIO: Final = ScenarioSpec()

__all__ = [
    "GP01_SCENARIO_SOURCE",
    "GP01_SCENARIO_SOURCE_NAME",
    "load_scenario",
    "scenario_source_bytes",
    "semantic_sha256",
    "source_file_sha256",
]


def _semantic_payload(scenario: ScenarioSpec) -> dict[str, object]:
    """Ordered semantic JSON payload for :class:`ScenarioSpec`."""
    return {
        "id": scenario.id,
        "version": scenario.version,
        "rule_id": scenario.rule_id,
        "expected_verdict": scenario.expected_verdict,
        "control_role": scenario.control_role,
        "facts": [
            {"id": fact_id, "description": description}
            for fact_id, description in scenario.facts
        ],
        "semantic_roles": list(scenario.semantic_roles),
        "failure_roles": list(scenario.failure_roles),
        "required_evidence_roles": list(scenario.required_evidence_roles),
    }


def load_scenario(source_path: Path | None = None) -> ScenarioSpec:
    """Load the committed GP-01 scenario as a parsed :class:`ScenarioSpec`.

    ``source_path`` is accepted for callers that ship a real committed scenario
    file; for GP-01 the canonical spec is built from the frozen committed
    constants, so it is returned directly.
    """
    if source_path is not None:
        raise FileNotFoundError(
            f"no committed GP-01 scenario file exists; source_path={source_path} is not supported"
        )
    return _CANONICAL_SCENARIO


def scenario_source_bytes() -> bytes:
    """Exact committed GP-01 scenario source bytes (UTF-8)."""
    return GP01_SCENARIO_SOURCE.encode("utf-8")


def source_file_sha256() -> str:
    """SHA-256 of the exact committed GP-01 scenario source bytes."""
    return hashlib.sha256(scenario_source_bytes()).hexdigest()


def semantic_sha256(scenario: ScenarioSpec | None = None) -> str:
    """SHA-256 of the canonical JSON of ``scenario`` semantic content.

    Uses the project-owned canonical serializer, so the digest changes only when
    scenario meaning changes, not formatting/order. Defaults to the committed
    GP-01 spec.
    """
    spec = scenario if scenario is not None else _CANONICAL_SCENARIO
    return sha256_hex(canonical_json(_semantic_payload(spec)))
