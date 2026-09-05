"""Application-layer tests for investigation start/cancel using in-memory fakes."""

from __future__ import annotations

from uuid import uuid4

import pytest

from hisiem_soc_copilot.application.commands.investigation import (
    CancelInvestigation,
    CompleteInvestigation,
    StartAlertInvestigation,
    StartInvestigation,
)
from hisiem_soc_copilot.application.errors import IdempotencyConflictError, NotFoundError
from hisiem_soc_copilot.application.handlers.investigation import (
    InvestigationCommandHandler,
)
from hisiem_soc_copilot.application.handlers.workflow import InvestigationWorkflowHandler
from hisiem_soc_copilot.domain.investigation.enums import (
    InvestigationStatus,
    TerminationReason,
)
from hisiem_soc_copilot.domain.investigation.value_objects import BudgetLimits, ExternalResourceRef
from tests.fixtures.fakes import FakeUnitOfWorkFactory


def _ref(address_id: str) -> ExternalResourceRef:
    return ExternalResourceRef(
        provider="hisiem", resource_type="alert", address_id=address_id
    )



class FakeHisiem:
    def __init__(self, *, alerts: dict[str, object] | None = None) -> None:
        self._alerts = alerts or {
            "alert-1": {"_id": "alert-1", "alert": {"rule_name": "SSH brute force"}}
        }
        self.calls: list[tuple[str, str]] = []

    async def get_alert(self, *, tenant_id: str, alert_id: str) -> object | None:
        self.calls.append((tenant_id, alert_id))
        return self._alerts.get(alert_id)

    async def search_events(self, **kwargs: object) -> list[dict[str, object]]:
        return []

    async def get_detection_rule(self, *, tenant_id: str, rule_id: str) -> object | None:
        return None


def _handler(hisiem: FakeHisiem | None = None):
    """Build a handler over a shared-store factory (short per-step transactions)."""
    factory = FakeUnitOfWorkFactory()
    return factory, InvestigationCommandHandler(
        unit_of_work_factory=factory,
        hisiem=hisiem or FakeHisiem(),
        budget_limits=BudgetLimits(),
    )


def _total_commits(factory: FakeUnitOfWorkFactory) -> int:
    return sum(inst.commits for inst in factory.instances)


async def test_start_creates_investigation_and_hydrates_authoritative_alert() -> None:
    factory, handler = _handler()
    hisiem = handler._hisiem

    cmd = StartAlertInvestigation(
        tenant_id="tenant-a",
        source_alert_ref=_ref("alert-1"),
        initiated_by_subject="analyst@corp",
    )
    inv = await handler.start_alert_investigation(cmd)

    assert inv.status == InvestigationStatus.CREATED
    assert inv.tenant_id == "tenant-a"
    assert inv.source_alert_ref.address_id == "alert-1"
    assert inv.initiated_by.subject_id == "analyst@corp"
    assert hisiem.calls == [("tenant-a", "alert-1")]
    # The two active-investigation reads are non-committing; only the create commit.
    assert _total_commits(factory) == 1
    # created event emitted once and durably flushed to the event ledger.
    assert [e.event_type for e in inv.pending_events].count("investigation_created") == 0
    assert [e.event_type for e in factory.events.events].count("investigation_created") == 1


async def test_start_returns_existing_active_investigation() -> None:
    factory, handler = _handler()

    first = await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_ref=_ref("alert-1"),
            initiated_by_subject="analyst@corp",
        )
    )
    # second start on the same alert + tenant reuses it (no new hydration, no new row)
    second = await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_ref=_ref("alert-1"),
            initiated_by_subject="analyst@corp",
        )
    )
    assert first.id == second.id
    assert len(factory.instances[-1].investigations.added) == 1
    # one authoritative hydration only for the first start
    assert handler._hisiem.calls == [("tenant-a", "alert-1")]


async def test_start_with_unknown_alert_raises_not_found() -> None:
    _factory, handler = _handler(hisiem=FakeHisiem(alerts={}))
    with pytest.raises(NotFoundError):
        await handler.start_alert_investigation(
            StartAlertInvestigation(
                tenant_id="tenant-a",
                source_alert_ref=_ref("alert-missing"),
                initiated_by_subject="analyst@corp",
            )
        )


