"""Tests for the Copilot DB target guard + migration automation (E1-C0 §10, §21).

Offline: verifies the fail-closed URL validation (5433/copilot accepted; 5432,
wrong database, remote host rejected) without touching any real database. The
Alembic subprocess execution is exercised only when a real reachable Copilot DB
is present (skipped otherwise).
"""

from __future__ import annotations

import pytest
from scripts.dev.copilot_db import (
    COPILOT_DB_NAME,
    HISIEM_DB_PORT,
    WrongDatabaseTargetError,
    validate_copilot_db_url,
)

_OK = "postgresql+psycopg://copilot:copilot@127.0.0.1:5433/copilot"


def test_accepts_copilot_localhost_5433_copilot() -> None:
    target = validate_copilot_db_url(_OK)
    assert target.port == 5433
    assert target.database == COPILOT_DB_NAME
    assert target.is_local


def test_accepts_localhost_alias() -> None:
    url = "postgresql+psycopg://copilot:copilot@localhost:5433/copilot"
    assert validate_copilot_db_url(url).is_local


def test_rejects_hisiem_port_5432() -> None:
    url = "postgresql+psycopg://copilot:copilot@127.0.0.1:5432/copilot"
    with pytest.raises(WrongDatabaseTargetError):
        validate_copilot_db_url(url)


def test_rejects_wrong_database() -> None:
    url = "postgresql+psycopg://copilot:copilot@127.0.0.1:5433/siem"
    with pytest.raises(WrongDatabaseTargetError):
        validate_copilot_db_url(url)


def test_rejects_nonlocal_host() -> None:
    url = "postgresql+psycopg://copilot:copilot@10.0.0.5:5433/copilot"
    with pytest.raises(WrongDatabaseTargetError):
        validate_copilot_db_url(url)


def test_rejects_missing_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate from any ambient COPILOT_DATABASE_URL so "no URL configured" truly
    # means the env var is absent (works both when a DB is up and when it is not).
    monkeypatch.delenv("COPILOT_DATABASE_URL", raising=False)
    with pytest.raises(WrongDatabaseTargetError):
        validate_copilot_db_url(None)


def test_rejects_wrong_port() -> None:
    url = "postgresql+psycopg://copilot:copilot@127.0.0.1:5434/copilot"
    with pytest.raises(WrongDatabaseTargetError):
        validate_copilot_db_url(url)


def test_hisiem_port_constant_is_5432() -> None:
    # The core local-dev invariant: 5432 is HISIEM's, never a valid Copilot target.
    assert HISIEM_DB_PORT == 5432
