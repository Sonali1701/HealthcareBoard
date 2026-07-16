"""Shared schema primitives."""
from __future__ import annotations

from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class ORMModel(BaseModel):
    """Base for response models read from SQLAlchemy ORM objects."""

    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    """Generic pagination envelope.

    ``total`` is optional: on very large tables an exact COUNT(*) per request is
    prohibitively expensive, so callers can skip it and rely on ``has_next``
    (computed by fetching one extra row) for Prev/Next navigation.
    """

    items: list[T]
    total: Optional[int] = None
    limit: int
    offset: int
    has_next: bool = False


class Message(BaseModel):
    detail: str
