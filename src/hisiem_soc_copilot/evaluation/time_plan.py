"""Watermark-aligned deterministic GP-01 event time plan (E1-B.3 §6).

The plan mirrors the reference HISIEM simulator's verified watermark strategy
(``infra/simulator/brute-force-test.sh``): window rules run on EVENT TIME and a
5-minute sliding window only closes when the Flink watermark passes its boundary.
So besides in-window events, the plan sends a WATERMARK-CONTROL event whose
event-time lies past the NEXT 300-second window boundary — that event advances
Flink's watermark across the real window edge and triggers the alert.

Role-aware time semantics (NOT "all events must be past"):

- ``F1..F5, S1`` are near-now PAST events: same attack source/user/host, five
  ``authentication_failure`` events inside the 5-minute window, ``S1`` strictly
  after the last failure (``authentication_success``). All are strictly before
  ``now`` at plan-build time so the materializer never injects a future
  ground-truth event.
- ``W1`` is the distinct WATERMARK-CONTROL event and is the ONLY role allowed a
  FUTURE event-time. It is placed at ``((epoch_now // 300) + 1) * 300 + 15`` — the
  next 5-minute window boundary plus 15s — to advance Flink's watermark across the
  boundary that closes the detection window containing the failures. Its future
  skew is therefore explicitly bounded (never more than ~5 min + 15 s past now).
- The whole F1..W1 wall-clock span must share ONE local calendar year (the SSH
  syslog form carries no year), guarded exactly as before.

A plan that would straddle a natural calendar year raises
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

# How far back the near-now ground-truth cluster is anchored. F1..F5,S1 must ALL
# be strictly in the past at build time while staying near the current watermark
# so Flink does not treat them as late (reference simulator sends failures at
# NOW-1..NOW-5). The GP-01 internal spacing (F1..F5 10 s apart, S1 20 s after F5)
# needs ~70 s of runway, so F1 sits a short window before now.
ANCHOR_HISTORY_SECONDS = 90

# Relative offsets from the local anchor (Asia/Shanghai wall clock).
F1_OFFSET_SECONDS = 10
FAILURE_SPACING_SECONDS = 10
SUCCESS_GAP_SECONDS = 20  # S1 gap measured after the last failure (F5).
# S1 = F1 + 4*spacing + success gap = 10 + 40 + 20 = anchor + 70s (spec §6). With
# anchor = now - 90s this puts S1 at now - 20s (strictly past, near-now).
S1_OFFSET_SECONDS = 70

# W1 future-skew bound (seconds): W1 may be at most one 300s window boundary ahead
# of now, plus the 15 s advance. Guards the "only W1 may be future" invariant.
W1_MAX_FUTURE_SKEW_SECONDS = 5 * 60 + 15
# The window-advance event sits 15 s past the boundary (reference simulator).
_W1_BOUNDARY_ADVANCE_SECONDS = 15

# Number of consecutive wall-clock-year checks around the plan span.
_YEAR_GUARD_ROLES = GP01_EVENT_ORDER


@dataclass(frozen=True)
class TimePlanOffsets:
    """Recommended anchor/offset constants for one run (E1-B.3 §6)."""

    history_seconds: int = ANCHOR_HISTORY_SECONDS
    f1_seconds: int = F1_OFFSET_SECONDS
    failure_spacing_seconds: int = FAILURE_SPACING_SECONDS
    success_seconds: int = S1_OFFSET_SECONDS
    watermark_boundary_advance_seconds: int = _W1_BOUNDARY_ADVANCE_SECONDS


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


def _next_window_boundary_epoch(now: datetime) -> int:
    """The next 5-minute (300 s) aligned UTC epoch boundary strictly after ``now``.

    Mirrors ``BOUNDARY=$(( (NOW / 300 + 1) * 300 ))`` in the reference simulator:
    the first 300 s boundary that closes a detection window in the future.
    """
    epoch_now = int(now.astimezone(_UTC).timestamp())
    return ((epoch_now // 300) + 1) * 300


def _role_offset_seconds(role: str, *, failure_spacing_seconds: int) -> int:
    """Offset in seconds from the local anchor to ``role``."""
    if role[0] == "F":  # F1..F5: evenly spaced failures after F1.
        ordinal = int(role[1])
        return F1_OFFSET_SECONDS + (ordinal - 1) * failure_spacing_seconds
    if role == "S1":
        # S1 must stay strictly after the last failure whatever the spacing.
        return F1_OFFSET_SECONDS + 4 * failure_spacing_seconds + SUCCESS_GAP_SECONDS
    raise ValueError(f"unknown GP-01 ground-truth role: {role!r}")


def _check_year_boundary(wall: dict[str, datetime]) -> None:
    """Reject a plan whose wall-clock span crosses a natural calendar year.

    All seven roles (F1..W1) must share one local wall-clock year or the plan is
    refused.
    """
    years = {wall[role].year for role in _YEAR_GUARD_ROLES}
    if len(years) != 1:
        raise EventPlanCrossesYearBoundary(
            "GP-01 time plan crosses a natural calendar-year boundary; "
            f"wall-clock years {sorted(years)} — the SSH syslog form carries no "
            "year, so refusal is mandatory rather than guessing parser year "
            "auto-completion"
        )


def _build_ground_truth_wall(
    now: datetime, failure_spacing_seconds: int
) -> tuple[datetime, dict[str, datetime]]:
    """Wall-clock instants for F1..S1 (near-now past), anchored from ``now``.

    Returns ``(anchor_local, wall)`` where ``anchor_local`` is the floored local
    anchor instant used to derive every role offset.
    """
    anchor = _floor_local(now - timedelta(seconds=ANCHOR_HISTORY_SECONDS))
    wall: dict[str, datetime] = {}
    for role in GP01_FAILURE_ROLES + ("S1",):
        offset = _role_offset_seconds(role, failure_spacing_seconds=failure_spacing_seconds)
        wall[role] = anchor + timedelta(seconds=offset)
    return anchor, wall


def _build_watermark_wall(now: datetime) -> dict[str, datetime]:
    """The W1 wall-clock instant: next 300 s boundary + 15 s (may be future)."""
    boundary_epoch = _next_window_boundary_epoch(now)
    boundary_dt = datetime.fromtimestamp(
        boundary_epoch, tz=_UTC
    ).astimezone(SYSLOG_TIMEZONE)
    return {"W1": boundary_dt + timedelta(seconds=_W1_BOUNDARY_ADVANCE_SECONDS)}


def build_event_time_plan(
    now: datetime | None = None,
    *,
    failure_spacing_seconds: int = FAILURE_SPACING_SECONDS,
) -> EventTimePlan:
    """Build the deterministic, watermark-aligned GP-01 time plan.

    ``now`` may be any timezone-aware datetime (defaults to the current UTC
    instant); it is interpreted in Asia/Shanghai so the anchor is always local.

    - ``F1..F5`` sit inside the 5-minute brute-force window and are strictly in
      the past (near-now); ``S1`` follows the last failure and is also strictly
      past.
    - ``W1`` is placed at the NEXT 300 s window boundary + 15 s — which may be in
      the future — so it advances Flink's watermark across the detection-window
      boundary (the reference simulator's strategy). Only W1 may be future.
    - The whole F1..W1 span must share one local wall-clock year, else
      :class:`EventPlanCrossesYearBoundary` is raised.
    """
    if now is None:
        now = datetime.now(_UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if failure_spacing_seconds <= 0:
        raise ValueError("failure_spacing_seconds must be positive")

    anchor, gt_wall = _build_ground_truth_wall(now, failure_spacing_seconds)
    wall = gt_wall
    wall.update(_build_watermark_wall(now))
    _check_year_boundary(wall)

    # Role-aware invariants (spec: F1..S1 must be past; only W1 may be future).
    now_utc = now.astimezone(_UTC)
    for role in GP01_FAILURE_ROLES + ("S1",):
        if wall[role].astimezone(_UTC) >= now_utc:
            raise ValueError(
                f"ground-truth role {role} must be strictly in the past when "
                "injection starts"
            )
    # Ordering + window containment.
    last_failure = max(wall[role].astimezone(_UTC) for role in GP01_FAILURE_ROLES)
    if wall["S1"].astimezone(_UTC) <= last_failure:
        raise ValueError("S1 must occur after the last brute-force failure (F5)")
    if (last_failure - wall["F1"].astimezone(_UTC)) > timedelta(
        minutes=RULE_WINDOW_MINUTES
    ):
        raise ValueError(
            "F1..F5 must all sit inside the configured detection window "
            f"({RULE_WINDOW_MINUTES} minutes)"
        )
    w1_utc = wall["W1"].astimezone(_UTC)
    if w1_utc <= now_utc:
        # W1 is EXPECTED to be future (it advances the watermark); if the boundary
        # math ever lands it in the past the plan cannot close the window.
        raise ValueError(
            "W1 watermark-advance event must be after now to close the detection "
            "window"
        )
    skew = (w1_utc - now_utc).total_seconds()
    if skew > W1_MAX_FUTURE_SKEW_SECONDS:
        raise ValueError(
            "W1 future skew exceeds the bounded window-advance horizon "
            f"({skew:.0f}s > {W1_MAX_FUTURE_SKEW_SECONDS}s)"
        )

    events = {role: dt.astimezone(_UTC) for role, dt in wall.items()}
    return EventTimePlan(
        anchor_local=anchor,
        events=events,
        wall_clock=wall,
        built_at=now_utc,
    )


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
    "W1_MAX_FUTURE_SKEW_SECONDS",
    "build_event_time_plan",
]
