"""Offline materializer + ledger state-machine tests (E1-B.3 §9-§18).

Drives :class:`Gp01Materializer` over in-memory injector/reader fakes — no
TCP/HTTP. Covers the typed state-machine transition gates (E1-B.3 §9/§12), the
real run-collision preflight check (E1-B.3 §10.4), preflight contract mismatch
(nothing injected after), fixed injection order, the §12 indeterminate barrier
(run INDETERMINATE + no further siblings + no blind re-inject on resume),
verify() invariant failures, and ledger dump/load round-tripping including the
frozen ``verified_at`` recovery contract (E1-B.4 §21).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hisiem_soc_copilot.evaluation.contracts import (
    GP01_RULE_CONDITION,
    GP01_RULE_ID,
    GP01_RULE_KEY_FIELD,
    GP01_RULE_THRESHOLD,
    GP01_RULE_WINDOW_MINUTES,
    DatasetInvariantViolation,
    EventInjectionError,
    EventTimePlan,
    InjectionAttempt,
    InjectionOutcomeIndeterminate,
    InvalidMaterializationTransition,
    MaterializationState,
    ResolvedAlert,
    ResolvedEvent,
    RuleContractMismatch,
    RunIdentity,
    RunIdentityCollision,
    ScenarioSpec,
)
from hisiem_soc_copilot.evaluation.hisiem_reader import FoundEvent, RuleContract
from hisiem_soc_copilot.evaluation.identity import derive_run_identity
from hisiem_soc_copilot.evaluation.ledger import dump_draft, load_draft_text
from hisiem_soc_copilot.evaluation.materializer import Gp01Materializer
from hisiem_soc_copilot.evaluation.time_plan import build_event_time_plan
from hisiem_soc_copilot.evaluation.verifier import DatasetVerifier
from tests.fixtures.evaluation_fakes import (
    fixed_now,
    resolved_events_for,
    source_alert,
)

_NOW = fixed_now()


def _rule(**overrides) -> RuleContract:
    contract = RuleContract(
        rule_id=GP01_RULE_ID,
        name="SSH Brute Force",
        enabled=True,
        rule_type="threshold",
        severity="high",
        status="enabled",
        key_field=GP01_RULE_KEY_FIELD,
        window_minutes=GP01_RULE_WINDOW_MINUTES,
        threshold=GP01_RULE_THRESHOLD,
        condition_action=GP01_RULE_CONDITION,
    )
    for name, value in overrides.items():
        object.__setattr__(contract, name, value)
    return contract


@dataclass
class FakeInjector:
    """Records every write attempt; may force an outcome per role."""

    forced: dict[str, str] = field(default_factory=dict)
    attempts: list[InjectionAttempt] = field(default_factory=list)

    async def inject(self, event: object) -> InjectionAttempt:
        attempt = InjectionAttempt(
            logical_role=event.role,
            attempted_at="2026-09-05T12:00:05Z",
            payload_sha256=event.payload_sha256,
            socket_target="127.0.0.1:5007",
            write_status=self.forced.get(event.role, "accepted"),
        )
        self.attempts.append(attempt)
        return attempt

    def roles(self) -> list[str]:
        return [a.logical_role for a in self.attempts]


@dataclass
class FakeReader:
    """Returns canned RuleContract / resolved events / alert per call.

    Preflight run-collision (E1-B.3 §10.4) now searches the bounded current-run
    scope via ``search_events`` before any write: by default it returns ``[]``
    (a fresh run). ``collision_events`` injects canned prior events (mirroring the
    real reader's :class:`FoundEvent`) to exercise the reconciliation/collision
    decision. The alert poll mirrors the corrected reader signature with the
    ``from_``/``to`` window bounds (E1-B.3 §14).
    """

    rule: RuleContract | None = None
    alert: ResolvedAlert | None = None
    resolved_events: dict[str, ResolvedEvent] = field(default_factory=dict)
    collision_events: list[FoundEvent] = field(default_factory=list)
    event_search_calls: list[str] = field(default_factory=list)

    async def ping(self) -> bool:
        return True

    async def get_rule_contract(self, rule_id: str) -> RuleContract | None:
        return self.rule

    async def search_events(
        self,
        *,
        from_: datetime | str,
        to: datetime | str,
        conditions: list[dict[str, object]],
        size: int = 50,
    ) -> list[FoundEvent]:
        del from_, to, conditions, size
        self.event_search_calls.append("search")
        return list(self.collision_events)

    async def wait_for_event(
        self,
        *,
        logical_role: str,
        from_: datetime,
        to: datetime,
        conditions: list[dict],
        deadline: datetime,
        interval: float = 2.0,
    ) -> ResolvedEvent:
        del from_, to, conditions, deadline, interval
        self.event_search_calls.append(logical_role)
        event = self.resolved_events.get(logical_role)
        if event is None:
            raise RuntimeError(f"FakeReader has no canned event for {logical_role}")
        return event

    async def wait_for_alert(
        self,
        *,
        attack_source_ip: str,
        event_time_from: datetime,
        event_time_to: datetime,
        deadline: datetime,
        interval: float = 2.0,
        processing_time_not_before: datetime | None = None,
    ) -> ResolvedAlert:
        del (
            attack_source_ip,
            event_time_from,
            event_time_to,
            deadline,
            interval,
            processing_time_not_before,
        )
        if self.alert is None:
            raise RuntimeError("FakeReader has no canned alert")
        return self.alert


def _scenario() -> ScenarioSpec:
    return ScenarioSpec()


def _materializer(
    *,
    run_id: str = "mat-run-1",
    tenant_id: str = "tenant-a",
    injector: FakeInjector | None = None,
    reader: FakeReader | None = None,
) -> Gp01Materializer:
    identity = derive_run_identity(run_id)
    time_plan = build_event_time_plan(now=_NOW)
    return Gp01Materializer(
        run_id=run_id,
        tenant_id=tenant_id,
        scenario=_scenario(),
        identity=identity,
        time_plan=time_plan,
        injector=injector or FakeInjector(),
        reader=reader or FakeReader(),
    )


def _reader_with_resolved(identity_run_id: str = "mat-run-1") -> FakeReader:
    """A reader whose canned resolution matches the materializer's run identity."""
    identity = derive_run_identity(identity_run_id)
    plan = build_event_time_plan(now=_NOW)
    reader = FakeReader(rule=_rule(), alert=source_alert(identity))
    reader.resolved_events = resolved_events_for(_scenario(), identity, plan)
    return reader


def _run_identity(run_id: str) -> tuple[RunIdentity, EventTimePlan]:
    """Deterministic (identity, plan) pair for a run_id (the materializer's own)."""
    return derive_run_identity(run_id), build_event_time_plan(now=_NOW)


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _found_from_resolved(resolved: ResolvedEvent) -> FoundEvent:
    """Mirror a contracts ResolvedEvent back to the reader's FoundEvent shape so
    canned ``search_events`` results correlate on the same identity fields.

    FoundEvent carries no ``message_fingerprint`` field (it is a computed property
    of ``message``), so the fingerprint is dropped — the preflight collision check
    only correlates on source/user/host/action/timestamp."""
    return FoundEvent(
        document_id=resolved.document_id,
        index=resolved.index,
        timestamp=resolved.timestamp,
        event_category=resolved.event_category,
        event_action=resolved.event_action,
        event_outcome=resolved.event_outcome,
        source_ip=resolved.source_ip,
        user_name=resolved.user_name,
        host_name=resolved.host_name,
        log_source_id=resolved.log_source_id,
    )


def _aligned_collision_events(run_id: str) -> list[FoundEvent]:
    """Prior events whose timestamps ALIGN (within ±3 s) to this run's committed
    F1..F5 instants — the signature of the SAME run already injected."""
    identity, plan = _run_identity(run_id)
    resolved = resolved_events_for(_scenario(), identity, plan)
    return [_found_from_resolved(resolved[r]) for r in ("F1", "F2", "F3", "F4", "F5")]


def _shifted_collision_events(run_id: str, *, minutes: float = 5.0) -> list[FoundEvent]:
    """Prior events on the SAME identity whose F1..F5 timestamps are shifted by
    ``minutes`` — NOT aligned to this run's committed instants, so a DIFFERENT
    run appears to reuse this identity."""
    identity, plan = _run_identity(run_id)
    resolved = resolved_events_for(_scenario(), identity, plan)
    shifted: list[FoundEvent] = []
    for role in ("F1", "F2", "F3", "F4", "F5"):
        event = resolved[role]
        shifted_ts = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00")) + timedelta(
            minutes=minutes
        )
        shifted.append(
            FoundEvent(
                document_id=event.document_id,
                index=event.index,
                timestamp=_rfc3339(shifted_ts),
                event_category=event.event_category,
                event_action=event.event_action,
                event_outcome=event.event_outcome,
                source_ip=event.source_ip,
                user_name=event.user_name,
                host_name=event.host_name,
                log_source_id=event.log_source_id,
            )
        )
    return shifted


