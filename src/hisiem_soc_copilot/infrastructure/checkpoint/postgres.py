"""LangGraph PostgreSQL checkpointer integration.

The checkpointer owns the ``langgraph_checkpoint`` schema; Copilot's Alembic
never manages it. ``setup()`` runs LangGraph's own migrations against that schema
by pinning ``search_path`` on the connection (verified against
``langgraph.checkpoint.postgres.aio.AsyncPostgresSaver``, v3.x API).
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

from ...config import LangGraphSettings


def _checkpoint_dsn(database_url: str, schema: str) -> str:
    """Return a psycopg conninfo string for the checkpointer.

    The SQLAlchemy-style URL (``postgresql+psycopg://``) is converted to a plain
    ``postgresql://`` URL and the schema is pinned via ``options`` so LangGraph's
    unqualified DDL lands in ``langgraph_checkpoint``.
    """
    url = make_url(database_url)
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    connector = "&" if "?" in dsn else "?"
    dsn = f"{dsn}{connector}options=-csearch_path%3D{schema}"
    return dsn


class PostgresCheckpointer:
    """Thin wrapper owning an AsyncPostgresSaver for one investigation thread."""

    def __init__(self, settings: LangGraphSettings) -> None:
        self._settings = settings
        self._dsn = _checkpoint_dsn(settings.database_url, settings.schema_name)
        self._conn: AsyncConnection[Any] | None = None

    async def __aenter__(self) -> AsyncPostgresSaver:
        self._conn = await AsyncConnection.connect(
            self._dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row
        )
        saver = AsyncPostgresSaver(self._conn)
        await saver.setup()
        return saver

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
