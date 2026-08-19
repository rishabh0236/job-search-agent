"""Column types chosen so the same models run on SQLite now and PostgreSQL later."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar

from sqlalchemy import DateTime, String, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

EnumT = TypeVar("EnumT", bound=StrEnum)

#: JSON everywhere; JSONB (indexable, binary) when the dialect is PostgreSQL.
JsonType = JSON().with_variant(JSONB(), "postgresql")


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetimes, stored as UTC.

    SQLite has no native tz support and silently returns naive datetimes, which
    then compare unequal to aware ones and corrupt time arithmetic. This coerces
    on the way in and re-attaches UTC on the way out, so application code only
    ever sees aware datetimes on either backend.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected; construct with datetime.now(UTC)")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class StrEnumType(TypeDecorator[EnumT], Generic[EnumT]):
    """A ``StrEnum`` stored as VARCHAR and returned as the enum.

    A plain ``String`` column annotated ``Mapped[SomeEnum]`` is a trap: writes
    work (the enum stringifies), but reads hand back a ``str`` while the type
    checker still believes it is an enum. Every ``fact.category.value`` then
    type-checks and fails at runtime — which is exactly what happened.

    Values stay human-readable in the database, and no native PostgreSQL enum is
    created, so adding a member never needs a migration.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type[EnumT], length: int = 40) -> None:
        super().__init__(length=length)
        self._enum_class = enum_class
        #: Kept explicitly so migration rendering can emit a plain ``sa.String``.
        self.column_length = length

    def process_bind_param(self, value: EnumT | str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        # Accept the raw string too: filters and seed data legitimately pass one.
        return self._enum_class(value).value

    def process_result_value(self, value: str | None, dialect: Any) -> EnumT | None:
        if value is None:
            return None
        return self._enum_class(value)


def utcnow() -> datetime:
    """Timezone-aware current time. The only clock the ORM should use."""
    return datetime.now(UTC)
