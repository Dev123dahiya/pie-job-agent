from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.core.config import Settings, get_settings
from backend.services.dashboard import DashboardOverview, build_overview
from database.session import get_db

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/overview", response_model=DashboardOverview)
def overview(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DashboardOverview:
    return build_overview(db, tz=settings.timezone)
