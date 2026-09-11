"""Durable response-execution worker.

Consumes a ``response_execution_queued`` outbox delivery and performs the ONE
approved side effect through the :class:`SoarPort`. It is UNTRUSTED-INPUT-FREE: it
is handed only persisted identifiers (proposal_id, tenant_id) and RELOADS the
authoritative approved contract from the database before submitting — it never
executes a payload carried in the queue, and it NEVER calls the LLM (spec §16/§17).

Idempotency: the stable ``submission_key`` on the execution ref is presented to
HISIEM on every (re)submission, so a retry converges on ONE provider execution.
A definitive provider rejection is persisted as FAILED (and the outbox row is
acknowledged); a transport/transient error is re-raised so the dispatcher retries
with backoff (spec §25/§26/§48).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import UUID

from ...application.errors import ExternalServiceError
from ...application.ports.soar import SoarExecutionResult, SoarPort
from ...application.ports.unit_of_work import UnitOfWork
from ...domain.response.aggregate import ResponseProposal
from ...domain.response.enums import ResponseProposalStatus
from ...domain.response.events import (
    response_execution_failed,
    response_execution_started,
    response_execution_succeeded,
)
from ...domain.response.value_objects import ResponseExecutionRef
from ...domain.shared.identifiers import utc_now
from .investigation_runner import NonRetryableRunError

_TERMINAL = {"SUCCEEDED", "FAILED"}
# A definitive (non-retryable) provider rejection: a bad request/action/target.
_DEFINITIVE_CODES = {"SOAR_NOT_FOUND", "SOAR_CONFIGURATION"}


class ResponseExecutionRunner:
    """Reloads an approved proposal and submits it to HISIEM SOAR (durable)."""

    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        soar: SoarPort,
        max_poll_attempts: int = 30,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._soar = soar
        self._max_poll_attempts = max_poll_attempts
        self._poll_interval = poll_interval_seconds

    async def run(self, *, aggregate_id: str, tenant_id: str) -> None:
        proposal_id = UUID(aggregate_id)
        loaded = await self._load(tenant_id=tenant_id, proposal_id=proposal_id)
        if loaded is None:
            # The queued event's proposal vanished — nothing to execute.
            return
        proposal, execution = loaded
        if execution.status in _TERMINAL:
            return  # already settled; a duplicate delivery is a no-op
        if proposal.status != ResponseProposalStatus.APPROVED:
            # A non-approved proposal must never execute — deterministic config fault.
            # (``approval_request_id`` is a transient link set during the decide
            # transaction; the authoritative, reloadable fact is the APPROVED status,
            # which is reachable only through a recorded human approval.)
            raise NonRetryableRunError(code="RESPONSE_NOT_APPROVED")

        target_ref = proposal.target_refs[0] if proposal.target_refs else None
        if target_ref is None:
            await self._fail_execution(
                tenant_id, execution, "RESPONSE_NO_TARGET", "approved proposal has no target"
            )
            return

        try:
            result = await self._soar.submit_execution(
                tenant_id=tenant_id,
                proposal_id=proposal.id,
                submission_key=execution.submission_key,
                action_key=proposal.action_key,
                parameters=dict(proposal.parameters),
                target_ref=target_ref,
            )
        except ExternalServiceError as exc:
            if _is_definitive(exc):
                await self._fail_execution(
                    tenant_id,
                    execution,
                    exc.upstream_code or "SOAR_REJECTED",
                    _safe_message(exc),
                )
                return
            raise  # transient → dispatcher retries with backoff

        result = await self._poll_to_terminal(
            tenant_id=tenant_id, execution_id=result.execution_id, last=result
        )
        await self._settle(tenant_id, execution, result)

    # ------------------------------------------------------------------
    async def _load(
        self, *, tenant_id: str, proposal_id: UUID
    ) -> tuple[ResponseProposal, ResponseExecutionRef] | None:
        uow = self._uow_factory()
        try:
            proposal = await uow.response_proposals.get(
                tenant_id=tenant_id, proposal_id=proposal_id
            )
            execution = await uow.response_executions.get_by_proposal(
                tenant_id=tenant_id, proposal_id=proposal_id
            )
            if proposal is None or execution is None:
                return None
            return proposal, execution
        finally:
            await uow.close()

    async def _poll_to_terminal(
        self, *, tenant_id: str, execution_id: str, last: SoarExecutionResult
    ) -> SoarExecutionResult:
        result = last
        for _ in range(self._max_poll_attempts):
            if result.status in _TERMINAL:
                return result
            await asyncio.sleep(self._poll_interval)
            result = await self._soar.get_execution_status(
                tenant_id=tenant_id, execution_id=execution_id
            )
        return result

    async def _settle(
        self,
        tenant_id: str,
        execution: ResponseExecutionRef,
        result: SoarExecutionResult,
    ) -> None:
        now = utc_now()
        status = result.status if result.status in _TERMINAL else "RUNNING"
        updated = ResponseExecutionRef(
            proposal_id=execution.proposal_id,
            provider=execution.provider,
            execution_id=result.execution_id,
            submission_key=execution.submission_key,
            status=status,
            submitted_at=execution.submitted_at,
            last_observed_at=now,
            started_at=execution.started_at or execution.submitted_at,
            finished_at=now if status in _TERMINAL else None,
            safe_result=dict(result.safe_result) or None,
            safe_error_code=result.safe_error_code,
            safe_error_message=result.safe_error_message,
        )
        uow = self._uow_factory()
        try:
            await uow.response_executions.update(updated)
            if status == "SUCCEEDED":
                await uow.events.append(
                    response_execution_succeeded(
                        execution.proposal_id,
                        external_execution_id=result.execution_id,
                        tenant_id=tenant_id,
                    ),
                    aggregate_revision=0,
                )
            elif status == "RUNNING":
                await uow.events.append(
                    response_execution_started(
                        execution.proposal_id, tenant_id=tenant_id
                    ),
                    aggregate_revision=0,
                )
            await uow.commit()
        finally:
            await uow.close()

    async def _fail_execution(
        self,
        tenant_id: str,
        execution: ResponseExecutionRef,
        error_code: str,
        error_message: str,
    ) -> None:
        now = utc_now()
        updated = ResponseExecutionRef(
            proposal_id=execution.proposal_id,
            provider=execution.provider,
            execution_id=execution.execution_id,
            submission_key=execution.submission_key,
            status="FAILED",
            submitted_at=execution.submitted_at,
            last_observed_at=now,
            started_at=execution.started_at or execution.submitted_at,
            finished_at=now,
            safe_error_code=error_code,
            safe_error_message=error_message,
        )
        uow = self._uow_factory()
        try:
            await uow.response_executions.update(updated)
            await uow.events.append(
                response_execution_failed(
                    execution.proposal_id,
                    error_code=error_code,
                    tenant_id=tenant_id,
                ),
                aggregate_revision=0,
            )
            await uow.commit()
        finally:
            await uow.close()


def _is_definitive(exc: ExternalServiceError) -> bool:
    code = exc.upstream_code or ""
    if code in _DEFINITIVE_CODES:
        return True
    if code.startswith("HTTP_"):
        try:
            return 400 <= int(code.removeprefix("HTTP_")) < 500
        except ValueError:
            return False
    return False


def _safe_message(exc: ExternalServiceError) -> str:
    text = str(exc)
    return text[:500]
