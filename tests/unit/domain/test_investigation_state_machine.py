"""Unit tests for the Investigation state machine (investigation-analysis only).

No database. Pure aggregate-method assertions on legal/illegal transitions,
terminal-state immutability, the post-investigation response association, and the
per-status operation constraints of domain-model.md §36/§37.

The lifecycle modeled here is ONLY the investigation ANALYSIS lifecycle
(``CREATED → RUNNING → COMPLETED | FAILED | CANCELLED``). The response workflow
(proposal → approval → SOAR execution) is an INDEPENDENT post-completion aggregate
lifecycle, so an Investigation never enters an approval/execution status and none
of approval/rejection/execution changes ``Investigation.status`` (spec §5).
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
from hisiem_soc_copilot.domain.shared.errors import DomainError, StateTransitionError


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


def _completed() -> Investigation:
    inv = _investigation()
    inv.start(actor=inv.initiated_by)
    inv.complete_without_response()
    return inv


# ---------------------------------------------------------------------------
# the investigation-analysis lifecycle
# ---------------------------------------------------------------------------


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


def test_the_status_set_is_the_investigation_analysis_lifecycle_only() -> None:
    assert {s.value for s in InvestigationStatus} == {
        "CREATED",
        "RUNNING",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
    }
    # No approval/execution status exists on the Investigation any more.
    assert not hasattr(InvestigationStatus, "WAITING_APPROVAL")
    assert not hasattr(InvestigationStatus, "EXECUTING_RESPONSE")


@pytest.mark.parametrize(
    "method",
    [
        "request_response_approval",
        "approve_response",
        "reject_response",
        "observe_terminal_execution",
    ],
)
def test_the_dead_response_transition_methods_are_gone(method: str) -> None:
    """The response workflow must not be expressible as an Investigation transition."""
    assert not hasattr(Investigation, method)


@pytest.mark.parametrize(
    ("setup", "command", "must_fail"),
    [
        ("fresh", "start", False),
        ("fresh", "cancel", False),
        ("fresh", "fail", False),
        ("fresh", "continue", True),
        ("fresh", "finalize_without_response", True),
        ("running", "start", True),
        ("running", "continue", False),
        ("running", "finalize_without_response", False),
        ("running", "cancel", False),
        ("running", "fail", False),
        ("completed", "start", True),
        ("completed", "cancel", True),
        ("completed", "continue", True),
        ("completed", "finalize_without_response", True),
        ("cancelled", "start", True),
        ("cancelled", "cancel", True),
        ("failed", "start", True),
        ("failed", "finalize_without_response", True),
    ],
)
def test_transition_table(setup: str, command: str, must_fail: bool) -> None:
    inv = _investigation()
    actor = inv.initiated_by
    if setup != "fresh":
        inv.start(actor=actor)  # CREATED -> RUNNING
    if setup == "completed":
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
        elif command == "cancel":
            inv.cancel()
        elif command == "fail":
            inv.fail()
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


def test_active_status_set_matches_the_lifecycle() -> None:
    assert InvestigationStatus.CREATED.is_active
    assert InvestigationStatus.RUNNING.is_active
    for terminal in (
        InvestigationStatus.COMPLETED,
        InvestigationStatus.FAILED,
        InvestigationStatus.CANCELLED,
    ):
        assert terminal.is_terminal
        assert not terminal.is_active


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


# ---------------------------------------------------------------------------
# the post-investigation response association (domain-model.md §37)
# ---------------------------------------------------------------------------


def test_link_response_proposal_establishes_the_association_once() -> None:
    inv = _completed()
    proposal_id = uuid4()
    assert inv.response_proposal_id is None

    assert inv.link_response_proposal(proposal_id) is True
    assert inv.response_proposal_id == proposal_id


def test_link_response_proposal_is_idempotent_for_the_same_proposal() -> None:
    inv = _completed()
    proposal_id = uuid4()
    inv.link_response_proposal(proposal_id)
    revision = inv.revision

    assert inv.link_response_proposal(proposal_id) is False
    assert inv.revision == revision  # an identical replay is not a state change


def test_link_response_proposal_conflicts_on_a_different_proposal() -> None:
    inv = _completed()
    first = uuid4()
    inv.link_response_proposal(first)

    with pytest.raises(DomainError):
        inv.link_response_proposal(uuid4())
    assert inv.response_proposal_id == first  # never a silent overwrite


@pytest.mark.parametrize(
    "setup",
    ["created", "running", "failed", "cancelled"],
)
def test_link_response_proposal_requires_a_completed_investigation(setup: str) -> None:
    inv = _investigation()
    if setup != "created":
        inv.start(actor=inv.initiated_by)
    if setup == "failed":
        inv.fail()
    elif setup == "cancelled":
        inv.cancel()

    with pytest.raises(StateTransitionError):
        inv.link_response_proposal(uuid4())
    assert inv.response_proposal_id is None


def test_link_response_proposal_never_perturbs_the_terminal_lifecycle() -> None:
    inv = _completed()
    before = (inv.status, inv.finished_at, inv.termination_reason, inv.cancelled_at)

    inv.link_response_proposal(uuid4())

    assert (inv.status, inv.finished_at, inv.termination_reason, inv.cancelled_at) == (
        before
    )
    assert inv.status == InvestigationStatus.COMPLETED


def test_link_response_proposal_emits_no_lifecycle_event() -> None:
    inv = _completed()
    inv.clear_events()

    inv.link_response_proposal(uuid4())

    # The association is a field-level post-terminal association, not a status
    # transition, so it never fabricates a lifecycle event.
    assert inv.pending_events == []


def test_response_proposal_id_survives_a_failed_link_attempt() -> None:
    """A rejected link must leave the aggregate exactly as it was."""
    inv = _completed()
    established = uuid4()
    inv.link_response_proposal(established)

    with pytest.raises(DomainError):
        inv.link_response_proposal(uuid4())

    assert inv.response_proposal_id == established
