"""GP-01 Dataset Materializer — turns the committed logical scenario into REAL
HISIEM resources and resolves their provider identities (E1-B.3).

The materializer is the only stage that may inject into the real SSH TCP input and
resolve events/alerts through HISIEM. It drives an explicit state machine
(NEW → PREFLIGHTED → EVENTS_RENDERED → EVENTS_INJECTED → EVENTS_RESOLVED →
ALERT_RESOLVED → VERIFIED → MATERIALIZED, plus FAILED/INDETERMINATE) over a
persisted mutable draft ledger. It NEVER runs the Copilot investigation, decides
the verdict, scores the result, or seals the manifest — those are separate stages.

Authority boundaries (E1-B.3 §2): injection is ONLY via the SSH TCP syslog socket;
resolution is ONLY via HISIEM's control API. No direct Elasticsearch/Kafka/
siem-alerts writes, no Copilot DB writes, no locally-derived provider identity.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from .contracts import (
    GP01_LOGICAL_DATASET,
    GP01_RULE_CONDITION,
    GP01_RULE_ID,
    GP01_RULE_KEY_FIELD,
    GP01_RULE_THRESHOLD,
    GP01_RULE_WINDOW_MINUTES,
    GP01_SEMANTIC_ROLES,
    DatasetInvariantViolation,
    EventInjectionError,
    EventTimePlan,
    InjectionOutcomeIndeterminate,
    LogicalEvent,
    MaterializationDraft,
    MaterializationState,
    PreflightError,
    RenderedEvent,
    RuleContractMismatch,
    RunIdentity,
    ScenarioSpec,
    VerifiedDataset,
)
from .hisiem_reader import HisiemEvaluationReader, RuleContract
from .identity import derive_event_process_id, derive_run_identity
from .injector import WRITE_STATUS_CONNECTION_ERROR, WRITE_STATUS_INDETERMINATE, EventInjector
from .time_plan import EventPlanCrossesYearBoundary, build_event_time_plan

_FIVE_MIN = timedelta(minutes=5)
_YEAR_BOUNDARY_CODE = "EVENT_PLAN_CROSSES_YEAR_BOUNDARY"

# Event-time isolation for resolution. F1..F5 are spaced 10 s apart (and S1 20 s
# after F5) and the parser sets @timestamp to the rendered wall-clock second, so a
# tight ±3 s window around a role's committed instant isolates that role from its
# neighbours without risking cross-role ambiguity (E1-B.3 §13: >1 → AMBIGUOUS).
_RESOLVE_WINDOW_SECONDS = 3


class PreflightProbe(Protocol):
    """Reachability probe for preflight (E1-B.3 §10.1)."""

    async def ping(self) -> bool: ...


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _utc_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class Gp01Materializer:
    """Drives one GP-01 run through the materialization state machine.

    Owns a mutable :class:`MaterializationDraft` ledger. The caller passes an
    already-built identity/time plan + preflight rule contract (pure), and the
    injector + reader (IO). A resume reuses the persisted draft and NEVER
    re-injects an already-attempted event.
    """

    def __init__(
        self,
        *,
        run_id: str,
        tenant_id: str,
        scenario: ScenarioSpec,
        identity: RunIdentity,
        time_plan: EventTimePlan,
        injector: EventInjector,
        reader: HisiemEvaluationReader,
        draft: MaterializationDraft | None = None,
    ) -> None:
        if run_id == "" or tenant_id == "":
            raise ValueError("run_id and tenant_id must be non-empty")
        self._run_id = run_id
        self._tenant_id = tenant_id
        self._scenario = scenario
        self._identity = identity
        self._time_plan = time_plan
        self._injector = injector
        self._reader = reader
        self._draft = draft or MaterializationDraft(
            run_id=run_id,
            scenario_id=scenario.id,
            tenant_id=tenant_id,
            created_at=_iso_now(),
            updated_at=_iso_now(),
            bound=True,
            identity=identity,
            time_plan=time_plan,
        )

    @property
    def draft(self) -> MaterializationDraft:
        return self._draft

    # -- preflight ---------------------------------------------------------

    async def preflight(
        self,
        *,
        rule: RuleContract | None,
        reachable: bool = True,
    ) -> None:
        """Validate the environment + rule contract BEFORE any write occurs."""
        if not reachable:
            raise PreflightError("HISIEM control surface is not reachable")
        if rule is None:
            raise PreflightError(f"detection rule {GP01_RULE_ID} was not found")
        if not rule.enabled:
            raise RuleContractMismatch(f"detection rule {rule.rule_id} is not enabled")
        if rule.rule_id != GP01_RULE_ID:
            raise RuleContractMismatch(f"rule id {rule.rule_id!r} != expected {GP01_RULE_ID}")
        if rule.threshold != GP01_RULE_THRESHOLD:
            raise RuleContractMismatch(
                f"rule threshold {rule.threshold} != expected {GP01_RULE_THRESHOLD}"
            )
        if rule.window_minutes != GP01_RULE_WINDOW_MINUTES:
            raise RuleContractMismatch(
                f"rule windowMinutes {rule.window_minutes} != expected {GP01_RULE_WINDOW_MINUTES}"
            )
        if rule.key_field != GP01_RULE_KEY_FIELD:
            raise RuleContractMismatch(
                f"rule keyField {rule.key_field!r} != expected {GP01_RULE_KEY_FIELD}"
            )
        if rule.condition_action != GP01_RULE_CONDITION:
            raise RuleContractMismatch(
                f"rule condition {rule.condition_action!r} != expected {GP01_RULE_CONDITION}"
            )
        # Year boundary is checked by the pure time-plan builder at bind time; a
        # plan that crossed a year could never have been built, so this is a
        # defensive confirmation the plan is bound in the past (E1-B.3 §6).
        try:
            build_event_time_plan(now=_now_utc())  # may raise EventPlanCrossesYearBoundary
        except EventPlanCrossesYearBoundary:
            raise PreflightError(_YEAR_BOUNDARY_CODE) from None
        # Run collision: GP-01 only ever resolves one alert per attack source; the
        # reader's wait_for_alert raises AmbiguousSourceAlertError if two runs
        # collide on the same source/rule. Identity is run-scoped (E1-B.3 §5).
        self._set_state(MaterializationState.PREFLIGHTED)

    # -- render + inject ---------------------------------------------------

    def _logical(self) -> list[LogicalEvent]:
        return list(GP01_LOGICAL_DATASET)

    def render_events(self) -> list[RenderedEvent]:
        """Bind every logical event to a rendered syslog line (E1-B.3 §11)."""
        if self._draft.rendered:
            return list(self._draft.rendered)  # resume: never re-render
        rendered: list[RenderedEvent] = []
        for logical in self._logical():
            wall = self._time_plan.wall_clock[logical.role]
            if logical.classification == "WATERMARK_CONTROL":
                from .syslog import render_event

                rendered.append(
                    render_event(
                        role=logical.role,
                        action=logical.action,
                        outcome=logical.outcome,
                        host_name=self._identity.host_name,
                        source_ip=self._identity.watermark_source_ip,
                        user_name=self._watermark_user_name(),
                        wall_clock=wall,
                        process_id=derive_event_process_id(self._run_id, logical.role),
                    )
                )
                continue
            from .syslog import render_event

            rendered.append(
                render_event(
                    role=logical.role,
                    action=logical.action,
                    outcome=logical.outcome,
                    host_name=self._identity.host_name,
                    source_ip=self._identity.attack_source_ip,
                    user_name=self._identity.user_name,
                    wall_clock=wall,
                    process_id=derive_event_process_id(self._run_id, logical.role),
                )
            )
        self._draft.rendered = rendered
        self._set_state(MaterializationState.EVENTS_RENDERED)
        return rendered

    def _watermark_user_name(self) -> str:
        from .identity import derive_watermark_user_name

        return derive_watermark_user_name(self._run_id)

    async def inject_events(self) -> None:
        """Inject in the FIXED order F1..F5,S1,W1 (E1-B.3 §11) — the caller may
        not reorder. Never re-injects an already-attempted role on resume."""
        rendered = self.render_events()
        attempted = {attempt.logical_role for attempt in self._draft.injected}
        for event in rendered:
            if event.role in attempted:
                continue  # resume: reconcile, never blind re-inject (§12)
            attempt = await self._injector.inject(event)
            self._draft.injected.append(attempt)
            if attempt.write_status == WRITE_STATUS_INDETERMINATE:
                # §12: the server-side outcome cannot be proven. The run MUST stop
                # in INDETERMINATE and MUST NOT auto-resend this event.
                self._draft.state = MaterializationState.INDETERMINATE.value
                raise InjectionOutcomeIndeterminate(
                    f"injection of {event.role} is INDETERMINATE (server-side "
                    "acceptance not provable); refusing to re-send"
                )
            if attempt.write_status == WRITE_STATUS_CONNECTION_ERROR:
                # Nothing reached the wire — this is a bounded, provably-not-ambiguous
                # failure the caller may choose to abort on.
                self._draft.state = MaterializationState.FAILED.value
                raise EventInjectionError(
                    f"injection of {event.role} failed to connect: {attempt.socket_target}"
                )
        self._set_state(MaterializationState.EVENTS_INJECTED)

    # -- resolution --------------------------------------------------------

    async def resolve_events(self, *, deadline: datetime, interval: float = 2.0) -> None:
        """Resolve F1..F5, S1, W1 through HISIEM log-search (E1-B.3 §13)."""
        for logical in self._logical():
            if logical.role in self._draft.resolved_events:
                continue
            conditions = self._event_conditions(logical)
            # A tight window around the role's committed wall-clock instant keeps
            # each resolved event unambiguous (the parser stamps @timestamp to the
            # rendered second; F1..F5 are only 10 s apart).
            instant = self._time_plan.wall_clock[logical.role]
            from_ = instant - timedelta(seconds=_RESOLVE_WINDOW_SECONDS)
            to = instant + timedelta(seconds=_RESOLVE_WINDOW_SECONDS)
            resolved = await self._reader.wait_for_event(
                logical_role=logical.role,
                from_=from_,
                to=to,
                conditions=conditions,
                deadline=deadline,
                interval=interval,
            )
            self._draft.resolved_events[logical.role] = resolved
        self._set_state(MaterializationState.EVENTS_RESOLVED)

    def _event_conditions(self, logical: LogicalEvent) -> list[dict[str, object]]:
        """Build the log-search conditions that uniquely identify this logical role."""
        base: list[dict[str, object]] = [
            {"field": "event.action", "operator": "is", "value": logical.action},
            {"field": "event.category", "operator": "is", "value": "authentication"},
            {"field": "host.name", "operator": "is", "value": self._identity.host_name},
        ]
        if logical.classification == "WATERMARK_CONTROL":
            source = self._identity.watermark_source_ip
            user = self._watermark_user_name()
        else:
            source = self._identity.attack_source_ip
            user = self._identity.user_name
        base.append({"field": "source.ip", "operator": "is", "value": source})
        base.append({"field": "user.name", "operator": "is", "value": user})
        return base

    async def resolve_alert(self, *, deadline: datetime, interval: float = 2.0) -> None:
        """Resolve the real brute-force alert for this run (E1-B.3 §14)."""
        if self._draft.resolved_alert is not None:
            return
        resolved = await self._reader.wait_for_alert(
            attack_source_ip=self._identity.attack_source_ip,
            deadline=deadline,
            interval=interval,
        )
        self._draft.resolved_alert = resolved
        self._set_state(MaterializationState.ALERT_RESOLVED)

    def verify(self) -> VerifiedDataset:
        """Produce the immutable VerifiedDataset only when every invariant holds
        (E1-B.3 §16). Raises DatasetInvariantViolation otherwise."""
        if self._draft.resolved_alert is None:
            raise DatasetInvariantViolation("cannot verify before the source alert is resolved")
        events = dict(self._draft.resolved_events)
        missing = [role for role in GP01_SEMANTIC_ROLES if role not in events]
        if missing:
            raise DatasetInvariantViolation(f"cannot verify with unresolved events: {missing}")
        from .verifier import DatasetVerifier

        dataset = DatasetVerifier(
            scenario=self._scenario,
            run=self._identity,
            tenant_id=self._tenant_id,
            time_plan=self._time_plan,
        ).verify(
            resolved_events=events,
            source_alert=self._draft.resolved_alert,
            materialized_at=_iso_now(),
        )
        self._draft.state = MaterializationState.VERIFIED.value
        return dataset

    # -- state helpers -----------------------------------------------------

    def _set_state(self, state: MaterializationState) -> None:
        self._draft.state = state.value
        self._draft.updated_at = _iso_now()

    def mark_materialized(self) -> None:
        self._set_state(MaterializationState.MATERIALIZED)


async def materialize(
    *,
    tenant_id: str,
    run_id: str | None = None,
    reader: HisiemEvaluationReader,
    injector: EventInjector,
    deadline_seconds: int = 120,
    interval: float = 2.0,
) -> tuple[MaterializationDraft, VerifiedDataset | None]:
    """Top-level convenience: bind identity + time plan, preflight, render,
    inject, resolve events + alert, and verify — producing the VerifiedDataset
    when the run completes (E1-B.3 §16). Pure helpers build identity/time; IO is
    injected by the caller. Never seals a manifest (E1-B.4 is a later stage)."""
    run_id = run_id or uuid4().hex
    scenario = _canonical_scenario()
    identity = derive_run_identity(run_id)
    now = _now_utc()
    plan = build_event_time_plan(now=now)

    materializer = Gp01Materializer(
        run_id=run_id,
        tenant_id=tenant_id,
        scenario=scenario,
        identity=identity,
        time_plan=plan,
        injector=injector,
        reader=reader,
    )
    rule = await reader.get_rule_contract(GP01_RULE_ID)
    reachable = await reader.ping()
    await materializer.preflight(rule=rule, reachable=reachable)
    materializer.render_events()
    deadline = now + timedelta(seconds=deadline_seconds)
    await materializer.inject_events()
    await materializer.resolve_events(deadline=deadline, interval=interval)
    await materializer.resolve_alert(deadline=deadline, interval=interval)
    dataset = materializer.verify()
    materializer.mark_materialized()
    return materializer.draft, dataset


def _canonical_scenario() -> ScenarioSpec:
    from .contracts import ScenarioSpec

    return ScenarioSpec()


__all__ = [
    "Gp01Materializer",
    "materialize",
]
