"""Investigation Aggregate Root and state machine.

The aggregate owns the lifecycle: legal status transitions, terminal-state
immutability, and emitting domain events. Nothing outside the aggregate mutates
status/phase directly (see python-package-boundary.md §4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from ..shared.errors import DomainError, StateTransitionError
from ..shared.identifiers import utc_now
from . import events as ev
from .enums import (
    InvestigationPhase,
    InvestigationStatus,
    TerminationReason,
)
from .value_objects import ActorRef, BudgetLimits, ExternalResourceRef

# Legal transitions per domain-model.md §36 / v1-user-flow-and-scope.md §14.
_TRANSITIONS: dict[InvestigationStatus, dict[str, InvestigationStatus]] = {
    InvestigationStatus.CREATED: {
        "start": InvestigationStatus.RUNNING,
        "cancel": InvestigationStatus.CANCELLED,
        "fail": InvestigationStatus.FAILED,
    },
    InvestigationStatus.RUNNING: {
        "continue": InvestigationStatus.RUNNING,
        "finalize_without_response": InvestigationStatus.COMPLETED,
        "cancel": InvestigationStatus.CANCELLED,
        "fail": InvestigationStatus.FAILED,
    },
}
# NOTE: there is deliberately NO approval/execution status here. The response
# workflow is an independent post-completion aggregate lifecycle (see
# ``InvestigationStatus``); an Investigation never transitions because a proposal
# was approved, rejected, or executed.


@dataclass
class Investigation:
    """Aggregate root for one alert-driven investigation.

    Invariants (domain-model.md §4):
    - belongs to exactly one tenant, originates from exactly one alert (both immutable)
    - terminal status never transitions
    - every lifecycle change goes through aggregate methods below
    """

    id: UUID
    tenant_id: str
    source_alert_ref: ExternalResourceRef
    initiated_by: ActorRef
    status: InvestigationStatus = InvestigationStatus.CREATED
    phase: InvestigationPhase | None = None
    current_plan_revision: int = 0
    budget_limits: BudgetLimits = field(default_factory=BudgetLimits)
    termination_reason: TerminationReason | None = None
    lock_version: int = 0
    result_id: UUID | None = None
    response_proposal_id: UUID | None = None
    revision: int = 0
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancelled_at: datetime | None = None
    _pending_events: list[ev.InvestigationEvent] = field(
        default_factory=list, init=False
    )

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        id: UUID,
        tenant_id: str,
        source_alert_ref: ExternalResourceRef,
        initiated_by: ActorRef,
        budget_limits: BudgetLimits,
        now: datetime | None = None,
    ) -> Investigation:
        investigation = cls(
            id=id,
            tenant_id=tenant_id,
            source_alert_ref=source_alert_ref,
            initiated_by=initiated_by,
            budget_limits=budget_limits,
            created_at=now or utc_now(),
        )
        investigation._pending_events.append(
            ev.investigation_created(
                id,
                tenant_id=tenant_id,
                actor_subject_id=initiated_by.subject_id,
            )
        )
        return investigation

    # ------------------------------------------------------------------
    # domain API
    # ------------------------------------------------------------------
    def start(self, *, actor: ActorRef, now: datetime | None = None) -> None:
        if self.status != InvestigationStatus.CREATED:
            self._raise_invalid("start")
        self.status = InvestigationStatus.RUNNING
        self.phase = InvestigationPhase.HYDRATING
        self.started_at = now or utc_now()
        self._bump()
        self._pending_events.append(
            ev.investigation_started(
                self.id, tenant_id=self.tenant_id, actor_subject_id=actor.subject_id
            )
        )

    def update_phase(self, phase: InvestigationPhase) -> None:
        self._require_status(InvestigationStatus.RUNNING, "update_phase")
        self.phase = phase
        self._pending_events.append(
            ev.investigation_phase_changed(
                self.id, phase, tenant_id=self.tenant_id
            )
        )

    def complete_without_response(self) -> None:
        self._transition(
            "finalize_without_response", TerminationReason.COMPLETED_WITHOUT_RESPONSE
        )

    def link_response_proposal(self, proposal_id: UUID) -> bool:
        """Associate a post-investigation response proposal (one-time, immutable).

        Lifecycle-vs-association rule (domain-model.md §37): a terminal
        Investigation's LIFECYCLE STATE is immutable — it never reopens, and
        ``status`` / ``finished_at`` / ``termination_reason`` are never touched
        here. Establishing the single post-investigation response association is
        explicitly allowed, once, and is the ONLY post-terminal write.

        Returns ``True`` when the association was newly established and ``False``
        when it was already identical (idempotent replay). A DIFFERENT proposal id
        is a deterministic conflict — never a silent overwrite.
        """
        if self.status is not InvestigationStatus.COMPLETED:
            raise StateTransitionError(
                aggregate_type="investigation",
                current_status=self.status.value,
                command="link_response_proposal",
            )
        if self.response_proposal_id is None:
            self.response_proposal_id = proposal_id
            self._bump()
            return True
        if self.response_proposal_id == proposal_id:
            return False
        raise DomainError(
            "investigation already has a different response proposal associated"
        )

    def cancel(self, *, actor: ActorRef | None = None) -> None:
        """Cancel while in CREATED/RUNNING."""
        self._transition(
            "cancel",
            TerminationReason.CANCELLED_BY_USER,
            actor_subject_id=actor.subject_id if actor else None,
        )

    def fail(self, *, reason: TerminationReason = TerminationReason.FAILED_FATAL) -> None:
        self._transition("fail", reason)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _transition(
        self,
        command: str,
        reason: TerminationReason | None,
        *,
        actor_subject_id: str | None = None,
    ) -> None:
        allowed = _TRANSITIONS.get(self.status, {})
        if command not in allowed:
            raise StateTransitionError(
                aggregate_type="investigation",
                current_status=self.status.value,
                command=command,
            )
        self.status = allowed[command]
        now = utc_now()
        if command == "cancel":
            self.cancelled_at = now
        if self.status in (
            InvestigationStatus.COMPLETED,
            InvestigationStatus.FAILED,
            InvestigationStatus.CANCELLED,
        ):
            self.finished_at = now
        self.termination_reason = reason
        self._bump()
        self._pending_events.append(
            ev.investigation_terminated(
                self.id,
                self.status,
                reason.value if reason else self.status.value,
                tenant_id=self.tenant_id,
                actor_subject_id=actor_subject_id,
            )
        )

    def _require_status(self, status: InvestigationStatus, command: str) -> None:
        if self.status != status:
            raise StateTransitionError(
                aggregate_type="investigation",
                current_status=self.status.value,
                command=command,
            )

    def _raise_invalid(self, command: str) -> None:
        raise StateTransitionError(
            aggregate_type="investigation",
            current_status=self.status.value,
            command=command,
        )

    def _bump(self) -> None:
        # ``revision`` is the domain change counter emitted with events. ``lock_version``
        # is the persistence CAS counter: it is managed by the repository update
        # (WHERE old → SET old+1) and never mutated inside domain methods.
        self.revision += 1

    def clear_events(self) -> None:
        self._pending_events = []

    @property
    def pending_events(self) -> list[ev.InvestigationEvent]:
        return list(self._pending_events)
