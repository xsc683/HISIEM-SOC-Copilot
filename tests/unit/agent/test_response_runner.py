"""Durable response-execution workers — SUBMIT and OBSERVE (P2 closure, spec §2/§3).

These tests pin the two load-bearing properties of the response side effect:

* SUBMIT performs the ONE approved submission, reloading the authoritative contract
  from the store (never a queue payload), presenting the STABLE deterministic
  submission key as the idempotency key, and creating the provider projection ONLY
  after HISIEM returns a real, non-empty execution id.
* OBSERVE reconciles an already-submitted execution durably. A legitimately
  non-terminal status (QUEUED/RUNNING/WAITING/WAITING_HUMAN) schedules the NEXT
  observation with a future ``available_at`` — it is never signalled by raising a
  retry exception, so a long-running execution can never exhaust ``_MAX_ATTEMPTS``
  into DEAD_LETTER, and reconciliation survives a process restart.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from hisiem_soc_copilot.application.errors import ExternalServiceError
from hisiem_soc_copilot.application.ports.soar import SoarExecutionResult
from hisiem_soc_copilot.domain.response.enums import ResponseProposalStatus
from hisiem_soc_copilot.infrastructure.durable.dispatcher import (
    _MAX_ATTEMPTS,
    RESPONSE_OBSERVE_DESTINATION,
    RESPONSE_SUBMIT_DESTINATION,
    AsyncOutboxDispatcher,
)
from hisiem_soc_copilot.infrastructure.durable.investigation_runner import (
    NonRetryableRunError,
)
from hisiem_soc_copilot.infrastructure.durable.response_runner import (
    ResponseObserveRunner,
    ResponseSubmitRunner,
)
from tests.fixtures.fakes import FakeSoar, FakeUnitOfWorkFactory
from tests.fixtures.response_flow import (
    TENANT,
    approve,
    create_proposal,
    seed_investigation,
)

EXEC_ID = "hisiem-exec-1"
STABLE_KEY_PREFIX = f"response:{TENANT}:"


async def _approved(factory: FakeUnitOfWorkFactory):
    """Create + approve one proposal; returns the APPROVED proposal."""
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None
    proposal = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id,)
    )
    await approve(factory, proposal)
    reloaded = await factory().response_proposals.get(
        tenant_id=TENANT, proposal_id=proposal.id
    )
    assert reloaded is not None
    assert reloaded.status is ResponseProposalStatus.APPROVED
    return reloaded


def _submit(
    factory: FakeUnitOfWorkFactory, soar: FakeSoar, *, observe_delay: float = 15.0
) -> ResponseSubmitRunner:
    return ResponseSubmitRunner(
        unit_of_work_factory=factory, soar=soar, observe_delay_seconds=observe_delay
    )


def _observe(
    factory: FakeUnitOfWorkFactory, soar: FakeSoar, *, observe_delay: float = 15.0
) -> ResponseObserveRunner:
    return ResponseObserveRunner(
        unit_of_work_factory=factory, soar=soar, observe_delay_seconds=observe_delay
    )


async def _run_submit(factory, soar, proposal_id, **kwargs) -> None:
    await _submit(factory, soar, **kwargs).run(
        aggregate_id=str(proposal_id), tenant_id=TENANT
    )


async def _ref(factory: FakeUnitOfWorkFactory, proposal_id: UUID):
    return await factory().response_executions.get_by_proposal(
        tenant_id=TENANT, proposal_id=proposal_id
    )


async def _proposal(factory: FakeUnitOfWorkFactory, proposal_id: UUID):
    return await factory().response_proposals.get(
        tenant_id=TENANT, proposal_id=proposal_id
    )


async def _submission(factory: FakeUnitOfWorkFactory, proposal_id: UUID):
    return await factory().response_submissions.get_by_proposal(
        tenant_id=TENANT, proposal_id=proposal_id
    )


def _observe_rows(factory: FakeUnitOfWorkFactory) -> list[dict[str, object]]:
    return [
        row
        for row in factory.outbox.rows.values()
        if row["destination"] == RESPONSE_OBSERVE_DESTINATION
    ]


# ---------------------------------------------------------------------------
# SUBMIT — reload the approved contract, present the stable key
# ---------------------------------------------------------------------------


async def test_submit_reloads_the_persisted_contract_and_uses_the_stable_key() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal = await _approved(factory)
    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id=EXEC_ID, status="RUNNING")
    )

    await _run_submit(factory, soar, proposal.id)

    assert len(soar.submitted) == 1
    call = soar.submitted[0]
    assert call["submission_key"] == f"{STABLE_KEY_PREFIX}{proposal.id}"
    assert call["action_key"] == proposal.action_key
    assert call["parameters"] == proposal.parameters
    assert call["tenant_id"] == TENANT


async def test_submit_creates_the_ref_only_after_a_real_execution_id() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal = await _approved(factory)
    assert await _ref(factory, proposal.id) is None  # approval fabricated nothing

    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id=EXEC_ID, status="RUNNING")
    )
    await _run_submit(factory, soar, proposal.id)

    execution = await _ref(factory, proposal.id)
    assert execution is not None
    assert execution.provider == "hisiem"
    assert execution.execution_id == EXEC_ID
    assert execution.submission_key == f"{STABLE_KEY_PREFIX}{proposal.id}"
    assert execution.status == "RUNNING"

    reloaded = await _proposal(factory, proposal.id)
    assert reloaded is not None
    assert reloaded.status is ResponseProposalStatus.SUBMITTED
    assert reloaded.execution_ref is not None

    kinds = [e.event_type for e in factory.events.events]
    assert "response_execution_started" in kinds
    assert "response_execution_submitted" in kinds


async def test_submit_refuses_to_fabricate_an_execution_id() -> None:
    """An empty provider id is never persisted as a placeholder identity."""
    factory = FakeUnitOfWorkFactory()
    proposal = await _approved(factory)
    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id="", status="QUEUED")
    )

    with pytest.raises(RuntimeError):
        await _run_submit(factory, soar, proposal.id)

    assert await _ref(factory, proposal.id) is None
    reloaded = await _proposal(factory, proposal.id)
    assert reloaded is not None
    assert reloaded.status is ResponseProposalStatus.APPROVED


async def test_duplicate_submit_delivery_is_a_noop() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal = await _approved(factory)
    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id=EXEC_ID, status="RUNNING")
    )

    await _run_submit(factory, soar, proposal.id)
    await _run_submit(factory, soar, proposal.id)  # duplicate outbox delivery

    assert len(soar.submitted) == 1  # exactly ONE provider execution
    refs = list(factory._response_executions._store.values())
    assert len(refs) == 1


async def test_crash_after_provider_create_converges_on_the_same_key() -> None:
    """Local commit lost after HISIEM created the execution → same key, same execution."""
    factory = FakeUnitOfWorkFactory()
    proposal = await _approved(factory)
    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id=EXEC_ID, status="RUNNING")
    )
    await _run_submit(factory, soar, proposal.id)

    # Simulate the crash: the provider execution exists, the local transaction did not.
    factory._response_executions._store.clear()
    factory._response_proposals._store[proposal.id] = replace(
        factory._response_proposals._store[proposal.id],
        status=ResponseProposalStatus.APPROVED,
        execution_ref=None,
    )

    await _run_submit(factory, soar, proposal.id)

    keys = [call["submission_key"] for call in soar.submitted]
    assert keys == [f"{STABLE_KEY_PREFIX}{proposal.id}"] * 2  # same Idempotency-Key
    refs = list(factory._response_executions._store.values())
    assert len(refs) == 1  # one local projection, never two
    assert refs[0].execution_id == EXEC_ID


async def test_submit_of_a_non_approved_proposal_is_nonretryable() -> None:
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None
    proposal = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id,)
    )  # WAITING_APPROVAL — no human decision recorded
    soar = FakeSoar()

    with pytest.raises(NonRetryableRunError):
        await _run_submit(factory, soar, proposal.id)

    assert soar.submitted == []
    assert await _ref(factory, proposal.id) is None


async def test_submit_missing_proposal_is_a_noop() -> None:
    factory = FakeUnitOfWorkFactory()
    soar = FakeSoar()
    await _run_submit(factory, soar, uuid4())
    assert soar.submitted == []


async def test_definitive_rejection_dead_letters_without_a_fake_ref() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal = await _approved(factory)
    soar = FakeSoar(
        raise_on_submit=ExternalServiceError(
            "bad target", service="hisiem", code="HTTP_422"
        )
    )

    with pytest.raises(NonRetryableRunError):
        await _run_submit(factory, soar, proposal.id)

    # No provider execution was created, so no provider identity may be persisted.
    assert await _ref(factory, proposal.id) is None
    reloaded = await _proposal(factory, proposal.id)
    assert reloaded is not None
    assert reloaded.status is ResponseProposalStatus.APPROVED


async def test_transient_submit_failure_is_reraised_and_stays_approved() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal = await _approved(factory)
    soar = FakeSoar(
        raise_on_submit=ExternalServiceError(
            "unavailable", service="hisiem", code="HTTP_503"
        )
    )

    with pytest.raises(ExternalServiceError):
        await _run_submit(factory, soar, proposal.id)

    assert await _ref(factory, proposal.id) is None
    reloaded = await _proposal(factory, proposal.id)
    assert reloaded is not None
    assert reloaded.status is ResponseProposalStatus.APPROVED


# ---------------------------------------------------------------------------
# SUBMIT → OBSERVE handoff
# ---------------------------------------------------------------------------


async def test_nonterminal_submit_schedules_a_durable_observe() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal = await _approved(factory)
    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id=EXEC_ID, status="RUNNING")
    )

    await _run_submit(factory, soar, proposal.id)

    rows = _observe_rows(factory)
    assert len(rows) == 1
    # Delayed, NOT immediately claimable: the observation is scheduled for later.
    assert rows[0]["available_at"] > factory.outbox.now
    assert rows[0]["status"] == "PENDING"


async def test_terminal_submit_settles_without_scheduling_an_observe() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal = await _approved(factory)
    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id=EXEC_ID, status="SUCCEEDED")
    )

    await _run_submit(factory, soar, proposal.id)

    execution = await _ref(factory, proposal.id)
    assert execution is not None
    assert execution.status == "SUCCEEDED"
    assert execution.finished_at is not None
    assert _observe_rows(factory) == []
    assert "response_execution_succeeded" in {
        e.event_type for e in factory.events.events
    }


# ---------------------------------------------------------------------------
# OBSERVE — durable reconciliation
# ---------------------------------------------------------------------------


async def _submitted(
    factory: FakeUnitOfWorkFactory,
    *,
    status_sequence: list[str],
) -> tuple[UUID, FakeSoar]:
    proposal = await _approved(factory)
    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id=EXEC_ID, status="RUNNING"),
        status_sequence=status_sequence,
    )
    await _run_submit(factory, soar, proposal.id)
    return proposal.id, soar


async def test_observe_never_reposts_the_execution() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal_id, soar = await _submitted(factory, status_sequence=["RUNNING"])

    await _observe(factory, soar).run(
        aggregate_id=str(proposal_id), tenant_id=TENANT
    )

    assert len(soar.submitted) == 1
    assert soar.status_calls == [EXEC_ID]


async def test_running_observation_reschedules_without_a_retry_exception() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal_id, soar = await _submitted(factory, status_sequence=["RUNNING"])
    before = len(_observe_rows(factory))

    # No exception: a legitimately RUNNING execution is not a failure.
    await _observe(factory, soar).run(
        aggregate_id=str(proposal_id), tenant_id=TENANT
    )

    execution = await _ref(factory, proposal_id)
    assert execution is not None
    assert execution.status == "RUNNING"
    assert execution.finished_at is None
    rows = _observe_rows(factory)
    assert len(rows) == before + 1
    assert rows[-1]["available_at"] > factory.outbox.now
    assert "response_execution_observed" in {
        e.event_type for e in factory.events.events
    }


async def test_a_long_running_execution_never_dead_letters() -> None:
    """The §3 blocker: RUNNING must not exhaust attempts into DEAD_LETTER."""
    factory = FakeUnitOfWorkFactory()
    proposal_id, soar = await _submitted(
        factory, status_sequence=["RUNNING"] * (_MAX_ATTEMPTS + 5)
    )

    for _ in range(_MAX_ATTEMPTS + 5):
        await _observe(factory, soar).run(
            aggregate_id=str(proposal_id), tenant_id=TENANT
        )

    assert factory.outbox.dead_letter_ids == []
    assert factory.outbox.failed_ids == []
    statuses = {row["status"] for row in factory.outbox.rows.values()}
    assert "DEAD_LETTER" not in statuses
    execution = await _ref(factory, proposal_id)
    assert execution is not None
    assert execution.status == "RUNNING"
    reloaded = await _proposal(factory, proposal_id)
    assert reloaded is not None
    assert reloaded.status is ResponseProposalStatus.SUBMITTED


async def test_reconciliation_survives_a_process_restart() -> None:
    """State is durable: a BRAND-NEW worker continues an in-flight execution."""
    factory = FakeUnitOfWorkFactory()
    proposal_id, soar = await _submitted(factory, status_sequence=["RUNNING"])

    await _observe(factory, soar).run(
        aggregate_id=str(proposal_id), tenant_id=TENANT
    )

    # Cold start: a fresh runner instance with no in-memory state of its own.
    restart_soar = FakeSoar(status_sequence=["SUCCEEDED"])
    await _observe(factory, restart_soar).run(
        aggregate_id=str(proposal_id), tenant_id=TENANT
    )

    execution = await _ref(factory, proposal_id)
    assert execution is not None
    assert execution.status == "SUCCEEDED"
    assert execution.finished_at is not None
    assert restart_soar.status_calls == [EXEC_ID]


async def test_observe_settles_a_succeeded_execution() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal_id, soar = await _submitted(factory, status_sequence=["SUCCEEDED"])
    before = len(_observe_rows(factory))

    await _observe(factory, soar).run(
        aggregate_id=str(proposal_id), tenant_id=TENANT
    )

    execution = await _ref(factory, proposal_id)
    assert execution is not None
    assert execution.status == "SUCCEEDED"
    assert execution.finished_at is not None
    kinds = [e.event_type for e in factory.events.events]
    assert "response_execution_succeeded" in kinds
    # A terminal execution no longer schedules observation.
    assert len(_observe_rows(factory)) == before


async def test_observe_settles_a_failed_execution() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal_id, soar = await _submitted(factory, status_sequence=["FAILED"])
    before = len(_observe_rows(factory))

    await _observe(factory, soar).run(
        aggregate_id=str(proposal_id), tenant_id=TENANT
    )

    execution = await _ref(factory, proposal_id)
    assert execution is not None
    assert execution.status == "FAILED"
    assert execution.finished_at is not None
    assert "response_execution_failed" in {
        e.event_type for e in factory.events.events
    }
    assert len(_observe_rows(factory)) == before


async def test_terminal_execution_makes_a_further_observe_a_noop() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal_id, soar = await _submitted(factory, status_sequence=["SUCCEEDED"])
    await _observe(factory, soar).run(
        aggregate_id=str(proposal_id), tenant_id=TENANT
    )

    events_before = [e.event_type for e in factory.events.events]
    await _observe(factory, soar).run(
        aggregate_id=str(proposal_id), tenant_id=TENANT
    )

    assert soar.status_calls == [EXEC_ID]  # no further provider GET
    assert [e.event_type for e in factory.events.events] == events_before


async def test_observe_before_submission_is_a_noop() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal = await _approved(factory)
    soar = FakeSoar()

    await _observe(factory, soar).run(
        aggregate_id=str(proposal.id), tenant_id=TENANT
    )

    assert soar.status_calls == []
    assert await _ref(factory, proposal.id) is None


async def test_definitive_observe_error_settles_the_execution_as_failed() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal_id, soar = await _submitted(factory, status_sequence=[])
    soar.raise_on_status = ExternalServiceError(
        "gone", service="hisiem", code="SOAR_NOT_FOUND"
    )

    await _observe(factory, soar).run(
        aggregate_id=str(proposal_id), tenant_id=TENANT
    )

    execution = await _ref(factory, proposal_id)
    assert execution is not None
    assert execution.status == "FAILED"
    assert execution.safe_error_code == "SOAR_NOT_FOUND"
    assert "response_execution_failed" in {
        e.event_type for e in factory.events.events
    }


async def test_transient_observe_error_is_reraised_for_backoff_retry() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal_id, soar = await _submitted(factory, status_sequence=[])
    soar.raise_on_status = ExternalServiceError(
        "unavailable", service="hisiem", code="HTTP_503"
    )

    with pytest.raises(ExternalServiceError):
        await _observe(factory, soar).run(
            aggregate_id=str(proposal_id), tenant_id=TENANT
        )

    execution = await _ref(factory, proposal_id)
    assert execution is not None
    assert execution.status == "RUNNING"


# ---------------------------------------------------------------------------
# A 4xx is not automatically permanent: throttling/timeouts stay retryable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [408, 425, 429])
async def test_a_transient_4xx_observe_never_settles_a_live_execution(
    status: int,
) -> None:
    """Rate limiting / in-flight timeouts must NOT be read as "the execution failed".

    HISIEM owns the execution. If a GET is throttled or times out we know nothing
    about the execution's state, so settling the projection as FAILED would make our
    record permanently disagree with the system of record — and, because a terminal
    projection stops reconciliation, with no way back.
    """
    factory = FakeUnitOfWorkFactory()
    proposal_id, soar = await _submitted(factory, status_sequence=[])
    soar.raise_on_status = ExternalServiceError(
        "throttled", service="hisiem", code=f"HTTP_{status}"
    )

    with pytest.raises(ExternalServiceError):
        await _observe(factory, soar).run(
            aggregate_id=str(proposal_id), tenant_id=TENANT
        )

    execution = await _ref(factory, proposal_id)
    assert execution is not None
    assert execution.status == "RUNNING"
    assert "response_execution_failed" not in {
        e.event_type for e in factory.events.events
    }


@pytest.mark.parametrize("status", [408, 425, 429])
async def test_a_transient_4xx_submit_is_retried_not_dead_lettered(
    status: int,
) -> None:
    """A throttled approved submission must stay retryable under the SAME key."""
    factory = FakeUnitOfWorkFactory()
    proposal = await _approved(factory)
    soar = FakeSoar(
        raise_on_submit=ExternalServiceError(
            "throttled", service="hisiem", code=f"HTTP_{status}"
        )
    )

    with pytest.raises(ExternalServiceError):
        await _run_submit(factory, soar, proposal.id)

    # No provider execution identity exists, so there is nothing to project — and
    # the delivery must stay retryable rather than being dead-lettered.
    assert await _ref(factory, proposal.id) is None
    reloaded = await _proposal(factory, proposal.id)
    assert reloaded is not None
    assert reloaded.status is ResponseProposalStatus.APPROVED


@pytest.mark.parametrize("status", [400, 404, 409, 422])
async def test_a_genuine_client_rejection_stays_definitive(status: int) -> None:
    """A malformed request/action/target IS permanent: it must not retry forever."""
    factory = FakeUnitOfWorkFactory()
    proposal = await _approved(factory)
    soar = FakeSoar(
        raise_on_submit=ExternalServiceError(
            "bad request", service="hisiem", code=f"HTTP_{status}"
        )
    )

    with pytest.raises(NonRetryableRunError):
        await _run_submit(factory, soar, proposal.id)

    assert await _ref(factory, proposal.id) is None


# ---------------------------------------------------------------------------
# The LOCAL submission lifecycle (independent of the provider execution ref)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 404, 409, 422])
async def test_a_definitive_rejection_is_recorded_as_a_submission_failure(
    status: int,
) -> None:
    """HISIEM refused the SUBMISSION: no execution exists, so none may be recorded.

    The proposal stays APPROVED (nothing was accepted), the local submission
    becomes FAILED_DEFINITIVE so the workspace stops claiming "awaiting
    submission", and the delivery is dead-lettered rather than retried forever.
    """
    factory = FakeUnitOfWorkFactory()
    proposal = await _approved(factory)
    soar = FakeSoar(
        raise_on_submit=ExternalServiceError(
            "rejected", service="hisiem", code=f"HTTP_{status}"
        )
    )

    with pytest.raises(NonRetryableRunError):
        await _run_submit(factory, soar, proposal.id)

    submission = await _submission(factory, proposal.id)
    assert submission is not None
    assert submission.status == "FAILED_DEFINITIVE"
    assert submission.last_error_code == f"HTTP_{status}"
    assert submission.safe_error_message
    assert submission.failed_at is not None
    assert submission.attempt_count >= 1

    assert await _ref(factory, proposal.id) is None
    reloaded = await _proposal(factory, proposal.id)
    assert reloaded is not None
    assert reloaded.status is ResponseProposalStatus.APPROVED

    types = {e.event_type for e in factory.events.events}
    assert "response_submission_failed" in types
    # NOT an execution failure: there is no execution to have failed.
    assert "response_execution_failed" not in types
    assert "response_execution_started" not in types


@pytest.mark.parametrize("status", [408, 425, 429, 500, 503])
async def test_an_uncertain_submission_failure_is_recorded_as_retrying(
    status: int,
) -> None:
    """Throttling/timeouts/5xx leave us NOT KNOWING what the provider did.

    Nothing may be claimed about a provider execution, and the delivery must stay
    retryable under the same key.
    """
    factory = FakeUnitOfWorkFactory()
    proposal = await _approved(factory)
    soar = FakeSoar(
        raise_on_submit=ExternalServiceError(
            "uncertain", service="hisiem", code=f"HTTP_{status}"
        )
    )

    with pytest.raises(ExternalServiceError):
        await _run_submit(factory, soar, proposal.id)

    submission = await _submission(factory, proposal.id)
    assert submission is not None
    assert submission.status == "RETRYING"
    assert submission.last_error_code == f"HTTP_{status}"
    assert submission.failed_at is None
    assert await _ref(factory, proposal.id) is None

    types = {e.event_type for e in factory.events.events}
    assert "response_submission_retrying" in types
    assert "response_submission_failed" not in types


async def test_an_empty_execution_id_is_uncertain_not_failed() -> None:
    """A provider contract violation is retryable: an execution may exist."""
    factory = FakeUnitOfWorkFactory()
    proposal = await _approved(factory)
    soar = FakeSoar(submit_result=SoarExecutionResult(execution_id="", status="RUNNING"))

    with pytest.raises(RuntimeError):
        await _run_submit(factory, soar, proposal.id)

    submission = await _submission(factory, proposal.id)
    assert submission is not None
    assert submission.status == "RETRYING"
    assert submission.last_error_code == "SOAR_NO_EXECUTION_ID"
    assert await _ref(factory, proposal.id) is None


async def test_a_successful_submit_settles_the_local_submission() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal = await _approved(factory)
    sebelum = await _submission(factory, proposal.id)
    assert sebelum is not None
    assert sebelum.status == "PENDING"
    assert sebelum.submission_key == f"response:{TENANT}:{proposal.id}"
    assert sebelum.attempt_count == 0

    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id=EXEC_ID, status="RUNNING")
    )
    await _run_submit(factory, soar, proposal.id)

    submission = await _submission(factory, proposal.id)
    assert submission is not None
    assert submission.status == "SUBMITTED"
    assert submission.submitted_at is not None
    assert submission.submission_key == f"response:{TENANT}:{proposal.id}"
    execution = await _ref(factory, proposal.id)
    assert execution is not None
    assert execution.execution_id == EXEC_ID


async def test_approval_creates_a_pending_submission_intent() -> None:
    """Approval persists the local intent — and still creates NO execution ref."""
    factory = FakeUnitOfWorkFactory()
    investigation_id, evidence_id = seed_investigation(factory)
    assert evidence_id is not None
    proposal = await create_proposal(
        factory, investigation_id, evidence_ids=(evidence_id,)
    )

    submission = await _submission(factory, proposal.id)
    assert submission is None  # nothing is planned before a human approves

    await approve(factory, proposal)

    submission = await _submission(factory, proposal.id)
    assert submission is not None
    assert submission.status == "PENDING"
    assert submission.attempt_count == 0
    assert submission.submission_key == f"response:{TENANT}:{proposal.id}"
    assert await _ref(factory, proposal.id) is None


# ---------------------------------------------------------------------------
# status_checks_per_delivery is a real bound, not decoration
# ---------------------------------------------------------------------------


async def test_status_checks_per_delivery_polls_within_one_delivery() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal_id, soar = await _submitted(factory, status_sequence=[])
    soar.status_sequence = ["RUNNING"] * 5
    # The submit itself already queued one observation for the fresh RUNNING ref.
    before = len(_observe_rows(factory))

    runner = ResponseObserveRunner(
        unit_of_work_factory=factory,
        soar=soar,
        observe_delay_seconds=15.0,
        status_checks_per_delivery=3,
    )
    await runner.run(aggregate_id=str(proposal_id), tenant_id=TENANT)

    # Bounded: exactly the configured number of GETs, and exactly ONE further
    # durable reschedule — never an in-process loop.
    assert len(soar.status_calls) == 3
    assert len(_observe_rows(factory)) == before + 1
    execution = await _ref(factory, proposal_id)
    assert execution is not None
    assert execution.status == "RUNNING"


async def test_status_checks_per_delivery_stops_early_on_a_terminal_status() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal_id, soar = await _submitted(factory, status_sequence=[])
    soar.status_sequence = ["RUNNING", "SUCCEEDED", "RUNNING"]
    before = len(_observe_rows(factory))

    runner = ResponseObserveRunner(
        unit_of_work_factory=factory,
        soar=soar,
        observe_delay_seconds=15.0,
        status_checks_per_delivery=3,
    )
    await runner.run(aggregate_id=str(proposal_id), tenant_id=TENANT)

    assert len(soar.status_calls) == 2  # stopped at the terminal status
    assert len(_observe_rows(factory)) == before  # settled ⇒ nothing rescheduled
    execution = await _ref(factory, proposal_id)
    assert execution is not None
    assert execution.status == "SUCCEEDED"


# ---------------------------------------------------------------------------
# Dispatcher integration — the delayed observation is what actually drives it
# ---------------------------------------------------------------------------


class _Resolver:
    def __init__(self, mapping: dict[UUID, tuple[str, str]]) -> None:
        self._mapping = mapping

    async def resolve(self, *, event_id: UUID) -> tuple[str, str] | None:
        return self._mapping.get(event_id)


async def test_dispatcher_reconciles_a_running_execution_over_many_cycles() -> None:
    """End-to-end over the real dispatcher: claim → observe → reschedule, forever."""
    factory = FakeUnitOfWorkFactory()
    proposal_id, soar = await _submitted(
        factory, status_sequence=["RUNNING"] * (_MAX_ATTEMPTS + 3)
    )
    mapping = {event_id: (TENANT, str(proposal_id)) for event_id in factory.outbox.rows}
    dispatcher = AsyncOutboxDispatcher(
        outbox_store=factory.outbox,
        resolver=_Resolver(mapping),
        runner=_observe(factory, soar),
        worker_name="copilot-response-observe-dispatcher",
        destination=RESPONSE_OBSERVE_DESTINATION,
    )

    for _ in range(_MAX_ATTEMPTS + 3):
        factory.outbox.advance(60)
        mapping.update(
            {event_id: (TENANT, str(proposal_id)) for event_id in factory.outbox.rows}
        )
        await dispatcher.drain_once()

    assert factory.outbox.dead_letter_ids == []
    assert factory.outbox.failed_ids == []
    execution = await _ref(factory, proposal_id)
    assert execution is not None
    assert execution.status == "RUNNING"


async def test_dispatcher_settles_when_the_provider_finishes() -> None:
    factory = FakeUnitOfWorkFactory()
    proposal_id, soar = await _submitted(
        factory, status_sequence=["RUNNING", "RUNNING", "SUCCEEDED"]
    )
    mapping = {event_id: (TENANT, str(proposal_id)) for event_id in factory.outbox.rows}
    dispatcher = AsyncOutboxDispatcher(
        outbox_store=factory.outbox,
        resolver=_Resolver(mapping),
        runner=_observe(factory, soar),
        worker_name="copilot-response-observe-dispatcher",
        destination=RESPONSE_OBSERVE_DESTINATION,
    )

    for _ in range(3):
        factory.outbox.advance(60)
        mapping.update(
            {event_id: (TENANT, str(proposal_id)) for event_id in factory.outbox.rows}
        )
        await dispatcher.drain_once()

    execution = await _ref(factory, proposal_id)
    assert execution is not None
    assert execution.status == "SUCCEEDED"
    assert factory.outbox.dead_letter_ids == []


async def test_submit_and_observe_destinations_are_distinct() -> None:
    assert RESPONSE_SUBMIT_DESTINATION != RESPONSE_OBSERVE_DESTINATION


# ---------------------------------------------------------------------------
# Structural: the workers can never call the LLM (spec §16/§17)
# ---------------------------------------------------------------------------


def test_workers_have_no_model_dependency() -> None:
    assert set(inspect.signature(ResponseSubmitRunner.__init__).parameters) == {
        "self",
        "unit_of_work_factory",
        "soar",
        "observe_delay_seconds",
    }
    assert set(inspect.signature(ResponseObserveRunner.__init__).parameters) == {
        "self",
        "unit_of_work_factory",
        "soar",
        "observe_delay_seconds",
        "status_checks_per_delivery",
    }


def test_workers_use_no_wall_clock_sleep() -> None:
    """Reconciliation is DURABLE scheduling, never an in-process poll loop."""
    import hisiem_soc_copilot.infrastructure.durable.response_runner as module

    source = inspect.getsource(module)
    assert "asyncio.sleep" not in source
    assert "import asyncio" not in source


def test_no_fixture_ever_constructs_an_empty_execution_id() -> None:
    """Guards against re-introducing the ``execution_id=""`` fabrication (spec §2)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "tests"
    needle = 'execution_id=' + chr(34) + chr(34)
    self_path = Path(__file__).resolve()
    offenders = [
        path
        for path in root.rglob("*.py")
        if path != self_path and needle in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
