from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.core.config import Settings, get_settings
from backend.services.dashboard import DashboardOverview, build_overview
from backend.services.limits import LimitsConfigError, usage
from database.session import get_db

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/overview", response_model=DashboardOverview)
def overview(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DashboardOverview:
    return build_overview(db, tz=settings.timezone)


class DailyLimitOut(BaseModel):
    platform: str  # total | naukri | linkedin | company_site
    used: int
    limit: int
    remaining: int


@router.get("/limits", response_model=list[DailyLimitOut])
def limits(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[DailyLimitOut]:
    """Today's live submissions against the daily limits (read only)."""
    try:
        rows = usage(db, settings.automation_path, settings.timezone)
    except LimitsConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return [
        DailyLimitOut(platform=r.platform, used=r.used, limit=r.limit, remaining=r.remaining)
        for r in rows
    ]
