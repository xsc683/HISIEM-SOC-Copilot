"""SQLAlchemy ORM base with named constraints.

The naming convention (persistence-schema.md §35) keeps Alembic-generated
constraint names deterministic: ``pk_*``/``fk_*``/``uq_*``/``ck_*``/``ix_*``.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class CopilotBase(DeclarativeBase):
    """Declarative base for all ``copilot`` schema ORM models.

    Tables are NOT schema-bound in the ORM: every connection is pinned to the
    ``copilot`` schema via ``search_path`` (see infrastructure/persistence/
    database.py), so unqualified DDL and FK references resolve into ``copilot``.
    Keeping tables schema-less makes Alembic's FK comparison match PostgreSQL's
    reflected (unqualified) constraint definitions, so ``alembic check`` stays
    drift-free. The ``langgraph_checkpoint`` schema is never touched.
    """

    metadata = metadata
