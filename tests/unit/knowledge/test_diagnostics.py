"""``collect_diagnostics`` tests with a fake engine and a spy unit of work.

``doctor`` is the FIRST command an operator runs, which means it has to survive
the states that make an operator run it -- including a database that has not been
migrated yet. That case has a specific trap: the readiness check for an ACTIVE
embedding profile queries the knowledge tables, so on an un-migrated database it
raises ``UndefinedTable`` and takes the whole command down with a traceback
instead of reporting the missing schema.

These tests pin the invariant that makes the command trustworthy: a failing check
is reported, it never pre-empts the checks that explain it, and no check runs
against objects that are known to be absent.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from hisiem_soc_copilot.application.ports.knowledge import EmbeddingProfileRecord
from hisiem_soc_copilot.infrastructure.knowledge.diagnostics import (
    DEGRADED,
    FAIL,
    KNOWLEDGE_TABLES,
    NOT_READY,
    OK,
    READY,
    WARN,
    collect_diagnostics,
    redact_database_url,
)

DATABASE_URL = "postgresql+psycopg://copilot:copilot@127.0.0.1:5433/copilot"

PROFILE = EmbeddingProfileRecord(
    id=UUID("0d1c4a10-0000-4000-8000-000000000010"),
    provider="openai_compatible",
    model_id="text-embedding-3-small",
    dimension=1536,
    distance_metric="COSINE",
    normalization="NONE",
    profile_version=1,
    status="ACTIVE",
    created_at=datetime(2026, 1, 1, tzinfo=UTC),
)


class _FakeResult:
    """Enough of a SQLAlchemy ``Result`` for the three catalog questions."""

    def __init__(self, *, single: Any = None, rows: Sequence[Any] = ()) -> None:
        self._single = single
        self._rows = rows

    def scalar_one(self) -> Any:
        return self._single

    def scalars(self) -> Sequence[Any]:
        return self._rows


class _FakeConnection:
    def __init__(self, engine: _FakeEngine) -> None:
        self._engine = engine

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, statement: Any) -> _FakeResult:
        sql = str(statement)
        if "pg_extension" in sql:
            return _FakeResult(single=1 if self._engine.vector_installed else 0)
        if "to_regtype" in sql:
            return _FakeResult(single=self._engine.vector_visible)
        if "information_schema.tables" in sql:
            return _FakeResult(rows=self._engine.tables)
        return _FakeResult(single=1)


class _FakeEngine:
    """A stand-in for ``AsyncEngine`` that answers only the catalog questions.

    ``connect()`` returns an async context manager, matching the real pool API,
    so the diagnostic code under test is exercised unmodified.
    """

    def __init__(
        self,
        *,
        reachable: bool = True,
        vector_installed: bool = True,
        vector_visible: bool = True,
        tables: Sequence[str] = (),
    ) -> None:
        self.reachable = reachable
        self.vector_installed = vector_installed
        self.vector_visible = vector_visible
        self.tables = tables

    def connect(self) -> _FakeConnection:
        if not self.reachable:
            raise OSError("connection refused")
        return _FakeConnection(self)


class _SpyUnitOfWork:
    """Records whether the knowledge tables were queried at all."""

    def __init__(self, profile: EmbeddingProfileRecord | None, calls: list[str]) -> None:
        self._profile = profile
        self._calls = calls
        self.embedding_profiles = self

    async def __aenter__(self) -> _SpyUnitOfWork:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_active(self) -> EmbeddingProfileRecord | None:
        self._calls.append("embedding_profiles.get_active")
        return self._profile


def _factory(profile: EmbeddingProfileRecord | None, calls: list[str]) -> Any:
    def build() -> _SpyUnitOfWork:
        return _SpyUnitOfWork(profile, calls)

    return build


@pytest.mark.asyncio
async def test_a_missing_schema_is_reported_without_raising() -> None:
    """The un-migrated database must yield a verdict, not a traceback.

    This is the regression that matters: before the guard, the profile check ran
    unconditionally and the process died with ``UndefinedTable``, so an operator
    pointing ``doctor`` at a database that had not been migrated saw a stack
    trace instead of being told to run ``alembic upgrade head``.
    """
    calls: list[str] = []
    report = await collect_diagnostics(
        engine=_FakeEngine(tables=()),
        unit_of_work_factory=_factory(None, calls),
        database_url=DATABASE_URL,
        embedding_configured=True,
        embedding_detail="configured",
    )

    by_name = {check.name: check for check in report.checks}
    assert by_name["knowledge_schema"].status == FAIL
    assert "alembic upgrade head" in by_name["knowledge_schema"].detail
    assert by_name["active_embedding_profile"].status == FAIL
    assert report.overall == NOT_READY


@pytest.mark.asyncio
async def test_the_profile_check_does_not_run_against_a_missing_schema() -> None:
    """No check may query objects known to be absent.

    Skipping is the point: the schema failure is already reported, and issuing a
    query anyway is what turned a diagnostic into a crash.
    """
    calls: list[str] = []
    await collect_diagnostics(
        engine=_FakeEngine(tables=()),
        unit_of_work_factory=_factory(None, calls),
        database_url=DATABASE_URL,
        embedding_configured=True,
        embedding_detail="configured",
    )
    assert calls == []


@pytest.mark.asyncio
async def test_the_profile_check_still_runs_once_the_schema_exists() -> None:
    """The guard must not disable the check it protects."""
    calls: list[str] = []
    report = await collect_diagnostics(
        engine=_FakeEngine(tables=KNOWLEDGE_TABLES),
        unit_of_work_factory=_factory(None, calls),
        database_url=DATABASE_URL,
        embedding_configured=True,
        embedding_detail="configured",
    )
    assert calls == ["embedding_profiles.get_active"]
    by_name = {check.name: check for check in report.checks}
    assert by_name["active_embedding_profile"].status == WARN
    assert report.overall == DEGRADED


@pytest.mark.asyncio
async def test_an_unreachable_database_reports_every_dependent_check_as_skipped() -> None:
    """One cause, stated once -- not four echoes of the same failure."""
    calls: list[str] = []
    report = await collect_diagnostics(
        engine=_FakeEngine(reachable=False),
        unit_of_work_factory=_factory(None, calls),
        database_url=DATABASE_URL,
        embedding_configured=True,
        embedding_detail="configured",
    )
    by_name = {check.name: check for check in report.checks}
    assert by_name["database"].status == FAIL
    for name in ("vector_extension", "knowledge_schema", "active_embedding_profile"):
        assert by_name[name].status == FAIL
        assert "unreachable" in by_name[name].detail
    assert calls == []
    assert report.overall == NOT_READY


@pytest.mark.asyncio
async def test_a_fully_provisioned_deployment_is_ready() -> None:
    """The happy path: every check OK, so the verdict is READY and not DEGRADED."""
    calls: list[str] = []
    report = await collect_diagnostics(
        engine=_FakeEngine(tables=KNOWLEDGE_TABLES),
        unit_of_work_factory=_factory(PROFILE, calls),
        database_url=DATABASE_URL,
        embedding_configured=True,
        embedding_detail="configured",
    )
    assert all(check.status == OK for check in report.checks), report.render()
    assert report.overall == READY
    assert "1536" in report.render()


@pytest.mark.asyncio
async def test_a_lexical_only_deployment_is_degraded_rather_than_broken() -> None:
    """No provider means no vector channel -- which is not the same as "broken"."""
    report = await collect_diagnostics(
        engine=_FakeEngine(tables=KNOWLEDGE_TABLES),
        unit_of_work_factory=_factory(None, []),
        database_url=DATABASE_URL,
        embedding_configured=False,
        embedding_detail="no embedding provider configured",
    )
    by_name = {check.name: check for check in report.checks}
    assert by_name["embedding_provider"].status == WARN
    assert report.overall == DEGRADED


@pytest.mark.asyncio
async def test_an_invisible_vector_extension_names_its_own_remediation() -> None:
    """'Installed but not visible' and 'absent' need different fixes."""
    report = await collect_diagnostics(
        engine=_FakeEngine(tables=KNOWLEDGE_TABLES, vector_visible=False),
        unit_of_work_factory=_factory(None, []),
        database_url=DATABASE_URL,
        embedding_configured=True,
        embedding_detail="configured",
    )
    by_name = {check.name: check for check in report.checks}
    assert by_name["vector_extension"].status == FAIL
    assert "ALTER EXTENSION vector SET SCHEMA copilot" in by_name["vector_extension"].detail
    assert report.overall == NOT_READY


def test_the_rendered_url_never_carries_the_password() -> None:
    """The report is an operator artifact; it must be safe to paste."""
    redacted = redact_database_url(DATABASE_URL)
    assert "copilot:copilot@" not in redacted
    assert "***" in redacted
    assert "@127.0.0.1:5433/copilot" in redacted


def test_an_unparseable_url_is_not_echoed_back() -> None:
    """A string that cannot be parsed may still contain a credential."""
    assert redact_database_url("not a url at all") == "<unparseable database url>"
