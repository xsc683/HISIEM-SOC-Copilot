"""A downgrade must be lossless, or it must refuse (brief section 3).

``c41f7b2e9d08`` dropped ``knowledge_content_chunk``, ``knowledge_chunk_embedding``
and ``attack_release`` unconditionally, and stopped updating the legacy
``knowledge_chunk`` table on the way up. Once the application had written anything
through the new tables, ``downgrade -1`` therefore destroyed it silently -- and no
test noticed, because the only downgrade test in the suite ran upgrade and
downgrade back to back with no application write in between, so the backfill's own
rows were the only rows present and they happened to be exactly what the old
schema could hold.

These tests are the missing half. Each one WRITES a specific category of state and
then asserts that the guard refuses -- and, just as importantly, that the refusal
changed nothing.

The safe path is asserted too, and it is the more fragile of the two: a guard that
false-positives on a clean round trip would make the correct operation impossible,
which is a worse bug than the one being fixed. The subtle case is
``test_adopted_releases_alone_do_not_block_the_downgrade`` -- the upgrade ADOPTS one
``attack_release`` row per ``(framework, source_release)`` it finds in
``attack_technique``, so a clean upgrade always leaves rows behind, and any guard
phrased as "``attack_release`` is not empty" would refuse every honest downgrade.

Raw SQL on purpose, exactly as ``test_migration_round_trip`` seeds it: the point is
whether a database written by an operator's deployment still downgrades, so seeding
through today's repositories would ask a different question.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from hisiem_soc_copilot.domain.knowledge.value_objects import (
    compute_content_hash,
    normalize_knowledge_content,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

_HOST = "127.0.0.1"
_PORT = 5434
_USER = "copilot"
_PASSWORD = "copilot"

#: The revision the closure branched from, and the current head. ``downgrade -1``
#: from head crosses into the revision that destroys data, which is why the guard
#: lives at the head rather than one step further down.
_PREVIOUS_REVISION = "c41f7b2e9d08"

#: The revision a pre-closure deployment would be sitting on.
_PRE_UPGRADE_REVISION = "ed6af82d9b13"
_HEAD_REVISION = "a5e93c07fd21"

_UNSAFE_CODE = "P3A_DOWNGRADE_UNSAFE"

_FRAMEWORK = "mitre-attack"

#: Fixed ids so the seeded rows are addressable without a round trip. Version 4
#: UUIDs because the columns are real ``uuid`` types.
_DOCUMENT = "00000000-0000-4000-8000-0000000b0001"
_VERSION_1 = "00000000-0000-4000-8000-0000000b0002"
_VERSION_2 = "00000000-0000-4000-8000-0000000b0003"
_PROFILE = "00000000-0000-4000-8000-0000000b0004"
_TECHNIQUE = "00000000-0000-4000-8000-0000000b0005"
_RELEASE = "00000000-0000-4000-8000-0000000b0006"

_LEGACY_CONTENT = "legacy chunk body"
_VERSION_1_CONTENT = "legacy version body"


def _database_url(name: str) -> str:
    return f"postgresql+psycopg://{_USER}:{_PASSWORD}@{_HOST}:{_PORT}/{name}"


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


def _db_reachable() -> bool:
    try:
        with _connect("postgres"):
            return True
    except Exception:
        return False


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


def _scalar(sql: str, *, database: str) -> object:
    return _rows(sql, database=database)[0][0]


def _alembic(*args: str, database_url: str) -> subprocess.CompletedProcess[str]:
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


def _revision(database_url: str) -> str:
    return _alembic_ok("current", database_url=database_url).split()[0]


@pytest.fixture
def scratch_database() -> Iterator[str]:
    """A throwaway database on the pgvector server, dropped afterwards.

    The ``copilot`` schema is created by hand -- the migration creates the
    extension itself, so the database is deliberately left WITHOUT one, which is
    the state a fresh clone is in.
    """
    name = f"copilot_c2_dg_{uuid.uuid4().hex[:12]}"
    _execute(f'CREATE DATABASE "{name}"')
    try:
        _execute("CREATE SCHEMA IF NOT EXISTS copilot", database=name)
        yield name
    finally:
        _execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _seed_pre_upgrade(database: str) -> None:
    """Write what ``ed6af82d9b13`` could have written, and nothing newer.

    A profile, one document with one version, one LEGACY chunk holding content and
    embedding together (the shape ``c41f7b2e9d08`` splits), and one ATT&CK release's
    canonical rows -- the last so the upgrade ADOPTS a release and the safe-path
    test has rows to be fooled by.
    """
    chunk_hash = compute_content_hash(normalize_knowledge_content(_LEGACY_CONTENT))
    version_hash = compute_content_hash(
        normalize_knowledge_content(_VERSION_1_CONTENT)
    )
    technique_hash = compute_content_hash(normalize_knowledge_content("T1110"))

    _execute(
        f"""
        INSERT INTO copilot.embedding_profile (
            id, provider, model_id, dimension, distance_metric, normalization,
            profile_version, status, created_at, retired_at
        ) VALUES (
            '{_PROFILE}', 'legacy', 'legacy-model', 4, 'COSINE', 'L2', 1,
            'ACTIVE', TIMESTAMP '2026-09-01 00:00:00', NULL
        );

        INSERT INTO copilot.knowledge_document (
            id, source_kind, external_key, visibility, tenant_id, title, status,
            active_version_id, revision, lock_version, created_at, retired_at
        ) VALUES (
            '{_DOCUMENT}', 'CURATED_GUIDANCE', 'curated:dg', 'GLOBAL', NULL,
            'Downgrade runbook', 'ACTIVE', '{_VERSION_1}', 1, 1,
            TIMESTAMP '2026-09-01 00:00:00', NULL
        );

        INSERT INTO copilot.knowledge_document_version (
            id, document_id, version, content_hash, title, normalized_content,
            language, source_version, metadata, ingested_at, effective_at
        ) VALUES (
            '{_VERSION_1}', '{_DOCUMENT}', 1, '{version_hash}',
            'Downgrade runbook', '{_VERSION_1_CONTENT}', 'en', NULL,
            '{{}}'::jsonb, TIMESTAMP '2026-09-01 00:00:00', NULL
        );

        INSERT INTO copilot.knowledge_chunk (
            id, document_id, document_version_id, ordinal, heading_path, content,
            content_hash, token_count, language, chunker_version,
            embedding_profile_id, embedding, created_at
        ) VALUES (
            '00000000-0000-4000-8000-0000000b0007', '{_DOCUMENT}', '{_VERSION_1}',
            0, 'Legacy', '{_LEGACY_CONTENT}', '{chunk_hash}', 3, 'en',
            'structure-aware-v1', '{_PROFILE}', '[1,0,0,0]',
            TIMESTAMP '2026-09-01 00:00:00'
        );

        INSERT INTO copilot.attack_technique (
            id, framework, technique_id, source_release, name, description,
            tactics, platforms, source_stix_id, content_hash, active, created_at
        ) VALUES (
            '{_TECHNIQUE}', '{_FRAMEWORK}', 'T1110', 'v14.1', 'Brute Force',
            'v14.1 body', '["credential-access"]'::jsonb, '["Linux"]'::jsonb,
            'attack-pattern--dg', '{technique_hash}', TRUE,
            TIMESTAMP '2026-09-01 00:00:00'
        );
        """,
        database=database,
    )


def _add_version_2_and_chunk(database: str, *, generation: int = 1) -> None:
    """Write a NEW version and content chunk the legacy table knows nothing about."""
    content = "post-upgrade chunk body"
    content_hash = compute_content_hash(normalize_knowledge_content(content))
    version_hash = compute_content_hash(normalize_knowledge_content("post-upgrade v2"))

    _execute(
        f"""
        INSERT INTO copilot.knowledge_document_version (
            id, document_id, version, content_hash, title, normalized_content,
            language, source_version, metadata, ingested_at, effective_at
        ) VALUES (
            '{_VERSION_2}', '{_DOCUMENT}', 2, '{version_hash}',
            'Downgrade runbook', 'post-upgrade v2', 'en', NULL,
            '{{}}'::jsonb, TIMESTAMP '2026-09-02 00:00:00', NULL
        );

        INSERT INTO copilot.knowledge_content_chunk (
            id, document_id, document_version_id, ordinal, heading_path, content,
            content_hash, token_count, language, chunker_version, generation,
            created_at
        ) VALUES (
            gen_random_uuid(), '{_DOCUMENT}', '{_VERSION_2}', 0, 'New', '{content}',
            '{content_hash}', 3, 'en', 'structure-aware-v1', {generation},
            TIMESTAMP '2026-09-02 00:00:00'
        );
        """,
        database=database,
    )


def _mutate_backfilled_projection(database: str) -> None:
    """Re-embed the backfilled chunk, leaving the legacy row stale.

    This is the case where a downgrade is not merely lossy but WRONG: restoring the
    legacy vector would present a superseded embedding as the current projection.
    """
    _execute(
        """
        UPDATE copilot.knowledge_chunk_embedding
           SET embedding = '[0,1,0,0]'::vector
         WHERE embedding::text <> '[0,1,0,0]'
        """,
        database=database,
    )


def _pin_a_release(database: str) -> None:
    """Register a release the way an import does: fingerprinted, hence immutable."""
    _execute(
        f"""
        INSERT INTO copilot.attack_release (
            id, framework, source_release, content_fingerprint, status,
            technique_count, created_at, activated_at
        ) VALUES (
            '{_RELEASE}', '{_FRAMEWORK}', 'v15.1', repeat('b', 64), 'INACTIVE',
            1, TIMESTAMP '2026-09-02 00:00:00', NULL
        );
        """,
        database=database,
    )


def _record_a_binding(database: str) -> None:
    """Stage one release -> version binding, as the importer does."""
    content_hash = compute_content_hash(normalize_knowledge_content(_VERSION_1_CONTENT))
    _execute(
        f"""
        INSERT INTO copilot.attack_release_projection (
            id, framework, source_release, technique_id, document_id,
            document_version_id, content_hash, created_at
        ) VALUES (
            gen_random_uuid(), '{_FRAMEWORK}', 'v14.1', 'T1110', '{_DOCUMENT}',
            '{_VERSION_1}', '{content_hash}', TIMESTAMP '2026-09-02 00:00:00'
        );
        """,
        database=database,
    )


def _upgraded(database: str) -> str:
    """Build a PRE-CLOSURE database, seed it, then upgrade it to head.

    The order matters and is the whole point: the seed writes the shape
    ``ed6af82d9b13`` could have written, so it can only run once that revision is
    applied. Upgrading afterwards is what puts the database in the state these
    tests are about -- a deployment that has been running the closure.
    """
    url = _database_url(database)
    _alembic_ok("upgrade", _PRE_UPGRADE_REVISION, database_url=url)
    _seed_pre_upgrade(database)
    _alembic_ok("upgrade", "head", database_url=url)
    assert _revision(url) == _HEAD_REVISION
    return url


# ---------------------------------------------------------------------------
# the safe path -- the one a guard must NOT break
# ---------------------------------------------------------------------------
@requires_postgres
def test_a_clean_upgrade_downgrades_all_the_way_back(scratch_database: str) -> None:
    """Nothing written through the new tables means nothing to lose.

    Two steps, because head is the projection revision: the first crosses it, the
    second crosses ``c41f7b2e9d08`` -- the revision that actually drops the tables.
    Both must be allowed on a database the application has not written to.
    """
    url = _upgraded(scratch_database)

    _alembic_ok("downgrade", "-1", database_url=url)
    assert _revision(url) == _PREVIOUS_REVISION
    _alembic_ok("downgrade", "-1", database_url=url)
    assert _revision(url) == "ed6af82d9b13"
    # The legacy row survives both steps: it belongs to ed6af82d9b13, and the
    # closure only ever backfills FROM it.
    assert _scalar(
        "SELECT count(*) FROM copilot.knowledge_chunk", database=scratch_database
    ) == 1

    _alembic_ok("upgrade", "head", database_url=url)
    assert _revision(url) == _HEAD_REVISION


@requires_postgres
def test_adopted_releases_alone_do_not_block_the_downgrade(
    scratch_database: str,
) -> None:
    """The trap: a clean upgrade ALWAYS leaves ``attack_release`` rows behind.

    ``_adopt_releases`` inserts one row per ``(framework, source_release)`` found in
    ``attack_technique``, so "the table is not empty" is not evidence of anything. A
    guard written that way would refuse every honest downgrade -- so this asserts
    both that the rows exist and that the downgrade still succeeds.
    """
    url = _upgraded(scratch_database)

    adopted = _rows(
        "SELECT source_release, content_fingerprint FROM copilot.attack_release",
        database=scratch_database,
    )
    assert adopted == [("v14.1", None)], (
        "the upgrade should adopt exactly the seeded release, unpinned"
    )

    _alembic_ok("downgrade", "-1", database_url=url)
    assert _revision(url) == _PREVIOUS_REVISION


# ---------------------------------------------------------------------------
# the unsafe path -- one test per category
# ---------------------------------------------------------------------------
@requires_postgres
def test_new_content_chunks_block_the_downgrade(scratch_database: str) -> None:
    """State the legacy table has no row for cannot be represented below head."""
    url = _upgraded(scratch_database)
    _add_version_2_and_chunk(scratch_database)

    completed = _alembic("downgrade", "-1", database_url=url)

    assert completed.returncode != 0
    output = completed.stdout + completed.stderr
    assert _UNSAFE_CODE in output
    assert "NEW_CONTENT_CHUNKS" in output


@requires_postgres
def test_a_new_generation_blocks_the_downgrade(scratch_database: str) -> None:
    """The old schema has no generation column at all.

    Downgrading would re-present the stale generation-1 rows as the current
    chunking, so a rechunk is a loss the schema cannot even describe.
    """
    url = _upgraded(scratch_database)
    _add_version_2_and_chunk(scratch_database, generation=2)

    completed = _alembic("downgrade", "-1", database_url=url)

    assert completed.returncode != 0
    output = completed.stdout + completed.stderr
    assert _UNSAFE_CODE in output
    assert "MULTIPLE_CHUNK_GENERATIONS" in output


@requires_postgres
def test_a_changed_projection_blocks_the_downgrade(scratch_database: str) -> None:
    """A re-embedding makes the downgrade WRONG, not merely lossy.

    The legacy row still holds the superseded vector; restoring it would present
    that as the current projection of unchanged content.
    """
    url = _upgraded(scratch_database)
    _mutate_backfilled_projection(scratch_database)

    completed = _alembic("downgrade", "-1", database_url=url)

    assert completed.returncode != 0
    output = completed.stdout + completed.stderr
    assert _UNSAFE_CODE in output
    assert "PROJECTION_CHANGED" in output


@requires_postgres
def test_a_pinned_release_blocks_the_downgrade(scratch_database: str) -> None:
    """A fingerprint is the release's immutability claim, and has nowhere to go."""
    url = _upgraded(scratch_database)
    _pin_a_release(scratch_database)

    completed = _alembic("downgrade", "-1", database_url=url)

    assert completed.returncode != 0
    output = completed.stdout + completed.stderr
    assert _UNSAFE_CODE in output
    assert "PINNED_ATTACK_RELEASE" in output


