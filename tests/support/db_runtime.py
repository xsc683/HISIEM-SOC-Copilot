"""Test-side PostgreSQL runtime: ONE place that decides which server the
destructive integration tests may write to.

THE HAZARD THIS MODULE EXISTS TO REMOVE
---------------------------------------
The Copilot DEVELOPMENT database is the one published on the operator's host at
port 5433. It holds real data, so a test that TRUNCATEs it is, from the
operator's point of view, indistinguishable from a breach. Port 5432 is HISIEM's
and is not ours to touch at all. The pgvector TEST server is the one at port
5434; every destructive test in this suite must run there, inside a database
this module creates and drops. (The ports are spelled as numbers rather than as
address literals on purpose: ``tests/architecture/test_test_isolation.py`` fails
any test module that writes a forbidden host:port into a string, and this module
is not above its own rule.)

The suite could not previously make that guarantee. A dozen test modules each
carried a private ``_settings()`` that assigned the 5433 literal AFTER
``Settings()`` had already been constructed -- and ``COPILOT_DATABASE_URL`` is
read DURING construction -- so the environment could not redirect a single one
of them. Each module also re-implemented the same twenty-line reachability
probe.

WHAT THIS MODULE PROVIDES
-------------------------
``TEST_DATABASE_URL``                the resolved test-server URL
``admin_url()``                      the maintenance database on that server
``server_reachable()``               one memoised probe, replacing the copies
``assert_not_operator_database()``   the hard, FAIL-not-SKIP guard
``apply_settings()``                 point BOTH settings URLs at one argument
``scratch_database_url()``           context manager: create/drop a throwaway DB
``session_scratch_database_url()``   the same, once per pytest session
``migrated_scratch_database_url()``  the same, migrated to alembic head
``drop_scratch_database()``          explicit drop (used by cleanup)

Every scratch URL is a PLAIN url with no ``options`` parameter: the ``copilot``
schema is made the database's own default ``search_path`` when it is created
(``ALTER DATABASE ... SET search_path = copilot``), so an unqualified engine lands
in the right schema without any connection-level override. See
``_create_scratch_database`` for why the ``?options=-csearch_path%3Dcopilot``
suffix a reader might expect is deliberately NOT used.
"""

from __future__ import annotations

import atexit
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.engine import make_url

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a config import here
    from hisiem_soc_copilot.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The pgvector-equipped PostgreSQL that exists FOR TESTS.
TEST_SERVER_HOST = "127.0.0.1"
TEST_SERVER_PORT = 5434
TEST_MAINTENANCE_DATABASE = "postgres"

#: The two servers a destructive test must never reach. 5433 is the operator's
#: Copilot development database (real data); 5432 is HISIEM's own.
OPERATOR_DATABASE_PORT = 5433
OPERATOR_DATABASE_NAME = "copilot"
HISIEM_DATABASE_PORT = 5432

COPILOT_SCHEMA = "copilot"
#: LangGraph owns this one and its ``AsyncPostgresSaver.setup()`` does NOT create
#: the namespace -- it pins ``search_path`` and then issues unqualified
#: ``CREATE TABLE``. ``scripts/dev/up-agent.ps1`` creates both namespaces for the
#: operator for exactly this reason, so a scratch database has to as well.
CHECKPOINT_SCHEMA = "langgraph_checkpoint"

#: Overridable so CI can point the suite at a differently-addressed test server.
#: It must never be pointed at 5433 or 5432 -- ``assert_not_operator_database``
#: refuses that, loudly, for every test in the session.
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://copilot:copilot@127.0.0.1:5434/copilot"

