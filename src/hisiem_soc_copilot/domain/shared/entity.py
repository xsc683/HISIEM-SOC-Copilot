"""Base class for domain entities that carry an identity."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass
class Entity:
    """Base class for domain entities.

    Domain aggregates and entities are plain dataclasses; they never import
    SQLAlchemy/LangGraph/FastAPI. The persistence layer maps them to ORM rows.
    """

    id: UUID

    def __post_init__(self) -> None:
        if self.id is None or self.id.int == 0:
            raise ValueError("entity id must be a non-null UUID")
