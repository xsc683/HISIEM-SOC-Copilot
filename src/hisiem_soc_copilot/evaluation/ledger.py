"""Recovery-ledger serialization for the GP-01 materialization draft (E1-B.3 §17).

The run's mutable ``MaterializationDraft`` is persisted to ``.eval-runs/…/
materialization.json`` so a ``resume`` can reconcile instead of re-injecting. The
time plan is persisted (same bound timestamps) because resume re-resolves the
already-injected events inside the SAME windows. This ledger is NEVER a manifest
and MUST NOT be consumed by the scorer. Secrets / bearer tokens / raw credentials
never appear here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import (
    EventTimePlan,
    InjectionAttempt,
    MaterializationDraft,
    RelatedEventRef,
    RenderedEvent,
    ResolvedAlert,
    ResolvedEvent,
    RunIdentity,
)

_VERSION = 1
_TIMEZONE = "Asia/Shanghai"


def _rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _identity_to_dict(identity: RunIdentity) -> dict[str, str]:
    return {
        "run_id": identity.run_id,
        "run_tag": identity.run_tag,
        "attack_source_ip": identity.attack_source_ip,
        "watermark_source_ip": identity.watermark_source_ip,
        "user_name": identity.user_name,
        "host_name": identity.host_name,
    }


def _plan_to_dict(plan: EventTimePlan) -> dict[str, Any]:
    return {
        "anchor_local": _rfc3339(plan.anchor_local),
        "events": {role: _rfc3339(dt) for role, dt in plan.events.items()},
        "wall_clock": {role: _rfc3339(dt) for role, dt in plan.wall_clock.items()},
    }


def _attempt_to_dict(attempt: InjectionAttempt) -> dict[str, str]:
    return {
        "logical_role": attempt.logical_role,
        "attempted_at": attempt.attempted_at,
        "payload_sha256": attempt.payload_sha256,
        "socket_target": attempt.socket_target,
        "write_status": attempt.write_status,
    }


def _rendered_to_dict(event: RenderedEvent) -> dict[str, Any]:
    return {
        "role": event.role,
        "action": event.action,
        "outcome": event.outcome,
        "host_name": event.host_name,
        "source_ip": event.source_ip,
        "user_name": event.user_name,
        "wall_second": event.wall_second,
        "line": event.line,
        "payload_sha256": event.payload_sha256,
    }


def _resolved_to_dict(event: ResolvedEvent) -> dict[str, Any]:
    return {
        "logical_role": event.logical_role,
        "provider": event.provider,
        "index": event.index,
        "document_id": event.document_id,
        "timestamp": event.timestamp,
        "event_category": event.event_category,
        "event_action": event.event_action,
        "event_outcome": event.event_outcome,
        "source_ip": event.source_ip,
        "user_name": event.user_name,
        "host_name": event.host_name,
        "log_source_id": event.log_source_id,
        "message_fingerprint": event.message_fingerprint,
    }


def _alert_to_dict(alert: ResolvedAlert) -> dict[str, Any]:
    return {
        "provider": alert.provider,
        "address_id": alert.address_id,
        "business_id": alert.business_id,
        "rule_id": alert.rule_id,
        "rule_name": alert.rule_name,
        "entity": alert.entity,
        "created_at": alert.created_at,
        "event_count": alert.event_count,
        "status": alert.status,
        "related_event_refs": [
            {"index": ref.index, "document_id": ref.document_id}
            for ref in alert.related_event_refs
        ],
    }


def dump_draft(draft: MaterializationDraft) -> str:
    """Serialize the mutable draft to deterministic JSON text (ledger only)."""
    payload = {
        "version": _VERSION,
        "run_id": draft.run_id,
        "scenario_id": draft.scenario_id,
        "tenant_id": draft.tenant_id,
        "state": draft.state,
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
        "bound": draft.bound,
        "identity": _identity_to_dict(draft.identity) if draft.identity else None,
        "time_plan": _plan_to_dict(draft.time_plan) if draft.time_plan else None,
        "injected": [_attempt_to_dict(i) for i in draft.injected],
        "rendered": [_rendered_to_dict(r) for r in draft.rendered],
        "resolved_events": {
            role: _resolved_to_dict(event)
            for role, event in sorted(draft.resolved_events.items())
        },
        "resolved_alert": _alert_to_dict(draft.resolved_alert) if draft.resolved_alert else None,
        "failure": draft.failure,
        "verified_at": draft.verified_at,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def load_draft(
    path: Path, *, run_id: str, scenario_id: str, tenant_id: str
) -> MaterializationDraft | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return load_draft_text(
        text, run_id=run_id, scenario_id=scenario_id, tenant_id=tenant_id
    )


def load_draft_text(
    text: str, *, run_id: str, scenario_id: str, tenant_id: str
) -> MaterializationDraft:
    """Rehydrate a persisted draft. Missing/partial state is defaulted so a resume
    can reconcile; the injected/rendered/resolved records are read back verbatim."""
    data = json.loads(text) if text else {}
    identity_raw = data.get("identity")
    plan_raw = data.get("time_plan")
    alert_raw = data.get("resolved_alert")
    return MaterializationDraft(
        run_id=str(data.get("run_id") or run_id),
        scenario_id=str(data.get("scenario_id") or scenario_id),
        tenant_id=str(data.get("tenant_id") or tenant_id),
        state=str(data.get("state") or "NEW"),
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
        bound=bool(data.get("bound", False)),
        identity=(
            _identity_from_dict(identity_raw, run_id)
            if isinstance(identity_raw, dict)
            else None
        ),
        time_plan=_plan_from_dict(plan_raw) if isinstance(plan_raw, dict) else None,
        injected=_attempts_from(data.get("injected")),
        rendered=_rendered_from(data.get("rendered")),
        resolved_events=_events_from(data.get("resolved_events")),
        resolved_alert=_alert_from(alert_raw) if isinstance(alert_raw, dict) else None,
        failure=_opt_str(data.get("failure")),
        verified_at=str(data.get("verified_at") or ""),
    )


def _identity_from_dict(raw: dict[str, Any], run_id: str) -> RunIdentity:
    return RunIdentity(
        run_id=str(raw.get("run_id") or run_id),
        run_tag=str(raw.get("run_tag") or ""),
        attack_source_ip=str(raw.get("attack_source_ip") or ""),
        watermark_source_ip=str(raw.get("watermark_source_ip") or ""),
        user_name=str(raw.get("user_name") or ""),
        host_name=str(raw.get("host_name") or ""),
    )


def _plan_from_dict(raw: dict[str, Any]) -> EventTimePlan:
    def _dt(value: object) -> datetime:
        return datetime.fromisoformat(str(value))

    def _clock(mapping: object) -> dict[str, datetime]:
        out: dict[str, datetime] = {}
        if isinstance(mapping, dict):
            for role, value in mapping.items():
                out[str(role)] = _dt(value)
        return out

    return EventTimePlan(
        anchor_local=_dt(raw.get("anchor_local")),
        events=_clock(raw.get("events")),
        wall_clock=_clock(raw.get("wall_clock")),
    )


def _attempts_from(raw: object) -> list[InjectionAttempt]:
    if not isinstance(raw, list):
        return []
    attempts: list[InjectionAttempt] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        attempts.append(
            InjectionAttempt(
                logical_role=str(item.get("logical_role") or ""),
                attempted_at=str(item.get("attempted_at") or ""),
                payload_sha256=str(item.get("payload_sha256") or ""),
                socket_target=str(item.get("socket_target") or ""),
                write_status=str(item.get("write_status") or ""),
            )
        )
    return attempts


def _rendered_from(raw: object) -> list[RenderedEvent]:
    if not isinstance(raw, list):
        return []
    events: list[RenderedEvent] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        events.append(
            RenderedEvent(
                role=str(item.get("role") or ""),
                action=str(item.get("action") or ""),
                outcome=str(item.get("outcome") or ""),
                host_name=str(item.get("host_name") or ""),
                source_ip=str(item.get("source_ip") or ""),
                user_name=str(item.get("user_name") or ""),
                wall_second=int(item.get("wall_second") or 0),
                line=str(item.get("line") or ""),
                payload_sha256=str(item.get("payload_sha256") or ""),
            )
        )
    return events


def _events_from(raw: object) -> dict[str, ResolvedEvent]:
    resolved: dict[str, ResolvedEvent] = {}
    if isinstance(raw, dict):
        for role, item in raw.items():
            if isinstance(item, dict):
                resolved[str(role)] = _resolved_from(item, str(role))
    return resolved


def _resolved_from(item: dict[str, Any], role: str) -> ResolvedEvent:
    return ResolvedEvent(
        logical_role=str(item.get("logical_role") or role),
        provider=str(item.get("provider") or "hisiem"),
        index=str(item.get("index") or ""),
        document_id=str(item.get("document_id") or ""),
        timestamp=str(item.get("timestamp") or ""),
        event_category=str(item.get("event_category") or ""),
        event_action=str(item.get("event_action") or ""),
        event_outcome=_opt_str(item.get("event_outcome")),
        source_ip=str(item.get("source_ip") or ""),
        user_name=str(item.get("user_name") or ""),
        host_name=str(item.get("host_name") or ""),
        log_source_id=_opt_str(item.get("log_source_id")),
        message_fingerprint=_opt_str(item.get("message_fingerprint")),
    )


def _alert_from(item: dict[str, Any]) -> ResolvedAlert:
    refs: list[RelatedEventRef] = []
    refs_raw = item.get("related_event_refs")
    if isinstance(refs_raw, list):
        for r in refs_raw:
            if isinstance(r, dict) and r.get("index") and r.get("document_id"):
                refs.append(
                    RelatedEventRef(index=str(r["index"]), document_id=str(r["document_id"]))
                )
    return ResolvedAlert(
        provider=str(item.get("provider") or "hisiem"),
        address_id=str(item.get("address_id") or ""),
        business_id=_opt_str(item.get("business_id")),
        rule_id=str(item.get("rule_id") or ""),
        rule_name=_opt_str(item.get("rule_name")),
        entity=_opt_str(item.get("entity")),
        created_at=str(item.get("created_at") or ""),
        event_count=int(item.get("event_count") or 0),
        status=str(item.get("status") or ""),
        related_event_refs=refs,
    )


__all__ = [
    "dump_draft",
    "load_draft",
    "load_draft_text",
]
