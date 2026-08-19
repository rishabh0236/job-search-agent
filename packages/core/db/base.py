"""Declarative base with project-wide type mappings."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase, MappedAsDataclass

from packages.core.db.types import JsonType, UtcDateTime


class Base(DeclarativeBase):
    """Base for all ORM models.

    ``type_annotation_map`` means model definitions stay declarative — a
    ``Mapped[datetime]`` is automatically the UTC-safe column type, and a
    ``Mapped[dict[str, Any]]`` becomes JSON/JSONB, with no per-column repetition.
    """

    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this as a plain dict
        datetime: UtcDateTime,
        dict[str, Any]: JsonType,
        list[str]: JsonType,
        str: String,
    }


__all__ = ["Base", "MappedAsDataclass"]
