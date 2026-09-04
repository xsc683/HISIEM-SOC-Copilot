"""Application-layer tests for investigation start/cancel using in-memory fakes."""

from __future__ import annotations

from uuid import uuid4

import pytest

from hisiem_soc_copilot.application.commands.investigation import (
    CancelInvestigation,
    StartAlertInvestigation,
)
from hisiem_soc_copilot.application.errors import NotFoundError
from hisiem_soc_copilot.application.handlers.investigation import (
    InvestigationCommandHandler,
)
from hisiem_soc_copilot.domain.investigation.enums import (
    InvestigationStatus,
    TerminationReason,
)
from hisiem_soc_copilot.domain.investigation.value_objects import BudgetLimits
from tests.fixtures.fakes import FakeUnitOfWork


class FakeHisiem:
    def __init__(self, *, alerts: dict[str, object] | None = None) -> None:
        self._alerts = alerts or {"alert-1": {"id": "alert-1", "title": "SSH brute force"}}
        self.calls: list[tuple[str, str]] = []

    async def get_alert(self, *, tenant_id: str, alert_id: str) -> object | None:
        self.calls.append((tenant_id, alert_id))
        return self._alerts.get(alert_id)

    async def search_events(self, **kwargs: object) -> list[dict[str, object]]:
        return []


def _handler(uow: FakeUnitOfWork | None = None, hisiem: FakeHisiem | None = None):
    return InvestigationCommandHandler(
        unit_of_work=uow or FakeUnitOfWork(),
        hisiem=hisiem or FakeHisiem(),
        budget_limits=BudgetLimits(),
    )


async def test_start_creates_investigation_and_hydrates_authoritative_alert() -> None:
    uow = FakeUnitOfWork()
    hisiem = FakeHisiem()
    handler = _handler(uow, hisiem)

    cmd = StartAlertInvestigation(
        tenant_id="tenant-a",
        source_alert_id="alert-1",
        initiated_by_subject="analyst@corp",
    )
    inv = await handler.start_alert_investigation(cmd)

    assert inv.status == InvestigationStatus.CREATED
    assert inv.tenant_id == "tenant-a"
    assert inv.source_alert_ref.address_id == "alert-1"
    assert inv.initiated_by.subject_id == "analyst@corp"
    assert hisiem.calls == [("tenant-a", "alert-1")]
    assert uow.commits == 1
    # created event emitted once
    assert [e.event_type for e in inv.pending_events].count("investigation_created") == 1


async def test_start_returns_existing_active_investigation() -> None:
    uow = FakeUnitOfWork()
    hisiem = FakeHisiem()
    handler = _handler(uow, hisiem)

    first = await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_id="alert-1",
            initiated_by_subject="analyst@corp",
        )
    )
    # second start on the same alert + tenant reuses it (no new hydration, no new row)
    second = await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_id="alert-1",
            initiated_by_subject="analyst@corp",
        )
    )
    assert first.id == second.id
    assert len(uow.investigations.added) == 1
    # one authoritative hydration only for the first start
    assert hisiem.calls == [("tenant-a", "alert-1")]


async def test_start_with_unknown_alert_raises_not_found() -> None:
    handler = _handler(hisiem=FakeHisiem(alerts={}))
    with pytest.raises(NotFoundError):
        await handler.start_alert_investigation(
            StartAlertInvestigation(
                tenant_id="tenant-a",
                source_alert_id="alert-missing",
                initiated_by_subject="analyst@corp",
            )
        )


async def test_cancel_running_investigation() -> None:
    uow = FakeUnitOfWork()
    handler = _handler(uow)
    inv = await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_id="alert-1",
            initiated_by_subject="analyst@corp",
        )
    )
    inv.start(actor=inv.initiated_by)
    await uow.investigations.update(inv)

    await handler.cancel_investigation(
        CancelInvestigation(
            tenant_id="tenant-a",
            investigation_id=inv.id,
            initiated_by_subject="analyst@corp",
        )
    )
    reloaded = await uow.investigations.get(tenant_id="tenant-a", investigation_id=inv.id)
    assert reloaded is not None
    assert reloaded.status == InvestigationStatus.CANCELLED
    assert reloaded.termination_reason == TerminationReason.CANCELLED_BY_USER
    assert reloaded.cancelled_at is not None


async def test_cancel_missing_investigation_raises_not_found() -> None:
    handler = _handler()
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
    uow = FakeUnitOfWork()
    handler = _handler(uow)
    await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-a",
            source_alert_id="alert-1",
            initiated_by_subject="a@corp",
        )
    )
    # same alert, different tenant → must create a new investigation
    inv_b = await handler.start_alert_investigation(
        StartAlertInvestigation(
            tenant_id="tenant-b",
            source_alert_id="alert-1",
            initiated_by_subject="b@corp",
        )
    )
    assert inv_b.tenant_id == "tenant-b"
    assert len(uow.investigations.added) == 2
