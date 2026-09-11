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

    This is the DOMAIN allowlist (domain-model.md §22). Only the subset in
    :data:`response.actions.EXECUTABLE_ACTION_KEYS` can actually be executed
    against HISIEM in V1 — the rest are reserved semantic keys that map to no
    supported HISIEM capability yet and therefore can never form an executable
    proposal.
    """

    BLOCK_SOURCE_IP = "BLOCK_SOURCE_IP"
    DISABLE_ACCOUNT = "DISABLE_ACCOUNT"
    ISOLATE_HOST = "ISOLATE_HOST"
    START_SOAR_PLAYBOOK = "START_SOAR_PLAYBOOK"


class ResponseExecutionStatus(enum.StrEnum):
    """Lifecycle of one approved response execution (a ResponseExecutionRef).

    Distinct from :class:`ResponseProposalStatus`: the proposal tracks the
    approval lifecycle, the execution ref tracks the side-effect fact. Neither is
    the other's status. ``SUCCEEDED``/``FAILED`` are terminal.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {ResponseExecutionStatus.SUCCEEDED, ResponseExecutionStatus.FAILED}
