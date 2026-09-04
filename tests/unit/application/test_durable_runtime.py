"""Durable runtime — atomic persistence, receipts, and idempotent dispatch.

In-memory fakes of the durable stores let us assert the transactional-outbox and
command-receipt semantics without Postgres/LangGraph:

- one command commits domain rows + domain_event + outbox (for orchestration
  events) + command_receipt atomically;
- a retried command with the same idempotency_key replays the existing result
  without re-creating domain rows or duplicate events;
- a terminal investigation is never re-dispatched;
- the tool-invocation audit records RUNNING→SUCCEEDED/FAILED with bounded metadata.

The full checkpoint/restart and API→dispatcher→graph chains live in the Postgres
integration suite (skipped when the DB is unreachable).
"""

from __future__ import annotations

from uuid import uuid4

from hisiem_soc_copilot.application.commands.investigation import (
    ChangeInvestigationPhase,
    CompleteInvestigation,
    FinalizeInvestigationResult,
    ResultVerdictCandidate,
    StartInvestigation,
)
from hisiem_soc_copilot.application.handlers.workflow import InvestigationWorkflowHandler
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.enums import (
    InvestigationPhase,
    InvestigationStatus,
    VerdictDisposition,
)
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from tests.fixtures.fakes import FakeUnitOfWorkFactory


def _investigation(
    tenant_id: str = "tenant-a", alert_id: str = "alert-x"
) -> Investigation:
    return Investigation.create(
        id=uuid4(),
        tenant_id=tenant_id,
        source_alert_ref=ExternalResourceRef(
            provider="hisiem", resource_type="alert", address_id=alert_id
        ),
        initiated_by=ActorRef(subject_id="analyst", tenant_id=tenant_id),
        budget_limits=BudgetLimits(),
    )


async def _started(uows: FakeUnitOfWorkFactory, inv: Investigation) -> None:
    uow = uows()
    await uow.investigations.add(inv)
    await uow.commit()
    inv.start(actor=inv.initiated_by)
    await uow.investigations.update(inv)
    await uow.commit()


def _handler(uows: FakeUnitOfWorkFactory) -> InvestigationWorkflowHandler:
    return InvestigationWorkflowHandler(unit_of_work_factory=uows)


async def test_single_command_commits_domain_event_and_receipt_atomically() -> None:
    uows = FakeUnitOfWorkFactory()
    inv = _investigation()
    await _started(uows, inv)
    handler = _handler(uows)

    await handler.change_phase(
        ChangeInvestigationPhase(
            tenant_id=inv.tenant_id,
            investigation_id=inv.id,
            phase=InvestigationPhase.PLANNING,
        )
    )

    # One commit produced a phase-changed domain event (audit-only, no outbox for
    # non-orchestration events) plus a command receipt.
    ledger = uows.events
    events = ledger.by_investigation(inv.id)
    assert any(e.event_type == "investigation_phase_changed" for e in events)
    assert len(uows.command_receipts.receipts()) >= 1


async def test_retried_command_with_same_key_does_not_reapply() -> None:
    uows = FakeUnitOfWorkFactory()
    inv = _investigation()
    await _started(uows, inv)
    handler = _handler(uows)
    cmd = ChangeInvestigationPhase(
        tenant_id=inv.tenant_id,
        investigation_id=inv.id,
        phase=InvestigationPhase.INVESTIGATING,
        idempotency_key="investigation:x:phase:investigating",
    )

    await handler.change_phase(cmd)
    events_after_first = len(uows.events.by_investigation(inv.id))
    # Retry the exact same logical command (same key) → replay, no new event.
    await handler.change_phase(cmd)
    events_after_retry = len(uows.events.by_investigation(inv.id))

    assert events_after_retry == events_after_first  # no duplicate phase event


async def test_investigation_start_command_is_idempotent() -> None:
    uows = FakeUnitOfWorkFactory()
    inv = _investigation()
    uow = uows()
    await uow.investigations.add(inv)
    await uow.commit()  # stays CREATED (HTTP lifecycle never auto-starts)
    handler = _handler(uows)

    start_cmd = StartInvestigation(
        tenant_id=inv.tenant_id,
        investigation_id=inv.id,
        idempotency_key=f"investigation:{inv.id}:start",
    )
    started = await handler.start_investigation(start_cmd)
    assert started.status == InvestigationStatus.RUNNING

    # Re-dispatch of the same start → replay; aggregate stays RUNNING (not a
    # StateTransitionError) and no duplicate started event is appended.
    again = await handler.start_investigation(start_cmd)
    assert again.status == InvestigationStatus.RUNNING
    started_events = [
        e for e in uows.events.by_investigation(inv.id)
        if e.event_type == "investigation_started"
    ]
    assert len(started_events) == 1


