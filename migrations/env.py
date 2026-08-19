"""Alembic environment.

Reads the database URL from application settings so there is one source of truth,
and enables batch mode for SQLite (which cannot ALTER most constraints in place —
without this, any future column change would fail locally but pass on PostgreSQL).
"""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import engine_from_config, pool

# Import for metadata registration side effects; autogenerate needs every model.
import packages.core.db.models  # noqa: F401
from packages.core.db.base import Base
from packages.core.db.types import StrEnumType, UtcDateTime
from packages.core.settings import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()

# An explicitly configured URL wins, so callers (tests, one-off scripts) can point
# a migration run at a different database. Falling straight through to settings
# would silently ignore that override and migrate the wrong database.
database_url = config.get_main_option("sqlalchemy.url") or settings.database_url
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def _is_sqlite() -> bool:
    return database_url.startswith("sqlite")


def render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    """Render our ``TypeDecorator`` columns as plain SQLAlchemy types.

    Autogenerate renders a custom type by name and cannot reconstruct its
    constructor arguments, so ``StrEnumType`` came out as ``StrEnumType()`` and the
    migration failed to run. Both decorators are thin wrappers whose database type is
    exactly the underlying one, so emitting that keeps migrations correct *and*
    independent of application code — a migration should not break because a Python
    class moved.
    """
    if type_ == "type":
        if isinstance(obj, StrEnumType):
            return f"sa.String(length={obj.column_length})"
        if isinstance(obj, UtcDateTime):
            return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(),
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=_is_sqlite(),
            compare_type=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
