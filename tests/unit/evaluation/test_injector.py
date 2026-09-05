"""Injection-safety tests for the TCP syslog injector (E1-B.3 §11, §12).

Offline: a local asyncio TCP server (loopback, never the real SSH input) and a
monkeypatched ``asyncio.open_connection``. Verifies the fixed InjectionAttempt
fields + write_status vocabulary, the §12 no-retry rule when a peer closes
immediately (exactly ONE write attempt), and the bounded connection-error path.
"""

from __future__ import annotations

import asyncio

from hisiem_soc_copilot.evaluation.contracts import RenderedEvent
from hisiem_soc_copilot.evaluation.injector import (
    WRITE_STATUS_ACCEPTED,
    WRITE_STATUS_CONNECTION_ERROR,
    WRITE_STATUS_INDETERMINATE,
    WRITE_STATUS_REJECTED,
    TcpSyslogEventInjector,
)
from hisiem_soc_copilot.evaluation.syslog import render_event

_WRITE_STATUSES = frozenset(
    {
        WRITE_STATUS_ACCEPTED,
        WRITE_STATUS_INDETERMINATE,
        WRITE_STATUS_REJECTED,
        WRITE_STATUS_CONNECTION_ERROR,
    }
)

_FIELDS = frozenset(
    {"logical_role", "attempted_at", "payload_sha256", "socket_target", "write_status"}
)


def _rendered(role: str = "F1") -> RenderedEvent:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    return render_event(
        role=role,
        action="authentication_failure",
        outcome="failure",
        host_name="app-test",
        source_ip="198.18.0.5",
        user_name="svc_test",
        wall_clock=datetime(2026, 9, 5, 10, 30, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
        - timedelta(minutes=10),
        process_id=1234,
    )


class _LineRecorder:
    """Peer that records every line and (optionally) closes immediately."""

    def __init__(self, *, close_after_first: bool) -> None:
        self.close_after_first = close_after_first
        self.received: list[bytes] = []

    async def __call__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            line = await reader.readline()
            if not line:
                break
            self.received.append(line)
            if self.close_after_first:
                break  # simulate an immediate peer close (EOF) after the frame
        writer.close()
        await writer.wait_closed()


async def test_inject_accepted_returns_fixed_fields(tmp_path) -> None:
    recorder = _LineRecorder(close_after_first=False)
    server = await asyncio.start_server(recorder, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    injector = TcpSyslogEventInjector("127.0.0.1", port, write_timeout=1.0)
    try:
        attempt = await injector.inject(_rendered())
    finally:
        server.close()
        await server.wait_closed()
    assert set(attempt.__dataclass_fields__) == _FIELDS
    assert attempt.logical_role == "F1"
    assert attempt.socket_target == f"127.0.0.1:{port}"
    assert attempt.write_status in _WRITE_STATUSES
    assert attempt.write_status == WRITE_STATUS_ACCEPTED
    assert attempt.payload_sha256 == _rendered().payload_sha256
    assert attempt.attempted_at  # RFC3339-ish instant recorded


async def test_peer_close_is_indeterminate_and_never_retried(monkeypatch) -> None:
    """A peer that closes immediately yields write_status=indeterminate and the
    injector must NEVER auto-retry: assert exactly ONE write attempt occurred."""
    recorder = _LineRecorder(close_after_first=True)
    server = await asyncio.start_server(recorder, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    open_calls: list[str] = []
    real_open = asyncio.open_connection

    async def counting_open(host: str, port: int, **kwargs):
        open_calls.append(f"{host}:{port}")
        return await real_open(host, port, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", counting_open)
    injector = TcpSyslogEventInjector("127.0.0.1", port, write_timeout=1.0)
    try:
        attempt = await injector.inject(_rendered())
    finally:
        server.close()
        await server.wait_closed()
    assert attempt.write_status == WRITE_STATUS_INDETERMINATE
    assert len(open_calls) == 1  # exactly ONE write attempt, no auto-resend
    assert len(recorder.received) == 1


class _DrainFailingWriter:
    """StreamWriter stub whose drain() always fails (partial write, §12)."""

    def write(self, data: bytes) -> None:
        del data

    async def drain(self) -> None:
        raise OSError("simulated partial write")

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


class _SilentReader:
    """StreamReader stub — never reached when drain fails first."""

    async def read(self, n: int) -> bytes:
        del n
        return b""


async def test_write_drain_failure_is_indeterminate() -> None:
    """A partial/timed-out write yields write_status=indeterminate (§12)."""
    injector = TcpSyslogEventInjector("127.0.0.1", 5007)
    event = _rendered()
    wire = event.line.encode("utf-8") + b"\n"
    attempt = await injector._write_and_probe(
        event, _DrainFailingWriter(), _SilentReader(), wire
    )
    assert attempt.write_status == WRITE_STATUS_INDETERMINATE


async def test_connection_refused_is_connection_error(monkeypatch) -> None:
    async def refused(host: str, port: int, **kwargs):
        raise ConnectionRefusedError("nothing listening")

    monkeypatch.setattr(asyncio, "open_connection", refused)
    injector = TcpSyslogEventInjector("127.0.0.1", 1, connect_timeout=0.2)
    attempt = await injector.inject(_rendered())
    assert attempt.write_status == WRITE_STATUS_CONNECTION_ERROR
    assert attempt.payload_sha256 == _rendered().payload_sha256