@requires_postgres
def test_an_attack_projection_binding_blocks_the_downgrade(
    scratch_database: str,
) -> None:
    """The binding is the only record of which version a release staged."""
    url = _upgraded(scratch_database)
    _record_a_binding(scratch_database)

    completed = _alembic("downgrade", "-1", database_url=url)

    assert completed.returncode != 0
    output = completed.stdout + completed.stderr
    assert _UNSAFE_CODE in output
    assert "ATTACK_PROJECTION_BINDING" in output


@requires_postgres
def test_a_refused_downgrade_changes_nothing(scratch_database: str) -> None:
    """Fail-closed means the refusal itself is inert.

    The guard runs before the first ``op.drop_*``, so this is true of the run
    rather than a property of the caller's transaction configuration -- which is
    what makes it hold under ``alembic downgrade --sql`` too.
    """
    url = _upgraded(scratch_database)
    _add_version_2_and_chunk(scratch_database)
    before = _scalar(
        "SELECT count(*) FROM copilot.knowledge_content_chunk",
        database=scratch_database,
    )

    completed = _alembic("downgrade", "-1", database_url=url)
    assert completed.returncode != 0

    assert _revision(url) == _HEAD_REVISION, "a refused downgrade must not move"
    assert (
        _scalar(
            "SELECT count(*) FROM copilot.knowledge_content_chunk",
            database=scratch_database,
        )
        == before
    ), "the rows the guard exists to protect must still be there"
    assert _scalar(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'copilot' AND table_name = 'attack_release'",
        database=scratch_database,
    ) == 1, "no table may have been dropped by a run that refused"
