"""Durable response-execution workers: SUBMIT and OBSERVE.

The response side effect has TWO distinct durable responsibilities (spec §3):

``ResponseSubmitRunner`` (destination ``response.execution.submit``)
    Consumes a ``response_execution_queued`` delivery and performs the ONE approved
    submission. It RELOADS the authoritative approved contract from the database
    (never a queue payload), presents the STABLE deterministic ``submission_key`` as
    HISIEM's idempotency key, and only after HISIEM returns a REAL, non-empty
    execution id does it create the ``ResponseExecutionRef`` and transition the
    proposal APPROVED → SUBMITTED — in the same local transaction. It never calls
    the LLM (spec §16/§17).

``ResponseObserveRunner`` (destination ``response.execution.observe``)
    Advances an ALREADY-SUBMITTED execution toward a terminal state with at most a
    small bounded number of HISIEM GETs per delivery. A terminal provider status is
    settled and reconciliation stops. A non-terminal status (QUEUED/RUNNING/
    WAITING/WAITING_HUMAN) is persisted as an observation and the next observation is
    DURABLY scheduled by appending ``response_execution_observed`` with a future
    ``available_at`` — it is deliberately NOT signalled by raising a retry exception,
    because a legitimately RUNNING execution is not a failure and must never exhaust
    ``_MAX_ATTEMPTS`` into DEAD_LETTER. Reconciliation therefore survives process
    restart and an execution may run for minutes or hours without being lost.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID

from ...application.errors import ExternalServiceError
from ...application.ports.soar import SoarExecutionResult, SoarPort
from ...application.ports.unit_of_work import UnitOfWork
from ...domain.response.aggregate import ResponseProposal
from ...domain.response.enums import ResponseProposalStatus, ResponseSubmissionStatus
from ...domain.response.events import (
    ResponseEvent,
    response_execution_failed,
    response_execution_observation_failed,
    response_execution_observed,
    response_execution_started,
    response_execution_submitted,
    response_execution_succeeded,
    response_submission_attention_required,
    response_submission_failed,
    response_submission_retrying,
)
from ...domain.response.value_objects import (
    ResponseExecutionRef,
    ResponseSubmission,
    submission_key,
)
from ...domain.shared.identifiers import utc_now
from .investigation_runner import NonRetryableRunError

_PROVIDER = "hisiem"
_SUBMISSION_FAILED_DEFINITIVE = ResponseSubmissionStatus.FAILED_DEFINITIVE.value
_SUBMISSION_ATTENTION_REQUIRED = ResponseSubmissionStatus.ATTENTION_REQUIRED.value
_SUBMISSION_SUBMITTED = ResponseSubmissionStatus.SUBMITTED.value
_TERMINAL = {"SUCCEEDED", "FAILED"}
# A definitive (non-retryable) provider rejection: a bad request/action/target.
_DEFINITIVE_CODES = {"SOAR_NOT_FOUND", "SOAR_CONFIGURATION"}
# 4xx responses that are nevertheless TRANSIENT: the provider is throttling us or
# the request timed out in flight. Classifying them as definitive would permanently
# settle a still-RUNNING provider execution as FAILED (observe) or dead-letter a
# perfectly valid approved submission (submit) — the provider, which owns the
# execution, would then disagree with our record forever. They stay retryable.
_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429})


class ResponseSubmitRunner:
    """Submits ONE approved proposal to HISIEM SOAR exactly once (idempotency-keyed)."""

    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        soar: SoarPort,
        observe_delay_seconds: float = 15.0,
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._soar = soar
        self._observe_delay = timedelta(seconds=observe_delay_seconds)

    async def run(self, *, aggregate_id: str, tenant_id: str) -> None:
        proposal_id = UUID(aggregate_id)
        uow = self._uow_factory()
        try:
            proposal = await uow.response_proposals.get(
                tenant_id=tenant_id, proposal_id=proposal_id
            )
            if proposal is None:
                return  # the queued event's proposal vanished — nothing to submit
            execution = await uow.response_executions.get_by_proposal(
                tenant_id=tenant_id, proposal_id=proposal_id
            )
            submission = await uow.response_submissions.get_by_proposal(
                tenant_id=tenant_id, proposal_id=proposal_id
            )
        finally:
            await uow.close()

        if submission is not None and submission.status == _SUBMISSION_FAILED_DEFINITIVE:
            # The provider DEFINITIVELY refused this submission and that fact is
            # already durable. This delivery is a stale/reclaimed one (the worker
            # crashed after committing the refusal but before dead-lettering):
            # calling the provider again would re-attempt a submission we already
            # know was rejected. Settle it as a no-op — the dispatcher publishes it.
            return
        if submission is not None and submission.status == _SUBMISSION_ATTENTION_REQUIRED:
            # The automatic retry budget is already spent and a human owns this.
            # A reclaimed delivery must never silently resume submitting.
            return
        if submission is not None and submission.status == _SUBMISSION_SUBMITTED:
            # Converged: the provider accepted it, whether or not the ref row is
            # visible to this read.
            return
        if execution is not None:
            # A provider execution identity is already durably attached: a duplicate
            # submit delivery CONVERGES (no-op) and never executes a second time.
            return
        if proposal.status is ResponseProposalStatus.SUBMITTED:
            # Already converged without a ref row: nothing to submit.
            return
        if proposal.status is not ResponseProposalStatus.APPROVED:
            # Only a recorded human approval enqueues this delivery, so any other
            # status is a deterministic fault — never retryable.
            raise NonRetryableRunError(code="RESPONSE_NOT_APPROVED")

        target_ref = proposal.target_refs[0] if proposal.target_refs else None
        if target_ref is None:
            # No target ⇒ nothing may be submitted. There is no provider execution,
            # therefore NO ResponseExecutionRef row is fabricated for this failure.
            raise NonRetryableRunError(code="RESPONSE_NO_TARGET")

        key = submission_key(tenant_id, proposal.id)
        try:
            result = await self._soar.submit_execution(
                tenant_id=tenant_id,
                proposal_id=proposal.id,
                submission_key=key,
                action_key=proposal.action_key,
                parameters=dict(proposal.parameters),
                target_ref=target_ref,
            )
        except ExternalServiceError as exc:
            if _is_definitive(exc):
                # HISIEM DEFINITIVELY refused this submission and created NO execution.
                # That is a fact about the SUBMISSION, not about an execution: no
                # provider execution exists, so none may be recorded as failed and no
                # projection row may be fabricated. The proposal legitimately stays
                # APPROVED (nothing was accepted), the local submission becomes
                # FAILED_DEFINITIVE so the workspace stops claiming "awaiting
                # submission", and the delivery is dead-lettered with a bounded code.
                code = exc.upstream_code or "SOAR_REJECTED"
                message = _safe_message(exc)
                await self._record_submission(
                    tenant_id=tenant_id,
                    proposal_id=proposal.id,
                    key=key,
                    status=ResponseSubmissionStatus.FAILED_DEFINITIVE,
                    bump_attempts=True,
                    error_code=code,
                    safe_error_message=message,
                    event_factory=lambda attempts: response_submission_failed(
                        proposal.id,
                        submission_key=key,
                        attempt_count=attempts,
                        error_code=code,
                        safe_error_message=message,
                        tenant_id=tenant_id,
                    ),
                )
                raise NonRetryableRunError(code=code) from None
            # TRANSIENT/UNCERTAIN: the provider may or may not have processed the
            # request. Nothing about a provider execution is claimed; the local
            # submission is recorded as RETRYING and the delivery stays in the outbox
            # to be retried under the SAME idempotency key.
            code = exc.upstream_code or "SOAR_UNAVAILABLE"
            message = _safe_message(exc)
            await self._record_submission(
                tenant_id=tenant_id,
                proposal_id=proposal.id,
                key=key,
                status=ResponseSubmissionStatus.RETRYING,
                bump_attempts=True,
                error_code=code,
                safe_error_message=message,
                event_factory=lambda attempts: response_submission_retrying(
                    proposal.id,
                    submission_key=key,
                    attempt_count=attempts,
                    error_code=code,
                    tenant_id=tenant_id,
                ),
            )
            raise

        if not result.execution_id:
            # HISIEM's contract is to return a real execution id. An empty one means
            # we cannot durably identify the execution, so we must not persist a
            # fabricated/placeholder identity - retry (same key => same execution).
            # UNCERTAIN rather than failed: an execution may exist provider-side.
            await self._record_submission(
                tenant_id=tenant_id,
                proposal_id=proposal.id,
                key=key,
                status=ResponseSubmissionStatus.RETRYING,
                bump_attempts=True,
                error_code="SOAR_NO_EXECUTION_ID",
                safe_error_message="provider returned no execution id",
                event_factory=lambda attempts: response_submission_retrying(
                    proposal.id,
                    submission_key=key,
                    attempt_count=attempts,
                    error_code="SOAR_NO_EXECUTION_ID",
                    tenant_id=tenant_id,
                ),
            )
            raise RuntimeError("SOAR submit returned no execution id")

        await self._attach_and_mark_submitted(
            tenant_id=tenant_id,
            proposal=proposal,
            key=key,
            result=result,
        )

    # ------------------------------------------------------------------
    async def _record_submission(
        self,
        *,
        tenant_id: str,
        proposal_id: UUID,
        key: str,
        status: ResponseSubmissionStatus,
        bump_attempts: bool = False,
        error_code: str | None = None,
        safe_error_message: str | None = None,
        event_factory: Callable[[int], ResponseEvent] | None = None,
    ) -> None:
        """Persist the local submission state in its OWN committed transaction.

        It must be committed separately from the caller's outcome: this runs on the
        failure path, where the caller then raises and the outbox delivery is retried
        or dead-lettered. The local truth about the attempt has to survive that.
        """
        uow = self._uow_factory()
        try:
            current = await uow.response_submissions.get_by_proposal(
                tenant_id=tenant_id, proposal_id=proposal_id
            )
            now = utc_now()
            attempts = current.attempt_count if current is not None else 0
            if bump_attempts:
                attempts += 1
            record = ResponseSubmission(
                proposal_id=proposal_id,
                submission_key=current.submission_key if current is not None else key,
                status=status.value,
                attempt_count=attempts,
                last_error_code=error_code,
                safe_error_message=safe_error_message,
                created_at=current.created_at if current is not None else now,
                updated_at=now,
                submitted_at=current.submitted_at if current is not None else None,
                failed_at=(
                    now
                    if status is ResponseSubmissionStatus.FAILED_DEFINITIVE
                    else (current.failed_at if current is not None else None)
                ),
            )
            if current is None:
                await uow.response_submissions.add(record)
            else:
                await uow.response_submissions.update(record)
            if event_factory is not None:
                await uow.events.append(event_factory(attempts), aggregate_revision=0)
            await uow.commit()
        finally:
            await uow.close()

    # ------------------------------------------------------------------
    async def _attach_and_mark_submitted(
        self,
        *,
        tenant_id: str,
        proposal: ResponseProposal,
        key: str,
        result: SoarExecutionResult,
    ) -> None:
        """Create the provider projection + SUBMIT the proposal atomically.

        Crash-after-provider-create-before-local-commit is safe: the retry presents
        the SAME ``Idempotency-Key``, HISIEM returns the SAME execution, and this
        local persist then completes — one logical execution, one provider execution.
        """
        now = utc_now()
        terminal = result.status in _TERMINAL
        execution = ResponseExecutionRef(
            proposal_id=proposal.id,
            provider=_PROVIDER,
            execution_id=result.execution_id,
            submission_key=key,
            status=result.status,
            submitted_at=now,
            last_observed_at=now,
            started_at=now,
            finished_at=now if terminal else None,
            safe_result=dict(result.safe_result) or None,
            safe_error_code=result.safe_error_code,
            safe_error_message=result.safe_error_message,
        )

        uow = self._uow_factory()
        try:
            reloaded = await uow.response_proposals.get(
                tenant_id=tenant_id, proposal_id=proposal.id
            )
            if reloaded is None:
                return
            existing = await uow.response_executions.get_by_proposal(
                tenant_id=tenant_id, proposal_id=proposal.id
            )
            if existing is not None:
                # A concurrent delivery already attached a real execution: converge.
                return
            if reloaded.status is not ResponseProposalStatus.APPROVED:
                # SUBMITTED already converged, or a decision changed underneath us:
                # never submit a non-approved contract.
                return

            await uow.response_executions.add(execution)
            reloaded.mark_submitted(execution)
            await uow.response_proposals.update(reloaded)
            # The local submission lifecycle settles in the SAME transaction that
            # creates the provider projection: a proposal can never be SUBMITTED
            # without both facts agreeing.
            await _mark_submission_submitted(
                uow, tenant_id=tenant_id, proposal_id=proposal.id, key=key, now=now
            )

            await uow.events.append(
                response_execution_started(proposal.id, tenant_id=tenant_id),
                aggregate_revision=reloaded.lock_version,
            )
            if terminal:
                await uow.events.append(
                    _terminal_event(proposal.id, tenant_id, result),
                    aggregate_revision=reloaded.lock_version,
                )
            else:
                # Enqueues the FIRST observe delivery — durable reconciliation.
                await uow.events.append(
                    response_execution_submitted(
                        proposal.id,
                        external_execution_id=result.execution_id,
                        tenant_id=tenant_id,
                    ),
                    aggregate_revision=reloaded.lock_version,
                    available_at=utc_now() + self._observe_delay,
                )
            await uow.commit()
        finally:
            await uow.close()


class ResponseObserveRunner:
    """Reconciles ONE submitted execution toward a terminal state (durable)."""

    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        soar: SoarPort,
        observe_delay_seconds: float = 15.0,
        status_checks_per_delivery: int = 1,
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._soar = soar
        self._observe_delay = timedelta(seconds=observe_delay_seconds)
        self._checks = max(1, status_checks_per_delivery)

    async def run(self, *, aggregate_id: str, tenant_id: str) -> None:
        proposal_id = UUID(aggregate_id)
        uow = self._uow_factory()
        try:
            proposal = await uow.response_proposals.get(
                tenant_id=tenant_id, proposal_id=proposal_id
            )
            execution = await uow.response_executions.get_by_proposal(
                tenant_id=tenant_id, proposal_id=proposal_id
            )
        finally:
            await uow.close()

        if proposal is None or execution is None:
            return  # nothing submitted yet — a queued observe is a no-op
        if execution.status in _TERMINAL:
            # A terminal execution no longer schedules observation.
            return
        if proposal.status is not ResponseProposalStatus.SUBMITTED:
            return
        if not execution.execution_id:
            raise NonRetryableRunError(code="RESPONSE_NO_EXECUTION_ID")

        last: SoarExecutionResult | None = None
        for _ in range(self._checks):
            try:
                result = await self._soar.get_execution_status(
                    tenant_id=tenant_id, execution_id=execution.execution_id
                )
            except ExternalServiceError as exc:
                if _is_definitive(exc):
                    await self._settle_terminal_failure(
                        tenant_id=tenant_id,
                        execution=execution,
                        error_code=exc.upstream_code or "SOAR_REJECTED",
                        error_message=_safe_message(exc),
                    )
                    return
                # TRANSIENT/UNCERTAIN READ: a timeout, transport error, 408/425/429
                # or 5xx tells us NOTHING about the execution — HISIEM still owns
                # that truth and may well be running it. This is the same situation
                # as a non-terminal status, so it uses the same durable mechanism:
                # persist the failed read and schedule the next observation with a
                # future available_at, then complete THIS delivery normally. Driving
                # it through the generic retry/backoff exception path instead would
                # let a long provider outage exhaust _MAX_ATTEMPTS and permanently
                # dead-letter the reconciliation responsibility.
                await self._reschedule_observation(
                    tenant_id=tenant_id,
                    execution=execution,
                    error_code=exc.upstream_code or "SOAR_UNAVAILABLE",
                )
                return

            if result.status in _TERMINAL:
                await self._settle_terminal(
                    tenant_id=tenant_id, execution=execution, result=result
                )
                return
            # Still running: keep the remaining (bounded) checks of THIS delivery,
            # then hand off to the durable reschedule below.
            last = result

        assert last is not None  # _checks >= 1 guarantees at least one attempt
        await self._settle_nonterminal(
            tenant_id=tenant_id, execution=execution, result=last
        )

    # ------------------------------------------------------------------
    async def _reschedule_observation(
        self,
        *,
        tenant_id: str,
        execution: ResponseExecutionRef,
        error_code: str,
    ) -> None:
        """Durably schedule the next observation after a failed READ.

        The execution projection is deliberately NOT touched: we learned nothing
        about the provider's execution, so its recorded status, timestamps and
        errors stay exactly as the last successful observation left them. Only the
        reconciliation FACT is persisted, and that fact carries the schedule.
        """
        uow = self._uow_factory()
        try:
            current = await uow.response_executions.get_by_proposal(
                tenant_id=tenant_id, proposal_id=execution.proposal_id
            )
            if current is None or current.status in _TERMINAL:
                return  # already settled by another delivery
            await uow.events.append(
                response_execution_observation_failed(
                    execution.proposal_id,
                    external_execution_id=execution.execution_id,
                    error_code=error_code,
                    tenant_id=tenant_id,
                ),
                aggregate_revision=0,
                available_at=utc_now() + self._observe_delay,
            )
            await uow.commit()
        finally:
            await uow.close()

    async def _settle_nonterminal(
        self,
        *,
        tenant_id: str,
        execution: ResponseExecutionRef,
        result: SoarExecutionResult,
    ) -> None:
        """Persist the observation and DURABLY schedule the next one."""
        now = utc_now()
        observed = _observed_ref(execution, status=result.status, now=now, result=result)
        uow = self._uow_factory()
        try:
            current = await uow.response_executions.get_by_proposal(
                tenant_id=tenant_id, proposal_id=execution.proposal_id
            )
            if current is None or current.status in _TERMINAL:
                return  # already settled by another delivery
            await uow.response_executions.update(observed)
            await uow.events.append(
                response_execution_observed(
                    execution.proposal_id,
                    external_execution_id=execution.execution_id,
                    status=result.status,
                    tenant_id=tenant_id,
                ),
                aggregate_revision=0,
                available_at=now + self._observe_delay,
            )
            await uow.commit()
        finally:
            await uow.close()

    async def _settle_terminal(
        self,
        *,
        tenant_id: str,
        execution: ResponseExecutionRef,
        result: SoarExecutionResult,
    ) -> None:
        now = utc_now()
        settled = _observed_ref(
            execution,
            status=result.status,
            now=now,
            result=result,
            finished_at=now,
        )
        await self._persist_terminal(
            tenant_id=tenant_id,
            execution=execution,
            settled=settled,
            event=_terminal_event(execution.proposal_id, tenant_id, result),
        )

    async def _settle_terminal_failure(
        self,
        *,
        tenant_id: str,
        execution: ResponseExecutionRef,
        error_code: str,
        error_message: str,
    ) -> None:
        now = utc_now()
        settled = _observed_ref(
            execution,
            status="FAILED",
            now=now,
            result=None,
            finished_at=now,
            safe_error_code=error_code,
            safe_error_message=error_message,
        )
        await self._persist_terminal(
            tenant_id=tenant_id,
            execution=execution,
            settled=settled,
            event=response_execution_failed(
                execution.proposal_id, error_code=error_code, tenant_id=tenant_id
            ),
        )

    async def _persist_terminal(
        self,
        *,
        tenant_id: str,
        execution: ResponseExecutionRef,
        settled: ResponseExecutionRef,
        event: ResponseEvent,
    ) -> None:
        uow = self._uow_factory()
        try:
            current = await uow.response_executions.get_by_proposal(
                tenant_id=tenant_id, proposal_id=execution.proposal_id
            )
            if current is None or current.status in _TERMINAL:
                return  # idempotent: never re-settle a terminal execution
            await uow.response_executions.update(settled)
            await uow.events.append(event, aggregate_revision=0)
            await uow.commit()
        finally:
            await uow.close()


def _observed_ref(
    execution: ResponseExecutionRef,
    *,
    status: str,
    now: datetime,
    result: SoarExecutionResult | None,
    finished_at: datetime | None = None,
    safe_error_code: str | None = None,
    safe_error_message: str | None = None,
) -> ResponseExecutionRef:
    return ResponseExecutionRef(
        proposal_id=execution.proposal_id,
        provider=execution.provider,
        execution_id=execution.execution_id,
        submission_key=execution.submission_key,
        status=status,
        submitted_at=execution.submitted_at,
        last_observed_at=now,
        started_at=execution.started_at or execution.submitted_at,
        finished_at=finished_at,
        safe_result=(
            dict(result.safe_result) or None
            if result is not None
            else execution.safe_result
        ),
        safe_error_code=(
            result.safe_error_code if result is not None else safe_error_code
        ),
        safe_error_message=(
            result.safe_error_message if result is not None else safe_error_message
        ),
    )


def _terminal_event(
    proposal_id: UUID, tenant_id: str, result: SoarExecutionResult
) -> ResponseEvent:
    if result.status == "SUCCEEDED":
        return response_execution_succeeded(
            proposal_id,
            external_execution_id=result.execution_id,
            tenant_id=tenant_id,
        )
    return response_execution_failed(
        proposal_id,
        error_code=result.safe_error_code or "SOAR_EXECUTION_FAILED",
        tenant_id=tenant_id,
    )


class ResponseSubmitExhaustionHandler:
    """Persists the business meaning of an exhausted SUBMIT retry budget.

    Injected into the response submit dispatcher as its ``on_retry_exhausted``
    hook, so the generic dispatcher never has to know what exhaustion means for a
    response. It runs BEFORE the outbox row is dead-lettered: the workspace must
    never be able to read "the delivery is dead" while the submission still says
    "retrying".
    """

    def __init__(self, *, unit_of_work_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = unit_of_work_factory

    async def on_retry_exhausted(
        self, *, aggregate_id: str, tenant_id: str, error_code: str
    ) -> None:
        proposal_id = UUID(aggregate_id)
        uow = self._uow_factory()
        try:
            current = await uow.response_submissions.get_by_proposal(
                tenant_id=tenant_id, proposal_id=proposal_id
            )
            if current is None:
                return
            if current.status in (
                _SUBMISSION_SUBMITTED,
                _SUBMISSION_FAILED_DEFINITIVE,
                _SUBMISSION_ATTENTION_REQUIRED,
            ):
                # Already terminal: a later exhaustion must never overwrite a
                # recorded verdict, and never resurrect a settled submission.
                return
            now = utc_now()
            record = replace(
                current,
                status=_SUBMISSION_ATTENTION_REQUIRED,
                updated_at=now,
                attention_required_at=now,
            )
            await uow.response_submissions.update(record)
            await uow.events.append(
                response_submission_attention_required(
                    proposal_id,
                    submission_key=current.submission_key,
                    attempt_count=current.attempt_count,
                    error_code=current.last_error_code or error_code,
                    safe_error_message=current.safe_error_message,
                    tenant_id=tenant_id,
                ),
                aggregate_revision=0,
            )
            await uow.commit()
        finally:
            await uow.close()


async def _mark_submission_submitted(
    uow: UnitOfWork, *, tenant_id: str, proposal_id: UUID, key: str, now: datetime
) -> None:
    """Settle the local submission as SUBMITTED inside the caller's transaction."""
    current = await uow.response_submissions.get_by_proposal(
        tenant_id=tenant_id, proposal_id=proposal_id
    )
    record = ResponseSubmission(
        proposal_id=proposal_id,
        submission_key=current.submission_key if current is not None else key,
        status=ResponseSubmissionStatus.SUBMITTED.value,
        attempt_count=current.attempt_count if current is not None else 0,
        last_error_code=None,
        safe_error_message=None,
        created_at=current.created_at if current is not None else now,
        updated_at=now,
        submitted_at=now,
        failed_at=None,
    )
    if current is None:
        await uow.response_submissions.add(record)
    else:
        await uow.response_submissions.update(record)


def _is_definitive(exc: ExternalServiceError) -> bool:
    """Is this provider error a permanent rejection rather than a retryable blip?"""
    code = exc.upstream_code or ""
    if code in _DEFINITIVE_CODES:
        return True
    if code.startswith("HTTP_"):
        try:
            status = int(code.removeprefix("HTTP_"))
        except ValueError:
            return False
        return 400 <= status < 500 and status not in _TRANSIENT_HTTP_STATUSES
    return False


def _safe_message(exc: ExternalServiceError) -> str:
    return str(exc)[:500]


__all__ = ["ResponseObserveRunner", "ResponseSubmitRunner"]
