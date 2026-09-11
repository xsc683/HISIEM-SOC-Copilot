"""Shared seeding helpers for the response-workflow tests (P2 closure patch).

The response path now derives its target server-side and resolves evidence
authoritatively, so every test needs a COMPLETED investigation that OWNS the
evidence it cites. These helpers build exactly that, and are shared by the unit,
integration and API tests so the setup can never drift from the contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from hisiem_soc_copilot.application.commands.response import (
    CreateResponseProposal,
    DecideResponseApproval,
)
from hisiem_soc_copilot.application.handlers.response import ResponseCommandHandler
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.entities import (
    Evidence,
    EvidenceSource,
    InvestigationResult,
    Verdict,
)
from hisiem_soc_copilot.domain.investigation.enums import (
    EvidenceSourceType,
    VerdictDisposition,
)
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from hisiem_soc_copilot.domain.response.enums import ApprovalDecisionKind
from tests.fixtures.fakes import FakeUnitOfWorkFactory

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"
ALERT = "alert-0001"
OTHER_ALERT = "alert-0002"
T0 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)
PLAYBOOK_ID = "11111111-2222-3333-4444-555555555555"


def target(*, alert: str = ALERT, business_id: str = "AL-1") -> ExternalResourceRef:
    return ExternalResourceRef(
        provider="hisiem",
        resource_type="alert",
        address_id=alert,
        business_id=business_id,
    )


def seed_evidence(
    factory: FakeUnitOfWorkFactory,
    *,
    investigation_id: UUID,
    summary: str = "sudo auth failure from 203.0.113.9",
) -> UUID:
    """Persist one Evidence row owned by ``investigation_id`` (sync, no await).

    The fake repository is sync-backed, so this can be called from a fixture or a
    test body before any command runs.
    """
    evidence = Evidence(
        id=uuid4(),
        investigation_id=investigation_id,
        source=EvidenceSource(
            type=EvidenceSourceType.HISIEM_LOG_SEARCH,
            provider="hisiem",
            operation="log_search",
        ),
        collected_at=T0,
        observation={"summary": summary},
        summary=summary,
    )
    factory._evidence._store[evidence.id] = evidence
    return evidence.id


def seed_investigation(
    factory: FakeUnitOfWorkFactory,
    *,
    tenant_id: str = TENANT,
    alert: str = ALERT,
    business_id: str = "AL-1",
    disposition: VerdictDisposition = VerdictDisposition.MALICIOUS,
    completed: bool = True,
    with_evidence: bool = True,
) -> tuple[UUID, UUID | None]:
    """Create an Investigation (COMPLETED by default) + result (+ evidence).

    Returns ``(investigation_id, evidence_id_or_None)``. The fake repositories are
    plain in-memory dicts, so no async UoW is needed to seed them.
    """
    investigation_id = uuid4()
    ref = target(alert=alert, business_id=business_id)
    inv = Investigation.create(
        id=investigation_id,
        tenant_id=tenant_id,
        source_alert_ref=ref,
        initiated_by=ActorRef(subject_id="analyst", tenant_id=tenant_id),
        budget_limits=BudgetLimits(),
        now=T0,
    )
    inv.start(actor=ActorRef(subject_id="analyst", tenant_id=tenant_id), now=T0)
    if completed:
        inv.complete_without_response()
    factory._investigations._store[investigation_id] = inv

    result = InvestigationResult(
        id=uuid4(),
        investigation_id=investigation_id,
        verdict=Verdict(disposition=disposition, summary="s", confidence=0.8),
        finding_ids=[],
        created_at=T0,
    )
    factory._results._store[result.id] = result

    evidence_id = (
        seed_evidence(factory, investigation_id=investigation_id)
        if with_evidence
        else None
    )
    return investigation_id, evidence_id


def handler(factory: FakeUnitOfWorkFactory) -> ResponseCommandHandler:
    return ResponseCommandHandler(unit_of_work_factory=factory)


async def create_proposal(
    factory: FakeUnitOfWorkFactory,
    investigation_id: UUID,
    *,
    tenant_id: str = TENANT,
    evidence_ids: tuple[UUID, ...] | None = None,
    action_key: str = "START_SOAR_PLAYBOOK",
    parameters: dict[str, object] | None = None,
    reason: str = "contain the intrusion",
    actor: str = "analyst",
):
    """Create a proposal with a valid in-scope evidence citation by default."""
    if evidence_ids is None:
        evidence_ids = (seed_evidence(factory, investigation_id=investigation_id),)
    return await handler(factory).create_response_proposal(
        CreateResponseProposal(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            action_key=action_key,
            evidence_ids=tuple(str(e) for e in evidence_ids),
            parameters=(
                {"playbook_id": PLAYBOOK_ID} if parameters is None else parameters
            ),
            reason=reason,
            initiated_by_subject=actor,
        )
    )


async def approval_request_id(
    factory: FakeUnitOfWorkFactory, *, proposal_id: UUID, tenant_id: str = TENANT
) -> UUID:
    request = await factory().response_approvals.get_request_by_proposal(
        tenant_id=tenant_id, proposal_id=proposal_id
    )
    assert request is not None, "proposal has no approval request"
    return request.id


async def approve(
    factory: FakeUnitOfWorkFactory,
    proposal,
    *,
    tenant_id: str = TENANT,
    actor: str = "operator",
):
    """Approve the exact proposal contract through the real handler."""
    request_id = await approval_request_id(
        factory, proposal_id=proposal.id, tenant_id=tenant_id
    )
    return await handler(factory).decide_response_approval(
        DecideResponseApproval(
            tenant_id=tenant_id,
            approval_request_id=request_id,
            decision=ApprovalDecisionKind.APPROVE,
            expected_revision=proposal.content_revision,
            expected_content_hash=proposal.content_hash,
            initiated_by_subject=actor,
        )
    )