async def test_cancel_running_investigation() -> None:
    factory, handler = _handler()
    inv = await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_ref=_ref("alert-1"),
            initiated_by_subject="analyst@corp",
        )
    )
    inv.start(actor=inv.initiated_by)
    await factory.instances[-1].investigations.update(inv)

    await handler.cancel_investigation(
        CancelInvestigation(
            tenant_id="tenant-a",
            investigation_id=inv.id,
            initiated_by_subject="analyst@corp",
        )
    )
    reloaded = await factory.instances[-1].investigations.get(
        tenant_id="tenant-a", investigation_id=inv.id
    )
    assert reloaded is not None
    assert reloaded.status == InvestigationStatus.CANCELLED
    assert reloaded.termination_reason == TerminationReason.CANCELLED_BY_USER
    assert reloaded.cancelled_at is not None


async def test_cancel_missing_investigation_raises_not_found() -> None:
    _factory, handler = _handler()
    with pytest.raises(NotFoundError):
        await handler.cancel_investigation(
            CancelInvestigation(
                tenant_id="tenant-a",
                investigation_id=uuid4(),
                initiated_by_subject="analyst@corp",
            )
        )


async def test_start_does_not_cross_tenants() -> None:
    """A start in tenant-a never returns an active investigation in tenant-b."""
    factory, handler = _handler()
    await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_ref=_ref("alert-1"),
            initiated_by_subject="a@corp",
        )
    )
    # same alert, different tenant → must create a new investigation
    inv_b = await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-b",
            source_alert_ref=_ref("alert-1"),
            initiated_by_subject="b@corp",
        )
    )
    assert inv_b.tenant_id == "tenant-b"
    assert len(factory.instances[-1].investigations.added) == 2


async def test_no_business_transaction_open_during_hisiem_hydration() -> None:
    """The HISIEM HTTP hydration must happen with NO open DB transaction.

    ``StartAlertInvestigation`` opens a short read, closes it, then hydrates over
    the network. During ``get_alert`` every short UnitOfWork instance the handler
    opened must already be closed (no transaction spans the HTTP call).
    """
    factory, handler = _handler()

    real_get_alert = handler._hisiem.get_alert

    async def asserting_get_alert(*, tenant_id: str, alert_id: str) -> object:
        # Assert every short UoW opened so far is closed (not mid-transaction).
        for inst in factory.instances:
            assert inst.is_closed, (
                "a DB transaction was left open across the HISIEM HTTP call"
            )
        return await real_get_alert(tenant_id=tenant_id, alert_id=alert_id)

    handler._hisiem.get_alert = asserting_get_alert  # type: ignore[method-assign]
    await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_ref=_ref("alert-1"),
            initiated_by_subject="analyst@corp",
        )
    )


async def test_recheck_after_hydration_returns_concurrent_winner() -> None:
    """A start that passes the pre-check but loses the race (another start commits
    while this one is hydrating over the network) must re-check after hydration and
    return the winner — never create a second active investigation.

    This mirrors the real partial-unique-index convergence: the app re-check after
    the HTTP call closes the gap so a duplicate insert is avoided, and the DB index
    is the final backstop.
    """
    factory, handler = _handler()

    real_get_alert = handler._hisiem.get_alert
    hydrated = False

    async def hydrating_get_alert(*, tenant_id: str, alert_id: str) -> object:
        nonlocal hydrated
        # Simulate a concurrent start that commits DURING our network hydration.
        if not hydrated:
            hydrated = True
            other = await handler.start_alert_investigation(
                StartAlertInvestigation(
                    tenant_id=tenant_id,
                    source_alert_ref=_ref(alert_id),
                    initiated_by_subject="rival@corp",
                )
            )
            assert other.status == InvestigationStatus.CREATED
        return await real_get_alert(tenant_id=tenant_id, alert_id=alert_id)

    handler._hisiem.get_alert = hydrating_get_alert  # type: ignore[method-assign]

    result = await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_ref=_ref("alert-1"),
            initiated_by_subject="analyst@corp",
        )
    )
    # The outer start returns the already-created active investigation (the rival's),
    # NOT a second CREATED row — the re-check after hydration converged.
    assert result.status == InvestigationStatus.CREATED
    uow = factory()
    reloaded = await uow.investigations.get_by_external_ref(
        tenant_id="tenant-a", provider="hisiem", resource_type="alert",
        address_id="alert-1",
    )
    assert reloaded is not None and reloaded.id == result.id
    # Exactly one investigation row exists for this tenant+alert.
    assert len(uow.investigations.added) == 1


