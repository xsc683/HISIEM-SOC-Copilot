"""Real OpenSSH-style syslog line renderer for GP-01 (E1-B.3 §6, §7, §11).

Each rendered line MUST match the SIEM ``ssh-auth`` parser grok::

    %{SYSLOGTIMESTAMP} %{HOSTNAME:host.name}
    sshd.*(Failed|Accepted) password for %{USERNAME:user.name}
    from %{IP:source.ip}

The timestamp uses the classic year-less syslog month-day form the grok expects
(e.g. ``Sep  5`` — two spaces before a single-digit day). ``process.pid`` is not
captured by the parser, so any plausible integer is acceptable.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .contracts import GP01_SYSLOG_TIMEZONE, RenderedEvent, sha256_hex

_SYSLOG_TIMEZONE = ZoneInfo(GP01_SYSLOG_TIMEZONE)

# Classic RFC 3164 English month abbreviations (locale-independent).
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_syslog_timestamp(dt: datetime) -> str:
    """Format a timezone-aware datetime as ``Mon dd hh:mm:ss`` (year-less).

    Single-digit days use TWO spaces (``Sep  5``) per the classic form; the
    ``%{SYSLOGTIMESTAMP}`` grok accepts both, but the two-space form is what the
    real parser sees. Locale-independent month abbreviations.
    """
    if dt.tzinfo is None:
        raise ValueError("dt must be timezone-aware")
    local = dt.astimezone(_SYSLOG_TIMEZONE)
    day = local.day
    day_field = f"{day:2d}" if day >= 10 else f" {day}"
    stamp = f"{local.hour:02d}:{local.minute:02d}:{local.second:02d}"
    return f"{_MONTHS[local.month - 1]} {day_field} {stamp}"


def _verb(action: str) -> str:
    """Map a logical event.action onto the OpenSSH syslog verb."""
    if action == "authentication_failure":
        return "Failed"
    if action == "authentication_success":
        return "Accepted"
    raise ValueError(f"unsupported action for ssh-auth syslog rendering: {action!r}")


def render_syslog_line(
    *,
    action: str,
    host_name: str,
    source_ip: str,
    user_name: str,
    wall_clock: datetime,
    process_id: int,
    port: int = 22,
) -> str:
    """Render the exact syslog bytes for one sshd authentication event.

    ``wall_clock`` is the Asia/Shanghai local instant produced by the time plan;
    the returned line is what gets written to the TCP socket.
    """
    if wall_clock.tzinfo is None:
        raise ValueError("wall_clock must be timezone-aware")
    ts = format_syslog_timestamp(wall_clock)
    verb = _verb(action)
    return (
        f"{ts} {host_name} sshd[{process_id}]: {verb} password for "
        f"{user_name} from {source_ip} port {port} ssh2"
    )


def render_event(
    *,
    role: str,
    action: str,
    outcome: str,
    host_name: str,
    source_ip: str,
    user_name: str,
    wall_clock: datetime,
    process_id: int,
    port: int = 22,
) -> RenderedEvent:
    """Bind one logical role to a rendered syslog line (E1-B.3 §7, §11).

    ``payload_sha256`` is the SHA-256 of the exact line string. ``wall_second``
    is the epoch second of the wall-clock instant.
    """
    if wall_clock.tzinfo is None:
        raise ValueError("wall_clock must be timezone-aware")
    line = render_syslog_line(
        action=action,
        host_name=host_name,
        source_ip=source_ip,
        user_name=user_name,
        wall_clock=wall_clock,
        process_id=process_id,
        port=port,
    )
    return RenderedEvent(
        role=role,
        action=action,
        outcome=outcome,
        host_name=host_name,
        source_ip=source_ip,
        user_name=user_name,
        wall_second=int(wall_clock.timestamp()),
        line=line,
        payload_sha256=sha256_hex(line),
    )


__all__ = [
    "format_syslog_timestamp",
    "render_event",
    "render_syslog_line",
]
