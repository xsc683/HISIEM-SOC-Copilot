"""Shared domain building blocks.

Pure stdlib only — no framework imports (see python-package-boundary.md §3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4


def new_uuid() -> UUID:
    return uuid4()


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)