async def test_same_idempotency_key_returns_same_logical_result() -> None:
    """A HISIEM retry reusing the same Idempotency-Key returns the same active
    investigation and records one receipt for the logical launch (contract §7)."""
    factory, handler = _handler()
    cmd = StartAlertInvestigation(
        tenant_id="tenant-a",
        source_alert_ref=_ref("alert-1"),
        initiated_by_subject="analyst@corp",
        idempotency_key="launch:tenant-a:alert-1:run-1",
    )
    first = await handler.start_alert_investigation(cmd)
    second = await handler.start_alert_investigation(cmd)  # simulated retry

    assert first.id == second.id  # same logical result
    # Only one created receipt for the logical operation.
    receipts = [
        r for r in factory.command_receipts.receipts()
        if r.idempotency_key == "launch:tenant-a:alert-1:run-1"
    ]
    assert len(receipts) == 1
    assert len(factory.instances[-1].investigations.added) == 1


async def test_non_alert_source_ref_is_rejected() -> None:
    """Only provider=hisiem + resource_type=alert may start an investigation."""
    _factory, handler = _handler()
    with pytest.raises(ValueError):
        await handler.start_alert_investigation(
            StartAlertInvestigation(
                tenant_id="tenant-a",
                source_alert_ref=ExternalResourceRef(
                    provider="elastic", resource_type="alert", address_id="x"
                ),
                initiated_by_subject="analyst@corp",
            )
        )
    with pytest.raises(ValueError):
        await handler.start_alert_investigation(
            StartAlertInvestigation(
                tenant_id="tenant-a",
                source_alert_ref=ExternalResourceRef(
                    provider="hisiem", resource_type="case", address_id="x"
                ),
                initiated_by_subject="analyst@corp",
            )
        )


async def _drive_terminal(factory: FakeUnitOfWorkFactory, inv_id) -> None:
    """CREATED → RUNNING → COMPLETED via the workflow handler over the same fakes."""
    workflow = InvestigationWorkflowHandler(unit_of_work_factory=factory)
    await workflow.start_investigation(
        StartInvestigation(
            tenant_id="tenant-a",
            investigation_id=inv_id,
            idempotency_key=f"investigation:{inv_id}:start",
        )
    )
    await workflow.complete(
        CompleteInvestigation(
            tenant_id="tenant-a",
            investigation_id=inv_id,
            idempotency_key=f"investigation:{inv_id}:complete",
        )
    )


async def test_idempotency_key_replayed_after_terminal_returns_original() -> None:
    """Replay contract (§7): K created Investigation A → A completes (terminal) →
    a HISIEM retry with K returns A itself — never a new investigation and never a
    second created event/outbox dispatch."""
    factory, handler = _handler()
    cmd = StartAlertInvestigation(
        tenant_id="tenant-a",
        source_alert_ref=_ref("alert-1"),
        initiated_by_subject="analyst@corp",
        idempotency_key="launch:tenant-a:alert-1:run-9",
    )
    first = await handler.start_alert_investigation(cmd)
    await _drive_terminal(factory, first.id)
    # first is now COMPLETED (terminal).
    reloaded = await factory.instances[-1].investigations.get(
        tenant_id="tenant-a", investigation_id=first.id
    )
    assert reloaded is not None and reloaded.status == InvestigationStatus.COMPLETED

    replayed = await handler.start_alert_investigation(cmd)
    assert replayed.id == first.id
    assert replayed.status == InvestigationStatus.COMPLETED
    # No second row was added (the replay short-circuits on the receipt).
    assert len(factory._investigations.added) == 1
    # Only ONE investigation_created domain event ever flushed.
    created = [
        e for e in factory.events.events if e.event_type == "investigation_created"
    ]
    assert len(created) == 1


async def test_idempotency_key_different_alert_is_conflict() -> None:
    """Reusing K for a DIFFERENT source_alert_ref is a deterministic conflict —
    never a silent wrong replay (contract §7)."""
    factory, handler = _handler()
    await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_ref=_ref("alert-1"),
            initiated_by_subject="analyst@corp",
            idempotency_key="launch:tenant-a:alert-1:run-9",
        )
    )
    # Same K now points at alert-2 → the receipt fingerprint differs.
    with pytest.raises(IdempotencyConflictError):
        await handler.start_alert_investigation(
            StartAlertInvestigation(
                tenant_id="tenant-a",
                source_alert_ref=_ref("alert-2"),
                initiated_by_subject="analyst@corp",
                idempotency_key="launch:tenant-a:alert-1:run-9",
            )
        )
    assert len(factory._investigations.added) == 1  # nothing was created


