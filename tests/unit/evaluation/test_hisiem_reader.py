"""Unit tests for the HISIEM reader pure mapping helpers + alert scoping (E1-B.3
§7, §14).

Offline: exercises only the deterministic mappers over realistic rule/alert/
log-search payload shapes and the reader's candidate-scoping decision over a
stubbed httpx transport. No network, no real httpx client construction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from hisiem_soc_copilot.evaluation.contracts import (
    AlertResolutionTimeout,
    AmbiguousSourceAlertError,
)
from hisiem_soc_copilot.evaluation.hisiem_reader import (
    HisiemEvaluationReader,
    _alert_in_window,
    _map_found_event,  # noqa: PLC2701
    _matches_run_alert,
    map_found_alert,
    map_rule_contract,
)

_RULE_ID = "rule-ssh-brute-force-001"


def _rule_payload(**overrides) -> dict:
    payload = {
        "id": _RULE_ID,
        "name": "SSH Brute Force",
        "enabled": True,
        "type": "threshold",
        "severity": "high",
        "status": "enabled",
        "keyField": "source.ip",
        "windowMinutes": 5,
        "threshold": 5,
        "condition": {"type": "expression", "value": "authentication_failure"},
    }
    payload.update(overrides)
    return payload


def test_map_rule_contract_keeps_preflight_fields() -> None:
    contract = map_rule_contract(_rule_payload())
    assert contract is not None
    assert contract.rule_id == "rule-ssh-brute-force-001"
    assert contract.enabled is True
    assert contract.key_field == "source.ip"
    assert contract.window_minutes == 5
    assert contract.threshold == 5
    assert contract.condition_action == "authentication_failure"


def test_map_rule_contract_maps_non_dict_to_none() -> None:
    assert map_rule_contract(None) is None
    assert map_rule_contract("not-a-dict") is None


def test_map_found_alert_address_id_never_from_alert_id() -> None:
    # payload has alert.id set but NO _id → not addressable by the detail API.
    no_es_id = {"alert": {"id": "biz-9000", "rule_id": "rule-ssh-brute-force-001"}}
    assert map_found_alert(no_es_id) is None

    # payload with BOTH: address_id MUST come from _id, never alert.id.
    both = {
        "_id": "es-doc-7f3a",
        "_index": "siem-alerts-gp01",
        "alert": {
            "id": "biz-9000",
            "rule_id": "rule-ssh-brute-force-001",
            "rule_name": "SSH Brute Force",
        },
        "source": {"ip": "198.18.0.5"},
    }
    alert = map_found_alert(both)
    assert alert is not None
    assert alert.address_id == "es-doc-7f3a"
    assert alert.business_id == "biz-9000"  # alert.id preserved as metadata only
    assert alert.rule_id == "rule-ssh-brute-force-001"


def test_map_found_alert_accepts_flat_and_list_envelope() -> None:
    flat = {"_id": "es-doc-1", "rule_id": "rule-ssh-brute-force-001"}
    nested = {"_source": {"_id": "es-doc-2", "rule_id": "rule-ssh-brute-force-001"}}
    assert map_found_alert(flat).address_id == "es-doc-1"
    assert map_found_alert(nested).address_id == "es-doc-2"


def test_map_found_alert_reads_related_events_from__id() -> None:
    payload = {
        "_id": "es-doc-1",
        "rule_id": "rule-ssh-brute-force-001",
        "related_events": [{"_index": "siem-events-x", "_id": "evt-1"}],
    }
    alert = map_found_alert(payload)
    assert alert is not None
    assert alert.event_count is None  # no count provided → bounded None
    assert [(r.index, r.document_id) for r in alert.related_events] == [
        ("siem-events-x", "evt-1")
    ]


def test_map_found_event_preserves__id__index_and_message() -> None:
    item = {
        "_id": "evt-0001",
        "_index": "siem-events-2026.09",
        "@timestamp": "2026-09-05T02:30:15Z",
        "event": {"category": "authentication", "action": "authentication_failure"},
        "source": {"ip": "198.18.0.5"},
        "user": {"name": "svc01deadbeef"},
        "host": {"name": "app-a1b2c3d4"},
        "log": {"source_id": "ls-54fc7d96"},
        "message": "Sep  5 10:30:15 app sshd[1234]: Failed password for svc01deadbeef",
    }
    event = _map_found_event(item)
    assert event is not None
    assert event.document_id == "evt-0001"
    assert event.index == "siem-events-2026.09"
    assert event.message is not None
    assert "Failed password for" in event.message
    assert event.source_ip == "198.18.0.5"
    assert event.log_source_id == "ls-54fc7d96"


def test_map_found_event_requires_document_id() -> None:
    assert _map_found_event({"_index": "siem-events-x"}) is None
    assert _map_found_event(None) is None


# ---------------------------------------------------------------------------
# §14 — current-run alert time scoping (correctness-round §12, cases D and E)
# ---------------------------------------------------------------------------

_ATTACK_SOURCE = "198.18.0.9"


def _alert_payload(
    *,
    address_id: str,
    rule_id: str = _RULE_ID,
    created_at: str,
    event_count: int = 5,
    status: str = "OPEN",
) -> dict:
    """One list-endpoint alert item (map_found_alert envelope shape)."""
    return {
        "_id": address_id,
        "_index": "siem-alerts-gp01",
        "alert": {
            "id": f"biz-{address_id}",
            "rule_id": rule_id,
            "rule_name": "SSH Brute Force",
            "created_at": created_at,
            "deduplicated_count": event_count,
            "status": status,
        },
        "source": {"ip": _ATTACK_SOURCE},
    }


def _alert_detail_payload(payload: dict) -> dict:
    """The alert-detail GET returns the SAME address; map_found_alert reads the
    top-level ``_id`` and flattens under ``alert.*`` — reuse the list payload."""
    return payload


def test_alert_in_window_true_only_for_parsed_in_scope_created_at() -> None:
    scope_from = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    scope_to = datetime(2026, 9, 5, 12, 6, 0, tzinfo=UTC)
    in_window = map_found_alert(
        _alert_payload(address_id="es-in", created_at="2026-09-05T12:03:00Z")
    )
    assert in_window is not None
    assert _alert_in_window(in_window, scope_from, scope_to) is True
    old = map_found_alert(_alert_payload(address_id="es-old", created_at="2026-09-01T09:00:00Z"))
    assert old is not None
    assert _alert_in_window(old, scope_from, scope_to) is False  # outside [from_, to]
    unparseable = map_found_alert(_alert_payload(address_id="es-un", created_at="not-a-timestamp"))
    assert unparseable is not None
    assert _alert_in_window(unparseable, scope_from, scope_to) is False  # safe: never selected


def test_matches_run_alert_requires_rule_and_attack_entity() -> None:
    own = map_found_alert(_alert_payload(address_id="es-own", created_at="2026-09-05T12:03:00Z"))
    assert own is not None
    assert _matches_run_alert(own, _RULE_ID, _ATTACK_SOURCE) is True
    other_rule = map_found_alert(
        _alert_payload(address_id="es-x", created_at="2026-09-05T12:03:00Z", rule_id="rule-other")
    )
    assert other_rule is not None
    assert _matches_run_alert(other_rule, _RULE_ID, _ATTACK_SOURCE) is False


def _scope() -> tuple[datetime, datetime]:
    """A bounded [F1-1m, W1+1m] current-run window derived from the plan anchor."""
    return datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC), datetime(
        2026, 9, 5, 12, 6, 0, tzinfo=UTC
    )


def _stub_reader(alerts: list[dict]) -> HisiemEvaluationReader:
    """A real HisiemEvaluationReader whose httpx transport is stubbed so the
    list/detail endpoints return canned alert payloads (no network)."""
    assert all(alert.get("_id") for alert in alerts)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/alerts":
            return httpx.Response(200, json={"items": alerts})
        # /api/alerts/{address_id} detail — the reader re-reads the candidate.
        address_id = request.url.path.rsplit("/", 1)[-1]
        for alert in alerts:
            if alert["_id"] == address_id:
                return httpx.Response(200, json=_alert_detail_payload(alert))
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return HisiemEvaluationReader(
        tenant_id="tenant-a",
        base_url="http://hisiem.test",
        bearer_token="",
        client=httpx.AsyncClient(transport=transport, base_url="http://hisiem.test"),
    )


async def test_alert_candidate_ignores_old_same_source_alert_outside_window() -> None:
    """D (§12): an OLD same-source alert OUTSIDE the current-run [from_, to] window
    must NOT be selected — the poll keeps waiting until no candidate is stable, so
    it raises AlertResolutionTimeout rather than judging a new run "same as old"."""
    from_, to = _scope()
    old_alert = _alert_payload(address_id="es-old", created_at="2026-09-01T09:00:00Z")
    reader = _stub_reader([old_alert])
    try:
        with pytest.raises(AlertResolutionTimeout):
            await reader.wait_for_alert(
                attack_source_ip=_ATTACK_SOURCE,
                from_=from_,
                to=to,
                deadline=datetime.now(UTC) - timedelta(seconds=1),  # already expired
                interval=0.001,
                stable_reads=3,
            )
    finally:
        await reader.close()


async def test_alert_candidate_selects_single_in_window_stable_alert() -> None:
    """D's positive counterpart: a single in-window alert is selected once stable."""
    from_, to = _scope()
    in_alert = _alert_payload(address_id="es-current", created_at="2026-09-05T12:03:00Z")
    reader = _stub_reader([in_alert])
    try:
        resolved = await reader.wait_for_alert(
            attack_source_ip=_ATTACK_SOURCE,
            from_=from_,
            to=to,
            deadline=datetime.now(UTC) + timedelta(seconds=10),
            interval=0.001,
            stable_reads=2,
        )
        assert resolved.address_id == "es-current"
        assert resolved.rule_id == _RULE_ID
    finally:
        await reader.close()


async def test_alert_candidate_two_in_window_same_source_is_ambiguous() -> None:
    """E (§12): two alerts INSIDE the window for the SAME source → the reader MUST
    raise AmbiguousSourceAlertError — ambiguity is never resolved by newest/risk."""
    from_, to = _scope()
    reader = _stub_reader(
        [
            _alert_payload(address_id="es-c1", created_at="2026-09-05T12:03:00Z"),
            _alert_payload(address_id="es-c2", created_at="2026-09-05T12:04:00Z"),
        ]
    )
    try:
        with pytest.raises(AmbiguousSourceAlertError):
            await reader.wait_for_alert(
                attack_source_ip=_ATTACK_SOURCE,
                from_=from_,
                to=to,
                deadline=datetime.now(UTC) + timedelta(seconds=10),
                interval=0.001,
                stable_reads=3,
            )
    finally:
        await reader.close()