async def _drive_to_alert_resolved(run_id: str = "mat-run-1") -> Gp01Materializer:
    """Drive a materializer over its resolved reader through the legal happy path
    up to (and including) ALERT_RESOLVED."""
    reader = _reader_with_resolved(run_id)
    materializer = _materializer(run_id=run_id, reader=reader)
    await materializer.preflight(rule=_rule(), reachable=True)
    materializer.render_events()
    await materializer.inject_events()
    await materializer.resolve_events(deadline=_NOW + timedelta(seconds=30), interval=0.01)
    await materializer.resolve_alert(deadline=_NOW + timedelta(seconds=30), interval=0.01)
    return materializer


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


async def test_preflight_rule_mismatch_injects_nothing() -> None:
    """E1-B.3 §10.3 + §12 (contract item 3): a mismatched detection rule fails
    preflight BEFORE any write. The draft stays NEW and the injector records zero
    attempts — no transition (render/inject) is ever driven on a failed preflight.
    """
    mismatches = (
        {"threshold": 6},
        {"window_minutes": 10},
        {"key_field": "user.name"},
        {"condition_action": "login_success"},
        {"enabled": False},
    )
    injector = FakeInjector()
    for index, mismatch in enumerate(mismatches):
        materializer = _materializer(
            run_id=f"mat-preflight-{index}", injector=injector
        )
        with pytest.raises(RuleContractMismatch):
            await materializer.preflight(rule=_rule(**mismatch), reachable=True)
        assert materializer.draft.state == "NEW"  # never leaves NEW on mismatch
    assert injector.attempts == []  # no write ever occurred after a failed preflight
    # The §12 gate also independently refuses render-after-failed-preflight: a NEW
    # draft may not render (render_events is legal-from PREFLIGHTED only).
    with pytest.raises(InvalidMaterializationTransition):
        _materializer(injector=FakeInjector()).render_events()