TEST_DATABASE_URL: str = os.environ.get("COPILOT_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)

#: Prefix for every database this module creates. Kept distinctive so an
#: operator scanning the server's database list can tell a leaked test database
#: from a real one.
SESSION_SCRATCH_PREFIX = "copilot_test_"

#: Message shown when the test server is simply absent. Absence is a SKIP, and
#: it is a different code path from the FAIL-when-pointed-at-the-operator-
#: database guard below; the two are never conflated.
SKIP_REASON = (
    f"test PostgreSQL not reachable on {TEST_SERVER_HOST}:{TEST_SERVER_PORT} "
    f"(the destructive-test server; the operator database on port "
    f"{OPERATOR_DATABASE_PORT} is never used by tests)"
)


# ---------------------------------------------------------------------------
# connection helpers
# ---------------------------------------------------------------------------


def _connect(database: str, *, url: str | None = None):  # type: ignore[no-untyped-def]
    """Open an autocommit psycopg connection to ``database`` on the test server.

    psycopg is used directly, and synchronously, on purpose: the async driver
    cannot run on Windows' default ProactorEventLoop, and every caller here is a
    probe or a CREATE/DROP DATABASE that has no business being async.
    """
    import psycopg

    parsed = make_url(url if url is not None else TEST_DATABASE_URL)
    connection = psycopg.connect(
        host=parsed.host,
        port=parsed.port,
        user=parsed.username,
        password=parsed.password,
        dbname=database,
        connect_timeout=2,
        autocommit=True,
    )
    connection.execute("SELECT 1")
    return connection


def admin_url() -> str:
    """The maintenance-database URL on the TEST server, for CREATE/DROP DATABASE."""
    return (
        make_url(TEST_DATABASE_URL)
        .set(database=TEST_MAINTENANCE_DATABASE)
        .render_as_string(hide_password=False)
    )


_reachable: bool | None = None


def server_reachable() -> bool:
    """True when the pgvector TEST server answers. Memoised for the whole session.

    ONE probe, replacing the dozen copies of ``_db_reachable()`` that each test
    module used to carry. Absence means "skip the DB tests" -- it never means
    "fall back to the operator's database".
    """
    global _reachable
    if _reachable is None:
        _reachable = _probe()
    return _reachable


def _probe() -> bool:
    try:
        with _connect(TEST_MAINTENANCE_DATABASE) as connection:
            row = connection.execute("SELECT 1").fetchone()
            return bool(row == (1,))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------


def assert_not_operator_database(url: str) -> None:
    """Refuse a URL that points at a database tests must never write to.

    Raises ``AssertionError`` -- deliberately NOT ``pytest.skip``. A skip would
    let the suite look green while a misconfigured environment quietly pointed
    the destructive tests at real data; a failure stops the run and names the
    problem. The message code is stable so it can be grepped in CI logs.
    """
    port = make_url(url).port
    if port == OPERATOR_DATABASE_PORT:
        raise AssertionError(
            "DESTRUCTIVE_TEST_DATABASE_FORBIDDEN: a test resolved "
            f"{TEST_SERVER_HOST}:{OPERATOR_DATABASE_PORT}, which is the operator's "
            "Copilot DEVELOPMENT database and holds real data. It must never be "
            "truncated. The destructive tests belong on the pgvector test server "
            f"at {TEST_SERVER_HOST}:{TEST_SERVER_PORT}; obtain a database there "
            "from tests.support.db_runtime (scratch_database_url / "
            "session_scratch_database_url) instead of naming a server directly."
        )
    if port == HISIEM_DATABASE_PORT:
        raise AssertionError(
            "HISIEM_DATABASE_FORBIDDEN: a test resolved "
            f"{TEST_SERVER_HOST}:{HISIEM_DATABASE_PORT}, which belongs to HISIEM "
            "and is not this project's to touch. The destructive tests belong on "
            f"the pgvector test server at {TEST_SERVER_HOST}:{TEST_SERVER_PORT}."
        )


def _strip_options(url: str) -> str:
    """The same URL with any ``options`` connection parameter removed.

    Every scratch URL this module hands out is already free of ``options``; this
    exists so ``apply_settings`` cannot be handed one that is not.

    Why ``options`` must not appear: ``infrastructure/persistence/database.py
    ::build_engine`` appends ``options=-csearch_path=copilot`` to any URL that
    starts with ``postgresql``, and ``infrastructure/checkpoint/postgres.py``
    appends its own ``options=-csearch_path=langgraph_checkpoint``. A URL that
    already carries ``options`` therefore ends up with the key twice, and
    SQLAlchemy collects a repeated query key into a tuple -- psycopg then
    receives ``options=('-csearch_path=copilot', '-csearch_path=copilot')`` and
    the connection fails. The search path is instead a property of the DATABASE
    (see ``_create_scratch_database``), which every one of those appends then
    merely restates.
    """
    return make_url(url).difference_update_query(["options"]).render_as_string(hide_password=False)


def apply_settings(settings: Settings, url: str | None = None) -> Settings:
    """Point every database URL in ``settings`` at ONE test-server URL.

    ``settings.database`` and ``settings.langgraph`` are two fields that five
    test modules used to hand-sync (``s.langgraph.database_url =
    s.database.database_url``); both are derived from the single ``url``
    argument here, so they cannot drift.

    The guard runs FIRST, so a re-pointed test cannot be constructed against the
    operator's database at all. ``url=None`` resolves the session scratch
    database, which is the safe default rather than a convenience.
    """
    target = session_scratch_database_url() if url is None else url
    assert_not_operator_database(target)
    plain = _strip_options(target)
    settings.database.database_url = plain
    settings.langgraph.database_url = plain
    return settings


# ---------------------------------------------------------------------------
# scratch databases
# ---------------------------------------------------------------------------


def _base_url(name: str) -> str:
    """Plain ``postgresql+psycopg://`` URL for ``name`` on the test server."""
    return make_url(TEST_DATABASE_URL).set(database=name).render_as_string(hide_password=False)


def _create_scratch_database(prefix: str) -> str:
    """Create an empty scratch database, its two schemas, and its search path.

    Idempotent: a pre-existing database of the same name is dropped first, so a
    leaked run cannot poison the next one.
    """
    name = f"{prefix}{uuid.uuid4().hex[:12]}"
    with _connect(TEST_MAINTENANCE_DATABASE) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        # The namespaces are created by hand, mirroring what the operations doc
        # tells an operator to do for an EXISTING volume (and what
        # scripts/dev/up-agent.ps1 does): the container's init script only runs
        # for an empty data directory, so an already initialised cluster gets
        # them from the operator, not from the entrypoint. Alembic pins
        # ``search_path=copilot``, so the very first ``CREATE TABLE
        # alembic_version`` fails without ``copilot``; LangGraph pins
        # ``search_path=langgraph_checkpoint`` and its setup() never creates the
        # namespace, so it needs the same head start.
        #
        # The search path is a DATABASE property, not a URL parameter. The ORM's
        # tables are deliberately schema-less, so an engine that opens this
        # database needs ``copilot`` on the path -- but ``build_engine`` and the
        # LangGraph checkpointer each APPEND their own ``options=`` to whatever
        # URL they are given, and two ``options`` keys collapse into a tuple that
        # psycopg cannot use. Setting it here means the appends merely restate
        # it. The LangGraph connection overrides it per-connection with
        # ``options=-csearch_path=langgraph_checkpoint``, exactly as production
        # does.
        with _connect(name) as connection:
            connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{COPILOT_SCHEMA}"')
            connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{CHECKPOINT_SCHEMA}"')
        with _connect(TEST_MAINTENANCE_DATABASE) as admin:
            admin.execute(f'ALTER DATABASE "{name}" SET search_path = {COPILOT_SCHEMA}')
    except BaseException:
        drop_scratch_database(name)
        raise
    return name


def drop_scratch_database(name: str) -> None:
    """Drop ``name`` on the test server, forcing any straggling sessions out.

    Never raises: cleanup must not turn a passing test red, and the caller is
    usually already on an exception path. A leak is caught by the leftover-
    database check in CI rather than by a confusing new traceback here.
    """
    try:
        with _connect(TEST_MAINTENANCE_DATABASE) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    except Exception:
        pass


@contextmanager
def scratch_database_url(
    prefix: str = SESSION_SCRATCH_PREFIX, *, migrate: bool = False
) -> Iterator[str]:
    """Create a throwaway database on the test server, drop it afterwards.

    Drop-in replacement for the ``scratch_database`` fixture the migration
    round-trip test used to own: function-scoped, creates the ``copilot``
    schema, random per-test name, ``DROP ... WITH (FORCE)`` in ``finally``.

    Yields the full URL, already suffixed so an unqualified engine lands in
    ``copilot``. ``migrate=True`` runs ``alembic upgrade head`` against it.
    """
    name = _create_scratch_database(prefix)
    try:
        url = _base_url(name)
        if migrate:
            run_alembic_upgrade(_base_url(name))
        yield url
    finally:
        drop_scratch_database(name)


# --- session-scoped variant -------------------------------------------------

_session_databases: set[str] = set()
_session_url: str | None = None


def _register_session_database(name: str) -> None:
    _session_databases.add(name)


def drop_session_databases() -> None:
    """Drop every database this module created for the current session."""
    for name in sorted(_session_databases):
        drop_scratch_database(name)
    _session_databases.clear()


atexit.register(drop_session_databases)


def session_scratch_database_url() -> str:
    """The session's scratch database, created once and migrated to head.

    Memoised at module level, so the dozen test modules that point their
    settings here all land in the SAME database and pay the ``alembic upgrade
    head`` cost exactly once. Dropped at session end (``atexit`` plus the
    conftest finalizer), and explicitly on a failed migration so an interrupted
    run does not leak a half-built database.
    """
    global _session_url
    if _session_url is not None:
        return _session_url
    name = _create_scratch_database(SESSION_SCRATCH_PREFIX)
    _register_session_database(name)
    try:
        run_alembic_upgrade(_base_url(name))
    except BaseException:
        drop_scratch_database(name)
        _session_databases.discard(name)
        raise
    _session_url = _base_url(name)
    return _session_url


def migrated_scratch_database_url(session_scoped: bool = True) -> str:
    """A migrated scratch database URL.

    ``session_scoped=True`` (the default) returns the memoised session database
    -- the same one ``session_scratch_database_url()`` returns, migration
    included. ``session_scoped=False`` creates a FRESH migrated database, also
    dropped at session end; prefer the ``scratch_database_url(migrate=True)``
    context manager when per-test isolation is what is wanted.
    """
    if session_scoped:
        return session_scratch_database_url()
    name = _create_scratch_database(SESSION_SCRATCH_PREFIX)
    _register_session_database(name)
    try:
        run_alembic_upgrade(_base_url(name))
    except BaseException:
        drop_scratch_database(name)
        _session_databases.discard(name)
        raise
    return _base_url(name)


def run_alembic_upgrade(database_url: str) -> str:
    """Run ``alembic upgrade head`` as a SUBPROCESS against ``database_url``.

    Subprocess on purpose: this mirrors how ``tests/integration/migrations/
    test_migration_round_trip.py`` and the operations doc run it, so a migration
    that only works when invoked in-process cannot pass here. The URL is passed
    through ``COPILOT_DATABASE_URL`` because that is the variable
    ``alembic/env.py`` reads; the environment is inherited rather than rebuilt so
    a run fails for the same reasons a real one would.
    """
    environment = dict(os.environ)
    environment["COPILOT_DATABASE_URL"] = database_url
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"`alembic upgrade head` failed with {completed.returncode} against "
            f"the scratch test database.\n--- stdout ---\n{completed.stdout}\n"
            f"--- stderr ---\n{completed.stderr}"
        )
    return completed.stdout + completed.stderr
