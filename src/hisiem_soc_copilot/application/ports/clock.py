"""Clock port — keeps time access injectable for tests."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class ClockPort(Protocol):
    def utc_now(self) -> datetime: ...


class SystemClock:
    """Real clock adapter (default)."""

    def utc_now(self) -> datetime:
        from ...domain.shared.identifiers import utc_now

        return utc_now()
