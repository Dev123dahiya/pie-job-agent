import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from agent.analysis.analyzers import describe_analyzer
from backend import __version__
from backend.core.config import Settings, get_settings
from database.session import get_db

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


class ComponentStatus(BaseModel):
    status: str  # ok | unavailable | not_configured
    detail: str | None = None


class HealthResponse(BaseModel):
    status: str  # ok | degraded
    version: str
    development_mode: bool
    components: dict[str, ComponentStatus]


@router.get("/health", response_model=HealthResponse)
def health(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HealthResponse:
    components: dict[str, ComponentStatus] = {}

    try:
        db.execute(text("SELECT 1"))
        components["database"] = ComponentStatus(status="ok")
    except Exception as exc:  # report, never crash the health check
        logger.warning("Database health check failed: %s", exc)
        components["database"] = ComponentStatus(status="unavailable", detail=type(exc).__name__)

    claude_status, claude_detail = describe_analyzer(settings)
    components["claude"] = ComponentStatus(status=claude_status, detail=claude_detail)
    gmail_ready = settings.gmail_credentials_path and settings.gmail_credentials_path.exists()
    components["gmail"] = ComponentStatus(status="ok" if gmail_ready else "not_configured")

    overall = "ok" if components["database"].status == "ok" else "degraded"
    return HealthResponse(
        status=overall,
        version=__version__,
        development_mode=settings.development_mode,
        components=components,
    )
