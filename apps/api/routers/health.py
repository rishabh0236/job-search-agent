"""Health and readiness.

More than a liveness ping: this is what the dashboard's system-status panel reads,
so it reports which capabilities are actually available. A missing LaTeX engine or
an unconfigured LLM provider should be visible in the UI, not discovered when a
user clicks Tailor.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from apps.api.deps import SessionDep, SettingsDep

router = APIRouter(tags=["system"])

Status = Literal["ok", "degraded", "unavailable"]


class CapabilityStatus(BaseModel):
    name: str
    status: Status
    detail: str = ""


class HealthResponse(BaseModel):
    status: Status
    app_env: str
    version: str
    capabilities: list[CapabilityStatus]


def _latex_status(bin_path: Path, engine: str) -> CapabilityStatus:
    resolved = bin_path if bin_path.exists() else Path(shutil.which(engine) or "")
    if resolved and resolved.exists():
        return CapabilityStatus(name="latex", status="ok", detail=str(resolved))
    return CapabilityStatus(
        name="latex",
        status="unavailable",
        detail=f"{engine} not found; run scripts/bootstrap.sh to install it",
    )


@router.get("/health", response_model=HealthResponse, summary="Service and capability health")
def health(session: SessionDep, settings: SettingsDep) -> HealthResponse:
    capabilities: list[CapabilityStatus] = []

    try:
        session.execute(text("SELECT 1"))
        capabilities.append(CapabilityStatus(name="database", status="ok"))
    except Exception as exc:
        capabilities.append(
            CapabilityStatus(name="database", status="unavailable", detail=type(exc).__name__)
        )

    if settings.llm_provider == "stub":
        capabilities.append(
            CapabilityStatus(
                name="llm",
                status="degraded",
                detail="stub provider: deterministic fixtures, no live model",
            )
        )
    elif settings.llm_configured:
        capabilities.append(
            CapabilityStatus(name="llm", status="ok", detail=f"anthropic:{settings.llm_model}")
        )
    else:
        capabilities.append(
            CapabilityStatus(
                name="llm", status="unavailable", detail="CA_ANTHROPIC_API_KEY is not set"
            )
        )

    capabilities.append(_latex_status(settings.latex_bin, settings.latex_engine))

    # A single unavailable capability degrades the service; only a broken database
    # makes it unavailable, since nothing else can proceed without it.
    database = next(c for c in capabilities if c.name == "database")
    if database.status != "ok":
        overall: Status = "unavailable"
    elif any(c.status != "ok" for c in capabilities):
        overall = "degraded"
    else:
        overall = "ok"

    return HealthResponse(
        status=overall,
        app_env=settings.app_env,
        version="0.1.0",
        capabilities=capabilities,
    )
