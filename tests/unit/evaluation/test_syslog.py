"""Unit tests for the real OpenSSH-style syslog renderer (E1-B.3 §6, §7, §11).

Offline: pure string rendering against the SIEM ``ssh-auth`` parser grok
(``Failed|Accepted password for <user> from <ip>``, host, ``sshd[pid]``) and the
classic year-less two-space single-digit-day timestamp.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from hisiem_soc_copilot.evaluation.syslog import (
    format_syslog_timestamp,
    render_event,
    render_syslog_line,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_HOST = "app-a1b2c3d4"
_SOURCE = "198.18.7.9"
_USER = "svc01deadbeef"
_PID = 4321


def _shanghai(hour: int, minute: int = 0, second: int = 0, *, month: int = 9, day: int = 5):
    return datetime(2026, month, day, hour, minute, second, tzinfo=_SHANGHAI)


def test_failure_line_matches_ssh_auth_grok() -> None:
    line = render_syslog_line(
        action="authentication_failure",
        host_name=_HOST,
        source_ip=_SOURCE,
        user_name=_USER,
        wall_clock=_shanghai(10, 30, 15),
        process_id=_PID,
    )
    assert "Failed password for" in line
    assert f"{_USER} from {_SOURCE}" in line
    assert _HOST in line
    assert f"sshd[{_PID}]" in line


def test_success_line_matches_ssh_auth_grok() -> None:
    line = render_syslog_line(
        action="authentication_success",
        host_name=_HOST,
        source_ip=_SOURCE,
        user_name=_USER,
        wall_clock=_shanghai(10, 31, 5),
        process_id=_PID,
    )
    assert "Accepted password for" in line
    assert f"{_USER} from {_SOURCE}" in line
    assert _HOST in line
    assert f"sshd[{_PID}]" in line


def test_render_event_binds_role_and_hashes_payload() -> None:
    event = render_event(
        role="F1",
        action="authentication_failure",
        outcome="failure",
        host_name=_HOST,
        source_ip=_SOURCE,
        user_name=_USER,
        wall_clock=_shanghai(10, 30, 15),
        process_id=_PID,
    )
    assert event.role == "F1"
    assert event.outcome == "failure"
    assert event.source_ip == _SOURCE
    assert event.line.startswith("Sep  5 10:30:15")
    assert len(event.payload_sha256) == 64


def test_format_syslog_timestamp_uses_two_spaces_for_single_digit_day() -> None:
    assert format_syslog_timestamp(_shanghai(9, day=5)) == "Sep  5 09:00:00"
    assert format_syslog_timestamp(_shanghai(9, day=15)) == "Sep 15 09:00:00"


def test_format_syslog_timestamp_normalizes_utc_to_shanghai() -> None:
    utc = datetime(2026, 9, 5, 1, 0, 0, tzinfo=UTC)  # 09:00 Shanghai
    assert format_syslog_timestamp(utc) == "Sep  5 09:00:00"


def test_format_syslog_timestamp_naive_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        format_syslog_timestamp(datetime(2026, 9, 5, 1, 0, 0))
