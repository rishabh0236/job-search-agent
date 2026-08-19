"""Migrations must match the models.

Without this test, ``create_all`` in the test fixtures would happily paper over a
forgotten ``make revision`` — the schema would work in tests and break on a real
database. This is the guard that keeps the two definitions honest.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from packages.core.db.base import Base
from packages.core.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(database_url: str) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_migrations_produce_the_model_schema(settings: Settings, tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.upgrade(_alembic_config(database_url), "head")

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diff == [], (
        'models and migrations have drifted; run `make revision m="describe change"`\n'
        f"differences: {diff}"
    )


def test_downgrade_to_base_is_clean(settings: Settings, tmp_path: Path) -> None:
    """A migration that cannot be reversed is a migration you cannot test twice."""
    database_url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    config = _alembic_config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            remaining = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()

    assert remaining is None
