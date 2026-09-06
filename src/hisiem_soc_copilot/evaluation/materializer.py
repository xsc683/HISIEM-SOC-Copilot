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
    EVENT_PLAN_CROSSES_YEAR_BOUNDARY,
    GP01_FAILURE_ROLES,
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
    InvalidMaterializationTransition,
    LogicalEvent,
    MaterializationDraft,
    MaterializationState,
    PreflightError,
    RenderedEvent,
    RuleContractMismatch,
    RunIdentity,
    RunIdentityCollision,
    ScenarioSpec,
    VerifiedDataset,
)
from .hisiem_reader import HisiemEvaluationReader, RuleContract
from .identity import derive_event_process_id, derive_run_identity
from .injector import WRITE_STATUS_CONNECTION_ERROR, WRITE_STATUS_INDETERMINATE, EventInjector
from .time_plan import W1_MAX_FUTURE_SKEW_SECONDS, build_event_time_plan

# Event-time isolation for resolution. F1..F5 are spaced 10 s apart (and S1 20 s
# after F5) and the parser sets @timestamp to the rendered wall-clock second, so a
# tight ±3 s window around a role's committed instant isolates that role from its
# neighbours without risking cross-role ambiguity (E1-B.3 §13: >1 → AMBIGUOUS).
_RESOLVE_WINDOW_SECONDS = 3