async def test_preflight_happy_path() -> None:
    materializer = _materializer(reader=_reader_with_resolved())
    await materializer.preflight(rule=_rule(), reachable=True)
    assert materializer.draft.state == MaterializationState.PREFLIGHTED.value
    assert materializer.draft.injected == []
    assert materializer.draft.rendered == []
    # Preflight freezes the alert processing-time freshness bound once.
    assert materializer.draft.alert_processing_not_before != ""


async def test_preflight_freeze_is_once_and_resume_reuses_persisted_bound() -> None:
    """E (§25): the alert processing-time lower bound is frozen ONCE on the first
    live preflight and a resume REUSES the persisted value — it is never re-derived
    from now(), so stale-alert protection does not drift across a resume."""
    materializer = _materializer(reader=_reader_with_resolved())
    await materializer.preflight(rule=_rule(), reachable=True)
    assert materializer.draft.state == MaterializationState.PREFLIGHTED.value
    frozen = materializer.draft.alert_processing_not_before
    assert frozen != ""

    # Simulate a persisted draft resumed much later (ledger rehydrated). The frozen
    # bound must survive and be reused, NOT replaced by a fresh now().
    from hisiem_soc_copilot.evaluation.ledger import dump_draft, load_draft_text

    draft_text = dump_draft(materializer.draft)
    resumed_draft = load_draft_text(
        draft_text, run_id=materializer.draft.run_id, scenario_id="gp-01", tenant_id="tenant-a"
    )
    resumed = Gp01Materializer(
        run_id=resumed_draft.run_id,
        tenant_id=resumed_draft.tenant_id,
        scenario=_scenario(),
        identity=resumed_draft.identity,
        time_plan=resumed_draft.time_plan,
        injector=FakeInjector(),
        reader=_reader_with_resolved(resumed_draft.run_id),
        draft=resumed_draft,
    )
    # A resumed preflight would be a NEW state transition — but resolve_alert reads
    # the persisted bound. Verify the resume draft retains the frozen bound exactly.
    assert resumed.draft.alert_processing_not_before == frozen
    assert resumed.draft.state == MaterializationState.PREFLIGHTED.value


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------


async def test_injection_fixed_order_f1_to_w1() -> None:
    injector = FakeInjector()
    materializer = _materializer(injector=injector)
    await materializer.preflight(rule=_rule(), reachable=True)
    await materializer.inject_events()
    assert injector.roles() == ["F1", "F2", "F3", "F4", "F5", "S1", "W1"]
    assert materializer.draft.state == MaterializationState.EVENTS_INJECTED.value
    assert len(materializer.draft.injected) == 7
    w1 = materializer.draft.rendered[-1]
    assert w1.role == "W1"
    assert w1.source_ip == materializer._identity.watermark_source_ip