async def test_new_key_same_alert_active_returns_active() -> None:
    """A NEW key + same alert + an existing ACTIVE investigation → return the active
    investigation (the active-alert uniqueness, not the key, governs)."""
    factory, handler = _handler()
    first = await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_ref=_ref("alert-1"),
            initiated_by_subject="analyst@corp",
            idempotency_key="key-active-1",
        )
    )
    second = await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_ref=_ref("alert-1"),
            initiated_by_subject="analyst@corp",
            idempotency_key="key-active-2",
        )
    )
    assert second.id == first.id


async def test_new_key_same_alert_terminal_may_create_new() -> None:
    """A NEW key + same alert + previous investigation TERMINAL → may create a new
    investigation (nothing active blocks it)."""
    factory, handler = _handler()
    first = await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_ref=_ref("alert-1"),
            initiated_by_subject="analyst@corp",
            idempotency_key="key-run-1",
        )
    )
    await _drive_terminal(factory, first.id)

    second = await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_ref=_ref("alert-1"),
            initiated_by_subject="analyst@corp",
            idempotency_key="key-run-2",
        )
    )
    assert second.id != first.id


async def test_new_key_returning_active_binds_receipt_and_survives_terminal() -> None:
    """A NEW key K2 that returns an existing ACTIVE investigation A must bind K2 → A:
    after A completes, retrying K2 returns A (no second investigation is created)."""
    factory, handler = _handler()

    first = await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_ref=_ref("alert-1"),
            initiated_by_subject="analyst@corp",
            idempotency_key="K1",
        )
    )
    assert len(factory.instances[-1].investigations.added) == 1

    # K2 (a NEW key) hits the active Investigation A and returns it — and must bind
    # receipt(K2) → A.
    second = await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_ref=_ref("alert-1"),
            initiated_by_subject="analyst@corp",
            idempotency_key="K2",
        )
    )
    assert second.id == first.id
    # Still only ONE investigation row was ever added (returning active ≠ creating).
    assert len(factory._investigations.added) == 1
    # K2 is now bound to A.
    k2_receipt = factory.command_receipts._receipts.get(
        ("tenant-a", "StartAlertInvestigation", "K2")
    )
    assert k2_receipt is not None and k2_receipt.aggregate_id == first.id

    # A completes (terminal).
    await _drive_terminal(factory, first.id)

    # Retry K2 → still returns A (terminal) via the bound receipt; no Investigation B.
    retried = await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_ref=_ref("alert-1"),
            initiated_by_subject="analyst@corp",
            idempotency_key="K2",
        )
    )
    assert retried.id == first.id
    assert retried.status == InvestigationStatus.COMPLETED
    assert len(factory._investigations.added) == 1  # no second investigation

    # No DomainEvent/Outbox was emitted by the bind (no new business fact).
    created_events = [
        e for e in factory.events.events if e.event_type == "investigation_created"
    ]
    assert len(created_events) == 1


async def test_active_hit_same_key_different_business_id_is_conflict() -> None:
    """A key K already bound to Investigation A (business A); a request with the SAME
    key K + same address but a DIFFERENT business_id that would otherwise return the
    active A must be a deterministic IdempotencyConflictError — never silently
    returning A."""
    factory, handler = _handler()

    def ref(business_id: str | None) -> ExternalResourceRef:
        return ExternalResourceRef(
            provider="hisiem", resource_type="alert", address_id="alert-1",
            business_id=business_id,
        )

    await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_ref=ref("biz-A"),
            initiated_by_subject="analyst@corp",
            idempotency_key="K",
        )
    )

    # Same key K, same address, different business_id → the top-level replay lookup
    # itself must reject it (fingerprint differs).
    with pytest.raises(IdempotencyConflictError):
        await handler.start_alert_investigation(
            StartAlertInvestigation(
                tenant_id="tenant-a",
                source_alert_ref=ref("biz-B"),
                initiated_by_subject="analyst@corp",
                idempotency_key="K",
            )
        )
    assert len(factory._investigations.added) == 1
