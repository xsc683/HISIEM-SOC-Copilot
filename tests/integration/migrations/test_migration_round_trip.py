"""Migration durability and dev-runtime proof — brief sections 6, 9 and 10.

Three claims the closure makes are about the DATABASE LIFE CYCLE rather than
about application code, and none of them can be proven by a unit test:

1. The default development runtime ships a PostgreSQL 16 that already contains
   the pgvector BINARIES, so a fresh clone's first migration is not the place
   where the vector type is discovered to be missing. Shipping the binary is not
   the same as having the extension -- the extension is created per database --
   so the compose file is asserted, not assumed.

2. A genuinely fresh database reaches the single Alembic head. This includes the
   ``copilot`` SCHEMA, which is the one thing Alembic cannot create for itself:
   it pins ``search_path=copilot``, so without the namespace the very first
   ``CREATE TABLE alembic_version`` fails.

3. A database written by the PREVIOUS revision upgrades without losing anything.
   This is the part with real risk: the closure split one chunk table into an
   immutable content table and a rebuildable projection, and moved ATT&CK
   authority from a per-technique flag to a per-release row. The legacy rows are
   seeded with RAW SQL, deliberately -- they have to be written the way the
   pre-closure code wrote them, which today's repositories can no longer do.

Every migration is run as the operator runs it, through ``python -m alembic`` in
a subprocess against a scratch database. Invoking the migration functions
in-process would test the functions; running the command tests the thing that
actually has to work.

Skipped when the pgvector-capable PostgreSQL on 127.0.0.1:5434 is unreachable.
The compose-file test is not skipped: it reads a file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.engine import make_url

from hisiem_soc_copilot.domain.knowledge.value_objects import (
    compute_content_hash,
    normalize_knowledge_content,
)
from tests.support.db_runtime import scratch_database_url

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "infra" / "docker-compose.yml"
INIT_SCRIPT = REPO_ROOT / "infra" / "postgres-init" / "01-copilot-schema.sql"

_HOST = "127.0.0.1"
_PORT = 5434
_USER = "copilot"
_PASSWORD = "copilot"

#: The revision the closure branches from. Every migration up to and including it
#: is frozen; the closure's new migration must revise exactly this one.
#: The revision the PREVIOUS closure branched from.
_PREVIOUS_REVISION = "ed6af82d9b13"
#: The previous closure's revision -- now the revision BELOW head, and the one
#: that actually drops the closure's tables when it is downgraded through.
_CLOSURE_REVISION = "c41f7b2e9d08"
_HEAD_REVISION = "a5e93c07fd21"

#: The reviewed P3-A baseline. Pinned so the "frozen migrations were not edited"
#: check compares against the commit a reviewer read rather than against HEAD.
_BASELINE_COMMIT = "4724720653b64afac5928965b41c7c1fc3721825"

#: The remote head the PREVIOUS closure was reviewed at. Every revision present
#: THERE is frozen too, which is what protects the previous closure's own
#: migration: it is an ADDITION relative to _BASELINE_COMMIT, so a
#: modification filter against that baseline cannot see an edit to it.
_REVIEWED_COMMIT = "94e779523019698f92a267c20dfa893bba6ca355"

_AMBIGUOUS_CODE = "ATTACK_RELEASE_AUTHORITY_AMBIGUOUS"

#: The framework whose legacy rows claim TWO releases at once, and the framework
#: whose legacy rows agree. The first must be left with no authority at all and
#: reported; the second must keep the authority its data already expressed.
_FRAMEWORK_AMBIGUOUS = "enterprise-attack"
_FRAMEWORK_UNANIMOUS = "mobile-attack"

#: The one pre-closure chunk a legacy database is seeded with. Its handles are
#: derived at run time from these strings through the domain's own hash function,
#: so the seeded row is exactly what the previous revision would have written.
_LEGACY_VERSION_CONTENT = "Legacy runbook body.\n"
_LEGACY_CHUNK_CONTENT = "Legacy runbook body."
_LEGACY_LEGACY_EMBEDDING = "[1,0,0,0]"

_LEGACY_DOCUMENT = UUID("00000000-0000-4000-8000-0000000e000a")
_LEGACY_VERSION = UUID("00000000-0000-4000-8000-0000000e000b")
_LEGACY_PROFILE = UUID("00000000-0000-4000-8000-0000000e000c")
_LEGACY_CHUNK = UUID("00000000-0000-4000-8000-0000000e000d")


# ---------------------------------------------------------------------------
# process and database helpers
# ---------------------------------------------------------------------------


def _database_url(name: str) -> str:
    return f"postgresql+psycopg://{_USER}:{_PASSWORD}@{_HOST}:{_PORT}/{name}"


def _db_reachable() -> bool:
    """Sync reachability probe so the per-test skipif can decide at import."""
    try:
        with _connect("postgres"):
            return True
    except Exception:
        return False


def _connect(database: str):  # type: ignore[no-untyped-def]
    import psycopg

    connection = psycopg.connect(
        host=_HOST,
        port=_PORT,
        user=_USER,
        password=_PASSWORD,
        dbname=database,
        connect_timeout=2,
        autocommit=True,
    )
    connection.execute("SELECT 1")
    return connection


_DB_REACHABLE = _db_reachable()

requires_postgres = pytest.mark.skipif(
    not _DB_REACHABLE,
    reason="pgvector PostgreSQL not reachable on 127.0.0.1:5434",
)


def _execute(sql: str, *, database: str = "postgres") -> None:
    with _connect(database) as connection:
        connection.execute(sql)


def _rows(sql: str, *, database: str) -> list[tuple[object, ...]]:
    with _connect(database) as connection:
        return [tuple(row) for row in connection.execute(sql).fetchall()]


def _alembic(*args: str, database_url: str) -> subprocess.CompletedProcess[str]:
    """Run the operator's command, in the operator's environment.

    The URL is passed exactly the way ``docs/p3/p3-a-operations.md`` documents it,
    and the environment is inherited rather than rebuilt, so a run here fails for
    the same reasons a real one would.
    """
    environment = dict(os.environ)
    environment["COPILOT_DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _alembic_ok(*args: str, database_url: str) -> str:
    completed = _alembic(*args, database_url=database_url)
    assert completed.returncode == 0, (
        f"`alembic {' '.join(args)}` failed with {completed.returncode}\n"
        f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
    )
    return completed.stdout + completed.stderr


@pytest.fixture
def scratch_database() -> Iterator[str]:
    """A throwaway database on the pgvector test server, dropped afterwards.

    Delegates to the suite-wide helper so there is ONE place that decides which
    server destructive tests may use. The name is random per test, so two runs
    never share a database.
    """
    with scratch_database_url(prefix="copilot_p3a_rt_") as url:
        yield make_url(url).database or ""


def _seed_legacy_rows(database: str) -> None:
    """Write what the PREVIOUS revision could have written, and nothing newer.

    Raw SQL on purpose. The closure moved the ORM out from under these tables, so
    seeding through today's repositories would prove that today's shape migrates
    -- which is not the question. The question is whether a database written by
    ``ed6af82d9b13`` still upgrades, and only SQL can ask it.

    The ATT&CK rows encode the two cases the adoption has to tell apart: one
    framework whose releases both claim authority (the legacy flag cannot say
    which is current) and one whose single release does not.
    """
    chunk_hash = compute_content_hash(normalize_knowledge_content(_LEGACY_CHUNK_CONTENT))
    version_hash = compute_content_hash(
        normalize_knowledge_content(_LEGACY_VERSION_CONTENT)
    )
    technique_hash = compute_content_hash(normalize_knowledge_content("T1110"))

    _execute(
        f"""
        INSERT INTO copilot.embedding_profile (
            id, provider, model_id, dimension, distance_metric, normalization,
            profile_version, status, created_at, retired_at
        ) VALUES (
            '{_LEGACY_PROFILE}', 'legacy', 'legacy-model', 4, 'COSINE', 'L2', 1,
            'ACTIVE', TIMESTAMP '2026-09-01 00:00:00', NULL
        );

        INSERT INTO copilot.knowledge_document (
            id, source_kind, external_key, visibility, tenant_id, title, status,
            active_version_id, revision, lock_version, created_at, retired_at
        ) VALUES (
            '{_LEGACY_DOCUMENT}', 'CURATED_GUIDANCE', 'curated:legacy', 'GLOBAL',
            NULL, 'Legacy runbook', 'ACTIVE', '{_LEGACY_VERSION}', 1, 1,
            TIMESTAMP '2026-09-01 00:00:00', NULL
        );

        INSERT INTO copilot.knowledge_document_version (
            id, document_id, version, content_hash, title, normalized_content,
            language, source_version, metadata, ingested_at, effective_at
        ) VALUES (
            '{_LEGACY_VERSION}', '{_LEGACY_DOCUMENT}', 1, '{version_hash}',
            'Legacy runbook', '{_LEGACY_VERSION_CONTENT}', 'en', NULL,
            '{{}}'::jsonb, TIMESTAMP '2026-09-01 00:00:00', NULL
        );

        INSERT INTO copilot.knowledge_chunk (
            id, document_id, document_version_id, ordinal, heading_path, content,
            content_hash, token_count, language, chunker_version,
            embedding_profile_id, embedding, created_at
        ) VALUES (
            '{_LEGACY_CHUNK}', '{_LEGACY_DOCUMENT}', '{_LEGACY_VERSION}', 0,
            'Legacy runbook', '{_LEGACY_CHUNK_CONTENT}', '{chunk_hash}', 3, 'en',
            'structure-aware-v1', '{_LEGACY_PROFILE}',
            '{_LEGACY_LEGACY_EMBEDDING}', TIMESTAMP '2026-09-01 00:00:00'
        );

        INSERT INTO copilot.attack_technique (
            id, framework, technique_id, source_release, name, description,
            tactics, platforms, source_stix_id, content_hash, active, created_at
        ) VALUES
            ('00000000-0000-4000-8000-0000000a0001', '{_FRAMEWORK_AMBIGUOUS}',
             'T1110', 'v14.1', 'Brute Force', 'v14.1 body',
             '["credential-access"]'::jsonb, '["Linux"]'::jsonb,
             'attack-pattern--a1', '{technique_hash}', true,
             TIMESTAMP '2026-09-01 00:00:00'),
            ('00000000-0000-4000-8000-0000000a0002', '{_FRAMEWORK_AMBIGUOUS}',
             'T1110.001', 'v15.1', 'Brute Force: Password Guessing', 'v15.1 body',
             '["credential-access"]'::jsonb, '["Linux"]'::jsonb,
             'attack-pattern--a2', '{technique_hash}', true,
             TIMESTAMP '2026-09-02 00:00:00'),
            ('00000000-0000-4000-8000-0000000a0003', '{_FRAMEWORK_UNANIMOUS}',
             'T1430', 'v14.1', 'Location Tracking', 'mobile body',
             '["collection"]'::jsonb, '["Android"]'::jsonb,
             'attack-pattern--a3', '{technique_hash}', true,
             TIMESTAMP '2026-09-01 00:00:00')
        """,
        database=database,
    )


def _table_names(database: str) -> set[str]:
    rows = _rows(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'copilot'", database=database
    )
    return {str(row[0]) for row in rows}


# ---------------------------------------------------------------------------
# 1. the default development runtime (brief section 6)
# ---------------------------------------------------------------------------


def test_the_default_dev_runtime_ships_a_pinned_pgvector_pg16_image() -> None:
    """Section 6.1: the standard fresh-clone path must not fail on an image gap.

    The previous ``postgres:16`` image had no pgvector binary, so the vector
    column in the knowledge schema was unrepresentable on a fresh clone: the
    first migration died with ``type "vector" does not exist`` and no amount of
    application-side care could fix it. Asserted on the compose file, because the
    image reference IS the fix.
    """
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    image_lines = [
        line.strip() for line in compose.splitlines() if line.strip().startswith("image:")
    ]
    assert image_lines == ["image: pgvector/pgvector:pg16"], image_lines

    reference = image_lines[0].split(":", 1)[1]
    tag = reference.rsplit(":", 1)[1] if ":" in reference.rsplit("/", 1)[-1] else ""
    # A floating tag would silently re-point the dev database at a different
    # server between two runs of the same commit.
    assert tag and tag != "latest", f"the image must be pinned, got {reference!r}"
    # PG16, because the migrations were written and validated against 16 and the
    # operator's existing volume is 16 -- a major bump is a separate decision.
    assert "pg16" in tag, f"the image must be PostgreSQL 16, got {reference!r}"
    assert "pgvector" in reference, reference

    # The dev-runtime contract that must survive the image swap untouched.
    assert "container_name: copilot-postgres" in compose
    assert "POSTGRES_USER: copilot" in compose
    assert "POSTGRES_DB: copilot" in compose
    assert '"5433:5432"' in compose
    # 5432 belongs to HISIEM's own siem-postgres and must not be claimed here.
    assert '"5432:5432"' not in compose
    assert "copilot_pgdata:/var/lib/postgresql/data" in compose
    assert compose.rstrip().endswith("copilot_pgdata:")


def test_the_container_creates_the_schema_alembic_cannot_create_for_itself() -> None:
    """Section 6.3: ``POSTGRES_DB`` creates the database, never the namespace.

    Every connection pins ``search_path=copilot``, so a fresh volume whose first
    ``alembic upgrade head`` runs without the namespace fails on
    ``CREATE TABLE alembic_version`` with ``InvalidSchemaName`` -- before a single
    migration executes. The init script is what closes that gap for a fresh
    volume, and it is mounted read-only.
    """
    assert INIT_SCRIPT.exists(), INIT_SCRIPT
    script = INIT_SCRIPT.read_text(encoding="utf-8")
    assert "CREATE SCHEMA IF NOT EXISTS copilot;" in script
    # It creates the NAMESPACE only. Alembic owns every object inside it, and a
    # second owner is how two authorities over one schema begin.
    statements = [
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    assert statements == ["CREATE SCHEMA IF NOT EXISTS copilot;"], statements

    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "./postgres-init:/docker-entrypoint-initdb.d:ro" in compose


def test_the_compose_postgres_service_does_not_require_a_volume_reset() -> None:
    """Section 6.2: an existing ``copilot_pgdata`` volume keeps working.

    ``docker compose down -v`` is never the answer, so the file must not tempt
    anyone into it: no init flag that re-seeds, no ``--force-recreate`` marker,
    no second volume holding state that a swap would strand.
    """
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "down -v" not in compose
    assert compose.count("/var/lib/postgresql/data") == 1
    assert compose.count("copilot_pgdata") == 2, "declared once, mounted once"


# ---------------------------------------------------------------------------
# 2. a fresh database reaches the single head (brief sections 6.3, 9)
# ---------------------------------------------------------------------------


def test_no_earlier_migration_was_edited_in_place() -> None:
    """Section 9: migrations are append-only.

    Compared against the reviewed BASELINE rather than against HEAD, because a
    commit that edited ``ed6af82d9b13`` and committed it would look clean against
    its own parent.

    The filter is what makes this test true in BOTH review states. While the
    closure is uncommitted its new revision is untracked, and ``git diff`` does
    not report untracked files at all; once it is committed the same file appears
    as an addition. Asserting "the diff is empty" therefore passes for the wrong
    reason in the first state and fails for the wrong reason in the second. What
    is actually forbidden is a MODIFICATION, DELETION, or RENAME of an existing
    revision, so that is what is filtered for -- additions are the whole point.
    """
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    if (
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        != 0
    ):
        pytest.skip("not a git work tree")
    if (
        subprocess.run(
            ["git", "cat-file", "-e", f"{_BASELINE_COMMIT}^{{commit}}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        != 0
    ):
        pytest.skip(f"baseline commit {_BASELINE_COMMIT} is not present locally")

    completed = subprocess.run(
        [
            "git",
            "diff",
            "--name-status",
            "--diff-filter=MDRT",
            _BASELINE_COMMIT,
            "--",
            "alembic/versions",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    rewritten = [line for line in completed.stdout.splitlines() if line.strip()]
    assert rewritten == [], (
        "every migration up to ed6af82d9b13 is frozen; the closure may only ADD "
        f"revisions. Rewritten: {rewritten}"
    )

    # The filter above only sees TRACKED files. An earlier revision that was
    # deleted from disk without being committed would be invisible to it, so
    # assert the baseline's own files are still present and unmodified by
    # comparing each blob against the working tree directly.
    tree = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", _BASELINE_COMMIT, "--", "alembic/versions"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert tree.returncode == 0, tree.stderr
    baseline_versions = [line for line in tree.stdout.splitlines() if line.strip()]
    assert baseline_versions, "the baseline should contain the pre-closure revisions"

    for path in baseline_versions:
        blob = subprocess.run(
            ["git", "rev-parse", f"{_BASELINE_COMMIT}:{path}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert blob.returncode == 0, blob.stderr
        current = subprocess.run(
            ["git", "hash-object", "--", path],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert current.returncode == 0, (
            f"{path} exists at the baseline but is no longer in the working tree"
        )
        assert current.stdout.strip() == blob.stdout.strip(), (
            f"{path} was rewritten; every revision up to ed6af82d9b13 is frozen"
        )


@requires_postgres
def test_alembic_reports_exactly_one_head(scratch_database: str) -> None:
    """One head, or ``upgrade head`` is ambiguous and no chain is reproducible."""
    heads = _alembic_ok("heads", database_url=_database_url(scratch_database))
    head_lines = [line for line in heads.splitlines() if _HEAD_REVISION in line]
    assert len(head_lines) == 1, heads
    assert _HEAD_REVISION in head_lines[0]
    assert " (head)" in head_lines[0] or "head" in head_lines[0]


@requires_postgres
def test_a_fresh_database_migrates_from_base_to_the_single_head(
    scratch_database: str,
) -> None:
    """Section 6.3: fresh volume -> schema -> ``upgrade head`` -> head.

    From BASE, with no intermediate stop, which is the path a new contributor
    takes. The three tables the closure introduced are asserted by name, because
    "the command exited 0" would also be true of a run that skipped them.
    """
    output = _alembic_ok("upgrade", "head", database_url=_database_url(scratch_database))
    assert f"-> {_HEAD_REVISION}" in output

    current = _alembic_ok("current", database_url=_database_url(scratch_database))
    assert f"{_HEAD_REVISION} (head)" in current

    tables = _table_names(scratch_database)
    assert {
        "knowledge_content_chunk",
        "knowledge_chunk_embedding",
        "attack_release",
        "attack_technique",
        "knowledge_chunk",
        "alembic_version",
    } <= tables

    # The extension ships in the image AND is created per database. Asserting the
    # binary is available is not the same assertion, so assert the extension.
    extensions = _rows(
        "SELECT e.extname, n.nspname FROM pg_extension e "
        "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'",
        database=scratch_database,
    )
    assert extensions == [("vector", "copilot")], extensions

    check = _alembic_ok("check", database_url=_database_url(scratch_database))
    assert "No new upgrade operations detected." in check


# ---------------------------------------------------------------------------
# 3. a pre-closure database upgrades without losing anything (brief section 9.1)
# ---------------------------------------------------------------------------


def _prepare_legacy_database(database: str) -> str:
    """Bring a scratch database to the previous revision, seeded, then upgrade.

    Returns the upgrade's output, which is where the ambiguity report appears --
    the operator sees it once, on the run that has to decide, so it is part of
    the contract rather than a debugging aid.
    """
    url = _database_url(database)
    _alembic_ok("upgrade", _PREVIOUS_REVISION, database_url=url)
    _seed_legacy_rows(database)
    return _alembic_ok("upgrade", "head", database_url=url)


@requires_postgres
def test_a_legacy_database_upgrades_and_keeps_its_historical_chunk(
    scratch_database: str,
) -> None:
    """Section 9.1: the backfill is additive and ID-PRESERVING.

    The seeded chunk's ``id`` is the id a pre-closure ``kcit:`` handle names. If
    the backfill regenerated it, every citation minted before the closure would
    dangle at exactly the moment it was needed -- so the id is asserted, not just
    the content.
    """
    chunk_hash = compute_content_hash(normalize_knowledge_content(_LEGACY_CHUNK_CONTENT))
    _prepare_legacy_database(scratch_database)

    content_rows = _rows(
        "SELECT id, document_id, document_version_id, generation, ordinal, content, "
        "content_hash, token_count, language, chunker_version "
        "FROM copilot.knowledge_content_chunk",
        database=scratch_database,
    )
    assert content_rows == [
        (
            _LEGACY_CHUNK,
            _LEGACY_DOCUMENT,
            _LEGACY_VERSION,
            1,
            0,
            _LEGACY_CHUNK_CONTENT,
            chunk_hash,
            3,
            "en",
            "structure-aware-v1",
        )
    ], content_rows

    # The vector channel survives the split without re-embedding anything.
    projection_rows = _rows(
        "SELECT content_chunk_id, embedding_profile_id, embedding::text "
        "FROM copilot.knowledge_chunk_embedding",
        database=scratch_database,
    )
    assert projection_rows == [
        (_LEGACY_CHUNK, _LEGACY_PROFILE, _LEGACY_LEGACY_EMBEDDING)
    ], projection_rows

    # The legacy table is retained, unchanged, so ``downgrade -1`` can put the
    # pre-closure rows back instead of having to reconstruct them.
    legacy_rows = _rows(
        "SELECT id, content, content_hash FROM copilot.knowledge_chunk",
        database=scratch_database,
    )
    assert legacy_rows == [(_LEGACY_CHUNK, _LEGACY_CHUNK_CONTENT, chunk_hash)]


@requires_postgres
def test_a_legacy_database_derives_release_authority_or_refuses_to_guess(
    scratch_database: str,
) -> None:
    """Sections 2.1/9.2: authority is derived where the data says so, else reported.

    A framework whose legacy flags claim two releases has no derivable answer.
    Picking one would re-create the very defect the release table exists to
    prevent, so the migration must leave BOTH inactive and name the framework --
    while a framework whose rows agree is adopted without ceremony.
    """
    output = _prepare_legacy_database(scratch_database)

    releases = _rows(
        "SELECT framework, source_release, status, technique_count, content_fingerprint "
        "FROM copilot.attack_release ORDER BY framework, source_release",
        database=scratch_database,
    )
    assert releases == [
        (_FRAMEWORK_AMBIGUOUS, "v14.1", "INACTIVE", 1, None),
        (_FRAMEWORK_AMBIGUOUS, "v15.1", "INACTIVE", 1, None),
        (_FRAMEWORK_UNANIMOUS, "v14.1", "ACTIVE", 1, None),
    ], releases

    # ``attack_technique.active`` is a MIRROR of the release and never decides it,
    # so a stale ``true`` on a non-authoritative release is now unrepresentable.
    mirrors = _rows(
        "SELECT framework, source_release, bool_and(active), count(*) "
        "FROM copilot.attack_technique GROUP BY framework, source_release "
        "ORDER BY framework, source_release",
        database=scratch_database,
    )
    assert mirrors == [
        (_FRAMEWORK_AMBIGUOUS, "v14.1", False, 1),
        (_FRAMEWORK_AMBIGUOUS, "v15.1", False, 1),
        (_FRAMEWORK_UNANIMOUS, "v14.1", True, 1),
    ], mirrors

    # The rule of section 2.1 as SQL: a second ACTIVE release cannot be inserted
    # at all, so no later code path can produce two authorities.
    with pytest.raises(Exception) as excinfo:
        _execute(
            "INSERT INTO copilot.attack_release (id, framework, source_release, "
            "content_fingerprint, status, technique_count, created_at, activated_at) "
            f"VALUES (gen_random_uuid(), '{_FRAMEWORK_UNANIMOUS}', 'v15.1', NULL, "
            "'ACTIVE', 0, now(), now())",
            database=scratch_database,
        )
    assert "uq_attack_release_single_active" in str(excinfo.value)

    # The operator is told which framework could not be resolved, and only that
    # one -- a report that named every framework would be noise, not a diagnosis.
    assert _AMBIGUOUS_CODE in output
    assert _FRAMEWORK_AMBIGUOUS in output
    assert _FRAMEWORK_UNANIMOUS not in output
    # And the ambiguity is advisory: the upgrade completes, because refusing to
    # CHOOSE is the requirement, not refusing to MIGRATE.
    assert f"-> {_HEAD_REVISION}" in output


@requires_postgres
def test_downgrade_and_upgrade_round_trip_converges(scratch_database: str) -> None:
    """Sections 9/10: ``downgrade -1`` returns to ``ed6af82d9b13``, and back.

    A downgrade that dropped the legacy rows would make the round trip lossy, and
    the next upgrade would have nothing to backfill from -- so the legacy chunk is
    asserted to survive the downgrade with its content intact.
    """
    chunk_hash = compute_content_hash(normalize_knowledge_content(_LEGACY_CHUNK_CONTENT))
    url = _database_url(scratch_database)
    _prepare_legacy_database(scratch_database)

    # TWO steps now. Head carries the projection binding and the downgrade
    # guard; only the SECOND step crosses the revision that drops the closure's
    # tables. Both are allowed here because this database has had no application
    # write since the upgrade, which is exactly the precondition the guard
    # checks -- a refusal on this path would be the bug, not the fix (brief
    # section 3.4).
    _alembic_ok("downgrade", "-1", database_url=url)
    assert _alembic_ok("current", database_url=url).split()[0] == _CLOSURE_REVISION
    _alembic_ok("downgrade", "-1", database_url=url)

    current = _alembic_ok("current", database_url=url)
    assert f"{_PREVIOUS_REVISION}" in current
    assert _HEAD_REVISION not in current

    tables = _table_names(scratch_database)
    assert {
        "knowledge_content_chunk",
        "knowledge_chunk_embedding",
        "attack_release",
    }.isdisjoint(tables)
    assert "knowledge_chunk" in tables
    assert _rows(
        "SELECT id, content, content_hash FROM copilot.knowledge_chunk",
        database=scratch_database,
    ) == [(_LEGACY_CHUNK, _LEGACY_CHUNK_CONTENT, chunk_hash)]
    # The legacy flag does NOT come back as it was, and that is the one accepted
    # loss -- stated in the downgrade's own docstring rather than discovered
    # here. ``active`` had been overwritten to MIRROR the owning release, so a
    # framework whose rows contradicted each other now reads ``false`` where it
    # once read ``true``. Re-deriving the old value would mean deriving authority
    # from the flag the closure exists to retire, restoring the ambiguity instead
    # of the information.
    assert _rows(
        "SELECT framework, source_release, bool_and(active) FROM copilot.attack_technique "
        "GROUP BY framework, source_release ORDER BY framework, source_release",
        database=scratch_database,
    ) == [
        (_FRAMEWORK_AMBIGUOUS, "v14.1", False),
        (_FRAMEWORK_AMBIGUOUS, "v15.1", False),
        (_FRAMEWORK_UNANIMOUS, "v14.1", True),
    ]

    again = _alembic_ok("upgrade", "head", database_url=url)
    assert f"-> {_HEAD_REVISION}" in again
    assert _alembic_ok("current", database_url=url).count(f"{_HEAD_REVISION} (head)") == 1
    assert _rows(
        "SELECT id, content, content_hash FROM copilot.knowledge_content_chunk",
        database=scratch_database,
    ) == [(_LEGACY_CHUNK, _LEGACY_CHUNK_CONTENT, chunk_hash)]
    # The rule survives the round trip: never two authorities, and the framework
    # whose data agreed keeps the authority it already had. The unresolved one
    # stays unresolved, and ``knowledge doctor`` reports it for as long as it
    # does -- which is what keeps an unset authority visible instead of silent.
    authorities = _rows(
        "SELECT framework, count(*) FROM copilot.attack_release "
        "WHERE status = 'ACTIVE' GROUP BY framework ORDER BY framework",
        database=scratch_database,
    )
    assert authorities == [(_FRAMEWORK_UNANIMOUS, 1)], authorities
    assert "No new upgrade operations detected." in _alembic_ok("check", database_url=url)



def _commit_exists(commit: str) -> bool:
    """Whether ``commit`` is present locally, so the guard can skip rather than fail."""
    completed = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def test_every_revision_up_to_the_previous_reviewed_head_is_frozen() -> None:
    """The previous closure's own migration must be as unmodifiable as the rest.

    ``test_no_earlier_migration_was_edited_in_place`` filters for
    MODIFICATION/DELETION/RENAME against ``_BASELINE_COMMIT``. That baseline
    PREDATES the previous closure, so its new revision appears there as an
    ADDITION -- and additions are deliberately allowed. An in-place edit to it
    would therefore have passed that check silently, which is a real gap and not
    a theoretical one: this closure's whole migration story depends on that
    revision being exactly what a reviewer read.

    Pinned by CONTENT rather than by a diff, so the only thing that can satisfy it
    is the same bytes.
    """
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    if not _commit_exists(_REVIEWED_COMMIT):
        pytest.skip(f"commit {_REVIEWED_COMMIT} is not present locally")

    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", _REVIEWED_COMMIT, "--", "alembic/versions"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert listed.returncode == 0, listed.stderr
    frozen = [line for line in listed.stdout.splitlines() if line.strip()]
    assert frozen, "the reviewed head should contain the migrations up to it"

    for path in frozen:
        expected = subprocess.run(
            ["git", "rev-parse", f"{_REVIEWED_COMMIT}:{path}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert expected.returncode == 0, expected.stderr
        actual = subprocess.run(
            ["git", "hash-object", "--", path],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert actual.returncode == 0, (
            f"{path} was reviewed at {_REVIEWED_COMMIT} but is no longer in the "
            "working tree"
        )
        assert actual.stdout.strip() == expected.stdout.strip(), (
            f"{path} was rewritten after review; every revision up to "
            f"{_CLOSURE_REVISION} is frozen"
        )

    # ...and this closure's own migration is an ADDITION, never an edit of one.
    assert _HEAD_REVISION not in " ".join(frozen), (
        "this closure must ADD a revision, not rewrite one that was reviewed"
    )
