-- Copilot's schema namespace, created at cluster init for a FRESH volume.
--
-- Why this file exists: the `copilot` database is created by the image's
-- POSTGRES_DB, but the `copilot` SCHEMA inside it is not, and every connection
-- pins `search_path=copilot`. Without the namespace the very first
-- `alembic upgrade head` dies before it runs a single migration:
--
--     psycopg.errors.InvalidSchemaName: no schema has been selected to create in
--     [SQL: CREATE TABLE alembic_version (...)]
--
-- Alembic still owns every OBJECT in here -- this only creates the empty
-- namespace, which is the one thing Alembic cannot do for itself (it needs the
-- namespace to exist before it can record its own version table).
--
-- Scope: `docker-entrypoint-initdb.d` runs ONLY when the data directory is
-- empty, i.e. exactly once, for a fresh `copilot_pgdata` volume. An EXISTING
-- volume never re-runs it, so the existing-volume upgrade path in
-- docs/p3/p3-a-operations.md issues the same statement by hand.

CREATE SCHEMA IF NOT EXISTS copilot;