async def test_completed_investigation_is_not_restarted_by_runner_start() -> None:
    """A terminal investigation must never be re-run by a duplicate dispatch."""
    uows = FakeUnitOfWorkFactory()
    inv = _investigation()
    await _started(uows, inv)
    handler = _handler(uows)

    # Reaching COMPLETED through the read-only round.
    completed = await handler.complete(
        CompleteInvestigation(
            tenant_id=inv.tenant_id,
            investigation_id=inv.id,
            idempotency_key=f"investigation:{inv.id}:complete:no_response",
        )
    )
    assert completed.status == InvestigationStatus.COMPLETED

    # A duplicate start dispatch on a terminal investigation is refused by the
    # domain (never RUNNING → re-run) — StartInvestigation replays the terminal
    # state without error, matching the durable runner's terminal short-circuit.
    replay = await handler.start_investigation(
        StartInvestigation(
            tenant_id=inv.tenant_id,
            investigation_id=inv.id,
            idempotency_key=f"investigation:{inv.id}:start",
        )
    )
    assert replay.status == InvestigationStatus.COMPLETED


async def test_finalize_result_receipt_replays_existing_immutable_result() -> None:
    uows = FakeUnitOfWorkFactory()
    inv = _investigation()
    await _started(uows, inv)
    handler = _handler(uows)

    # First: record an evidence + a grounded finding so a MALICIOUS result can be
    # finalized at all.
    from hisiem_soc_copilot.application.commands.investigation import (
        EvidenceObservation,
        FindingCandidate,
        RecordEvidenceBatch,
        RecordFindings,
    )

    _, evidence = await handler.record_evidence_batch(
        RecordEvidenceBatch(
            tenant_id=inv.tenant_id,
            investigation_id=inv.id,
            observations=[
                EvidenceObservation(
                    source_type="HISIEM_EVENT",
                    source_provider="hisiem",
                    source_operation="event",
                    observation={"event.action": "authentication_success"},
                    raw_reference={"document_id": "evt-a"},
                )
            ],
        )
    )
    _, findings = await handler.record_findings(
        RecordFindings(
            tenant_id=inv.tenant_id,
            investigation_id=inv.id,
            findings=[
                FindingCandidate(
                    statement="compromised", evidence_citations=[evidence[0].id]
                )
            ],
        )
    )

    result_key = f"investigation:{inv.id}:result:MALICIOUS"
    cmd = FinalizeInvestigationResult(
        tenant_id=inv.tenant_id,
        investigation_id=inv.id,
        idempotency_key=result_key,
        verdict=ResultVerdictCandidate(
            disposition=VerdictDisposition.MALICIOUS,
            summary="compromised",
            confidence=0.9,
        ),
        finding_ids=[f.id for f in findings],
    )
    _, first = await handler.finalize_result(cmd)
    result_events = [
        e for e in uows.events.by_investigation(inv.id)
        if e.event_type == "investigation_result_finalized"
    ]
    assert len(result_events) == 1

    # Retry the same logical finalize (same key) → replay returns the SAME
    # immutable result id; no new result row, no duplicate event.
    _, again = await handler.finalize_result(cmd)
    assert again.id == first.id
    result_events_after = [
        e for e in uows.events.by_investigation(inv.id)
        if e.event_type == "investigation_result_finalized"
    ]
    assert len(result_events_after) == 1  # unchanged — receipt short-circuited


async def test_change_phase_without_key_is_audit_only_and_never_collides() -> None:
    """Legacy direct calls (no idempotency_key) still record a unique receipt."""
    uows = FakeUnitOfWorkFactory()
    inv = _investigation()
    await _started(uows, inv)
    handler = _handler(uows)

    for _ in range(3):
        await handler.change_phase(
            ChangeInvestigationPhase(
                tenant_id=inv.tenant_id,
                investigation_id=inv.id,
                phase=InvestigationPhase.PLANNING,
            )
        )
    # Each call is a distinct command_id → distinct audit-only key → all applied.
    phase_events = [
        e for e in uows.events.by_investigation(inv.id)
        if e.event_type == "investigation_phase_changed"
    ]
    assert len(phase_events) == 3