async def test_indeterminate_injection_stops_run_and_injects_no_siblings() -> None:
    injector = FakeInjector(forced={"F3": "indeterminate"})
    materializer = _materializer(injector=injector)
    await materializer.preflight(rule=_rule(), reachable=True)
    with pytest.raises(InjectionOutcomeIndeterminate):
        await materializer.inject_events()
    assert materializer.draft.state == MaterializationState.INDETERMINATE.value
    # F1,F2 accepted then F3 indeterminate — F4..W1 never injected (§12).
    assert injector.roles() == ["F1", "F2", "F3"]
    assert materializer.draft.injected[-1].write_status == "indeterminate"


async def test_resume_with_same_draft_does_not_reinject_attempted_role() -> None:
    """§12 contract (memory §2, contract item 11): after an INDETERMINATE F3, a
    fresh materializer over the same draft must produce ZERO new injection
    attempts — INDETERMINATE is terminal; F4/F5/S1/W1 siblings are never sent."""
    injector = FakeInjector(forced={"F3": "indeterminate"})
    materializer = _materializer(injector=injector)
    await materializer.preflight(rule=_rule(), reachable=True)
    with pytest.raises(InjectionOutcomeIndeterminate):
        await materializer.inject_events()
    failed_draft = materializer.draft
    assert injector.roles() == ["F1", "F2", "F3"]

    # A fresh materializer over the SAME persisted draft + a healthy injector.
    resumed_injector = FakeInjector()
    resumed = Gp01Materializer(
        run_id=failed_draft.run_id,
        tenant_id=failed_draft.tenant_id,
        scenario=_scenario(),
        identity=failed_draft.identity,
        time_plan=failed_draft.time_plan,
        injector=resumed_injector,
        reader=_reader_with_resolved(failed_draft.run_id),
        draft=failed_draft,
    )
    assert resumed.draft.state == MaterializationState.INDETERMINATE.value
    # The §12 INDETERMINATE gate: any inject_events() attempt is refused with
    # ZERO writes — the ambiguous outcome must be reconciled, never re-sent.
    with pytest.raises(InvalidMaterializationTransition):
        await resumed.inject_events()
    assert resumed_injector.roles() == []  # zero new injection attempts
    assert resumed.draft.state == MaterializationState.INDETERMINATE.value


async def test_connection_error_injection_fails_run() -> None:
    injector = FakeInjector(forced={"F1": "connection_error"})
    materializer = _materializer(injector=injector)
    await materializer.preflight(rule=_rule(), reachable=True)
    with pytest.raises(EventInjectionError):
        await materializer.inject_events()
    assert materializer.draft.state == MaterializationState.FAILED.value


# ---------------------------------------------------------------------------
# verify()
# ---------------------------------------------------------------------------


async def test_verify_requires_resolved_alert() -> None:
    # Drive the full legal path to ALERT_RESOLVED, then strip the resolved alert
    # (simulating an inconsistent ledger). verify() must refuse with the typed
    # DatasetInvariantViolation — never verify an alert-less dataset.
    materializer = await _drive_to_alert_resolved()
    materializer.draft.resolved_alert = None
    with pytest.raises(DatasetInvariantViolation):
        materializer.verify()
    # Even before ALERT_RESOLVED the gate refuses: from EVENTS_RESOLVED (all
    # events resolved, alert not yet resolved) verify() cannot run at all.
    pre_alert = _materializer(reader=_reader_with_resolved())
    await pre_alert.preflight(rule=_rule(), reachable=True)
    pre_alert.render_events()
    await pre_alert.inject_events()
    await pre_alert.resolve_events(deadline=_NOW + timedelta(seconds=30), interval=0.01)
    assert pre_alert.draft.state == MaterializationState.EVENTS_RESOLVED.value
    with pytest.raises(InvalidMaterializationTransition):
        pre_alert.verify()


async def test_verify_requires_all_semantic_events_resolved() -> None:
    # Drive the legal path to EVENTS_RESOLVED, then drop F3 from the resolved set
    # (simulating a run where one semantic event never resolved) and complete the
    # alert. verify() must refuse — missing checks run BEFORE any role indexing
    # (memory §8: never a KeyError).
    reader = _reader_with_resolved()
    materializer = _materializer(reader=reader)
    await materializer.preflight(rule=_rule(), reachable=True)
    materializer.render_events()
    await materializer.inject_events()
    await materializer.resolve_events(deadline=_NOW + timedelta(seconds=30), interval=0.01)
    assert materializer.draft.state == MaterializationState.EVENTS_RESOLVED.value
    materializer.draft.resolved_events.pop("F3")
    await materializer.resolve_alert(deadline=_NOW + timedelta(seconds=30), interval=0.01)
    assert materializer.draft.state == MaterializationState.ALERT_RESOLVED.value
    with pytest.raises(DatasetInvariantViolation):
        materializer.verify()


