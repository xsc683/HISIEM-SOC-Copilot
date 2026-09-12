"""Knowledge deployment diagnostics (brief section 31).

``doctor`` answers one question -- "can this deployment actually serve knowledge
retrieval right now?" -- and it has to answer it BEFORE anything is written, so
the checks here are read-only and the raw SQL stays in infrastructure (section
72: raw SQL lives in Infrastructure, never in a handler or an interface layer).

The output is deliberately three-valued rather than a boolean. ``DEGRADED`` is
the honest answer for "the corpus is reachable but the vector channel is not",
and collapsing that into either ``READY`` or ``NOT_READY`` would either overstate
what works or hide a working lexical path.

Nothing here ever prints a secret: checks report whether a configuration is
present, never a value.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from ...application.ports.unit_of_work import UnitOfWork

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

READY = "READY"
DEGRADED = "DEGRADED"
NOT_READY = "NOT_READY"

#: The tables P3-A creates. Names are the frozen schema of sections 11-15/36.
KNOWLEDGE_TABLES: tuple[str, ...] = (
    "knowledge_document",
    "knowledge_document_version",
    "embedding_profile",
    "knowledge_chunk",
    "attack_technique",
)

#: The remediation for the one failure that is genuinely confusing: the vector
#: extension is installed, but into a schema that a ``search_path=copilot``
#: connection cannot see, so PostgreSQL reports the bare
#: ``type "vector" does not exist``.
_VECTOR_SCHEMA_HINT = (
    "the vector extension is installed but not visible on this connection's "
    "search_path; run: ALTER EXTENSION vector SET SCHEMA copilot;"
)


@dataclass(frozen=True)
class DiagnosticCheck:
    """One check's outcome, with a detail an operator can act on."""

    name: str
    status: str
    detail: str

    @property
    def failed(self) -> bool:
        return self.status == FAIL


@dataclass(frozen=True)
class DiagnosticsReport:
    """The full set of checks plus the single verdict they add up to."""

    checks: tuple[DiagnosticCheck, ...]
    database_url: str

    @property
    def overall(self) -> str:
        """``NOT_READY`` on any failure, ``DEGRADED`` on any warning, else READY.

        A warning is something that degrades a channel (no embedding provider, no
        ACTIVE profile) while leaving lexical retrieval usable -- so it must not
        read as READY, and must not read as "broken" either.
        """
        if any(check.failed for check in self.checks):
            return NOT_READY
        if any(check.status == WARN for check in self.checks):
            return DEGRADED
        return READY

    def render(self) -> str:
        lines = [f"knowledge doctor: {self.overall}", f"  database: {self.database_url}"]
        lines.extend(
            f"  [{check.status}] {check.name}: {check.detail}" for check in self.checks
        )
        return "\n".join(lines)


def redact_database_url(url: str) -> str:
    """Return ``url`` with any password replaced by ``***``.

    A connection string routinely embeds a password, and this one is printed by an
    operator command in both text and JSON form. The redaction therefore happens
    where the report is BUILT rather than at each render site: a credential that
    never reaches the report object cannot leak from a future consumer of it.

    A URL that cannot be parsed is reported as unparseable rather than echoed
    back: guessing at the structure of a string that contains a credential is a
    worse answer than saying less about it.
    """
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - a diagnostic reports, it does not raise
        return "<unparseable database url>"


