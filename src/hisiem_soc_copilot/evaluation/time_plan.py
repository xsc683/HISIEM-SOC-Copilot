"""Deterministic past-bound GP-01 event time plan (E1-B.3 §6).

Produces, per logical role, an RFC3339 UTC instant (what the provider
ingests/returns) and the SAME instant as an ``Asia/Shanghai`` wall clock used to
render the year-less SSH syslog line. The builder is a pure function of ``now``
and never consults the host default timezone.

Every generated event is anchored in the past so none is "in the future" when
injection starts: the watermark that advances the detection window is the latest
event and still lands ``ANCHOR_HISTORY_SECONDS - W1_OFFSET_SECONDS`` (default 3
minutes) before ``now``.

The year-boundary guard is deterministic: pass an explicit ``now`` whose local
wall-clock span [F1, W1] straddles 31 Dec / 1 Jan (e.g. ``now`` a few minutes
after midnight Asia/Shanghai on 1 Jan) and the builder raises
:class:`EventPlanCrossesYearBoundary` whose ``code`` is the committed
``EVENT_PLAN_CROSSES_YEAR_BOUNDARY`` constant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .contracts import (
    EVENT_PLAN_CROSSES_YEAR_BOUNDARY,
    GP01_EVENT_ORDER,
    GP01_FAILURE_ROLES,
    GP01_RULE_THRESHOLD,
    GP01_RULE_WINDOW_MINUTES,
    GP01_SYSLOG_TIMEZONE,
    EvaluationError,
    EventTimePlan,
)

SYSLOG_TIMEZONE = ZoneInfo(GP01_SYSLOG_TIMEZONE)
_UTC = ZoneInfo("UTC")

# Committed detection rule the plan must satisfy (contracts.GP01_RULE_*).
RULE_WINDOW_MINUTES = GP01_RULE_WINDOW_MINUTES
RULE_THRESHOLD = GP01_RULE_THRESHOLD

# Anchor math (E1-B.3 §6). Offsets are relative to the local anchor:
# ``anchor = floor_local(now - ANCHOR_HISTORY_SECONDS)``.
ANCHOR_HISTORY_SECONDS = 600  # 10 min -> W1 still ~3 min in the past.
F1_OFFSET_SECONDS = 10
FAILURE_SPACING_SECONDS = 10
SUCCESS_GAP_SECONDS = 20  # S1 gap measured after the last failure (F5).
# S1 = F1 + 4*spacing + success gap = 10 + 40 + 20 = anchor + 70s (spec §6).
S1_OFFSET_SECONDS = 70
W1_OFFSET_SECONDS = 7 * 60  # past the 5-minute detection-window close.

# Number of consecutive wall-clock-year checks around the plan span.
_YEAR_GUARD_ROLES = GP01_EVENT_ORDER


@dataclass(frozen=True)
class TimePlanOffsets:
    """Recommended anchor/offset constants for one run (E1-B.3 §6)."""

    history_seconds: int = ANCHOR_HISTORY_SECONDS
    f1_seconds: int = F1_OFFSET_SECONDS
    failure_spacing_seconds: int = FAILURE_SPACING_SECONDS
    success_seconds: int = S1_OFFSET_SECONDS
    watermark_seconds: int = W1_OFFSET_SECONDS


class EventPlanCrossesYearBoundary(EvaluationError):
    """Typed failure when the GP-01 plan crosses a calendar-year boundary.

    The SSH syslog form carries no year, so the parser auto-completes it; a plan
    straddling 31 Dec / 1 Jan would be ambiguous and is refused rather than
    guessed.
    """

    code: str = EVENT_PLAN_CROSSES_YEAR_BOUNDARY


def _floor_local(now: datetime) -> datetime:
    """Anchor wall clock: floor ``now`` to the whole second (Asia/Shanghai)."""
    return now.astimezone(SYSLOG_TIMEZONE).replace(microsecond=0)


def _role_offset_seconds(role: str, *, failure_spacing_seconds: int) -> int:
    """Offset in seconds from the local anchor to ``role``."""
    if role[0] == "F":  # F1..F5: evenly spaced failures after F1.
        ordinal = int(role[1])
        return F1_OFFSET_SECONDS + (ordinal - 1) * failure_spacing_seconds
    if role == "S1":
        # S1 must stay strictly after the last failure whatever the spacing.
        return F1_OFFSET_SECONDS + 4 * failure_spacing_seconds + SUCCESS_GAP_SECONDS
    if role == "W1":
        return W1_OFFSET_SECONDS
    raise ValueError(f"unknown GP-01 role: {role!r}")


def _check_year_boundary(wall: dict[str, datetime]) -> None:
    """Reject a plan whose wall-clock span crosses a natural calendar year.

    All seven roles must share one local wall-clock year or the plan is refused.
    """
    years = {wall[role].year for role in _YEAR_GUARD_ROLES}
    if len(years) != 1:
        raise EventPlanCrossesYearBoundary(
            "GP-01 time plan crosses a natural calendar-year boundary; "
            f"wall-clock years {sorted(years)} — the SSH syslog form carries no "
            "year, so refusal is mandatory rather than guessing parser year "
            "auto-completion"
        )


def build_event_time_plan(
    now: datetime | None = None,
    *,
    safe_history_offset_seconds: int = ANCHOR_HISTORY_SECONDS,
    failure_spacing_seconds: int = FAILURE_SPACING_SECONDS,
) -> EventTimePlan:
    """Build the deterministic GP-01 time plan anchored in the past.

    ``now`` may be any timezone-aware datetime (defaults to the current UTC
    instant); it is interpreted in Asia/Shanghai so the anchor is always local.
    F1..F5 sit inside the configured 5-minute brute-force window, S1 follows the
    last failure, and W1 lands past the window-close boundary. The whole plan is
    guaranteed to be in the past when injection starts, and plans that straddle a
    calendar year in local wall clock raise :class:`EventPlanCrossesYearBoundary`
    rather than guess parser year auto-completion.
    """
    if now is None:
        now = datetime.now(_UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if failure_spacing_seconds <= 0:
        raise ValueError("failure_spacing_seconds must be positive")

    anchor = _floor_local(now - timedelta(seconds=safe_history_offset_seconds))
    offsets = {
        role: _role_offset_seconds(role, failure_spacing_seconds=failure_spacing_seconds)
        for role in GP01_EVENT_ORDER
    }
    wall: dict[str, datetime] = {
        role: anchor + timedelta(seconds=offset) for role, offset in offsets.items()
    }
    _check_year_boundary(wall)

    # Ordering + pastness guards: any configuration that breaks a mandatory
    # invariant fails loudly instead of producing an unusable plan.
    last_failure = max(wall[role] for role in GP01_FAILURE_ROLES)
    if wall["S1"] <= last_failure:
        raise ValueError("S1 must occur after the last brute-force failure (F5)")
    window_close = wall["F1"] + timedelta(minutes=RULE_WINDOW_MINUTES)
    if wall["W1"] <= window_close:
        raise ValueError("W1 must occur after the detection-window close boundary")
    if max(wall.values()) >= now:
        raise ValueError("time plan must be fully in the past when injection starts")

    events = {role: dt.astimezone(_UTC) for role, dt in wall.items()}
    return EventTimePlan(anchor_local=anchor, events=events, wall_clock=wall)


__all__ = [
    "ANCHOR_HISTORY_SECONDS",
    "EventPlanCrossesYearBoundary",
    "F1_OFFSET_SECONDS",
    "FAILURE_SPACING_SECONDS",
    "RULE_THRESHOLD",
    "RULE_WINDOW_MINUTES",
    "S1_OFFSET_SECONDS",
    "SUCCESS_GAP_SECONDS",
    "SYSLOG_TIMEZONE",
    "TimePlanOffsets",
    "W1_OFFSET_SECONDS",
    "build_event_time_plan",
]