async def test_resolve_w1_queries_future_event_time_scope() -> None:
    """W1 is a FUTURE event-time watermark-control role; the resolver's search
    window for W1 must extend into the future (NOT be capped at wall-clock now),
    so a future-@timestamp W1 doc is resolvable."""
    from hisiem_soc_copilot.evaluation.materializer import _RESOLVE_WINDOW_SECONDS

    plan = build_event_time_plan(now=_NOW)
    assert plan.events["W1"] > _NOW  # sanity: W1 is future under the new plan

    captured: dict[str, tuple[datetime, datetime]] = {}

    class CapturingReader(FakeReader):
        async def wait_for_event(
            self,
            *,
            logical_role: str,
            from_: datetime,
            to: datetime,
            conditions: list[dict],
            deadline: datetime,
            interval: float = 2.0,
        ) -> ResolvedEvent:
            captured[logical_role] = (from_, to)
            event = self.resolved_events.get(logical_role)
            if event is None:
                raise RuntimeError(f"no canned event for {logical_role}")
            return event

    reader = _reader_with_resolved()
    capturing = CapturingReader(
        rule=reader.rule,
        alert=reader.alert,
        resolved_events=reader.resolved_events,
    )
    materializer = _materializer(reader=capturing)
    await materializer.preflight(rule=_rule(), reachable=True)
    materializer.render_events()
    await materializer.inject_events()
    await materializer.resolve_events(deadline=_NOW + timedelta(seconds=30), interval=0.01)

    w1_from, w1_to = captured["W1"]
    # The W1 search window is centered on W1's FUTURE instant (± resolve window)
    # and its `to` is strictly in the future — never clamped to wall-clock now.
    assert w1_to > _NOW
    assert abs((w1_to - plan.events["W1"]).total_seconds()) <= _RESOLVE_WINDOW_SECONDS
    assert w1_from > _NOW


def test_verify_offline_happy_path() -> None:
    """DatasetVerifier over the canned resolved set passes every invariant."""
    identity = derive_run_identity("mat-run-1")
    plan = build_event_time_plan(now=_NOW)
    events = resolved_events_for(_scenario(), identity, plan)
    dataset = DatasetVerifier(
        scenario=_scenario(),
        run=identity,
        tenant_id="tenant-a",
        time_plan=plan,
    ).verify(
        resolved_events=events,
        source_alert=source_alert(identity),
        materialized_at="2026-09-05T12:01:00Z",
    )
    assert dataset.run.run_id == "mat-run-1"
    assert dataset.source_alert.address_id == "es-doc-0001"


async def test_materializer_verify_when_all_resolved() -> None:
    reader = _reader_with_resolved()
    materializer = _materializer(reader=reader)
    await materializer.preflight(rule=_rule(), reachable=True)
    materializer.render_events()
    await materializer.inject_events()
    await materializer.resolve_events(deadline=_NOW + timedelta(seconds=30), interval=0.01)
    await materializer.resolve_alert(deadline=_NOW + timedelta(seconds=30), interval=0.01)
    dataset = materializer.verify()
    assert materializer.draft.state == MaterializationState.VERIFIED.value
    assert dataset.tenant_id == "tenant-a"
    assert dataset.run.run_id == "mat-run-1"


# ---------------------------------------------------------------------------
# Ledger round-trip
# ---------------------------------------------------------------------------


def test_ledger_round_trip_preserves_state() -> None:
    identity = derive_run_identity("mat-run-ledger")
    plan = build_event_time_plan(now=_NOW)
    reader = _reader_with_resolved("mat-run-ledger")
    materializer = Gp01Materializer(
        run_id="mat-run-ledger",
        tenant_id="tenant-b",
        scenario=_scenario(),
        identity=identity,
        time_plan=plan,
        injector=FakeInjector(),
        reader=reader,
    )
    asyncio.run(materializer.preflight(rule=_rule(), reachable=True))
    asyncio.run(materializer.inject_events())
    asyncio.run(
        materializer.resolve_events(deadline=_NOW + timedelta(seconds=30), interval=0.01)
    )
    asyncio.run(materializer.resolve_alert(deadline=_NOW + timedelta(seconds=30), interval=0.01))
    dataset = materializer.verify()
    # E1-B.4 §21 (memory §21): verify() must freeze the verification instant on the
    # draft (verified_at) and that instant IS the VerifiedDataset.materialized_at.
    assert materializer.draft.verified_at == dataset.materialized_at
    assert materializer.draft.state == MaterializationState.VERIFIED.value

    text = dump_draft(materializer.draft)
    reloaded = load_draft_text(
        text, run_id="mat-run-ledger", scenario_id="gp-01", tenant_id="tenant-b"
    )
    assert reloaded.run_id == "mat-run-ledger"
    assert reloaded.tenant_id == "tenant-b"
    assert reloaded.bound is True
    assert reloaded.state == MaterializationState.VERIFIED.value
    assert reloaded.identity == identity
    assert reloaded.time_plan == plan
    assert [a.logical_role for a in reloaded.injected] == [
        "F1",
        "F2",
        "F3",
        "F4",
        "F5",
        "S1",
        "W1",
    ]
    assert set(reloaded.resolved_events) == {"F1", "F2", "F3", "F4", "F5", "S1", "W1"}
    assert reloaded.resolved_events["F1"] == materializer.draft.resolved_events["F1"]
    assert reloaded.resolved_alert == source_alert(identity)
    assert reloaded.rendered == materializer.draft.rendered
    # §21 coupling: verified_at must survive the dump/load round-trip and equal the
    # dataset's materialized_at — later ledger checkpoints never rewrite it.
    assert reloaded.verified_at == materializer.draft.verified_at
    assert reloaded.verified_at == dataset.materialized_at


