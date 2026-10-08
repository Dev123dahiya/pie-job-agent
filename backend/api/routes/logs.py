"""Logs: search and download the backend's log, redacted (REQUIREMENTS 19.2.11). Read only."""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from backend.core.config import Settings, get_settings
from backend.services.logs import Level, LogFilter, export_text, search
from backend.services.platforms import Platform

router = APIRouter(prefix="/logs", tags=["logs"])
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _filter(
    q: Annotated[str | None, Query(max_length=200)] = None,
    level: Level | None = None,
    run: Annotated[str | None, Query(max_length=64, pattern=r"^[0-9a-fA-F-]+$")] = None,
    platform: Platform | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> LogFilter:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    return LogFilter(
        q=q or None,
        level=level,
        run=run,
        platform=platform,
        date_from=date_from,
        date_to=date_to,
    )


FilterDep = Annotated[LogFilter, Depends(_filter)]


@router.get("")
def list_logs(
    settings: SettingsDep,
    flt: FilterDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    page = search(settings.log_dir, flt, limit, offset)
    return {
        "items": [line.as_dict() for line in page.items],
        "total": page.total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/download", response_class=PlainTextResponse)
def download_logs(settings: SettingsDep, flt: FilterDep) -> PlainTextResponse:
    """The filtered log as a .txt file (secrets redacted)."""
    return PlainTextResponse(
        export_text(settings.log_dir, flt),
        headers={"Content-Disposition": 'attachment; filename="pie-logs.txt"'},
    )
