"""Investigation command handlers.

Thin orchestration: build trusted context → load aggregate → apply domain
method → persist via UnitOfWork. No SQL, no ORM, no infrastructure imports here.

Transaction discipline (persistence-schema.md §3, §31): NO network I/O may run
inside a database transaction. ``start_alert_investigation`` therefore never holds
one UnitOfWork across the HISIEM hydration call — each DB step is a SHORT,
independent transaction and the HISIEM get_alert HTTP call happens with NO open
business transaction. A second active-investigation re-check after hydration keeps
the database partial unique index as the final concurrency guard.

Request idempotency (hisiem-integration-contract.md §7): the same Idempotency-Key
must return the same logical result even after the original Investigation is
terminal. A stable ``idempotency_key`` is looked up by (tenant, command_type, key)
FIRST; a hit resolves the original aggregate and returns it, a miss proceeds with
the normal active-alert create flow. Reusing a key for a DIFFERENT source_alert_ref
is a deterministic idempotency conflict.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar
from uuid import UUID, uuid4

from ...domain.investigation.aggregate import Investigation
from ...domain.investigation.content import compute_content_hash
from ...domain.investigation.enums import InvestigationStatus
from ...domain.investigation.errors import ActiveInvestigationExistsError
from ...domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from ...domain.shared.identifiers import utc_now
from ..commands.investigation import (
    CancelInvestigation,
    StartAlertInvestigation,
)
from ..errors import (
    CommandReceiptConflictError,
    IdempotencyConflictError,
    NotFoundError,
)
from ..ports.durable import DurableCommand
from ..ports.hisiem import HisiemPort
from ..ports.unit_of_work import UnitOfWork
from .durable_support import _audit_only_key, flush_events

_C = TypeVar("_C", StartAlertInvestigation, CancelInvestigation)


class InvestigationCommandHandler:
    """Coordinates investigation lifecycle commands against short UoWs."""

    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        hisiem: HisiemPort,
        budget_limits: BudgetLimits,
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._hisiem = hisiem
        self._budget_limits = budget_limits

    async def start_alert_investigation(
        self, command: StartAlertInvestigation
    ) -> Investigation:
        """Create (or return the existing) investigation for one alert.

        Flow (no network inside a DB transaction):
          1. receipt replay lookup when a stable idempotency_key is supplied — a
             hit returns the ORIGINAL aggregate (even if terminal), after a
             fingerprint check;
          2. short DB read: return the existing active investigation when present;
          3. CLOSE that transaction;
          4. HISIEM get_alert HTTP hydration — NO DB transaction open;
          5. NEW short DB transaction: re-check the active investigation (a
             concurrent start may have won), then create if still absent and commit
             aggregate + InvestigationCreated domain_event + outbox + receipt
             atomically;
          6. on an active-investigation unique-index conflict, ROLLBACK, then in a
             FRESH transaction re-read the winner by tenant + alert and return it
             (never leak the IntegrityError to the API);
          7. on a command_receipt scoped-unique conflict (a CONCURRENT request with
             the SAME Idempotency-Key won the create), ROLLBACK, then in a FRESH
             transaction reload the winning receipt and compare the request
             fingerprint: SAME request → return the winner's original aggregate;
             DIFFERENT request → raise IdempotencyConflictError (409). Never a raw
             IntegrityError → 500, and never a silent return of the wrong
             investigation.
        """
        alert_ref = command.source_alert_ref
        # V1 integration contract: only HISIEM alerts may be started (hisiem-
        # integration-contract.md §4). Reject anything else deterministically.
        if not alert_ref.is_alert:
            raise ValueError(
                "StartAlertInvestigation requires provider=hisiem and "
                f"resource_type=alert, got {alert_ref.provider}:{alert_ref.resource_type}"
            )

        if command.idempotency_key:
            existing = await self._find_replayed(command, alert_ref)
            if existing is not None:
                return existing

        existing = await self._find_active(command.tenant_id, alert_ref)
        if existing is not None:
            return await self._bind_or_replay_existing(command, alert_ref, existing)

        # Authoritative hydration — NO DB transaction is open here.
        alert = await self._hisiem.get_alert(
            tenant_id=command.tenant_id, alert_id=alert_ref.address_id
        )
        if alert is None:
            raise NotFoundError(
                "alert not found or not accessible",
                resource_type="alert",
                resource_id=alert_ref.address_id,
            )

        # Re-check after hydration: a concurrent start may have committed while we
        # were on the network. If one exists now, return it (do not create a second).
        existing = await self._find_active(command.tenant_id, alert_ref)
        if existing is not None:
            return await self._bind_or_replay_existing(command, alert_ref, existing)

        try:
            return await self._create_investigation(command, alert_ref)
        except ActiveInvestigationExistsError as exc:
            # A concurrent start committed between our re-check and our insert. The
            # loser's partial rows were rolled back by the UoW translation; converge
            # on the winner.
            return await self._read_active_or_raise(command, alert_ref, exc)
        except CommandReceiptConflictError as exc:
            # A CONCURRENT request with the SAME Idempotency-Key (and same
            # tenant + command type) won the create. Converge deterministically by
            # re-reading the winning receipt and comparing the request fingerprint.
            return await self._resolve_receipt_conflict(command, alert_ref, exc)

    async def _create_investigation(
        self, command: StartAlertInvestigation, alert_ref: ExternalResourceRef
    ) -> Investigation:
        actor = ActorRef(
            subject_id=command.initiated_by_subject,
            tenant_id=command.tenant_id,
            display_name=command.initiated_by_display_name,
        )

        investigation = Investigation.create(
            id=uuid4(),
            tenant_id=command.tenant_id,
            source_alert_ref=alert_ref,
            initiated_by=actor,
            budget_limits=self._budget_limits,
            now=utc_now(),
        )
        # New short transaction: persist domain + event + outbox + receipt atomically.
        # On the active-investigation unique-index conflict the UoW commit raises
        # ActiveInvestigationExistsError (only for THAT constraint — never other
        # IntegrityErrors, which propagate unchanged).
        uow = self._uow_factory()
        try:
            await uow.investigations.add(investigation)
            await flush_events(uow, investigation)
            await self._record_receipt(uow, command, investigation.id, alert_ref)
            await uow.commit()
        finally:
            await uow.close()
        return investigation

    async def _read_active_or_raise(
        self,
        command: StartAlertInvestigation,
        alert_ref: ExternalResourceRef,
        conflict: ActiveInvestigationExistsError,
    ) -> Investigation:
        """Re-read the concurrent winner; re-raise if it vanished (never swallow).

        The winner is returned through ``_bind_or_replay_existing`` so a stable
        Idempotency-Key is bound to it AND the request-fingerprint check still runs —
        an active-investigation race that fires BEFORE the command_receipt race must
        never skip the same-key/different-request conflict detection.
        """
        winner = await self._find_active(command.tenant_id, alert_ref)
        if winner is not None:
            return await self._bind_or_replay_existing(command, alert_ref, winner)
        raise RuntimeError(
            f"active-investigation conflict for {alert_ref.address_id} but no "
            "winner was found"
        ) from conflict

    async def _bind_or_replay_existing(
        self,
        command: StartAlertInvestigation,
        alert_ref: ExternalResourceRef,
        investigation: Investigation,
    ) -> Investigation:
        """Return ``investigation`` while binding a stable Idempotency-Key to it.

        Called on EVERY 'return an existing/winner Investigation' path. Semantics:
          - no ``idempotency_key`` → return ``investigation`` unchanged (nothing to
            bind);
          - a scoped receipt for (tenant, command_type, key) ALREADY exists:
              * request fingerprint matches → return the receipt's aggregate (this
                key's logical result — the SAME investigation);
              * fingerprint differs → ``IdempotencyConflictError`` (same key bound
                to a DIFFERENT business request — never silently return a different
                investigation);
          - no receipt yet → bind the key by recording a CommandReceipt pointing at
            ``investigation.id``. This is a NEW key used while an investigation is
            already active: no new Investigation/DomainEvent/Outbox is created (no
            new business fact) — only the idempotency binding is persisted.

        On a concurrent command_receipt unique conflict the current transaction was
        already rolled back; this closes its UoW and converges via a fresh
        ``_resolve_receipt_conflict`` (reload winner receipt, compare fingerprint).
        """
        if not command.idempotency_key:
            return investigation
        key: str = command.idempotency_key
        try:
            return await self._bind_existing_inner(command, alert_ref, investigation, key)
        except CommandReceiptConflictError as exc:
            # A concurrent same-key request recorded its receipt first (the inner
            # UoW was already rolled back and closed). Converge on the winner
            # deterministically (fingerprint check inside).
            return await self._resolve_receipt_conflict(command, alert_ref, exc)

    async def _bind_existing_inner(
        self,
        command: StartAlertInvestigation,
        alert_ref: ExternalResourceRef,
        investigation: Investigation,
        key: str,
    ) -> Investigation:
        """Single-transaction bind of ``command.idempotency_key`` → investigation.

        Returns the investigation this key logically resolves to. When the key is
        already bound (a concurrent request won), returns that receipt's aggregate
        after a fingerprint check, or raises ``IdempotencyConflictError``. When the
        key is unbound, records a CommandReceipt pointing at ``investigation.id`` and
        commits. A concurrent receipt-unique conflict surfaces as
        ``CommandReceiptConflictError`` (the caller converges in a fresh UoW).
        """
        command_type = type(command).__name__
        fingerprint = _alert_fingerprint(alert_ref)
        uow = self._uow_factory()
        try:
            receipt = await uow.command_receipts.find(
                tenant_id=command.tenant_id,
                command_type=command_type,
                idempotency_key=key,
            )
            if receipt is not None and receipt.aggregate_id is not None:
                if (
                    receipt.request_fingerprint
                    and receipt.request_fingerprint != fingerprint
                ):
                    raise IdempotencyConflictError(
                        f"Idempotency-Key {key} was already used for a different "
                        "source_alert_ref"
                    )
                existing = await uow.investigations.get(
                    tenant_id=command.tenant_id,
                    investigation_id=receipt.aggregate_id,
                )
                if existing is None:
                    raise RuntimeError(
                        f"receipt for Idempotency-Key {key} references a missing "
                        f"investigation {receipt.aggregate_id}"
                    )
                return existing
            # Bind the key to the investigation we are about to return.
            await uow.command_receipts.record(
                DurableCommand(
                    command_id=command.command_id,
                    command_type=command_type,
                    idempotency_key=key,
                    tenant_id=command.tenant_id,
                    aggregate_type="investigation",
                    aggregate_id=investigation.id,
                    correlation_id=command.correlation_id,
                    request_fingerprint=fingerprint,
                )
            )
            await uow.commit()
            return investigation
        finally:
            await uow.close()

    async def _resolve_receipt_conflict(
        self,
        command: StartAlertInvestigation,
        alert_ref: ExternalResourceRef,
        conflict: CommandReceiptConflictError,
    ) -> Investigation:
        """Converge after losing a command_receipt scoped-unique race.

        The current transaction was already rolled back by the UoW translation. In
        a FRESH transaction, reload the winning receipt (tenant + command_type +
        key) and compare the request fingerprint:
          - SAME request  → load and return the winner's original aggregate;
          - DIFFERENT request → deterministic IdempotencyConflictError (409).
        Never a raw IntegrityError → 500, and never a silent return of the wrong
        investigation (the fingerprint check guards that).
        """
        uow = self._uow_factory()
        try:
            receipt = await uow.command_receipts.find(
                tenant_id=command.tenant_id,
                command_type=type(command).__name__,
                idempotency_key=command.idempotency_key or "",
            )
            if receipt is None or receipt.aggregate_id is None:
                raise RuntimeError(
                    f"command_receipt conflict for Idempotency-Key "
                    f"{command.idempotency_key} but no winning receipt was found"
                ) from conflict
            fingerprint = _alert_fingerprint(alert_ref)
            if receipt.request_fingerprint and receipt.request_fingerprint != fingerprint:
                raise IdempotencyConflictError(
                    f"Idempotency-Key {command.idempotency_key} was already used for "
                    "a different source_alert_ref"
                ) from conflict
            investigation = await uow.investigations.get(
                tenant_id=command.tenant_id, investigation_id=receipt.aggregate_id
            )
            if investigation is None:
                raise RuntimeError(
                    f"winning receipt {receipt.aggregate_id} for Idempotency-Key "
                    f"{command.idempotency_key} references a missing investigation"
                ) from conflict
            return investigation
        finally:
            await uow.close()

    async def _find_replayed(
        self, command: StartAlertInvestigation, alert_ref: ExternalResourceRef
    ) -> Investigation | None:
        """Receipt replay: same key → return the ORIGINAL aggregate (any status).

        A replay of the SAME source_alert_ref returns the original investigation.
        Reusing the key for a DIFFERENT source_alert_ref is a deterministic
        idempotency conflict (never a silent wrong replay).
        """
        uow = self._uow_factory()
        try:
            receipt = await uow.command_receipts.find(
                tenant_id=command.tenant_id,
                command_type=type(command).__name__,
                idempotency_key=command.idempotency_key or "",
            )
            if receipt is None or receipt.aggregate_id is None:
                return None
            fingerprint = _alert_fingerprint(alert_ref)
            if receipt.request_fingerprint and receipt.request_fingerprint != fingerprint:
                raise IdempotencyConflictError(
                    f"Idempotency-Key {command.idempotency_key} was already used for "
                    "a different source_alert_ref"
                )
            investigation = await uow.investigations.get(
                tenant_id=command.tenant_id, investigation_id=receipt.aggregate_id
            )
            return investigation
        finally:
            await uow.close()

    async def _find_active(
        self, tenant_id: str, alert_ref: ExternalResourceRef
    ) -> Investigation | None:
        """Short DB read (its own transaction) for an active investigation."""
        uow = self._uow_factory()
        try:
            return await uow.investigations.find_active_by_alert(
                tenant_id=tenant_id, source_alert_ref=alert_ref
            )
        finally:
            await uow.close()

    async def cancel_investigation(self, command: CancelInvestigation) -> Investigation:
        uow = self._uow_factory()
        try:
            return await self._cancel(uow, command)
        finally:
            await uow.close()

    async def _cancel(self, uow: UnitOfWork, command: CancelInvestigation) -> Investigation:
        investigation = await uow.investigations.get(
            tenant_id=command.tenant_id,
            investigation_id=command.investigation_id,
        )
        if investigation is None:
            raise NotFoundError(
                "investigation not found",
                resource_type="investigation",
                resource_id=str(command.investigation_id),
            )
        if investigation.status in (
            InvestigationStatus.CREATED,
            InvestigationStatus.RUNNING,
            InvestigationStatus.WAITING_APPROVAL,
        ):
            actor = ActorRef(
                subject_id=command.initiated_by_subject,
                tenant_id=command.tenant_id,
            )
            investigation.cancel(actor=actor)
            await uow.investigations.update(investigation)
            await flush_events(uow, investigation)
            await self._record_receipt(uow, command, investigation.id, None)
            await uow.commit()
        return investigation

    async def _record_receipt(
        self,
        uow: UnitOfWork,
        command: _C,
        investigation_id: UUID,
        alert_ref: ExternalResourceRef | None,
    ) -> None:
        key = command.idempotency_key or _audit_only_key(
            investigation_id=investigation_id,
            command_type=type(command).__name__,
            command_id=command.command_id,
        )
        await uow.command_receipts.record(
            DurableCommand(
                command_id=command.command_id,
                command_type=type(command).__name__,
                idempotency_key=key,
                tenant_id=command.tenant_id,
                aggregate_type="investigation",
                aggregate_id=investigation_id,
                correlation_id=command.correlation_id,
                request_fingerprint=_alert_fingerprint(alert_ref) if alert_ref else None,
            )
        )


def _alert_fingerprint(alert_ref: ExternalResourceRef) -> str:
    """Bounded, stable fingerprint of the request's business payload."""
    return compute_content_hash(
        {
            "provider": alert_ref.provider,
            "resource_type": alert_ref.resource_type,
            "address_id": alert_ref.address_id,
            "business_id": alert_ref.business_id,
        }
    )
