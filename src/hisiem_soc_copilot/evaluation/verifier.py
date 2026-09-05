"""Dataset verifier — proves the materialized dataset satisfies every GP-01
invariant and returns the only input the Sealer accepts (E1-B.3 §16, E1-B.4 §2).

A :class:`contracts.VerifiedDataset` is produced ONLY when all mandatory
invariants hold: five failures on the same source/account/host, an S1 success on
the SAME entity strictly after the last failure, a W1 control on a DIFFERENT
source, a real brute-force alert bound to the real rule + attack entity, and the
alert's ``address_id`` equal to the actual HISIEM addressing id. Any violation
raises a typed :class:`DatasetInvariantViolation`; nothing is verified or sealed.

This module is deterministic and performs NO provider/model/DB I/O — it reasons
over the resolved events + alert already obtained from HISIEM.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .contracts import (
    GP01_FAILURE_ROLES,
    GP01_RULE_ID,
    GP01_RULE_THRESHOLD,
    GP01_SEMANTIC_ROLES,
    DatasetInvariantViolation,
    EventTimePlan,
    ResolvedAlert,
    ResolvedEvent,
    RunIdentity,
    ScenarioSpec,
    VerifiedDataset,
)

_GROUND_TRUTH_CONTROL_ROLE = "W1"


@dataclass(frozen=True)
class VerifierResult:
    """Outcome of one verification call (success or the first violation)."""

    verified: bool
    dataset: VerifiedDataset | None = None
    error: str | None = None
    code: str | None = None


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class DatasetVerifier:
    """Deterministic GP-01 dataset invariant verifier (E1-B.3 §16)."""

    def __init__(
        self,
        *,
        scenario: ScenarioSpec,
        run: RunIdentity,
        tenant_id: str,
        time_plan: EventTimePlan,
    ) -> None:
        self._scenario = scenario
        self._run = run
        self._tenant_id = tenant_id
        self._time_plan = time_plan

    def verify(
        self,
        *,
        resolved_events: dict[str, ResolvedEvent],
        source_alert: ResolvedAlert,
        materialized_at: str | None = None,
    ) -> VerifiedDataset:
        """Verify the resolved dataset; raise ``DatasetInvariantViolation`` on the
        first broken invariant, else return an immutable ``VerifiedDataset``."""
        # Missing-first (no KeyError): assert every mandatory semantic role and the
        # W1 control is present BEFORE indexing into the event map.
        missing = [role for role in GP01_SEMANTIC_ROLES if role not in resolved_events]
        if missing:
            self._fail(f"missing resolved semantic events: {missing}")
        if _GROUND_TRUTH_CONTROL_ROLE not in resolved_events:
            self._fail(f"missing resolved watermark control event: {_GROUND_TRUTH_CONTROL_ROLE}")
        events: dict[str, ResolvedEvent] = {
            role: resolved_events[role] for role in GP01_SEMANTIC_ROLES
        }

        self._check_failures([events[role] for role in GP01_FAILURE_ROLES])
        self._check_success(events["S1"], [events[r] for r in GP01_FAILURE_ROLES])
        self._check_watermark(resolved_events)
        self._check_alert(source_alert, events)
        # Addressing invariant (E1-B.3 §16.3): the alert ref address is real.
        if not source_alert.address_id:
            self._fail("source alert address_id is empty; cannot be the real HISIEM addressing id")

        return VerifiedDataset(
            scenario=self._scenario,
            run=self._run,
            tenant_id=self._tenant_id,
            time_plan=self._time_plan,
            source_alert=source_alert,
            resolved_events=dict(resolved_events),
            materialized_at=materialized_at or _iso_now(),
        )

    def _fail(self, message: str) -> None:
        raise DatasetInvariantViolation(message)

    def _fail_role(self, role: str, field: str, actual: object, expected: object) -> None:
        self._fail(f"{role} {field} {actual!r} != expected {expected!r}")

    def _check_failures(self, failures: list[ResolvedEvent]) -> None:
        if len(failures) != 5:
            self._fail(f"expected 5 failures, got {len(failures)}")
        for f in failures:
            if f.event_action != "authentication_failure":
                self._fail_role(f.logical_role, "action", f.event_action, "authentication_failure")
            if f.source_ip != self._run.attack_source_ip:
                self._fail_role(f.logical_role, "source", f.source_ip, self._run.attack_source_ip)
            if f.user_name != self._run.user_name:
                self._fail_role(f.logical_role, "user", f.user_name, self._run.user_name)
            if f.host_name != self._run.host_name:
                self._fail_role(f.logical_role, "host", f.host_name, self._run.host_name)

    def _check_success(
        self, success: ResolvedEvent, failures: list[ResolvedEvent]
    ) -> None:
        if success.event_action != "authentication_success":
            self._fail_role("S1", "action", success.event_action, "authentication_success")
        if success.source_ip != self._run.attack_source_ip:
            self._fail_role("S1", "source", success.source_ip, self._run.attack_source_ip)
        if success.user_name != self._run.user_name:
            self._fail_role("S1", "user", success.user_name, self._run.user_name)
        if success.host_name != self._run.host_name:
            self._fail_role("S1", "host", success.host_name, self._run.host_name)
        # S1 must be strictly after the LAST resolved failure timestamp.
        s1_t = _ts(success.timestamp)
        max_f = max(_ts(f.timestamp) for f in failures)
        if s1_t <= max_f:
            self._fail("S1 timestamp is not after the last failure timestamp")

    def _check_watermark(self, resolved: dict[str, ResolvedEvent]) -> None:
        w1 = resolved.get(_GROUND_TRUTH_CONTROL_ROLE)
        if w1 is None:
            self._fail("W1 watermark event is not resolved")
            raise AssertionError("unreachable")
        if w1.source_ip == self._run.attack_source_ip:
            self._fail("W1 source must differ from the attack source (isolation invariant)")

    def _check_alert(self, alert: ResolvedAlert, events: dict[str, ResolvedEvent]) -> None:
        if alert.rule_id != GP01_RULE_ID:
            self._fail(f"source alert rule {alert.rule_id!r} != expected {GP01_RULE_ID}")
        # The alert entity reflects the attack source (rule keyField = source.ip).
        entity = alert.entity or ""
        if entity != self._run.attack_source_ip:
            self._fail(
                f"source alert entity {entity!r} != attack source {self._run.attack_source_ip!r}"
            )
        # The alert must represent the committed failure threshold (E1-B.3 §16.2):
        # at least GP01_RULE_THRESHOLD failures on one source.
        if alert.event_count < GP01_RULE_THRESHOLD:
            self._fail(
                f"source alert event_count {alert.event_count} < required "
                f"threshold {GP01_RULE_THRESHOLD}"
            )
        # If HISIEM exposes related-event refs, cross-check them against the
        # resolved FAILURE documents ONLY (E1-B.3 §16.2). S1 alone can never prove
        # the brute-force alert, so only F1..F5 overlap is meaningful.
        if alert.related_event_refs:
            failure_refs = {
                f"{events[r].index}:{events[r].document_id}" for r in GP01_FAILURE_ROLES
            }
            related = {f"{r.index}:{r.document_id}" for r in alert.related_event_refs}
            if not related.intersection(failure_refs):
                self._fail(
                    "source alert related-event refs do not overlap any resolved "
                    "GP-01 failure (F1..F5) document; an S1-only overlap cannot "
                    "prove the brute-force alert"
                )


def _ts(value: str) -> float:
    """RFC3339 -> epoch seconds for ordering checks (naive assumed UTC)."""
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def verify_dataset(
    *,
    scenario: ScenarioSpec,
    run: RunIdentity,
    tenant_id: str,
    time_plan: EventTimePlan,
    resolved_events: dict[str, ResolvedEvent],
    source_alert: ResolvedAlert,
    materialized_at: str | None = None,
) -> VerifiedDataset:
    """Convenience wrapper over :class:`DatasetVerifier`."""
    return DatasetVerifier(
        scenario=scenario, run=run, tenant_id=tenant_id, time_plan=time_plan
    ).verify(
        resolved_events=resolved_events,
        source_alert=source_alert,
        materialized_at=materialized_at,
    )


__all__ = ["DatasetVerifier", "VerifierResult", "verify_dataset"]
