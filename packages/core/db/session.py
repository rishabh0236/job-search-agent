"""Engine and session management.

SQLite needs two things the defaults get wrong for this app: foreign keys are off
unless asked for (so our ``ondelete`` clauses would be silently ignored), and WAL
mode is needed for a background worker to read while the API writes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from packages.core.settings import Settings, get_settings


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def create_db_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    url = settings.database_url
    kwargs: dict[str, Any] = {"future": True, "echo": False}

    if _is_sqlite(url):
        # check_same_thread=False: FastAPI serves requests on a threadpool.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
        if ":memory:" not in url:
            db_path = Path(url.split("sqlite:///", 1)[-1])
            db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, **kwargs)

    if _is_sqlite(url):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


def reset_engine() -> None:
    """Drop cached engine/factory. Used by tests to switch databases."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for service and script code.

    Commits on success, rolls back on any exception. Services take a ``Session``
    parameter rather than opening their own, so a whole workflow can share one
    transaction; this helper is for the outermost caller.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
