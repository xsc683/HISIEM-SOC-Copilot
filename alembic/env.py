"""Alembic environment for the ``copilot`` schema.

Owns migrations for ``copilot.*`` only. The ``langgraph_checkpoint`` schema is
managed by LangGraph itself and is explicitly excluded from autogenerate.
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from hisiem_soc_copilot.config import get_settings
from hisiem_soc_copilot.infrastructure.persistence import orm  # noqa: F401  (register tables)
from hisiem_soc_copilot.infrastructure.persistence.database import (
    COPILOT_SCHEMA,
    _append_search_path,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Resolve the copilot database URL from typed settings at runtime. Pin
# ``search_path`` to the copilot schema so the Alembic version table and any
# unqualified internal statements land in ``copilot`` while the (explicitly
# schema-qualified) ORM tables stay ``copilot.*`` regardless of connection state.
settings = get_settings()
copilot_url = _append_search_path(settings.database.database_url, COPILOT_SCHEMA)
# configparser interpolation treats % specially; escape as %% so the literal URL survives.
config.set_main_option("sqlalchemy.url", copilot_url.replace("%", "%%"))

target_metadata = orm.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DB connection)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations online.

    Windows defaults to the ProactorEventLoop, which psycopg async cannot use;
    force a SelectorEventLoop so migrations run everywhere.
    """
    if sys.platform == "win32":
        import selectors

        asyncio.run(
            run_async_migrations(),
            loop_factory=lambda: asyncio.SelectorEventLoop(
                selectors.SelectSelector()
            ),
        )
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
