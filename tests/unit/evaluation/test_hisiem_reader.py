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
    HISIEMNotReadyError,
    HISIEMReadinessAuthError,
    HISIEMReadinessContractMismatchError,
    HISIEMUnavailableError,
)
from hisiem_soc_copilot.evaluation.hisiem_reader import (
    HisiemEvaluationReader,
    _alert_in_event_time_scope,
    _alert_processing_not_before,
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


# ---------------------------------------------------------------------------
# §10.1 — readiness probe against /actuator/health (narrow 200+status==UP)
# ---------------------------------------------------------------------------


def _readiness_reader(response: httpx.Response) -> HisiemEvaluationReader:
    """A real HisiemEvaluationReader whose transport returns ONE canned response
    for the readiness endpoint (no network)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return HisiemEvaluationReader(
        tenant_id="tenant-a",
        base_url="http://hisiem.test",
        bearer_token="",
        client=httpx.AsyncClient(transport=transport, base_url="http://hisiem.test"),
    )


async def test_readiness_passes_on_200_up() -> None:
    """200 {status: UP} → readiness() returns None (ready)."""
    reader = _readiness_reader(httpx.Response(200, json={"status": "UP"}))
    try:
        await reader.readiness()  # must not raise
    finally:
        await reader.close()


async def test_readiness_passes_ignoring_component_details() -> None:
    """Readiness must NOT depend on component/db/kafka/flink details — a 200 with
    {status: UP} plus extra detail keys is still ready."""
    reader = _readiness_reader(
        httpx.Response(
            200,
            json={
                "status": "UP",
                "components": {"postgresql": {"status": "DOWN"}},
            },
        )
    )
    try:
        await reader.readiness()  # must not raise
    finally:
        await reader.close()


async def test_readiness_503_down_is_not_ready() -> None:
    """503 (or any status != UP) → typed HISIEMNotReadyError."""
    reader = _readiness_reader(httpx.Response(503, json={"status": "DOWN"}))
    try:
        with pytest.raises(HISIEMNotReadyError):
            await reader.readiness()
    finally:
        await reader.close()


async def test_readiness_200_but_body_not_up_is_not_ready() -> None:
    """200 but body.status != "UP" → typed HISIEMNotReadyError (narrow contract)."""
    reader = _readiness_reader(httpx.Response(200, json={"status": "DOWN"}))
    try:
        with pytest.raises(HISIEMNotReadyError):
            await reader.readiness()
    finally:
        await reader.close()


async def test_readiness_404_is_contract_mismatch() -> None:
    """404 (endpoint absent) → typed HISIEMReadinessContractMismatchError, NOT a
    generic unreachable/timeout."""
    reader = _readiness_reader(httpx.Response(404, json={}))
    try:
        with pytest.raises(HISIEMReadinessContractMismatchError):
            await reader.readiness()
    finally:
        await reader.close()


async def test_readiness_401_is_auth_error() -> None:
    reader = _readiness_reader(httpx.Response(401, json={}))
    try:
        with pytest.raises(HISIEMReadinessAuthError):
            await reader.readiness()
    finally:
        await reader.close()


async def test_readiness_403_is_auth_error() -> None:
    reader = _readiness_reader(httpx.Response(403, json={}))
    try:
        with pytest.raises(HISIEMReadinessAuthError):
            await reader.readiness()
    finally:
        await reader.close()


async def test_readiness_transport_error_is_unavailable() -> None:
    """Connection refused / timeout → typed HISIEMUnavailableError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    reader = HisiemEvaluationReader(
        tenant_id="tenant-a",
        base_url="http://hisiem.test",
        bearer_token="",
        client=httpx.AsyncClient(transport=transport, base_url="http://hisiem.test"),
    )
    try:
        with pytest.raises(HISIEMUnavailableError):
            await reader.readiness()
    finally:
        await reader.close()


async def test_readiness_hits_actuator_health_not_data_health() -> None:
    """The readiness probe must call /actuator/health and NEVER /api/data-health
    (heavy business health is not the reader readiness contract)."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, json={"status": "UP"})

    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    reader = HisiemEvaluationReader(
        tenant_id="tenant-a",
        base_url="http://hisiem.test",
        bearer_token="",
        client=httpx.AsyncClient(transport=transport, base_url="http://hisiem.test"),
    )
    try:
        await reader.readiness()
    finally:
        await reader.close()
    assert requested == ["/actuator/health"]
    assert "/api/data-health" not in requested


async def test_ping_swallows_typed_readiness_failures() -> None:
    """ping() (bool compat) returns False on a typed readiness failure instead of
    raising."""
    reader = _readiness_reader(httpx.Response(404, json={}))
    try:
        assert await reader.ping() is False
    finally:
        await reader.close()
    up_reader = _readiness_reader(httpx.Response(200, json={"status": "UP"}))
    try:
        assert await up_reader.ping() is True
    finally:
        await up_reader.close()


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
    created_at: str | None = None,
    timestamp: str | None = None,
    event_count: int = 5,
    status: str = "OPEN",
) -> dict:
    """One list-endpoint alert item (map_found_alert envelope shape).

    Mirrors the real HISIEM alert document: top-level ``@timestamp`` (event-time
    window end) is distinct from ``alert.created_at`` (processing-time). Callers
    may set either independently to drive the event-time/freshness split tests.
    """
    payload: dict[str, object] = {
        "_id": address_id,
        "_index": "siem-alerts-gp01",
        "alert": {
            "id": f"biz-{address_id}",
            "rule_id": rule_id,
            "rule_name": "SSH Brute Force",
            "deduplicated_count": event_count,
            "status": status,
        },
        "source": {"ip": _ATTACK_SOURCE},
    }
    if created_at is not None:
        cast_alert = payload["alert"]
        assert isinstance(cast_alert, dict)
        cast_alert["created_at"] = created_at
    if timestamp is not None:
        payload["@timestamp"] = timestamp
    return payload


def _alert_detail_payload(payload: dict) -> dict:
    """The alert-detail GET returns the SAME address; map_found_alert reads the
    top-level ``_id`` and flattens under ``alert.*`` — reuse the list payload."""
    return payload


def test_event_time_scope_binds_only_on_alert_timestamp() -> None:
    """Event-time binding uses the alert ``@timestamp`` (window end), NEVER
    ``created_at`` (processing time)."""
    scope_from = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    scope_to = datetime(2026, 9, 5, 12, 6, 0, tzinfo=UTC)
    # In-scope @timestamp (window end inside the F1..W1 bounding box) is selected
    # EVEN when created_at is recent (the blocker regression).
    recent = map_found_alert(
        _alert_payload(
            address_id="es-in",
            timestamp="2026-09-05T12:03:00Z",
            created_at="2026-09-05T12:05:59Z",
        )
    )
    assert recent is not None
    assert _alert_in_event_time_scope(recent, scope_from, scope_to) is True
    # Same-source alert whose @timestamp is OUTSIDE the current-run event-time
    # scope is NOT selected even when created_at sits inside the window.
    old = map_found_alert(
        _alert_payload(
            address_id="es-old",
            timestamp="2026-09-01T09:00:00Z",
            created_at="2026-09-05T12:03:00Z",
        )
    )
    assert old is not None
    assert _alert_in_event_time_scope(old, scope_from, scope_to) is False
    # An unparseable / missing @timestamp is NOT bound (never selected): a missing
    # @timestamp must not be silently substituted with created_at.
    missing_ts = map_found_alert(
        _alert_payload(address_id="es-missing-ts", created_at="2026-09-05T12:03:00Z")
    )
    assert missing_ts is not None
    assert missing_ts.timestamp is None
    assert _alert_in_event_time_scope(missing_ts, scope_from, scope_to) is False
    bad_ts = map_found_alert(
        _alert_payload(
            address_id="es-bad",
            timestamp="not-a-timestamp",
            created_at="2026-09-05T12:03:00Z",
        )
    )
    assert bad_ts is not None
    assert _alert_in_event_time_scope(bad_ts, scope_from, scope_to) is False


def test_processing_freshness_uses_created_at_only_against_run_bound() -> None:
    """created_at (processing-time) is only compared to the frozen run lower bound,
    never to the event-time window."""
    run_bound = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    fresh = map_found_alert(
        _alert_payload(
            address_id="es-fresh",
            timestamp="2026-09-05T12:03:00Z",
            created_at="2026-09-05T12:02:00Z",
        )
    )
    assert fresh is not None
    assert _alert_processing_not_before(fresh, run_bound) is True
    stale = map_found_alert(
        _alert_payload(
            address_id="es-stale",
            timestamp="2026-09-05T12:03:00Z",
            created_at="2026-09-05T11:00:00Z",
        )
    )
    assert stale is not None
    assert _alert_processing_not_before(stale, run_bound) is False
    # Missing created_at cannot be proven fresh.
    no_created = map_found_alert(
        _alert_payload(address_id="es-nc", timestamp="2026-09-05T12:03:00Z")
    )
    assert no_created is not None
    assert _alert_processing_not_before(no_created, run_bound) is False
    # Disabled freshness check passes anything.
    assert _alert_processing_not_before(no_created, None) is True


def test_matches_run_alert_requires_rule_and_attack_entity() -> None:
    own = map_found_alert(
        _alert_payload(address_id="es-own", timestamp="2026-09-05T12:03:00Z")
    )
    assert own is not None
    assert _matches_run_alert(own, _RULE_ID, _ATTACK_SOURCE) is True
    other_rule = map_found_alert(
        _alert_payload(
            address_id="es-x", timestamp="2026-09-05T12:03:00Z", rule_id="rule-other"
        )
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


async def test_alert_candidate_ignores_old_same_source_alert_outside_event_scope() -> None:
    """B (§25): an OLD same-source alert whose ``@timestamp`` is OUTSIDE the
    current-run event-time scope must NOT be selected even when ``created_at`` is
    recent — the poll keeps waiting and raises AlertResolutionTimeout."""
    event_from, event_to = _scope()
    old_alert = _alert_payload(
        address_id="es-old",
        timestamp="2026-09-01T09:00:00Z",  # event-time outside the run scope
        created_at="2026-09-05T12:03:00Z",  # processing-time would look "recent"
    )
    reader = _stub_reader([old_alert])
    try:
        with pytest.raises(AlertResolutionTimeout):
            await reader.wait_for_alert(
                attack_source_ip=_ATTACK_SOURCE,
                event_time_from=event_from,
                event_time_to=event_to,
                deadline=datetime.now(UTC) - timedelta(seconds=1),  # already expired
                interval=0.001,
                stable_reads=3,
            )
    finally:
        await reader.close()


async def test_alert_candidate_accepts_recent_alert_in_event_time_scope() -> None:
    """A (§25) — the blocker regression: F1/W1 event time is several minutes in the
    past, the alert's ``@timestamp`` (window end) is INSIDE the event-time scope,
    and ``created_at`` (processing-time) is NOW. The candidate MUST be accepted."""
    event_from, event_to = _scope()
    in_alert = _alert_payload(
        address_id="es-current",
        timestamp="2026-09-05T12:03:00Z",  # event-time inside the F1/W1 scope
        created_at="2026-09-05T12:05:59Z",  # processing-time == now-ish
    )
    reader = _stub_reader([in_alert])
    try:
        resolved = await reader.wait_for_alert(
            attack_source_ip=_ATTACK_SOURCE,
            event_time_from=event_from,
            event_time_to=event_to,
            deadline=datetime.now(UTC) + timedelta(seconds=10),
            interval=0.001,
            stable_reads=2,
        )
        assert resolved.address_id == "es-current"
        assert resolved.rule_id == _RULE_ID
        assert resolved.timestamp == "2026-09-05T12:03:00Z"
    finally:
        await reader.close()


async def test_alert_missing_event_timestamp_is_not_selectable() -> None:
    """C (§25): an alert with NO ``@timestamp`` must NOT pass event-time binding —
    the parser must not silently substitute ``created_at``. The poll keeps waiting
    and times out rather than selecting an event-time-unprovable alert."""
    event_from, event_to = _scope()
    no_ts = _alert_payload(
        address_id="es-no-ts", created_at="2026-09-05T12:03:00Z"  # no @timestamp
    )
    reader = _stub_reader([no_ts])
    try:
        with pytest.raises(AlertResolutionTimeout):
            await reader.wait_for_alert(
                attack_source_ip=_ATTACK_SOURCE,
                event_time_from=event_from,
                event_time_to=event_to,
                deadline=datetime.now(UTC) - timedelta(seconds=1),
                interval=0.001,
                stable_reads=2,
            )
    finally:
        await reader.close()


async def test_alert_detail_drift_outside_event_scope_is_rejected() -> None:
    """D (§25): a list item whose ``@timestamp`` is in scope but whose DETAIL read
    by the real ``_id`` returns an ``@timestamp`` OUTSIDE the scope must be
    rejected/reset — the authoritative detail re-read wins."""
    event_from, event_to = _scope()
    # list item: in-scope @timestamp; detail: out-of-scope @timestamp.
    list_item = _alert_payload(
        address_id="es-drift",
        timestamp="2026-09-05T12:03:00Z",
        created_at="2026-09-05T12:04:00Z",
    )
    drifted_detail = _alert_payload(
        address_id="es-drift",
        timestamp="2026-09-05T09:00:00Z",  # OUTSIDE event-time scope
        created_at="2026-09-05T12:04:00Z",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/alerts":
            return httpx.Response(200, json={"items": [list_item]})
        return httpx.Response(200, json=_alert_detail_payload(drifted_detail))

    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    reader = HisiemEvaluationReader(
        tenant_id="tenant-a",
        base_url="http://hisiem.test",
        bearer_token="",
        client=httpx.AsyncClient(transport=transport, base_url="http://hisiem.test"),
    )
    try:
        with pytest.raises(AlertResolutionTimeout):
            await reader.wait_for_alert(
                attack_source_ip=_ATTACK_SOURCE,
                event_time_from=event_from,
                event_time_to=event_to,
                deadline=datetime.now(UTC) - timedelta(seconds=1),
                interval=0.001,
                stable_reads=2,
            )
    finally:
        await reader.close()


async def test_alert_created_at_older_than_run_processing_bound_is_rejected() -> None:
    """E (§25): with processing freshness enabled, an alert whose ``@timestamp`` is
    in event-time scope but whose ``created_at`` predates the frozen run processing
    lower bound is rejected as stale (never silently bound by event time)."""
    event_from, event_to = _scope()
    run_bound = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    stale = _alert_payload(
        address_id="es-stale",
        timestamp="2026-09-05T12:03:00Z",  # event-time in scope
        created_at="2026-09-05T11:00:00Z",  # created BEFORE the run processing bound
    )
    reader = _stub_reader([stale])
    try:
        with pytest.raises(AlertResolutionTimeout):
            await reader.wait_for_alert(
                attack_source_ip=_ATTACK_SOURCE,
                event_time_from=event_from,
                event_time_to=event_to,
                deadline=datetime.now(UTC) - timedelta(seconds=1),
                interval=0.001,
                stable_reads=2,
                processing_time_not_before=run_bound,
            )
    finally:
        await reader.close()


async def test_alert_two_in_event_scope_same_source_is_ambiguous() -> None:
    """Two alerts INSIDE the event-time scope for the SAME source → the reader MUST
    raise AmbiguousSourceAlertError — ambiguity is never resolved by newest/risk."""
    event_from, event_to = _scope()
    reader = _stub_reader(
        [
            _alert_payload(address_id="es-c1", timestamp="2026-09-05T12:03:00Z"),
            _alert_payload(address_id="es-c2", timestamp="2026-09-05T12:04:00Z"),
        ]
    )
    try:
        with pytest.raises(AmbiguousSourceAlertError):
            await reader.wait_for_alert(
                attack_source_ip=_ATTACK_SOURCE,
                event_time_from=event_from,
                event_time_to=event_to,
                deadline=datetime.now(UTC) + timedelta(seconds=10),
                interval=0.001,
                stable_reads=3,
            )
    finally:
        await reader.close()
