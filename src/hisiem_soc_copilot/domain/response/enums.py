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


class ResponseSubmissionStatus(enum.StrEnum):
    """Local lifecycle of the ONE approved submission to the provider.

    This is deliberately NOT a provider execution status. It records whether the
    local submission command has been accepted by HISIEM yet, so the workspace can
    tell "approved, not submitted yet", "retrying" and "the provider definitively
    refused this submission" apart WITHOUT inventing a provider execution identity
    (which would be a lie, and would collide on ``(provider, execution_id)``).

    ``FAILED_DEFINITIVE`` means the provider did not accept the submission. It is
    NOT ``ResponseExecutionStatus.FAILED``: no provider execution was ever created,
    so there is nothing to observe and nothing to reconcile.

    ``ATTENTION_REQUIRED`` means the AUTOMATIC retry budget ran out while the
    failures were still TRANSIENT/UNCERTAIN. It deliberately does NOT claim that the
    provider refused the submission, and it does NOT claim that no execution exists
    — we genuinely do not know, because every attempt failed before the provider
    gave us an answer. Someone has to look. This is a terminal LOCAL state: the
    delivery is no longer retried automatically, so the workspace can stop telling
    the analyst that a retry is still in flight.
    """

    PENDING = "PENDING"
    RETRYING = "RETRYING"
    SUBMITTED = "SUBMITTED"
    FAILED_DEFINITIVE = "FAILED_DEFINITIVE"
    ATTENTION_REQUIRED = "ATTENTION_REQUIRED"

    @property
    def is_terminal(self) -> bool:
        """No further local submission work will happen in this state."""
        return self in {
            ResponseSubmissionStatus.SUBMITTED,
            ResponseSubmissionStatus.FAILED_DEFINITIVE,
            ResponseSubmissionStatus.ATTENTION_REQUIRED,
        }

    @property
    def is_awaiting_provider(self) -> bool:
        """Still local-only: no provider execution identity exists yet."""
        return not self.is_terminal


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
