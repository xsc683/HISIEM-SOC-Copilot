"""Response policy validation (V1 fixed rules).

Policy lives in the domain so no caller can bypass it. V1 only ever yields
DENY or REQUIRE_APPROVAL — never ALLOW_AUTOMATIC (domain-model.md §25).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..investigation.enums import VerdictDisposition
from .enums import PolicyDecision, ResponseActionKey


class PolicyDenyReason(StrEnum):
    UNKNOWN_ACTION = "unknown_action"
    MISSING_TARGET = "missing_target"
    CROSS_TENANT_TARGET = "cross_tenant_target"
    UNRESOLVED_TARGET = "unresolved_target"
    MISSING_EVIDENCE = "missing_evidence"
    DENYING_VERDICT = "denying_verdict"
    INVALID_PARAMETERS = "invalid_parameters"


@dataclass(frozen=True)
class PolicyOutcome:
    decision: PolicyDecision
    reason: PolicyDenyReason | str | None = None
    detail: str | None = None

    @property
    def is_allowed_to_ask_human(self) -> bool:
        return self.decision == PolicyDecision.REQUIRE_APPROVAL


def action_is_registered(action_key: str) -> bool:
    return any(action_key == action.value for action in ResponseActionKey)


def evaluate_response_policy(
    *,
    action_key: str,
    verdict_disposition: VerdictDisposition | None,
    target_refs: list[object],
    evidence_ids: list[object],
    parameters: dict[str, object],
    tenant_id: str,
) -> PolicyOutcome:
    """Static V1 policy evaluation for a proposed response action.

    Every check is deliberately conservative: any unknown/unresolved input DENies
    rather than escalating to a human.
    """
    if not action_is_registered(action_key):
        return PolicyOutcome(PolicyDecision.DENY, PolicyDenyReason.UNKNOWN_ACTION)
    if not target_refs:
        return PolicyOutcome(PolicyDecision.DENY, PolicyDenyReason.MISSING_TARGET)
    if not evidence_ids:
        return PolicyOutcome(PolicyDecision.DENY, PolicyDenyReason.MISSING_EVIDENCE)
    for target in target_refs:
        # A target entering domain policy must already be a resolved
        # ExternalResourceRef. Same-tenant ownership is enforced earlier at target
        # resolution against the authenticated tenant context (domain-model.md §24);
        # here we only hard-deny an explicitly mismatched owner if present.
        if not hasattr(target, "address_id") or not target.address_id:
            return PolicyOutcome(
                PolicyDecision.DENY, PolicyDenyReason.UNRESOLVED_TARGET
            )
        owner_tenant = getattr(target, "tenant_id", None)
        if owner_tenant is not None and owner_tenant != tenant_id:
            return PolicyOutcome(
                PolicyDecision.DENY, PolicyDenyReason.CROSS_TENANT_TARGET
            )
    # Low-confidence/unknown verdicts never bypass the human gate in V1.
    if (
        verdict_disposition is None
        or verdict_disposition == VerdictDisposition.INCONCLUSIVE
    ):
        return PolicyOutcome(
            PolicyDecision.DENY,
            PolicyDenyReason.DENYING_VERDICT,
            detail=(
                "V1 policy DENies responses without a definitive "
                "MALICIOUS/BENIGN verdict"
            ),
        )
    return PolicyOutcome(PolicyDecision.REQUIRE_APPROVAL)