def test_load_verified_from_draft_recovers_frozen_verified_at(tmp_path: Path) -> None:
    """E1-B.4 correctness round (contract item 24-I): the CLI's
    ``_load_verified_from_draft`` rehydrates a VerifiedDataset from a VERIFIED-state
    draft ledger and must recover the ORIGINAL verification instant
    (``draft.verified_at``), NEVER the later-checkpoint ``draft.updated_at`` — the
    sealed manifest records the instant the verifier ran, not when a later
    checkpoint rewrote the ledger."""
    from hisiem_soc_copilot.evaluation.cli import _load_verified_from_draft

    run_id = "mat-cli-recover"
    identity = derive_run_identity(run_id)
    plan = build_event_time_plan(now=_NOW)
    reader = _reader_with_resolved(run_id)
    materializer = Gp01Materializer(
        run_id=run_id,
        tenant_id="tenant-a",
        scenario=_scenario(),
        identity=identity,
        time_plan=plan,
        injector=FakeInjector(),
        reader=reader,
    )
    asyncio.run(materializer.preflight(rule=_rule(), reachable=True))
    asyncio.run(materializer.inject_events())
    asyncio.run(materializer.resolve_events(deadline=_NOW + timedelta(seconds=30), interval=0.01))
    asyncio.run(materializer.resolve_alert(deadline=_NOW + timedelta(seconds=30), interval=0.01))
    dataset = materializer.verify()
    assert materializer.draft.state == MaterializationState.VERIFIED.value
    # A later checkpoint rewrote updated_at (e.g. the CLI checkpoint after verify)
    # but MUST NOT rewrite the frozen verified_at (materialized_at).
    materializer.draft.updated_at = "2026-09-05T13:00:00Z"
    assert materializer.draft.verified_at == dataset.materialized_at
    assert materializer.draft.updated_at != materializer.draft.verified_at

    # Persist the VERIFIED-state draft ledger under the CLI's runs_dir layout.
    run_dir = tmp_path / "gp-01" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = run_dir / "materialization.json"
    ledger_path.write_text(dump_draft(materializer.draft), encoding="utf-8")

    from hisiem_soc_copilot.config import EvaluationSettings

    settings = EvaluationSettings(runs_dir=str(tmp_path))
    recovered = _load_verified_from_draft(settings, run_id)
    # The recovered VerifiedDataset carries the ORIGINAL verification instant, not
    # updated_at and not a fresh seal-time value.
    assert recovered.run.run_id == run_id
    assert recovered.materialized_at == dataset.materialized_at
    assert recovered.materialized_at == materializer.draft.verified_at
    assert recovered.materialized_at != materializer.draft.updated_at


# ---------------------------------------------------------------------------
# §12 — state-machine transition gates (E1-B.3 §9 / correctness-round §11)
# ---------------------------------------------------------------------------


def test_state_machine_new_state_rejects_inject_resolve_verify() -> None:
    """NEW → inject/resolve_events/verify are all ILLEGAL (must preflight first)."""
    materializer = _materializer()
    assert materializer.draft.state == MaterializationState.NEW.value
    with pytest.raises(InvalidMaterializationTransition):
        materializer.render_events()  # NEW may not even render
    with pytest.raises(InvalidMaterializationTransition):
        _ = materializer.verify()


