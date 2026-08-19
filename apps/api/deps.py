"""FastAPI dependencies.

The API layer's whole job is: parse the request, resolve dependencies, call a
service, serialise the result. Business logic lives in ``services/``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from packages.core.db.session import get_session_factory
from packages.core.llm.client import LLMClient, build_client
from packages.core.settings import Settings, get_settings


def db_session() -> Iterator[Session]:
    """Request-scoped session.

    Commits when the handler returns, rolls back on any exception, so a failed
    request can never leave a half-written application or an orphaned audit row.
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


def llm_client() -> LLMClient:
    return build_client()


SessionDep = Annotated[Session, Depends(db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
LLMDep = Annotated[LLMClient, Depends(llm_client)]
