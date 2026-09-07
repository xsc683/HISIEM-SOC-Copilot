"""Copilot DB target guard + Alembic migration automation (E1-C0 §10, §21).

Fail-closed guard: before any migration, the configured Copilot database URL
MUST point at Copilot's own PostgreSQL — host 127.0.0.1 (localhost) on the
reserved Copilot port **5433**, database **copilot**. A URL that points at
HISIEM's PostgreSQL (5432) or any other database is refused so Alembic can
never accidentally run against HISIEM's schema.

Schema ownership is respected:

    copilot              -> Alembic-owned
    langgraph_checkpoint -> LangGraph-owned (never migrated here)

The database URL is read from ``COPILOT_DATABASE_URL`` (env), not invented here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url

# Reserved local-dev port contract (E1-C0 §5).
COPILOT_DB_PORT = 5433
HISIEM_DB_PORT = 5432
COPILOT_DB_NAME = "copilot"

_ENV_URL = "COPILOT_DATABASE_URL"


class CopilotDbGuardError(Exception):
    """Base error for Copilot DB guard/migration failures."""


class WrongDatabaseTargetError(CopilotDbGuardError):
    """The configured DB URL does not point at Copilot's own PostgreSQL."""


class MigrationFailedError(CopilotDbGuardError):
    """alembic current/upgrade/check did not succeed."""


@dataclass(frozen=True)
class DbTarget:
    """Normalized view of the configured Copilot database target."""

    host: str
    port: int
    database: str
    username: str

    @property
    def is_local(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")


def _parse_url(url: str | None) -> DbTarget:
    if not url:
        raise WrongDatabaseTargetError(
            f"{_ENV_URL} is not set; refusing to guess a Copilot DB target"
        )
    try:
        parsed = make_url(url)
    except Exception as exc:  # pragma: no cover - defensive
        raise WrongDatabaseTargetError(f"cannot parse {_ENV_URL}: {exc}") from exc
    return DbTarget(
        host=str(parsed.host or ""),
        port=int(parsed.port or 0),
        database=str(parsed.database or ""),
        username=str(parsed.username or ""),
    )


def validate_copilot_db_url(url: str | None = None) -> DbTarget:
    """Fail-closed check that the URL is Copilot's own PostgreSQL.

    Raises WrongDatabaseTargetError unless the target is localhost:5433/copilot
    (host-local loopback on the reserved Copilot port). A URL pointing at
    HISIEM's 5432, or any non-local host, or a non-copilot database, is refused.
    """
    target = _parse_url(url if url is not None else os.environ.get(_ENV_URL))

    if not target.is_local:
        raise WrongDatabaseTargetError(
            f"Copilot DB host must be local loopback (127.0.0.1/localhost); "
            f"got {target.host!r}. Never point Alembic at a remote or HISIEM host."
        )
    if target.port == HISIEM_DB_PORT:
        raise WrongDatabaseTargetError(
            f"Copilot DB port is {HISIEM_DB_PORT} (HISIEM's PostgreSQL). "
            f"The reserved Copilot port is {COPILOT_DB_PORT}; refusing to run "
            "Alembic against HISIEM's database."
        )
    if target.port != COPILOT_DB_PORT:
        raise WrongDatabaseTargetError(
            f"Copilot DB port is {target.port}; expected {COPILOT_DB_PORT} "
            "(the reserved Copilot PostgreSQL port). Refusing to guess."
        )
    if target.database != COPILOT_DB_NAME:
        raise WrongDatabaseTargetError(
            f"Copilot DB name is {target.database!r}; expected "
            f"{COPILOT_DB_NAME!r}. Refusing to run Alembic on the wrong database."
        )
    return target


def _repo_root() -> Path:
    """The repository root (where alembic.ini lives)."""
    return Path(__file__).resolve().parents[2]


def _run_alembic(env: dict[str, str], *args: str) -> None:
    """Run one alembic subcommand in the repo root with the guarded env."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_repo_root(),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MigrationFailedError(
            f"alembic {' '.join(args)} failed (exit {result.returncode}):\n"
            f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
        )


def migrate_copilot_db(database_url: str | None = None, *, check: bool = True) -> None:
    """Validate the Copilot DB target, then apply Alembic migrations.

    Runs ``alembic current`` (report head), ``alembic upgrade head``, and
    (by default) ``alembic check`` (drift check). The langgraph_checkpoint
    schema is never touched here (LangGraph owns it at runtime).

    Raises CopilotDbGuardError if the target is wrong or a migration step fails.
    """
    target = validate_copilot_db_url(database_url)
    url = database_url or os.environ[_ENV_URL]
    # mypy: url is non-empty because validate_copilot_db_url succeeded with it.
    assert url is not None

    env = dict(os.environ)
    env[_ENV_URL] = url

    _run_alembic(env, "current")
    _run_alembic(env, "upgrade", "head")
    if check:
        _run_alembic(env, "check")

    # Surface the guarded target for the launcher's sanitized status line.
    print(
        f"Copilot DB migrated: {target.username}@{target.host}:{target.port}/"
        f"{target.database} (copilot schema, Alembic-owned)"
    )


if __name__ == "__main__":  # pragma: no cover - thin CLI for up-agent.ps1
    try:
        migrate_copilot_db()
    except CopilotDbGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
