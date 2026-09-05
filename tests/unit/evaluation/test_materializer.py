"""Offline materializer + ledger state-machine tests (E1-B.3 §9-§18).

Drives :class:`Gp01Materializer` over in-memory injector/reader fakes — no
TCP/HTTP. Covers preflight contract mismatch (nothing injected after), fixed
injection order, the §12 indeterminate barrier (run INDETERMINATE + no further
siblings + no blind re-inject on resume), verify() invariant failures, and
ledger dump/load round-tripping.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

from hisiem_soc_copilot.evaluation.contracts import (
    GP01_RULE_CONDITION,
    GP01_RULE_ID,
    GP01_RULE_KEY_FIELD,
    GP01_RULE_THRESHOLD,
    GP01_RULE_WINDOW_MINUTES,
    DatasetInvariantViolation,
    EventInjectionError,
    InjectionAttempt,
    InjectionOutcomeIndeterminate,
    MaterializationState,
    ResolvedAlert,
    ResolvedEvent,
    RuleContractMismatch,
    ScenarioSpec,
)
from hisiem_soc_copilot.evaluation.hisiem_reader import RuleContract
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
    """Returns canned RuleContract / resolved events / alert per call."""

    rule: RuleContract | None = None
    alert: ResolvedAlert | None = None
    resolved_events: dict[str, ResolvedEvent] = field(default_factory=dict)
    event_search_calls: list[str] = field(default_factory=list)

    async def ping(self) -> bool:
        return True

    async def get_rule_contract(self, rule_id: str) -> RuleContract | None:
        return self.rule

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
        deadline: datetime,
        interval: float = 2.0,
    ) -> ResolvedAlert:
        del attack_source_ip, deadline, interval
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


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


async def test_preflight_rule_mismatch_injects_nothing() -> None:
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
        materializer.render_events()
    assert injector.attempts == []  # no write ever occurred after a failed preflight


async def test_preflight_happy_path() -> None:
    materializer = _materializer(reader=_reader_with_resolved())
    await materializer.preflight(rule=_rule(), reachable=True)
    assert materializer.draft.state == MaterializationState.PREFLIGHTED.value
    assert materializer.draft.injected == []


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------


async def test_injection_fixed_order_f1_to_w1() -> None:
    injector = FakeInjector()
    materializer = _materializer(injector=injector)
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
    with pytest.raises(InjectionOutcomeIndeterminate):
        await materializer.inject_events()
    assert materializer.draft.state == MaterializationState.INDETERMINATE.value
    # F1,F2 accepted then F3 indeterminate — F4..W1 never injected (§12).
    assert injector.roles() == ["F1", "F2", "F3"]
    assert materializer.draft.injected[-1].write_status == "indeterminate"


async def test_resume_with_same_draft_does_not_reinject_attempted_role() -> None:
    injector = FakeInjector(forced={"F3": "indeterminate"})
    materializer = _materializer(injector=injector)
    with pytest.raises(InjectionOutcomeIndeterminate):
        await materializer.inject_events()
    failed_draft = materializer.draft

    # A fresh materializer over the SAME persisted draft + a healthy injector
    # reconciles: never re-injects F1/F2/F3; continues at the first unattempted.
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
    await resumed.inject_events()
    assert resumed_injector.roles() == ["F4", "F5", "S1", "W1"]
    assert [a.logical_role for a in resumed.draft.injected] == [
        "F1",
        "F2",
        "F3",
        "F4",
        "F5",
        "S1",
        "W1",
    ]


async def test_connection_error_injection_fails_run() -> None:
    injector = FakeInjector(forced={"F1": "connection_error"})
    materializer = _materializer(injector=injector)
    with pytest.raises(EventInjectionError):
        await materializer.inject_events()
    assert materializer.draft.state == MaterializationState.FAILED.value


# ---------------------------------------------------------------------------
# verify()
# ---------------------------------------------------------------------------


async def test_verify_requires_resolved_alert() -> None:
    materializer = _materializer()
    with pytest.raises(DatasetInvariantViolation):
        materializer.verify()


def _dummy_resolved(role: str) -> ResolvedEvent:
    return ResolvedEvent(
        logical_role=role,
        provider="hisiem",
        index="siem-events-gp01",
        document_id=f"es-doc-{role}",
        timestamp="2026-09-05T12:00:05Z",
        event_category="authentication",
        event_action="authentication_failure",
        event_outcome="failure",
        source_ip="198.18.0.1",
        user_name="svc_dummy",
        host_name="app-dummy",
        log_source_id=None,
        message_fingerprint=None,
    )


async def test_verify_requires_all_semantic_events_resolved() -> None:
    # Simulate a run where F3 never resolved: a draft with a subset of resolved
    # semantic events (but a resolved alert) → verify() must refuse.
    materializer = _materializer()
    materializer.draft.resolved_alert = source_alert(derive_run_identity("mat-run-1"))
    for role in ("F1", "F2", "F4", "F5", "S1", "W1"):
        materializer.draft.resolved_events[role] = _dummy_resolved(role)
    with pytest.raises(DatasetInvariantViolation):
        materializer.verify()


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
    materializer = Gp01Materializer(
        run_id="mat-run-ledger",
        tenant_id="tenant-b",
        scenario=_scenario(),
        identity=identity,
        time_plan=plan,
        injector=FakeInjector(),
        reader=_reader_with_resolved("mat-run-ledger"),
    )
    asyncio.run(materializer.inject_events())
    asyncio.run(
        materializer.resolve_events(deadline=_NOW + timedelta(seconds=30), interval=0.01)
    )
    materializer.draft.resolved_alert = source_alert(identity)

    text = dump_draft(materializer.draft)
    reloaded = load_draft_text(
        text, run_id="mat-run-ledger", scenario_id="gp-01", tenant_id="tenant-b"
    )
    assert reloaded.run_id == "mat-run-ledger"
    assert reloaded.tenant_id == "tenant-b"
    assert reloaded.bound is True
    assert reloaded.state == MaterializationState.EVENTS_RESOLVED.value
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
