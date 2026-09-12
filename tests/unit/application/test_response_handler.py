"""Response workflow command-handler tests over in-memory fakes (P2 closure).

Covers the two load-bearing invariants:

* "Agent can recommend; Agent cannot authorize." — every write requires an
  immutable human approval, and approval binds the exact proposal revision+hash.
* "Data can inform decisions; Data cannot authorize actions." — the action TARGET
  is derived from the persisted Investigation source alert and the supporting
  evidence is re-resolved authoritatively; no browser-supplied identity is ever
  persisted (spec §1), and approval creates a durable SUBMISSION INTENT — never a
  fabricated provider execution reference (spec §2).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from hisiem_soc_copilot.application.commands.response import DecideResponseApproval
from hisiem_soc_copilot.domain.investigation.enums import VerdictDisposition
from hisiem_soc_copilot.domain.response.actions import ResponseActionUnsupportedError
from hisiem_soc_copilot.domain.response.enums import (
    ApprovalDecisionKind,
    ResponseProposalStatus,
)
from hisiem_soc_copilot.domain.response.errors import (
    ApprovalContractError,
    ApprovalDecisionAlreadyExistsError,
    ResponseEvidenceInvalidError,
    ResponseInvestigationNotCompletedError,
)
from hisiem_soc_copilot.domain.shared.errors import DomainError
from tests.fixtures.fakes import FakeUnitOfWorkFactory
from tests.fixtures.response_flow import (
    ALERT,
    OTHER_TENANT,
    TENANT,
    approval_request_id,
    approve,
    create_proposal,
    handler,
    seed_investigation,
    target,
)

# ---------------------------------------------------------------------------
# §1.1 — the target is DERIVED, never accepted from the caller
# ---------------------------------------------------------------------------


async def test_proposal_target_is_derived_from_the_persisted_source_alert() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, _ = seed_investigation(factory)

    proposal = await create_proposal(factory, investigation_id)

    investigation = await factory().investigations.get(
        tenant_id=TENANT, investigation_id=investigation_id
    )
    assert investigation is not None
    assert proposal.target_refs == [investigation.source_alert_ref]
    assert proposal.target_refs[0].address_id == ALERT
    assert proposal.status == ResponseProposalStatus.WAITING_APPROVAL
    assert proposal.policy_decision is not None
    assert proposal.policy_decision.value == "REQUIRE_APPROVAL"


async def test_create_command_carries_no_target_field() -> None:
    """Structural proof: there is no caller-supplied target to forge (spec §1.1)."""
    import dataclasses

    from hisiem_soc_copilot.application.commands.response import CreateResponseProposal

    names = {f.name for f in dataclasses.fields(CreateResponseProposal)}
    assert "target_ref" not in names
    assert "target" not in names
    assert {"action_key", "evidence_ids", "parameters", "reason"} <= names


async def test_proposal_requires_a_completed_investigation() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, _ = seed_investigation(factory, completed=False)

    with pytest.raises(ResponseInvestigationNotCompletedError):
        await create_proposal(factory, investigation_id)


# ---------------------------------------------------------------------------
# §1.2 — authoritative evidence resolution
# ---------------------------------------------------------------------------


async def test_evidence_of_this_investigation_is_accepted_and_resolved() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None

    proposal = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id,)
    )

    # The SERVER-RESOLVED identity is persisted, not the caller's raw string.
    assert proposal.evidence_ids == [evidence_id]


async def test_unknown_evidence_id_is_rejected() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, _ = seed_investigation(factory)

    with pytest.raises(ResponseEvidenceInvalidError):
        await create_proposal(factory, investigation_id, evidence_ids=(uuid4(),))


async def test_evidence_of_another_investigation_is_rejected() -> None:
    """Same tenant, different Investigation — never resolvable in scope."""
    factory = FakeUnitOfWorkFactory()
    investigation_id, _ = seed_investigation(factory)
    _other_id, other_evidence_id = seed_investigation(
        factory, alert="alert-9999", business_id="AL-9"
    )
    assert other_evidence_id is not None

    with pytest.raises(ResponseEvidenceInvalidError):
        await create_proposal(
            factory, investigation_id, evidence_ids=(other_evidence_id,)
        )


async def test_foreign_tenant_evidence_is_rejected() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, _ = seed_investigation(factory)
    _foreign_id, foreign_evidence_id = seed_investigation(
        factory, tenant_id=OTHER_TENANT, alert="alert-7777", business_id="AL-7"
    )
    assert foreign_evidence_id is not None

    with pytest.raises(ResponseEvidenceInvalidError):
        await create_proposal(
            factory, investigation_id, evidence_ids=(foreign_evidence_id,)
        )


async def test_mixed_scope_evidence_is_rejected_as_a_whole() -> None:
    """One in-scope id cannot be used to smuggle an out-of-scope id through."""
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None

    with pytest.raises(ResponseEvidenceInvalidError):
        await create_proposal(
            factory, investigation_id, evidence_ids=(evidence_id, uuid4())
        )


async def test_duplicate_evidence_ids_are_canonicalised_not_a_bypass() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None

    proposal = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id, evidence_id)
    )
    assert proposal.evidence_ids == [evidence_id]


async def test_duplicate_cannot_smuggle_an_unresolvable_id_through() -> None:
    """The canonicalised set is validated as a WHOLE — no count/set bypass."""
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None

    with pytest.raises(ResponseEvidenceInvalidError):
        await create_proposal(
            factory,
            investigation_id,
            evidence_ids=(evidence_id, evidence_id, uuid4()),
        )


async def test_empty_evidence_is_rejected_by_the_handler() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, _ = seed_investigation(factory)

    with pytest.raises(ResponseEvidenceInvalidError):
        await handler(factory).create_response_proposal(
            _command(investigation_id, evidence_ids=())
        )


def _command(investigation_id, *, evidence_ids: tuple[str, ...]):
    from hisiem_soc_copilot.application.commands.response import CreateResponseProposal

    return CreateResponseProposal(
        tenant_id=TENANT,
        investigation_id=investigation_id,
        action_key="START_SOAR_PLAYBOOK",
        evidence_ids=evidence_ids,
        parameters={"playbook_id": "11111111-2222-3333-4444-555555555555"},
        reason="contain the intrusion",
        initiated_by_subject="analyst",
    )


# ---------------------------------------------------------------------------
# bounded action contract + policy + investigation linkage
# ---------------------------------------------------------------------------


async def test_unsupported_action_is_rejected() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None
    with pytest.raises(ResponseActionUnsupportedError):
        await create_proposal(
            factory,
            investigation_id,
            evidence_ids=(evidence_id,),
            action_key="BLOCK_SOURCE_IP",
            parameters={"ip": "203.0.113.9"},
        )


async def test_missing_required_parameter_is_rejected() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None
    with pytest.raises(DomainError):
        await create_proposal(
            factory, investigation_id, evidence_ids=(evidence_id,), parameters={}
        )


async def test_inconclusive_verdict_is_policy_denied_without_approval() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(
        factory, disposition=VerdictDisposition.INCONCLUSIVE
    )
    assert evidence_id is not None
    proposal = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id,)
    )
    assert proposal.status == ResponseProposalStatus.DENIED
    assert (
        await factory().response_approvals.get_request_by_proposal(
            tenant_id=TENANT, proposal_id=proposal.id
        )
        is None
    )


async def test_create_proposal_is_idempotent_per_investigation() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None
    first = await create_proposal(factory, investigation_id, evidence_ids=(evidence_id,))
    second = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id,)
    )
    assert first.id == second.id


async def test_creating_a_proposal_links_it_to_the_investigation() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None
    proposal = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id,)
    )

    investigation = await factory().investigations.get(
        tenant_id=TENANT, investigation_id=investigation_id
    )
    assert investigation is not None
    assert investigation.response_proposal_id == proposal.id
    # The association never reopens or perturbs the investigation lifecycle.
    assert investigation.status.value == "COMPLETED"
    assert investigation.finished_at is not None
    assert investigation.termination_reason is not None


# ---------------------------------------------------------------------------
# §2 — approval = durable SUBMISSION INTENT, never a fabricated execution
# ---------------------------------------------------------------------------


async def test_approve_binds_exact_contract_and_creates_no_execution_ref() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None
    proposal = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id,)
    )

    outcome = await approve(factory, proposal)

    assert outcome.execution_queued is True
    assert outcome.proposal.status == ResponseProposalStatus.APPROVED
    # HISIEM has not been called, so NO provider execution reference may exist.
    assert (
        await factory().response_executions.get_by_proposal(
            tenant_id=TENANT, proposal_id=proposal.id
        )
        is None
    )
    decisions = await factory().response_approvals.get_decision(
        tenant_id=TENANT,
        approval_request_id=await approval_request_id(
            factory, proposal_id=proposal.id
        ),
    )
    assert decisions is not None and decisions.actor_subject_id == "operator"


async def test_approve_creates_exactly_one_durable_submission_outbox_row() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None
    proposal = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id,)
    )
    before = len(factory.outbox.rows)

    await approve(factory, proposal)

    queued = [
        e for e in factory.events.events if e.event_type == "response_execution_queued"
    ]
    assert len(queued) == 1
    # The event payload carries the STABLE submission key, never an execution id.
    assert queued[0].payload["submission_key"] == f"response:{TENANT}:{proposal.id}"
    rows = [
        row
        for row in factory.outbox.rows.values()
        if row["destination"] == "response.execution.submit"
    ]
    assert len(rows) == 1
    assert len(factory.outbox.rows) == before + 1


async def test_two_approvals_before_any_provider_call_do_not_collide() -> None:
    """No fabricated ``ResponseExecutionRef`` means no UNIQUE(provider, execution_id)
    collision when two proposals are queued before either is submitted (spec §2)."""
    factory = FakeUnitOfWorkFactory()
    first_id, first_evidence = seed_investigation(factory, alert="alert-a", business_id="A")
    second_id, second_evidence = seed_investigation(
        factory, alert="alert-b", business_id="B"
    )
    assert first_evidence is not None and second_evidence is not None
    first = await create_proposal(factory, first_id, evidence_ids=(first_evidence,))
    second = await create_proposal(factory, second_id, evidence_ids=(second_evidence,))

    await approve(factory, first)
    await approve(factory, second)  # must NOT raise a uniqueness violation

    assert (
        await factory().response_executions.get_by_proposal(
            tenant_id=TENANT, proposal_id=first.id
        )
        is None
    )
    assert (
        await factory().response_executions.get_by_proposal(
            tenant_id=TENANT, proposal_id=second.id
        )
        is None
    )


async def test_reject_records_decision_and_zero_submission() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None
    proposal = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id,)
    )
    request_id = await approval_request_id(factory, proposal_id=proposal.id)

    outcome = await handler(factory).decide_response_approval(
        DecideResponseApproval(
            tenant_id=TENANT,
            approval_request_id=request_id,
            decision=ApprovalDecisionKind.REJECT,
            expected_revision=proposal.content_revision,
            expected_content_hash=proposal.content_hash,
            initiated_by_subject="operator",
            reason="insufficient evidence",
        )
    )
    assert outcome.proposal.status == ResponseProposalStatus.REJECTED
    assert outcome.execution_queued is False
    assert (
        await factory().response_executions.get_by_proposal(
            tenant_id=TENANT, proposal_id=proposal.id
        )
        is None
    )
    assert not [
        e for e in factory.events.events if e.event_type == "response_execution_queued"
    ]
    assert not [
        row
        for row in factory.outbox.rows.values()
        if row["destination"] == "response.execution.submit"
    ]


async def test_stale_revision_is_contract_mismatch() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None
    proposal = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id,)
    )
    with pytest.raises(ApprovalContractError):
        await handler(factory).decide_response_approval(
            DecideResponseApproval(
                tenant_id=TENANT,
                approval_request_id=await approval_request_id(
                    factory, proposal_id=proposal.id
                ),
                decision=ApprovalDecisionKind.APPROVE,
                expected_revision=proposal.content_revision + 1,
                expected_content_hash=proposal.content_hash,
                initiated_by_subject="operator",
            )
        )


async def test_stale_hash_is_contract_mismatch() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None
    proposal = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id,)
    )
    with pytest.raises(ApprovalContractError):
        await handler(factory).decide_response_approval(
            DecideResponseApproval(
                tenant_id=TENANT,
                approval_request_id=await approval_request_id(
                    factory, proposal_id=proposal.id
                ),
                decision=ApprovalDecisionKind.APPROVE,
                expected_revision=proposal.content_revision,
                expected_content_hash="deadbeef",
                initiated_by_subject="operator",
            )
        )


async def test_conflicting_second_decision_is_rejected() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None
    proposal = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id,)
    )
    request_id = await approval_request_id(factory, proposal_id=proposal.id)
    await approve(factory, proposal)

    with pytest.raises(ApprovalDecisionAlreadyExistsError):
        await handler(factory).decide_response_approval(
            DecideResponseApproval(
                tenant_id=TENANT,
                approval_request_id=request_id,
                decision=ApprovalDecisionKind.REJECT,
                expected_revision=proposal.content_revision,
                expected_content_hash=proposal.content_hash,
                initiated_by_subject="operator-2",
            )
        )


async def test_duplicate_approve_is_idempotent_and_queues_one_submission() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None
    proposal = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id,)
    )
    request_id = await approval_request_id(factory, proposal_id=proposal.id)
    command = DecideResponseApproval(
        tenant_id=TENANT,
        approval_request_id=request_id,
        decision=ApprovalDecisionKind.APPROVE,
        expected_revision=proposal.content_revision,
        expected_content_hash=proposal.content_hash,
        initiated_by_subject="operator",
    )
    await handler(factory).decide_response_approval(command)
    outcome = await handler(factory).decide_response_approval(command)  # exact retry

    assert outcome.execution_queued is True
    queued = [
        e for e in factory.events.events if e.event_type == "response_execution_queued"
    ]
    assert len(queued) == 1  # no second logical execution


async def test_foreign_tenant_cannot_create_a_proposal() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None
    from hisiem_soc_copilot.application.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await create_proposal(
            factory,
            investigation_id,
            tenant_id=OTHER_TENANT,
            evidence_ids=(evidence_id,),
        )


async def test_foreign_tenant_cannot_approve() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None
    proposal = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id,)
    )
    request_id = await approval_request_id(factory, proposal_id=proposal.id)
    from hisiem_soc_copilot.application.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await handler(factory).decide_response_approval(
            DecideResponseApproval(
                tenant_id=OTHER_TENANT,
                approval_request_id=request_id,
                decision=ApprovalDecisionKind.APPROVE,
                expected_revision=proposal.content_revision,
                expected_content_hash=proposal.content_hash,
                initiated_by_subject="operator",
            )
        )


def test_source_alert_target_helper_matches_alerts() -> None:
    assert target().address_id == ALERT


# ---------------------------------------------------------------------------
# proposer provenance (immutable, server-derived, never from the body)
# ---------------------------------------------------------------------------


async def test_proposal_records_the_server_derived_proposer() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None

    proposal = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id,)
    )

    assert proposal.created_by_subject == "analyst"
    # NOT borrowed from the investigation initiator — they answer different questions.
    investigation = await factory().investigations.get(
        tenant_id=TENANT, investigation_id=investigation_id
    )
    assert investigation is not None
    assert investigation.initiated_by.subject_id == "analyst"

    event = next(
        e for e in factory.events.events if e.event_type == "response_proposal_created"
    )
    assert event.actor_subject_id == "analyst"
    assert event.payload["created_by_subject"] == "analyst"

    # Survives a reload (it is persisted provenance, not an in-memory field).
    reloaded = await factory().response_proposals.get(
        tenant_id=TENANT, proposal_id=proposal.id
    )
    assert reloaded is not None
    assert reloaded.created_by_subject == "analyst"


async def test_an_empty_proposer_is_rejected_by_the_aggregate() -> None:
    """A proposal with no recorded proposer cannot be constructed at all."""
    from hisiem_soc_copilot.application.commands.response import CreateResponseProposal
    from hisiem_soc_copilot.domain.shared.errors import DomainError as _DomainError

    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None

    with pytest.raises(_DomainError):
        await handler(factory).create_response_proposal(
            CreateResponseProposal(
                tenant_id=TENANT,
                investigation_id=investigation_id,
                action_key="START_SOAR_PLAYBOOK",
                evidence_ids=(str(evidence_id),),
                parameters={"playbook_id": "11111111-2222-3333-4444-555555555555"},
                reason="contain the intrusion",
                initiated_by_subject="   ",
            )
        )


# ---------------------------------------------------------------------------
# concurrent first-create convergence
# ---------------------------------------------------------------------------


async def test_a_racing_second_create_converges_on_the_first_proposal() -> None:
    """The loser of the UNIQUE race returns the winner's proposal, never a 500.

    The in-memory store stages the same unique violation the real table raises, so
    the handler's convergence branch is exercised without a database.
    """
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None

    first = await create_proposal(factory, investigation_id, evidence_ids=(evidence_id,))
    second = await create_proposal(factory, investigation_id, evidence_ids=(evidence_id,))

    assert second.id == first.id
    assert len(factory._response_proposals._store) == 1
