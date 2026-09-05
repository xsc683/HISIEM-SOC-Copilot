"""Shared in-memory GP-01 dataset builders for offline evaluation unit tests.

Pure helpers only — no network, no injector/reader I/O. They construct the
bounded typed dataset (ResolvedEvents F1..W1, a ResolvedAlert, an EventTimePlan)
that a successful materialization would produce, so the materializer and sealer
tests can drive their state machines entirely offline.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hisiem_soc_copilot.evaluation.contracts import (
    GP01_LOGICAL_DATASET,
    GP01_RULE_ID,
    LogicalEvent,
    ResolvedAlert,
    ResolvedEvent,
    RunIdentity,
    ScenarioSpec,
    VerifiedDataset,
)
from hisiem_soc_copilot.evaluation.identity import (
    derive_run_identity,
    derive_watermark_user_name,
)
from hisiem_soc_copilot.evaluation.time_plan import (
    EventTimePlan,
    build_event_time_plan,
)

LOG_SOURCE_ID = "ls-54fc7d96"


def fixed_now() -> datetime:
    """Deterministic mid-year anchor instant (far from any year boundary)."""
    return datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def make_time_plan(now: datetime | None = None) -> EventTimePlan:
    return build_event_time_plan(now=now or fixed_now())


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def resolved_events_for(
    scenario: ScenarioSpec,
    identity: RunIdentity,
    plan: EventTimePlan,
) -> dict[str, ResolvedEvent]:
    """One bounded ResolvedEvent per committed logical role (F1..W1).

    Every semantic event mirrors the attack entity; the W1 watermark mirrors the
    watermark entity so the dataset would pass the DatasetVerifier invariants.
    """
    resolved: dict[str, ResolvedEvent] = {}
    for logical in GP01_LOGICAL_DATASET:
        resolved[logical.role] = _resolved_event(logical, identity, plan)
    return resolved


def _resolved_event(
    logical: LogicalEvent,
    identity: RunIdentity,
    plan: EventTimePlan,
) -> ResolvedEvent:
    if logical.classification == "WATERMARK_CONTROL":
        source = identity.watermark_source_ip
        user = derive_watermark_user_name(identity.run_id)
    else:
        source = identity.attack_source_ip
        user = identity.user_name
    return ResolvedEvent(
        logical_role=logical.role,
        provider="hisiem",
        index="siem-events-gp01",
        document_id=f"es-doc-{logical.role}-{identity.run_id}",
        timestamp=_rfc3339(plan.events[logical.role]),
        event_category="authentication",
        event_action=logical.action,
        event_outcome=logical.outcome,
        source_ip=source,
        user_name=user,
        host_name=identity.host_name,
        log_source_id=LOG_SOURCE_ID,
        message_fingerprint=None,
    )


def source_alert(
    identity: RunIdentity,
    *,
    address_id: str = "es-doc-0001",
    business_id: str | None = "biz-0001",
    rule_id: str | None = None,
    event_count: int = 5,
) -> ResolvedAlert:
    return ResolvedAlert(
        provider="hisiem",
        address_id=address_id,
        business_id=business_id,
        rule_id=rule_id or GP01_RULE_ID,
        rule_name="SSH Brute Force",
        entity=identity.attack_source_ip,
        created_at="2026-09-05T12:00:10Z",
        event_count=event_count,
        status="OPEN",
    )


def make_verified(
    *,
    run_id: str = "seal-run-1",
    tenant_id: str = "tenant-a",
    scenario: ScenarioSpec | None = None,
) -> VerifiedDataset:
    """A realistic VerifiedDataset as produced by a successful run."""
    scenario = scenario or ScenarioSpec()
    identity = derive_run_identity(run_id)
    plan = make_time_plan()
    return VerifiedDataset(
        scenario=scenario,
        run=identity,
        tenant_id=tenant_id,
        time_plan=plan,
        source_alert=source_alert(identity),
        resolved_events=resolved_events_for(scenario, identity, plan),
        materialized_at="2026-09-05T12:01:00Z",
    )
