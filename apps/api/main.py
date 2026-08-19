"""FastAPI application factory.

Domain errors are translated to HTTP in exactly one place. Services raise
``DomainError`` subclasses and never import ``fastapi``, which keeps them usable
from scripts, tests and the background worker.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from apps.api.routers import applications, candidates, health, jobs
from packages.core.errors import DomainError, SafetyStop
from packages.core.logging import configure_logging, get_logger, safe_extra
from packages.core.settings import get_settings

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.ensure_dirs()
    logger.info(
        "api.startup",
        extra={
            "app_env": settings.app_env,
            "llm_provider": settings.llm_provider,
            "llm_configured": settings.llm_configured,
        },
    )
    yield
    logger.info("api.shutdown")


def create_app(*, serve_frontend: bool = True) -> FastAPI:
    """Build the application.

    ``serve_frontend`` exists because the SPA is mounted at ``/`` and a mount matches
    every path beneath it: anything registered *after* it is unreachable. Production
    registers all API routers first, so this only matters for callers that add their
    own routes afterwards (tests do).
    """
    settings = get_settings()

    app = FastAPI(
        title="AI Career Agent",
        version="0.1.0",
        description=(
            "Local-first, evidence-grounded job discovery, resume tailoring and "
            "human-approved application preparation."
        ),
        lifespan=lifespan,
    )

    # Local-only frontend origins. Deliberately explicit rather than "*": the API
    # serves candidate PII and will later hold session credentials.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
        body: dict[str, object] = {
            "error": {"code": exc.code, "message": exc.message, "details": exc.details}
        }
        if isinstance(exc, SafetyStop):
            # The UI renders these as an interrupt with an explanation, not a toast.
            body["error"]["reason"] = exc.reason  # type: ignore[index]
            body["error"]["requires_user_action"] = True  # type: ignore[index]
            logger.warning(
                "safety_stop", extra=safe_extra({"reason": exc.reason, "detail": exc.message})
            )
        return JSONResponse(status_code=exc.http_status, content=body)

    app.include_router(health.router)
    app.include_router(candidates.router)
    app.include_router(jobs.router)
    app.include_router(applications.router)

    if serve_frontend:
        _mount_frontend(app)

    logger.info("api.configured", extra={"app_env": settings.app_env})
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built UI from the API when it exists.

    One process and one origin in the deployed path: no CORS, no second server, and a
    hash-routed SPA needs no rewrite rules. In development the Vite server proxies
    /api here instead, so the two modes have the same shape.
    """
    dist = Path(__file__).resolve().parents[2] / "apps" / "web" / "dist"
    if not (dist / "index.html").is_file():
        logger.info("api.frontend_not_built", extra={"expected_path": str(dist)})
        return

    # html=True serves index.html for unknown paths, which is what a SPA needs. It is
    # mounted last so every API route above still takes precedence.
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="web")
    logger.info("api.frontend_mounted", extra={"path": str(dist)})


app = create_app()
