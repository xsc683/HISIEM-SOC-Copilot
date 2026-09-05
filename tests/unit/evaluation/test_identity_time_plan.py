"""Unit tests for deterministic GP-01 run identity + event time plan (E1-B.3 §5, §6).

Offline: pure functions only, deterministic against an explicit ``now``. Covers
run-identity determinism/uniqueness, the ``attack != watermark`` invariant, the
failure-window / S1-ordering / W1-past-close time invariants, W1 entity
separation, and year-boundary rejection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hisiem_soc_copilot.evaluation.contracts import (
    EVENT_PLAN_CROSSES_YEAR_BOUNDARY,
    GP01_FAILURE_ROLES,
    GP01_RULE_WINDOW_MINUTES,
)
from hisiem_soc_copilot.evaluation.identity import (
    derive_event_process_id,
    derive_run_identity,
    derive_watermark_user_name,
)
from hisiem_soc_copilot.evaluation.syslog import render_syslog_line
from hisiem_soc_copilot.evaluation.time_plan import (
    EventPlanCrossesYearBoundary,
    build_event_time_plan,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_FIVE_MIN = timedelta(minutes=GP01_RULE_WINDOW_MINUTES)

_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def test_same_run_id_is_deterministic() -> None:
    a = derive_run_identity("run-abc")
    b = derive_run_identity("run-abc")
    assert a == b
    assert a.run_tag == b.run_tag


def test_different_run_id_yields_distinct_attack_source() -> None:
    a = derive_run_identity("run-abc")
    b = derive_run_identity("run-def")
    assert a.attack_source_ip != b.attack_source_ip


def test_attack_and_watermark_sources_always_differ() -> None:
    for run_id in ("run-1", "run-2", "run-3"):
        identity = derive_run_identity(run_id)
        assert identity.attack_source_ip != identity.watermark_source_ip
    # A pinned attack source still yields a distinct watermark.
    pinned = derive_run_identity("run-x", attack_source_ip="198.18.0.1")
    assert pinned.attack_source_ip == "198.18.0.1"
    assert pinned.watermark_source_ip != "198.18.0.1"


def test_watermark_account_differs_from_attack_account() -> None:
    identity = derive_run_identity("run-abc")
    assert derive_watermark_user_name("run-abc") != identity.user_name


def _plan(now: datetime | None = None):
    return build_event_time_plan(now=now or _NOW)


def test_failures_inside_window_and_past() -> None:
    plan = _plan()
    f1 = plan.events["F1"]
    f5 = plan.events["F5"]
    assert f5 - f1 < _FIVE_MIN  # all failures inside the 5-minute window
    assert plan.max_failure_time() == plan.events["F5"]
    for role in GP01_FAILURE_ROLES:
        assert plan.events[role] < _NOW  # strictly in the past


def test_s1_strictly_after_last_failure() -> None:
    plan = _plan()
    assert plan.success_time() > plan.max_failure_time()
    assert plan.success_time() < _NOW


def test_w1_after_window_close_and_past() -> None:
    plan = _plan()
    window_close = plan.failure_window_start() + _FIVE_MIN
    assert plan.watermark_time() > window_close
    assert plan.watermark_time() < _NOW


def test_wall_clock_is_shanghai_local() -> None:
    plan = _plan()
    anchor_utc = plan.events["F1"]
    assert plan.wall_clock["F1"].utcoffset() == timedelta(hours=8)
    assert plan.wall_clock["F1"] == anchor_utc.astimezone(_SHANGHAI)


def test_all_instants_past_for_every_role() -> None:
    plan = _plan()
    for role in ("F1", "F5", "S1", "W1"):
        assert plan.events[role] < _NOW


def test_w1_rendered_with_distinct_source_and_user() -> None:
    """W1 must carry a DISTINCT source.ip + user vs the attack events (§4, §5)."""
    run_id = "run-abc"
    identity = derive_run_identity(run_id)
    plan = _plan()
    pid = derive_event_process_id(run_id, "F1")

    attack_line = render_syslog_line(
        action="authentication_failure",
        host_name=identity.host_name,
        source_ip=identity.attack_source_ip,
        user_name=identity.user_name,
        wall_clock=plan.wall_clock["F1"],
        process_id=pid,
    )
    w1_line = render_syslog_line(
        action="authentication_failure",
        host_name=identity.host_name,
        source_ip=identity.watermark_source_ip,
        user_name=derive_watermark_user_name(run_id),
        wall_clock=plan.wall_clock["W1"],
        process_id=derive_event_process_id(run_id, "W1"),
    )
    assert identity.watermark_source_ip in w1_line
    assert identity.attack_source_ip in attack_line
    assert identity.attack_source_ip not in w1_line
    assert derive_watermark_user_name(run_id) in w1_line
    assert identity.user_name not in w1_line
    assert w1_line != attack_line


def test_year_boundary_crossing_is_rejected() -> None:
    # 2026-12-31T16:05 UTC = 2027-01-01T00:05 Asia/Shanghai. With a 10-minute
    # history offset the anchor lands 2026-12-31 23:55 local while W1 lands
    # 2027-01-01 00:02 local — the plan spans two wall-clock years → refused.
    straddling_now = datetime(2026, 12, 31, 16, 5, 0, tzinfo=UTC)
    assert straddling_now.astimezone(_SHANGHAI).year == 2027
    with pytest.raises(EventPlanCrossesYearBoundary) as exc:
        _plan(straddling_now)
    assert exc.value.code == EVENT_PLAN_CROSSES_YEAR_BOUNDARY


def test_year_boundary_local_midnight_crossing_is_rejected() -> None:
    # A "now" at local 00:04 on 1 Jan: the 10-minute history offset pushes F1 back
    # to 31 Dec local, so the plan straddles the year and is refused.
    local_midnight_plus = datetime(2027, 1, 1, 0, 4, 0, tzinfo=_SHANGHAI)
    assert local_midnight_plus.astimezone(UTC).year == 2026  # still prior year UTC
    with pytest.raises(EventPlanCrossesYearBoundary):
        _plan(local_midnight_plus)
