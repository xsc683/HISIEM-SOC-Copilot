"""Fake HISIEM port returning realistic SSH brute-force data for graph tests."""

from __future__ import annotations

from hisiem_soc_copilot.application.ports.hisiem import (
    DetectionRuleContext,
    EventSearchResult,
    HisiemAlertData,
    HisiemPort,
    LogEventHit,
)


class FakeHisiem(HisiemPort):
    """A scripted HISIEM returning SSH brute-force alert + events.

    Mirrors the real alert-detail shape (flat ``alert.*`` / promoted fields) and
    log-search items (``_id`` + ``_index`` + event fields).
    """

    def __init__(self, *, alert_id: str = "ssh-bruteforce-alert-1") -> None:
        self.alert_id = alert_id
        self._rule_id = "ssh_brute_force"
        self.calls: list[str] = []

    async def close(self) -> None:
        # Fake adapter owns no client; the container teardown calls close() on the
        # hisiem adapter (which may be this fake in integration tests).
        return None

    async def get_alert(
        self, *, tenant_id: str, alert_id: str
    ) -> HisiemAlertData | None:
        self.calls.append(f"get_alert:{alert_id}")
        if alert_id != self.alert_id:
            return None
        return HisiemAlertData(
            alert_id=self.alert_id,
            tenant_id=tenant_id,
            alert_uuid="alert-uuid-1",
            rule_id=self._rule_id,
            rule_name="SSH Brute Force",
            rule_type="bruteforce",
            severity="high",
            description="Possible SSH brute force from a single source",
            status="open",
            detected_at="2026-09-01T10:00:00Z",
            risk_score=85.0,
            source_ip="203.0.113.9",
            user_name="root",
            host_name="web-01",
            event_category="authentication",
            event_action="login_failure",
            log_source_id="log-ssh-1",
            rule_tags=["ssh", "brute-force", "t1110"],
            event_count=250,
            raw={},
        )

    async def search_events(
        self,
        *,
        tenant_id: str,
        from_: str,
        to: str,
        conditions: list[dict[str, object]],
        limit: int = 100,
        sort: str = "desc",
    ) -> EventSearchResult:
        self.calls.append("search_events")
        op = ""
        for c in conditions:
            if c.get("operator") == "is":
                op = str(c.get("value"))
        # The scripted answer depends on what the investigation asked for.
        if "success" in op.lower() or "success" in str(conditions):
            items = [
                self._hit("evt-succ-1", "authentication_success", "2026-09-01T10:02:11Z"),
            ]
        else:
            items = [
                self._hit("evt-fail-1", "authentication_failure", "2026-09-01T09:58:12Z"),
                self._hit("evt-fail-2", "authentication_failure", "2026-09-01T09:58:55Z"),
                self._hit("evt-fail-3", "authentication_failure", "2026-09-01T09:59:31Z"),
            ]
        return EventSearchResult(
            items=items,
            total=len(items),
            returned=len(items),
            from_=from_,
            to=to,
            truncated=False,
        )

    async def get_detection_rule(
        self, *, tenant_id: str, rule_id: str
    ) -> DetectionRuleContext | None:
        self.calls.append(f"get_detection_rule:{rule_id}")
        if rule_id != self._rule_id:
            return None
        return DetectionRuleContext(
            rule_id=self._rule_id,
            name="SSH Brute Force",
            category="credential-access",
            rule_type="threshold",
            severity="high",
            enabled=True,
            status="enabled",
            version="1",
            tags=["ssh", "brute-force", "t1110"],
            description="Detects many failed SSH logins from one source",
            logic_summary="count(user.name failures) > 5 in 10m from same source.ip",
        )

    def _hit(self, doc: str, action: str, ts: str) -> LogEventHit:
        return LogEventHit(
            document_id=doc,
            index="siem-events-2026.09.01",
            timestamp=ts,
            event_category="authentication",
            event_action=action,
            source_ip="203.0.113.9",
            user_name="root",
            host_name="web-01",
            log_source_id="log-ssh-1",
        )
