"""Unit tests for ResponseProposal aggregate + policy boundary."""

from __future__ import annotations

from uuid import uuid4

import pytest

from hisiem_soc_copilot.domain.investigation.enums import VerdictDisposition
from hisiem_soc_copilot.domain.investigation.value_objects import ExternalResourceRef
from hisiem_soc_copilot.domain.response.aggregate import ResponseProposal
from hisiem_soc_copilot.domain.response.enums import (
    PolicyDecision,
    ResponseActionKey,
    ResponseProposalStatus,
)
from hisiem_soc_copilot.domain.response.policy import (
    PolicyDenyReason,
    evaluate_response_policy,
)
from hisiem_soc_copilot.domain.shared.errors import DomainError, StateTransitionError


def _proposal(**overrides) -> ResponseProposal:
    values = dict(
        id=uuid4(),
        investigation_id=uuid4(),
        result_id=uuid4(),
        action_key=ResponseActionKey.BLOCK_SOURCE_IP.value,
        parameters={"ip": "203.0.113.7"},
        reason="Blocked repeat offender",
        target_refs=[
            ExternalResourceRef(
                provider="hisiem",
                resource_type="ip",
                address_id="203.0.113.7",
                business_id="203.0.113.7",
            )
        ],
        evidence_ids=[uuid4()],
        policy_decision=PolicyDecision.REQUIRE_APPROVAL,
    )
    values.update(overrides)
    return ResponseProposal(**values)


def test_proposal_content_hash_is_stable() -> None:
    a = _proposal()
    b = _proposal()
    assert a.content_hash == b.content_hash
    assert len(a.content_hash) == 64


def test_proposal_hash_binds_revision_and_content() -> None:
    p = _proposal()
    assert p.content_hash_matches(p.content_revision, p.content_hash)
    assert not p.content_hash_matches(2, p.content_hash)
    assert not p.content_hash_matches(p.content_revision, "0" * 64)


def test_proposal_full_approval_flow() -> None:
    p = _proposal()
    assert p.status == ResponseProposalStatus.CREATED
    p.request_approval()
    assert p.status == ResponseProposalStatus.WAITING_APPROVAL
    req_id = uuid4()
    p.approve(approval_request_id=req_id)
    assert p.status == ResponseProposalStatus.APPROVED
    assert p.approval_request_id == req_id


def test_proposal_deny() -> None:
    p = _proposal()
    p.deny(reason="policy cross-tenant")
    assert p.status == ResponseProposalStatus.DENIED
    with pytest.raises(StateTransitionError):
        p.request_approval()


def test_illegal_transition_from_approved() -> None:
    p = _proposal()
    p.request_approval()
    p.approve()
    with pytest.raises(StateTransitionError):
        p.reject()


def test_policy_denies_unknown_action() -> None:
    outcome = evaluate_response_policy(
        action_key="SHELL_EXECUTE",
        verdict_disposition=VerdictDisposition.MALICIOUS,
        target_refs=[_proposal().target_refs[0]],
        evidence_ids=[uuid4()],
        parameters={},
        tenant_id="t1",
    )
    assert outcome.decision == PolicyDecision.DENY
    assert outcome.reason == PolicyDenyReason.UNKNOWN_ACTION


def test_policy_denies_inconclusive_verdict() -> None:
    outcome = evaluate_response_policy(
        action_key=ResponseActionKey.BLOCK_SOURCE_IP.value,
        verdict_disposition=VerdictDisposition.INCONCLUSIVE,
        target_refs=[_proposal().target_refs[0]],
        evidence_ids=[uuid4()],
        parameters={},
        tenant_id="t1",
    )
    assert outcome.decision == PolicyDecision.DENY
    assert outcome.reason == PolicyDenyReason.DENYING_VERDICT


def test_policy_requires_approval_for_registered_malicious_action() -> None:
    outcome = evaluate_response_policy(
        action_key=ResponseActionKey.BLOCK_SOURCE_IP.value,
        verdict_disposition=VerdictDisposition.MALICIOUS,
        target_refs=[_proposal().target_refs[0]],
        evidence_ids=[uuid4()],
        parameters={},
        tenant_id="t1",
    )
    assert outcome.decision == PolicyDecision.REQUIRE_APPROVAL
    assert outcome.is_allowed_to_ask_human


def test_validate_for_approval_rejects_unresolved_or_unverified() -> None:
    # no policy decision yet → domain error
    p = _proposal(policy_decision=None)
    with pytest.raises(DomainError):
        p.validate_for_approval()