class PreflightProbe(Protocol):
    """Reachability probe for preflight (E1-B.3 §10.1)."""

    async def readiness(self) -> None: ...

    async def ping(self) -> bool: ...


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _utc_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _require_state(current: str, *allowed: MaterializationState) -> None:
    """Transition gate (E1-B.3 §9): raise ``InvalidMaterializationTransition``
    unless the draft's current state string is one of the legal-from states."""
    if current not in {state.value for state in allowed}:
        expected = ", ".join(state.value for state in allowed)
        raise InvalidMaterializationTransition(
            f"illegal materialization transition from state {current!r}; "
            f"expected one of: {expected}"
        )


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
        _require_state(self._draft.state, MaterializationState.NEW)
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
        # E1-B.3 §10.5: validate the plan this run is ACTUALLY bound to (never
        # build a second, unrelated plan).
        self._validate_time_plan()
        # E1-B.3 §10.4: an already-materialized run with this exact identity is a
        # reconciliation/resume (allowed); a DIFFERENT run on the same identity is
        # a typed collision. Both checks run before any state transition/write.
        await self._check_run_collision()
        # Freeze the processing-time freshness lower bound ONCE for a fresh run
        # (empty bound = not yet frozen). A resume keeps the persisted value; it is
        # never re-derived from now(), so stale-alert protection does not drift.
        self._freeze_alert_processing_not_before()
        self._set_state(MaterializationState.PREFLIGHTED)

    def _freeze_alert_processing_not_before(self) -> None:
        """Freeze ``alert_processing_not_before`` the first time preflight runs on
        a fresh draft. Alert ``created_at`` (processing-time) is only ever compared
        against this frozen run boundary — never the F1/W1 event-time window. A
        resume reuses the persisted value; never re-derives now()."""
        if self._draft.alert_processing_not_before:
            return  # already frozen on a prior live run / persisted draft
        self._draft.alert_processing_not_before = _iso_now()

    def _validate_time_plan(self) -> None:
        """E1-B.3 §10.5: validate the bound plan's past/detection-window/year
        invariants directly — no plan regeneration."""
        if self._time_plan is None:
            raise PreflightError(
                "no bound time plan; cannot validate a run that was not time-planned"
            )
        wall = self._time_plan.wall_clock
        events = self._time_plan.events
        missing = [role for role in GP01_LOGICAL_DATASET if role.role not in wall]
        if missing:
            raise PreflightError(
                f"bound time plan is missing wall-clock entries for: "
                f"{[logical.role for logical in missing]}"
            )
        now = self._time_plan.built_at
        # Role-aware pastness (E1-B.3 §6 / watermark-aligned): ground-truth F1..S1
        # must be strictly in the past; W1 is the WATERMARK-CONTROL window-advance
        # event and is EXPECTED to be (boundedly) in the future. Only W1 may be
        # future. The events dict is UTC; wall is Asia/Shanghai local.
        semantic_roles = [
            logical.role
            for logical in GP01_LOGICAL_DATASET
            if logical.classification != "WATERMARK_CONTROL"
        ]
        for role in semantic_roles:
            if events[role] >= now:
                raise PreflightError(
                    f"bound time plan has ground-truth role {role} at {events[role].isoformat()} "
                    "not strictly in the past when injection starts"
                )
        # The SSH syslog form carries no year; the parser auto-completes it, so all
        # roles must share ONE local wall-clock year (E1-B.3 §6, §10.5).
        roles = [
            logical.role
            for logical in GP01_LOGICAL_DATASET
            if logical.classification != "WATERMARK_CONTROL"
        ]
        years = {wall[role].year for role in roles}
        years.add(wall["W1"].year)  # W1 is the window-advance control; must agree too
        if len(years) != 1:
            raise PreflightError(
                "bound time plan crosses a natural calendar-year boundary",
                code=EVENT_PLAN_CROSSES_YEAR_BOUNDARY,
            )
        f1 = events["F1"]
        max_failure = max(events[role] for role in GP01_FAILURE_ROLES)
        if (max_failure - f1) > timedelta(minutes=GP01_RULE_WINDOW_MINUTES):
            raise PreflightError(
                "bound time plan spreads F1..F5 beyond the configured detection "
                f"window ({GP01_RULE_WINDOW_MINUTES} minutes)"
            )
        if events["S1"] <= max_failure:
            raise PreflightError("bound time plan has S1 not strictly after the last failure")
        # W1 must be FUTURE and boundedly skewed: it advances Flink's watermark
        # across the next detection-window boundary (E1-B.3 §6 / watermark-aligned).
        w1 = events["W1"]
        if w1 <= now:
            raise PreflightError(
                "bound time plan has W1 not in the future; the watermark-advance "
                "event must close the detection window"
            )
        skew = (w1 - now).total_seconds()
        if skew > W1_MAX_FUTURE_SKEW_SECONDS:
            raise PreflightError(
                "bound time plan has W1 future skew "
                f"({skew:.0f}s) beyond the bounded horizon "
                f"({W1_MAX_FUTURE_SKEW_SECONDS}s)"
            )

    async def _check_run_collision(self) -> None:
        """E1-B.3 §10.4: search HISIEM's bounded current-run scope for existing
        events matching this run's correlation identity (source.ip + user.name +
        host.name + event.action over the plan's bounded window).

        HISIEM does not persist ``run_id``, so correlation is on identity + bounded
        time only — NEVER a test-only HISIEM field. The plan's committed F1..F5
        instants are what distinguish the SAME run from a DIFFERENT run that
        reused/derived the same attack source:

        - no matching events            -> fresh run, proceed;
        - matching events aligned to    -> the provider already holds THIS run's
          this run's F1..F5 instants       events. Whether that authorizes a
          + draft carries an                resume/reconcile (zero duplicate
            authoritative injection          writes) or is a refused collision
            ledger (``injected``             depends on the LOCAL draft:
            non-empty)                       see below;
        - matching events NOT aligned   -> a DIFFERENT run on the same identity ->
          to this run's F1..F5 instants    raise ``RunIdentityCollision``.

        Blocker (correctness round): provider-aligned events prove the PROVIDER
        resources exist — they do NOT prove the CURRENT mutable draft has safely
        completed its injection stage. A fresh draft (no ``injected`` records) must
        therefore NOT proceed to ``inject_events`` just because aligned events
        exist: it cannot prove it owns them, and injecting again would duplicate
        writes. Only a draft whose authoritative ``injected`` ledger records prior
        injection attempts may reconcile (zero new writes for already-attempted
        roles).
        """
        f1 = self._time_plan.events["F1"]
        w1 = self._time_plan.events["W1"]
        scope_from = f1 - timedelta(minutes=1)
        scope_to = w1 + timedelta(minutes=1)
        conditions: list[dict[str, object]] = [
            {"field": "source.ip", "operator": "is", "value": self._identity.attack_source_ip},
            {"field": "user.name", "operator": "is", "value": self._identity.user_name},
            {"field": "host.name", "operator": "is", "value": self._identity.host_name},
            {"field": "event.action", "operator": "is", "value": GP01_RULE_CONDITION},
        ]
        existing = await self._reader.search_events(
            from_=scope_from, to=scope_to, conditions=conditions, size=50
        )
        if not existing:
            return  # fresh run — proceed
        # The detection window is [F1, F1 + windowMinutes]; events inside it are
        # ambiguous as to which run they belong to, so the collision decision is
        # made on the rendered-instant signature, not raw window membership.
        committed = [self._time_plan.events[role] for role in GP01_FAILURE_ROLES]
        aligned = 0
        for hit in existing:
            if hit.timestamp is None:
                continue
            ts = _utc_dt(hit.timestamp)
            for instant in committed:
                if abs((ts - instant).total_seconds()) <= _RESOLVE_WINDOW_SECONDS:
                    aligned += 1
                    break
        if aligned == 0:
            # Matching events that align to NONE of this run's committed F1..F5
            # instants: a different run that reused/derived this exact attack
            # identity.
            raise RunIdentityCollision(
                f"preflight found {len(existing)} existing authentication_failure "
                f"events for source={self._identity.attack_source_ip!r} "
                f"user={self._identity.user_name!r} host={self._identity.host_name!r} "
                "that align to none of this run's committed F1..F5 instants — a "
                "different run appears to reuse this identity; refusing to write"
            )
        # Aligned provider events exist. Only an authoritative LOCAL injection
        # ledger proves THIS draft already attempted these roles; a fresh/partial
        # draft must not blindly inject again (duplicate-write blocker).
        if not self._draft.injected:
            raise RunIdentityCollision(
                "aligned provider events exist but the local authoritative "
                "injection ledger is absent; refusing duplicate injection "
                "(re-run with the same run_id + persisted draft to reconcile, or "
                "use a NEW run_id for a genuinely fresh run)"
            )
        # The draft records prior injection attempts -> resume/reconcile is safe:
        # the caller's inject gate skips already-attempted roles, so no duplicate
        # writes occur.

    # -- render + inject ---------------------------------------------------

    def _logical(self) -> list[LogicalEvent]:
        return list(GP01_LOGICAL_DATASET)

    def render_events(self) -> list[RenderedEvent]:
        """Bind every logical event to a rendered syslog line (E1-B.3 §11).

        Legal-from: PREFLIGHTED (a NEW draft must preflight first). Idempotent
        resume exception: if the draft already carries rendered events (state
        EVENTS_RENDERED or later) the existing lines are returned WITHOUT
        re-rendering or re-transitioning, preserving the resume path."""
        if self._draft.rendered:
            return list(self._draft.rendered)  # resume: never re-render
        _require_state(self._draft.state, MaterializationState.PREFLIGHTED)
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
        not reorder.

        §12 resume-safety: a draft in INDETERMINATE/FAILED can NEVER be injected
        from again (zero writes — the ambiguous server-side outcome must be
        reconciled, not resent). A draft already in EVENTS_INJECTED (or later) is
        a completed injection window and is a no-op returning ``None``. Only a
        fresh/partial window (NEW/PREFLIGHTED/EVENTS_RENDERED with an incomplete
        ``injected`` record) actually sends; an already-attempted role is never
        blindly re-injected."""
        injected = MaterializationState.INDETERMINATE.value
        failed = MaterializationState.FAILED.value
        if self._draft.state in (injected, failed):
            raise InvalidMaterializationTransition(
                f"cannot inject from state {self._draft.state!r}: the ambiguous/"
                "failed TCP outcome must be reconciled (same run_id), never "
                "re-injected (E1-B.3 §12)"
            )
        if self._draft.state in (
            MaterializationState.EVENTS_INJECTED.value,
            MaterializationState.EVENTS_RESOLVED.value,
            MaterializationState.ALERT_RESOLVED.value,
            MaterializationState.VERIFIED.value,
            MaterializationState.MATERIALIZED.value,
        ):
            return  # completed/partial-injection window: already injected — no-op
        _require_state(
            self._draft.state,
            MaterializationState.NEW,
            MaterializationState.PREFLIGHTED,
            MaterializationState.EVENTS_RENDERED,
        )
        rendered = self.render_events()
        attempted = {attempt.logical_role for attempt in self._draft.injected}
        for event in rendered:
            if event.role in attempted:
                continue  # resume: reconcile, never blind re-inject (§12)
            attempt = await self._injector.inject(event)
            self._draft.injected.append(attempt)
            if attempt.write_status == WRITE_STATUS_INDETERMINATE:
                # §12: the server-side outcome cannot be proven. The run MUST stop
                # in INDETERMINATE and MUST NOT auto-resend this event; any later
                # resume attempt is refused zero-write by the gate above.
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
        """Resolve F1..F5, S1, W1 through HISIEM log-search (E1-B.3 §13).

        Legal-from: EVENTS_INJECTED (must have injected first). A draft already in
        EVENTS_RESOLVED is an idempotent resume reconcile — already-resolved roles
        are skipped and the state is re-asserted. An INDETERMINATE/FAILED injection
        window is TERMINAL (§12): its unattempted siblings were never sent and can
        never be injected (re-injection is refused), so the run cannot be completed
        by resolution — the operator abandons it for a NEW run_id."""
        if self._draft.state in (
            MaterializationState.NEW.value,
            MaterializationState.PREFLIGHTED.value,
            MaterializationState.EVENTS_RENDERED.value,
            MaterializationState.INDETERMINATE.value,
            MaterializationState.FAILED.value,
        ):
            raise InvalidMaterializationTransition(
                f"cannot resolve events from state {self._draft.state!r}; a run in "
                "INDETERMINATE/FAILED had its injection window interrupted — the "
                "unattempted siblings were never sent and may not be re-injected "
                "(E1-B.3 §12), so it cannot be completed by resolution. Abandon it "
                "and start a NEW run_id."
            )
        _require_state(
            self._draft.state,
            MaterializationState.EVENTS_INJECTED,
            MaterializationState.EVENTS_RESOLVED,
            MaterializationState.ALERT_RESOLVED,
            MaterializationState.VERIFIED,
            MaterializationState.MATERIALIZED,
        )
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
        """Resolve the real brute-force alert for this run (E1-B.3 §14).

        Legal-from: EVENTS_RESOLVED. If the alert was already resolved (resume) the
        state is simply re-asserted toward ALERT_RESOLVED."""
        _require_state(
            self._draft.state,
            MaterializationState.EVENTS_RESOLVED,
            MaterializationState.ALERT_RESOLVED,
        )
        if self._draft.resolved_alert is not None:
            self._set_state(MaterializationState.ALERT_RESOLVED)
            return
        # Event-time scope (E1-B.3 §6): the detection window [F1-1m, W1+1m] is the
        # alert's ``@timestamp`` (event-time / window-end) binding box. ``created_at``
        # (processing-time) is a SEPARATE clock and is only used as the optional
        # freshness lower bound the draft froze when THIS materialization began.
        window_from = self._time_plan.events["F1"] - timedelta(minutes=1)
        window_to = self._time_plan.events["W1"] + timedelta(minutes=1)
        processing_not_before = (
            _utc_dt(self._draft.alert_processing_not_before)
            if self._draft.alert_processing_not_before
            else None
        )
        resolved = await self._reader.wait_for_alert(
            attack_source_ip=self._identity.attack_source_ip,
            event_time_from=window_from,
            event_time_to=window_to,
            deadline=deadline,
            interval=interval,
            processing_time_not_before=processing_not_before,
        )
        self._draft.resolved_alert = resolved
        self._set_state(MaterializationState.ALERT_RESOLVED)

    def verify(self) -> VerifiedDataset:
        """Produce the immutable VerifiedDataset only when every invariant holds
        (E1-B.3 §16). Legal-from: ALERT_RESOLVED. Raises
        DatasetInvariantViolation otherwise."""
        _require_state(
            self._draft.state,
            MaterializationState.ALERT_RESOLVED,
            MaterializationState.VERIFIED,
        )
        if self._draft.resolved_alert is None:
            raise DatasetInvariantViolation("cannot verify before the source alert is resolved")
        events = dict(self._draft.resolved_events)
        missing = [role for role in GP01_SEMANTIC_ROLES if role not in events]
        if missing:
            raise DatasetInvariantViolation(f"cannot verify with unresolved events: {missing}")
        from .verifier import DatasetVerifier

        materialized_at = _iso_now()
        dataset = DatasetVerifier(
            scenario=self._scenario,
            run=self._identity,
            tenant_id=self._tenant_id,
            time_plan=self._time_plan,
        ).verify(
            resolved_events=events,
            source_alert=self._draft.resolved_alert,
            materialized_at=materialized_at,
        )
        # Record the frozen verification instant + state so a later resume can
        # recover the VerifiedDataset without re-verifying (E1-B.4 §2).
        self._draft.verified_at = dataset.materialized_at
        self._draft.state = MaterializationState.VERIFIED.value
        self._draft.updated_at = _iso_now()
        return dataset

    # -- state helpers -----------------------------------------------------

    def _set_state(self, state: MaterializationState) -> None:
        self._draft.state = state.value
        self._draft.updated_at = _iso_now()

    def mark_materialized(self) -> None:
        _require_state(self._draft.state, MaterializationState.VERIFIED)
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
    await reader.readiness()
    await materializer.preflight(rule=rule, reachable=True)
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
