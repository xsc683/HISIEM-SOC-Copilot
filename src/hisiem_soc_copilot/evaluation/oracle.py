"""Private GP-01 scenario oracle (E1-B.4 §12).

The oracle is a pure projection of the committed :class:`ScenarioSpec`: it records
semantic FACTS and evidence requirements — never fixed Finding wording — so the
scorer performs a grounded investigation evaluation rather than a prompt
memorization benchmark. Watermark/control roles (W1) are deliberately ABSENT from
the evidence requirements so they can never satisfy semantic evidence.

This module is pure and deterministic: no provider, model, or DB I/O. Production
packages MUST NOT import it (E1-B.4 §13); the harness exposes only the launch
projection of a sealed manifest.
"""

from __future__ import annotations

from .contracts import (
    GP01_SEMANTIC_ROLES,
    ScenarioOracle,
    ScenarioSpec,
    oracle_from_scenario,
)
from .errors import OracleIsolationViolation

__all__ = ["scenario_oracle"]


def scenario_oracle(scenario: ScenarioSpec) -> ScenarioOracle:
    """Derive the canonical private oracle for ``scenario``.

    ``expected_verdict`` is MALICIOUS, ``facts`` preserve the declared
    ScenarioSpec order (FAILURE_SEQUENCE, POST_FAILURE_SUCCESS), and
    ``required_evidence_roles`` are the semantic ground-truth roles F1..F5,S1.
    The W1 watermark control is never an evidence role.

    Raises :class:`OracleIsolationViolation` if ``scenario`` declares a control
    role among its evidence requirements, or its evidence roles diverge from the
    committed semantic ground-truth roles.
    """
    if scenario.control_role in scenario.required_evidence_roles:
        raise OracleIsolationViolation(
            f"scenario {scenario.id} lists control role {scenario.control_role!r} "
            "among required_evidence_roles — W1 must be isolated from semantic "
            "evidence requirements",
        )
    evidence = tuple(
        role for role in scenario.required_evidence_roles if role != scenario.control_role
    )
    if set(evidence) != set(GP01_SEMANTIC_ROLES):
        raise OracleIsolationViolation(
            f"scenario {scenario.id} evidence roles {list(evidence)!r} do not equal "
            f"the committed semantic roles {list(GP01_SEMANTIC_ROLES)!r}",
        )
    return oracle_from_scenario(scenario)