async def test_new_inject_and_new_resolve_events_are_illegal() -> None:
    injector = FakeInjector()
    materializer = _materializer(injector=injector)
    with pytest.raises(InvalidMaterializationTransition):
        await materializer.inject_events()
    with pytest.raises(InvalidMaterializationTransition):
        await materializer.resolve_events(
            deadline=_NOW + timedelta(seconds=30), interval=0.01
        )
    assert injector.attempts == []  # nothing may have been written


async def test_preflighted_inject_is_allowed_after_render_and_before_render_also_allowed() -> None:
    """PREFLIGHTED → inject_events is LEGAL (the gate requires NEW/PREFLIGHTED/
    EVENTS_RENDERED) — render_events happens inside inject_events."""
    injector = FakeInjector()
    materializer = _materializer(injector=injector)
    await materializer.preflight(rule=_rule(), reachable=True)
    assert materializer.draft.state == MaterializationState.PREFLIGHTED.value
    # inject_events from PREFLIGHTED is legal: it renders then injects.
    await materializer.inject_events()
    assert injector.roles() == ["F1", "F2", "F3", "F4", "F5", "S1", "W1"]
    assert materializer.draft.state == MaterializationState.EVENTS_INJECTED.value


async def test_events_rendered_state_requires_inject_before_resolve() -> None:
    materializer = _materializer(reader=_reader_with_resolved())
    await materializer.preflight(rule=_rule(), reachable=True)
    materializer.render_events()
    assert materializer.draft.state == MaterializationState.EVENTS_RENDERED.value
    with pytest.raises(InvalidMaterializationTransition):
        await materializer.resolve_events(
            deadline=_NOW + timedelta(seconds=30), interval=0.01
        )


async def test_indeterminate_gate_refuses_inject_with_zero_writes() -> None:
    # Reach INDETERMINATE through a forced indeterminate F3.
    injector = FakeInjector(forced={"F3": "indeterminate"})
    materializer = _materializer(injector=injector)
    await materializer.preflight(rule=_rule(), reachable=True)
    with pytest.raises(InjectionOutcomeIndeterminate):
        await materializer.inject_events()
    assert materializer.draft.state == MaterializationState.INDETERMINATE.value

    # A second inject attempt (e.g. from an unhealthy resume) is a typed gate
    # failure with ZERO additional writes.
    with pytest.raises(InvalidMaterializationTransition):
        await materializer.inject_events()
    assert injector.roles() == ["F1", "F2", "F3"]
    # Terminal: INDETERMINATE may not resolve events nor verify (the run is
    # abandoned for a NEW run_id).
    with pytest.raises(InvalidMaterializationTransition):
        await materializer.resolve_events(
            deadline=_NOW + timedelta(seconds=30), interval=0.01
        )
    with pytest.raises(InvalidMaterializationTransition):
        materializer.verify()


async def test_failed_gate_refuses_inject_with_zero_writes() -> None:
    # Reach FAILED through a forced connection error on F1.
    injector = FakeInjector(forced={"F1": "connection_error"})
    materializer = _materializer(injector=injector)
    await materializer.preflight(rule=_rule(), reachable=True)
    with pytest.raises(EventInjectionError):
        await materializer.inject_events()
    assert materializer.draft.state == MaterializationState.FAILED.value
    with pytest.raises(InvalidMaterializationTransition):
        await materializer.inject_events()
    assert injector.roles() == ["F1"]
    with pytest.raises(InvalidMaterializationTransition):
        materializer.verify()


async def test_alert_resolved_allows_verify_and_verified_allows_mark_materialized() -> None:
    materializer = await _drive_to_alert_resolved()
    assert materializer.draft.state == MaterializationState.ALERT_RESOLVED.value
    dataset = materializer.verify()  # ALERT_RESOLVED → VERIFIED is the happy path
    assert materializer.draft.state == MaterializationState.VERIFIED.value
    assert dataset.run.run_id == "mat-run-1"
    materializer.mark_materialized()
    assert materializer.draft.state == MaterializationState.MATERIALIZED.value


async def test_inject_from_events_injected_is_noop() -> None:
    # A completed injection window is idempotent: inject_events() is a NO-OP and
    # never re-sends the already-attempted events (§12 resume safety).
    injector = FakeInjector()
    materializer = _materializer(injector=injector)
    await materializer.preflight(rule=_rule(), reachable=True)
    await materializer.inject_events()
    assert injector.roles() == ["F1", "F2", "F3", "F4", "F5", "S1", "W1"]
    await materializer.inject_events()  # no-op
    assert injector.roles() == ["F1", "F2", "F3", "F4", "F5", "S1", "W1"]
    assert len(materializer.draft.injected) == 7


