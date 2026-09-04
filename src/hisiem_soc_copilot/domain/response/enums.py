"""Response domain enums.

Match domain-model.md / persistence-schema.md exactly.
"""

from __future__ import annotations

import enum


class ResponseProposalStatus(enum.StrEnum):
    CREATED = "CREATED"
    DENIED = "DENIED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUBMITTED = "SUBMITTED"


class PolicyDecision(enum.StrEnum):
    """V1 policy has only DENY and REQUIRE_APPROVAL (never ALLOW_AUTOMATIC)."""

    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class ApprovalDecisionKind(enum.StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class ResponseActionKey(enum.StrEnum):
    """Actions must come from a system allowlist; the model may not define arbitrary actions.

    V1 representative examples (domain-model.md §22). The actual SOAR playbook
    mapping is validated against the HISIEM action registry before execution.
    """

    BLOCK_SOURCE_IP = "BLOCK_SOURCE_IP"
    DISABLE_ACCOUNT = "DISABLE_ACCOUNT"
    ISOLATE_HOST = "ISOLATE_HOST"
    START_SOAR_PLAYBOOK = "START_SOAR_PLAYBOOK"
