"""Daily discovery + analysis: its schedule, the latest run's report, and a manual trigger.

Discovery and analysis only - nothing here creates or submits applications."""

import uuid
from datetime import datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from backend.api.deps import (
    SourceBuilder,
    profile_dep,
    rules_dep,
    session_factory_dep,
    source_builder_dep,
    sources_config_dep,
)
from backend.api.schemas import RunAccepted
from backend.core.config import Settings, get_settings
from backend.core.profile import UserProfile
from backend.services.daily import (
    DailyRunService,
    build_daily_plan,
    create_daily_run,
    latest_daily_report,
    next_slot,
    parse_run_time,
)
from backend.services.runs import RunConflictError
from backend.services.shortlist import ShortlistRules
from database.base import utcnow
from database.session import get_db
from integrations.job_sources.config import JobSourcesConfig

router = APIRouter(prefix="/daily-run", tags=["daily-run"])

DB = Annotated[Session, Depends(get_db)]


class DailyRunOut(BaseModel):
    run_id: uuid.UUID
    status: str
    trigger: str
    started_at: datetime
    ended_at: datetime | None
    summary: str | None
    report: dict[str, Any] | None  # counts, per-source results, newly shortlisted jobs


class DailyScheduleOut(BaseModel):
    enabled: bool
    time: str
    timezone: str
    next_run_at: datetime | None
    latest: DailyRunOut | None


@router.get("", response_model=DailyScheduleOut)
def daily_status(db: DB, settings: Annotated[Settings, Depends(get_settings)]) -> DailyScheduleOut:
    tz = ZoneInfo(settings.timezone)
    next_at = (
        next_slot(utcnow().astimezone(tz), parse_run_time(settings.daily_run_time))
        if settings.daily_run_enabled
        else None
    )
    found = latest_daily_report(db)
    latest = None
    if found is not None:
        run, report = found
        latest = DailyRunOut(
            run_id=run.id,
            status=run.status.value,
            trigger="scheduled" if "(scheduled)" in run.task else "manual",
            started_at=run.started_at,
            ended_at=run.ended_at,
            summary=run.summary,
            report=report,
        )
    return DailyScheduleOut(
        enabled=settings.daily_run_enabled,
        time=settings.daily_run_time,
        timezone=settings.timezone,
        next_run_at=next_at,
        latest=latest,
    )


@router.post("", response_model=RunAccepted, status_code=202)
def trigger_daily_run(
    background: BackgroundTasks,
    db: DB,
    settings: Annotated[Settings, Depends(get_settings)],
    config: Annotated[JobSourcesConfig, Depends(sources_config_dep)],
    profile: Annotated[UserProfile, Depends(profile_dep)],
    rules: Annotated[ShortlistRules, Depends(rules_dep)],
    build: Annotated[SourceBuilder, Depends(source_builder_dep)],
    factory: Annotated[sessionmaker[Session], Depends(session_factory_dep)],
) -> RunAccepted:
    """Run the daily discovery + analysis now (works even when the schedule is disabled)."""
    plan = build_daily_plan(
        settings, config=config, profile=profile, rules=rules, build_source=build
    )
    if not plan.sources:
        raise HTTPException(400, "No job sources are enabled in config/job_sources.yaml")
    try:
        run = create_daily_run(db, plan, "manual")
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    background.add_task(DailyRunService(factory).execute, run.id, plan)
    return RunAccepted(
        run_ids=[run.id], message=f"Daily run started for: {', '.join(plan.sources)}"
    )
