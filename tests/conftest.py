"""Shared pytest configuration.

The project uses a src layout installed in editable mode (``pip install -e .``),
so ``hisiem_soc_copilot`` resolves through normal package imports. ``pytest``'s
declarative ``pythonpath = ["."]`` (pyproject) exposes ``tests.fixtures`` helpers —
no ``sys.path`` manipulation in conftest.

Forces a SelectorEventLoop on Windows so psycopg async works in async tests (the
Windows default ProactorEventLoop is incompatible with psycopg async).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.support import db_runtime

if sys.platform == "win32":
    import selectors

    class _WindowsSelectorPolicy(asyncio.WindowsSelectorEventLoopPolicy):  # type: ignore[name-defined]
        def new_event_loop(self) -> asyncio.AbstractEventLoop:  # type: ignore[type-arg]
            return asyncio.SelectorEventLoop(selectors.SelectSelector())

    asyncio.set_event_loop_policy(_WindowsSelectorPolicy())


# ---------------------------------------------------------------------------
# Test-database isolation (P3-A closure-2 safety blocker)
#
# The suite's destructive integration tests TRUNCATE real tables. They must do
# it on the pgvector TEST server (127.0.0.1:5434, container ``copilot-pgvector``)
# inside a database the suite creates and drops -- never on the operator's
# Copilot development database, and never on HISIEM's. ``tests/support/
# db_runtime.py`` owns that decision; the fixtures below only wire it in.
#
# Absence of the test server is a SKIP. A URL that resolves to the operator's
# database (or HISIEM's) is a FAIL. Those are deliberately different code paths.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def requires_database() -> None:
    """Skip a DB-backed test when the pgvector TEST server is not running.

    Absence is a skip so the suite stays green on a dev machine without Docker.
    A URL pointed at the operator's database is a FAIL and is checked by the
    autouse fixture below -- never conflated with this one.
    """
    if not db_runtime.server_reachable():
        pytest.skip(db_runtime.SKIP_REASON)


@pytest.fixture(scope="session", autouse=True)
def _refuse_operator_database() -> None:
    """Fail the whole session if the resolved test URL is not the test server.

    Autouse, session-scoped and cheap: it only parses a URL, it never connects.
    A developer who exports ``COPILOT_TEST_DATABASE_URL`` pointing at 5433 gets
    a hard failure before any test can truncate real data.
    """
    db_runtime.assert_not_operator_database(db_runtime.TEST_DATABASE_URL)


@pytest.fixture(scope="session")
def scratch_db_url(requires_database: None) -> str:
    """The session's scratch database on the test server, migrated to head.

    Created once per pytest session and dropped at the end (atexit plus the
    finalizer below), so an interrupted run does not leak databases.
    """
    return db_runtime.session_scratch_database_url()


@pytest.fixture(scope="session", autouse=True)
def _drop_scratch_databases_at_session_end() -> Iterator[None]:
    """Ensure the session scratch database is dropped when the session ends."""
    yield
    db_runtime.drop_session_databases()


@pytest_asyncio.fixture
async def session_factory(
    scratch_db_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A function-scoped sessionmaker bound to the scratch database."""
    engine = create_async_engine(scratch_db_url)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()
