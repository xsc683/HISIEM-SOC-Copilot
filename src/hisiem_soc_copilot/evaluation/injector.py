"""Bounded TCP syslog event injector for GP-01 (E1-B.3 §2, §11, §12).

Each rendered event is written to the real SSH TCP log input (host/port with
newline framing). Authority boundary: this module ONLY writes the syslog socket —
it never touches Elasticsearch, Kafka, ``siem-alerts``, or the Copilot DB
(E1-B.3 §2).

§12 non-idempotent rule: a TCP write whose server-side acceptance CANNOT be
proven (timeout, peer closed before reading, partial write, or an otherwise
ambiguous outcome) MUST be recorded with ``write_status="indeterminate"`` and the
run MUST transition to state ``INDETERMINATE`` — the event is NEVER auto-resent.
This prevents an uncertain retry from turning a five-failure sequence into a
six-event sequence. A rerun with the same run_id reconciles and resolves instead
of re-injecting.

Every call returns a bounded, secret-free :class:`InjectionAttempt`; the caller
must inspect ``attempt.write_status`` and treat ``"indeterminate"`` as the §12
barrier (stop the run in INDETERMINATE, do not continue injecting siblings).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Protocol

from .contracts import InjectionAttempt, RenderedEvent

# Canonical write_status tokens (E1-B.3 §11 / InjectionAttempt docstring).
WRITE_STATUS_ACCEPTED = "accepted"
WRITE_STATUS_INDETERMINATE = "indeterminate"
WRITE_STATUS_REJECTED = "rejected"
WRITE_STATUS_CONNECTION_ERROR = "connection_error"

# Grace window after a full drain to detect an immediate peer close (EOF). The
# SIEM SSH input has no acknowledgement protocol, so post-write silence is the
# success signal; the probe is bounded and never blocks the run.
_PROBE_TIMEOUT = 0.05


class EventInjector(Protocol):
    """Boundary: inject one rendered event into the real SSH log input."""

    async def inject(self, event: RenderedEvent) -> InjectionAttempt: ...


class TcpSyslogEventInjector:
    """Write rendered SSH syslog lines to a TCP syslog input (newline framing).

    One NEW socket per event (the SIEM SSH input accepts plain TCP syslog
    frames; a fresh connection avoids any leftover framing/state and keeps each
    attempt's outcome independent). Each attempt is bounded: an explicit connect
    timeout and an explicit all-or-nothing write of the exact ``line`` bytes plus
    a newline, followed by a tiny bounded read probe for an immediate peer close.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        connect_timeout: float = 3.0,
        write_timeout: float = 3.0,
    ) -> None:
        if host is None or host == "":
            raise ValueError("host must be a non-empty string")
        if port <= 0 or port > 65535:
            raise ValueError(f"port must be in 1..65535, got {port}")
        if connect_timeout <= 0 or write_timeout <= 0:
            raise ValueError("connect/write timeouts must be positive")
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._write_timeout = write_timeout

    @property
    def socket_target(self) -> str:
        """The ``host:port`` string recorded on every InjectionAttempt."""
        return f"{self._host}:{self._port}"

    async def inject(self, event: RenderedEvent) -> InjectionAttempt:
        """Inject one rendered event and return the bounded audit record.

        The outcome is carried by ``attempt.write_status``:

        - ``accepted`` — the full payload was flushed to the socket and the peer
          did not close within the probe window (the syslog success signal).
        - ``indeterminate`` — §12: the peer closed before reading, the write was
          partial/timed out, or acceptance cannot otherwise be proven. The run
          MUST go INDETERMINATE and MUST NOT re-send this event.
        - ``rejected`` — reserved for a peer that refused the frame; currently
          never produced (kept for the InjectionAttempt status vocabulary).
        - ``connection_error`` — the connection itself could not be established;
          nothing reached the wire (provably not ambiguous).

        ``attempted_at`` is the instant the write was attempted (RFC3339 UTC),
        not the pre-flight rendering time.
        """
        if event.line == "":
            raise ValueError("refusing to inject an empty rendered line")
        wire = event.line.encode("utf-8") + b"\n"
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._connect_timeout,
            )
        except (TimeoutError, OSError):
            # Connection could not be established — nothing reached the wire.
            # Provably not ambiguous, so this is a bounded connection_error audit,
            # not an INDETERMINATE payload.
            return self._attempt(event, WRITE_STATUS_CONNECTION_ERROR)
        try:
            return await self._write_and_probe(event, writer, reader, wire)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, RuntimeError):
                pass

    async def _write_and_probe(
        self,
        event: RenderedEvent,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
        wire: bytes,
    ) -> InjectionAttempt:
        """Drain the write then probe briefly for an immediate peer close."""
        try:
            writer.write(wire)
            await asyncio.wait_for(writer.drain(), timeout=self._write_timeout)
        except (TimeoutError, OSError):
            # drain failed or timed out → partial/unproven delivery (§12).
            return self._attempt(event, WRITE_STATUS_INDETERMINATE)
        try:
            # Bounded probe (never an unbounded read): detect a peer that closed
            # right after our frame. Silence is the syslog success signal.
            data = await asyncio.wait_for(reader.read(1), timeout=_PROBE_TIMEOUT)
        except (TimeoutError, OSError):
            # No byte within the grace window: full flush + silent peer = accepted.
            return self._attempt(event, WRITE_STATUS_ACCEPTED)
        if data == b"":
            # Peer closed without acknowledging our frame — we cannot prove it was
            # consumed, so §12 forbids treating this as accepted (or re-sending).
            return self._attempt(event, WRITE_STATUS_INDETERMINATE)
        # An unexpected byte arrived after a full drain; the frame is on the wire.
        return self._attempt(event, WRITE_STATUS_ACCEPTED)

    def _attempt(self, event: RenderedEvent, write_status: str) -> InjectionAttempt:
        """Build a bounded, secret-free InjectionAttempt for the run ledger."""
        return InjectionAttempt(
            logical_role=event.role,
            attempted_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            payload_sha256=event.payload_sha256,
            socket_target=self.socket_target,
            write_status=write_status,
        )


__all__ = [
    "EventInjector",
    "TcpSyslogEventInjector",
    "WRITE_STATUS_ACCEPTED",
    "WRITE_STATUS_CONNECTION_ERROR",
    "WRITE_STATUS_INDETERMINATE",
    "WRITE_STATUS_REJECTED",
]
