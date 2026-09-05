"""Private GP-01 scenario oracle (E1-B.4 §12).

The oracle is a pure projection of the committed :class:`ScenarioSpec`: it records
semantic FACTS and evidence requirements — never fixed Finding wording — so the
scorer performs a grounded investigation evaluation rather than a prompt
memorization benchmark. Watermark/control roles (W1) are deliberately ABSENT from
the evidence requirements so they can never satisfy semantic evidence.

Evidence requirements are a SUBSET of the declared semantic ground-truth roles,
never the full ground-truth set by force. After the correctness freeze the source
alert authoritatively proves the brute-force failure threshold, so GP-01 requires
the agent to DISCOVER only the S1 success (``GP01_REQUIRED_EVIDENCE_ROLES ==
("S1",)``); F1..F5 stay ground-truth dataset events but are not mandatory
re-retrievals. The generic invariant enforced here is the subset containment, not
any specific GP-01 tuple.

This module is pure and deterministic: no provider, model, or DB I/O. Production
packages MUST NOT import it (E1-B.4 §13); the harness exposes only the launch
projection of a sealed manifest.
"""

from __future__ import annotations

from .contracts import (
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
    ``required_evidence_roles`` is carried VERBATIM from ``scenario``. It must be
    a SUBSET of ``scenario.semantic_roles`` — the generic invariant is containment,
    not equality, because the source alert already proves the brute-force
    threshold so GP-01 requires only the S1 success as agent-discovered evidence
    (a fresh ``ScenarioSpec()`` default is that contract). The W1 watermark
    control is never an evidence role.

    Raises :class:`OracleIsolationViolation` if ``scenario`` declares a control
    role among its evidence requirements, or an evidence role that is not among
    the declared semantic ground-truth roles.
    """
    if scenario.control_role in scenario.required_evidence_roles:
        raise OracleIsolationViolation(
            f"scenario {scenario.id} lists control role {scenario.control_role!r} "
            "among required_evidence_roles — W1 must be isolated from semantic "
            "evidence requirements",
        )
    semantic = set(scenario.semantic_roles)
    evidence = set(scenario.required_evidence_roles)
    if not evidence <= semantic:
        raise OracleIsolationViolation(
            f"scenario {scenario.id} evidence roles {sorted(evidence)} are not a "
            f"subset of the declared semantic roles {sorted(semantic)}",
        )
    return oracle_from_scenario(scenario)