async def collect_diagnostics(
    *,
    engine: AsyncEngine,
    unit_of_work_factory: Callable[[], UnitOfWork],
    database_url: str,
    embedding_configured: bool,
    embedding_detail: str,
) -> DiagnosticsReport:
    """Run every read-only knowledge deployment check.

    ``engine`` is used ONLY for catalog questions (is the database up, is the
    extension there, do the tables exist); everything that reads knowledge rows
    goes through the unit of work, so a diagnostic cannot become a second,
    unguarded way to query the corpus.

    ``database_url`` is redacted on the way in, so no downstream consumer -- the
    text renderer, ``doctor --json``, or a future caller -- can print a password
    that this function accepted.
    """
    checks: list[DiagnosticCheck] = []

    reachable, reachable_detail = await _check_reachable(engine)
    checks.append(DiagnosticCheck("database", OK if reachable else FAIL, reachable_detail))

    if reachable:
        checks.append(await _check_vector_extension(engine))
        schema_check = await _check_tables(engine)
        checks.append(schema_check)
        if schema_check.failed:
            # The profile check queries ``embedding_profile``, so on an
            # un-migrated database it would raise ``UndefinedTable`` and take the
            # whole command down with a traceback -- exactly the database an
            # operator runs ``doctor`` against FIRST. The missing schema is
            # already reported by the check above; say why this one did not run
            # rather than repeating the cause or crashing on it.
            checks.append(
                DiagnosticCheck(
                    "active_embedding_profile",
                    FAIL,
                    "not checked: the knowledge schema is missing "
                    "(run: alembic upgrade head)",
                )
            )
        else:
            checks.append(await _check_active_profile(unit_of_work_factory))
    else:
        # Do not pile on: every later check would fail for the same reason and
        # bury the one that matters.
        for name in ("vector_extension", "knowledge_schema", "active_embedding_profile"):
            checks.append(
                DiagnosticCheck(name, FAIL, "not checked: the database is unreachable")
            )

    checks.append(
        DiagnosticCheck(
            "embedding_provider",
            OK if embedding_configured else WARN,
            embedding_detail,
        )
    )
    return DiagnosticsReport(
        checks=tuple(checks), database_url=redact_database_url(database_url)
    )


async def _check_reachable(engine: AsyncEngine) -> tuple[bool, str]:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - a diagnostic reports, it does not raise
        return False, f"cannot connect: {type(exc).__name__}"
    return True, "connected"


async def _check_vector_extension(engine: AsyncEngine) -> DiagnosticCheck:
    """Distinguish 'absent' from 'present but invisible', which need different fixes."""
    async with engine.connect() as connection:
        installed = (
            await connection.execute(
                text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
            )
        ).scalar_one()
        visible = (
            await connection.execute(text("SELECT to_regtype('vector') IS NOT NULL"))
        ).scalar_one()

    if installed and visible:
        return DiagnosticCheck("vector_extension", OK, "installed and visible")
    if installed and not visible:
        return DiagnosticCheck("vector_extension", FAIL, _VECTOR_SCHEMA_HINT)
    return DiagnosticCheck(
        "vector_extension",
        FAIL,
        "the vector extension is not installed in this database; see "
        "docs/p3/p3-a-operations.md for the one-time prerequisite",
    )


async def _check_tables(engine: AsyncEngine) -> DiagnosticCheck:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema()"
                )
            )
        ).scalars()
        present = {str(name) for name in rows}

    missing = sorted(set(KNOWLEDGE_TABLES) - present)
    if missing:
        return DiagnosticCheck(
            "knowledge_schema",
            FAIL,
            "missing tables: " + ", ".join(missing) + " (run: alembic upgrade head)",
        )
    return DiagnosticCheck(
        "knowledge_schema", OK, f"{len(KNOWLEDGE_TABLES)} knowledge tables present"
    )


async def _check_active_profile(
    unit_of_work_factory: Callable[[], UnitOfWork],
) -> DiagnosticCheck:
    """An ACTIVE profile is what makes the vector channel addressable at all."""
    async with unit_of_work_factory() as uow:
        profile = await uow.embedding_profiles.get_active()
    if profile is None:
        return DiagnosticCheck(
            "active_embedding_profile",
            WARN,
            "no ACTIVE embedding profile: lexical retrieval works, vector and "
            "hybrid retrieval are unavailable until a document is ingested",
        )
    return DiagnosticCheck(
        "active_embedding_profile",
        OK,
        f"{profile.provider}/{profile.model_id} dim={profile.dimension} "
        f"{profile.distance_metric}",
    )
