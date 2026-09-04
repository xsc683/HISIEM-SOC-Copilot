"""Unit tests for the Investigation state machine.

No database. Pure aggregate-method assertions on legal/illegal transitions,
terminal-state immutability, and the per-status operation constraints of
domain-model.md §36/§37.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.enums import (
    InvestigationPhase,
    InvestigationStatus,
)
from hisiem_soc_copilot.domain.investigation.errors import ActiveInvestigationExistsError
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from hisiem_soc_copilot.domain.shared.errors import StateTransitionError


def _investigation(**overrides) -> Investigation:
    values = dict(
        id=uuid4(),
        tenant_id="tenant-a",
        source_alert_ref=ExternalResourceRef(
            provider="hisiem", resource_type="alert", address_id="alert-1"
        ),
        initiated_by=ActorRef(subject_id="analyst", tenant_id="tenant-a"),
        budget_limits=BudgetLimits(),
    )
    values.update(overrides)
    return Investigation.create(**values)


def test_created_can_start() -> None:
    inv = _investigation()
    inv.start(actor=inv.initiated_by)
    assert inv.status == InvestigationStatus.RUNNING
    assert inv.phase == InvestigationPhase.HYDRATING
    assert inv.started_at is not None
    assert any(e.event_type == "investigation_started" for e in inv.pending_events)


def test_created_can_cancel() -> None:
    inv = _investigation()
    inv.cancel()
    assert inv.status == InvestigationStatus.CANCELLED
    assert inv.termination_reason is not None


def test_running_finalize_without_response() -> None:
    inv = _investigation()
    inv.start(actor=inv.initiated_by)
    inv.complete_without_response()
    assert inv.status == InvestigationStatus.COMPLETED
    assert inv.finished_at is not None


def test_running_request_approval_then_reject() -> None:
    inv = _investigation()
    inv.start(actor=inv.initiated_by)
    inv.request_response_approval()
    assert inv.status == InvestigationStatus.WAITING_APPROVAL
    inv.reject_response()
    assert inv.status == InvestigationStatus.COMPLETED


def test_approve_then_observe_terminal_execution() -> None:
    inv = _investigation()
    inv.start(actor=inv.initiated_by)
    inv.request_response_approval()
    inv.approve_response()
    assert inv.status == InvestigationStatus.EXECUTING_RESPONSE
    inv.observe_terminal_execution()
    assert inv.status == InvestigationStatus.COMPLETED


@pytest.mark.parametrize(
    ("setup", "command", "must_fail"),
    [
        ("fresh", "start", False),
        ("fresh", "cancel", False),
        ("fresh", "finalize_without_response", True),
        ("fresh", "request_response_approval", True),
        ("running", "start", True),
        ("running", "continue", False),
        ("running", "finalize_without_response", False),
        ("running", "cancel", False),
        ("waiting", "approve_response", False),
        ("waiting", "reject_response", False),
        ("waiting", "cancel", False),
        ("waiting", "continue", True),
        ("executing", "observe_terminal_execution", False),
        ("executing", "cancel", True),
        ("executing", "reject_response", True),
        ("completed", "cancel", True),
        ("completed", "continue", True),
        ("completed", "observe_terminal_execution", True),
        ("cancelled", "start", True),
        ("failed", "start", True),
    ],
)
def test_transition_table(setup: str, command: str, must_fail: bool) -> None:
    inv = _investigation()
    actor = inv.initiated_by
    # Bring the aggregate into the setup status.
    if setup != "fresh":
        inv.start(actor=actor)  # CREATED -> RUNNING
    if setup == "waiting":
        inv.request_response_approval()
    elif setup == "executing":
        inv.request_response_approval()
        inv.approve_response()
    elif setup == "completed":
        inv.complete_without_response()
    elif setup == "cancelled":
        inv2 = _investigation()
        inv2.cancel()
        inv = inv2
    elif setup == "failed":
        inv.fail()

    try:
        if command == "start":
            inv.start(actor=actor)
        elif command == "continue":
            inv.update_phase(InvestigationPhase.INVESTIGATING)
        elif command == "finalize_without_response":
            inv.complete_without_response()
        elif command == "request_response_approval":
            inv.request_response_approval()
        elif command == "approve_response":
            inv.approve_response()
        elif command == "reject_response":
            inv.reject_response()
        elif command == "observe_terminal_execution":
            inv.observe_terminal_execution()
        elif command == "cancel":
            inv.cancel()
        raised = False
    except StateTransitionError:
        raised = True

    assert raised == must_fail, (
        f"setup={setup} command={command}: expected must_fail={must_fail}, got raised={raised}"
    )


def test_terminal_status_is_immutable() -> None:
    inv = _investigation()
    inv.cancel()
    with pytest.raises(StateTransitionError):
        inv.start(actor=inv.initiated_by)


def test_active_investigation_existence_error_metadata() -> None:
    err = ActiveInvestigationExistsError(alert_ref="hisiem:alert:alert-1")
    assert err.code == "ACTIVE_INVESTIGATION_EXISTS"
    assert err.details["source_alert_ref"] == "hisiem:alert:alert-1"


def test_events_accumulate_and_clear() -> None:
    inv = _investigation()
    inv.start(actor=inv.initiated_by)
    inv.update_phase(InvestigationPhase.INVESTIGATING)
    types = {e.event_type for e in inv.pending_events}
    expected = {
        "investigation_created",
        "investigation_started",
        "investigation_phase_changed",
    }
    assert expected <= types
    inv.clear_events()
    assert inv.pending_events == []