async def test_resolve_events_is_idempotent_reconcile_after_resolved() -> None:
    # A draft already at EVENTS_RESOLVED may reconcile: resolve_events() re-asserts
    # the state and never re-resolves an already-resolved role.
    materializer = await _drive_to_alert_resolved()
    reader: FakeReader = materializer._reader
    reader.event_search_calls.clear()
    await materializer.resolve_events(deadline=_NOW + timedelta(seconds=30), interval=0.01)
    assert materializer.draft.state == MaterializationState.EVENTS_RESOLVED.value
    assert reader.event_search_calls == []  # nothing re-resolved


# ---------------------------------------------------------------------------
# §10.4 / §13 — run-collision preflight (correctness-round §12, cases A-E)
# ---------------------------------------------------------------------------


async def test_run_collision_no_existing_events_preflight_passes() -> None:
    """A: search_events returns [] → fresh run; preflight succeeds."""
    materializer = _materializer(reader=_reader_with_resolved())
    await materializer.preflight(rule=_rule(), reachable=True)
    assert materializer.draft.state == MaterializationState.PREFLIGHTED.value
    assert materializer.draft.injected == []
    assert materializer.draft.rendered == []


async def test_run_collision_aligned_events_fresh_draft_refuses_duplicate() -> None:
    """K (correctness-round §25): provider events ALIGNED to this run's committed
    F1..F5 instants exist, but the CURRENT draft carries NO authoritative injection
    ledger → preflight MUST refuse with zero writes (the draft cannot prove it owns
    those provider events; injecting again would duplicate them)."""
    reader = _reader_with_resolved()
    reader.collision_events = _aligned_collision_events("mat-run-1")
    injector = FakeInjector()
    materializer = _materializer(run_id="mat-run-1", injector=injector, reader=reader)
    with pytest.raises(RunIdentityCollision) as exc_info:
        await materializer.preflight(rule=_rule(), reachable=True)
    assert "authoritative injection ledger is absent" in str(exc_info.value)
    assert materializer.draft.state == MaterializationState.NEW.value
    assert injector.attempts == []  # zero TCP writes


async def test_run_collision_conflicting_events_raise_identity_collision() -> None:
    """C: prior events on the SAME source/user/host/action whose timestamps do NOT
    align to this run's committed F1..F5 instants → a different run appears to
    reuse this identity → preflight raises RunIdentityCollision BEFORE any write."""
    reader = _reader_with_resolved()
    reader.collision_events = _shifted_collision_events("mat-run-1", minutes=5.0)
    injector = FakeInjector()
    materializer = _materializer(run_id="mat-run-1", injector=injector, reader=reader)
    with pytest.raises(RunIdentityCollision):
        await materializer.preflight(rule=_rule(), reachable=True)
    assert materializer.draft.state == MaterializationState.NEW.value
    assert injector.attempts == []  # nothing was ever written


async def test_run_collision_resume_with_persisted_ledger_reconciles_zero_writes() -> None:
    """L (correctness-round §25): provider events ALIGNED to this run's F1..F5
    instants exist AND the resumed draft carries an authoritative ``injected``
    ledger proving prior attempts → the resume reconciles with ZERO duplicate
    writes. A resume never re-runs preflight (the draft is no longer NEW); it
    reuses the persisted ledger, and ``inject_events()`` is a no-op because every
    role is already recorded as attempted."""
    # Build a run whose draft ALREADY records all injected attempts (EVENTS_INJECTED).
    reader = _reader_with_resolved("mat-run-1")
    materializer = _materializer(run_id="mat-run-1", reader=reader)
    await materializer.preflight(rule=_rule(), reachable=True)
    materializer.render_events()
    await materializer.inject_events()
    assert materializer.draft.state == MaterializationState.EVENTS_INJECTED.value
    assert len(materializer.draft.injected) == 7
    persisted = materializer.draft

    # Resume over the SAME persisted draft: the provider already holds aligned
    # events for this identity, but the authoritative ledger authorizes reconcile.
    reader2 = _reader_with_resolved("mat-run-1")
    reader2.collision_events = _aligned_collision_events("mat-run-1")
    injector2 = FakeInjector()
    resumed = Gp01Materializer(
        run_id=persisted.run_id,
        tenant_id=persisted.tenant_id,
        scenario=_scenario(),
        identity=persisted.identity,
        time_plan=persisted.time_plan,
        injector=injector2,
        reader=reader2,
        draft=persisted,
    )
    # Resume path (mirrors the CLI): the draft is EVENTS_INJECTED, so inject_events
    # is a NO-OP — zero new writes even though aligned provider events exist.
    await resumed.inject_events()
    assert injector2.roles() == []  # zero duplicate writes
    assert resumed.draft.state == MaterializationState.EVENTS_INJECTED.value
