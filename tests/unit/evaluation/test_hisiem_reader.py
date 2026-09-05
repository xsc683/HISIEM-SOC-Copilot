"""Unit tests for the HISIEM reader pure mapping helpers (E1-B.3 §7, §14).

Offline: exercises only the deterministic mappers over realistic rule/alert/
log-search payload shapes. No network, no httpx client construction.
"""

from __future__ import annotations

from hisiem_soc_copilot.evaluation.hisiem_reader import (
    _map_found_event,  # noqa: PLC2701
    map_found_alert,
    map_rule_contract,
)


def _rule_payload(**overrides) -> dict:
    payload = {
        "id": "rule-ssh-brute-force-001",
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
