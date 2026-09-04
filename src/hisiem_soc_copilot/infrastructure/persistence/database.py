"""SQLAlchemy Async engine/session factory for the ``copilot`` schema.

psycopg 3 driver; TIMESTAMPTZ/UTC; two independent engines exist at runtime
(``copilot`` here, ``langgraph_checkpoint`` in infrastructure/checkpoint). SQLAlchemy
session/transaction and LangGraph checkpointer connection are never shared.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ...config import DatabaseSettings

COPILOT_SCHEMA = "copilot"


def build_engine(settings: DatabaseSettings) -> AsyncEngine:
    """Create an AsyncEngine bound to the ``copilot`` schema connection path.

    The schema is created/migrated by Alembic (a deployment step); the connection
    only needs ``search_path`` set so unqualified table names resolve into
    ``copilot``.
    """
    url = settings.database_url
    # Search path is only meaningful for a postgres URL; other drivers (tests)
    # may use SQLite/aiohttp stubs, in which case we leave the URL untouched.
    if url.startswith("postgresql"):
        url = _append_search_path(url, COPILOT_SCHEMA)
    return create_async_engine(url, pool_pre_ping=True)


def _append_search_path(url: str, schema: str) -> str:
    connector = "&" if "?" in url else "?"
    return f"{url}{connector}options=-csearch_path%3D{schema}"


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Async context manager yielding a bound AsyncSession.

    Prefer the UnitOfWork over calling sessions directly; this is kept for
    infrastructure/bootstrap code that legitimately owns a session.
    """
    async with session_factory() as session:
        yield session
