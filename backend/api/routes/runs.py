"""Runs page: start, follow and stop runs (REQUIREMENTS 19.2.9). Safety checks live in
backend/services/pipeline.py (dry run by default; review mode needs the live switches and a
confirmation naming the platform and today's remaining limit)."""

import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agent.tools import ToolRegistry
from backend.api.deps import (
    browser_dep,
    profile_dep,
    registry_dep,
    rules_dep,
    session_factory_dep,
    source_builder_dep,
    sources_config_dep,
)
from backend.core.config import Settings, get_settings, live_execution_block
from backend.core.profile import UserProfile
from backend.services.daily import build_daily_plan
from backend.services.pipeline import (
    BUILT_PLATFORMS,
    MAX_ANALYSIS,
    STEPS,
    Mode,
    PipelineDeps,
    PipelineError,
    PipelineRequest,
    PipelineService,
    Step,
    live_confirmation,
    start_pipeline,
)
from backend.services.platforms import PLATFORMS, Platform
from backend.services.runs import RunConflictError, request_cancel
from backend.services.shortlist import ShortlistRules
from database.enums import JobLogAction, RunType
from database.models import AgentAction, AgentRun
from database.session import get_db
from integrations.job_sources.config import JobSourcesConfig

router = APIRouter(prefix="/runs", tags=["runs"])
DB = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
SHOWN_TYPES = (RunType.PIPELINE, RunType.DAILY)


def claude_factory_dep(settings: SettingsDep) -> Callable[[], Any]:
    """Claude for resume tailoring, resolved only when a tailor step needs it."""
    from agent.analysis.analyzers import resolve_structured_claude

    return lambda: resolve_structured_claude(settings)


class StartRunIn(BaseModel):
    steps: list[Step] = Field(min_length=1, max_length=4)
    platforms: list[Platform] = Field(default=["company_site"], min_length=1)
    mode: Mode = "dry-run"
    analysis_limit: int = Field(25, ge=1, le=MAX_ANALYSIS)
    tailor_limit: int = Field(5, ge=1, le=20)
    apply_limit: int = Field(5, ge=1, le=20)
    confirmation: str | None = Field(None, max_length=200)


def _run_out(run: AgentRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "task": run.task,
        "run_type": run.run_type.value,
        "status": run.status.value,
        "mode": run.mode,
        "platforms": run.source,
        "current_task": run.current_task,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "steps_total": run.searches_total,
        "steps_done": run.searches_completed,
        "jobs_processed": run.jobs_processed,
        "summary": run.summary,
        "errors": run.errors[-5:],
    }


@router.get("/options")
def options(db: DB, settings: SettingsDep) -> dict[str, Any]:
    """What can be started now: steps, platforms (built or not), modes and the exact
    confirmation each platform needs for review (live) mode."""
    block = live_execution_block(settings)
    return {
        "steps": list(STEPS),
        "platforms": [
            {
                "platform": p,
                "apply_supported": p in BUILT_PLATFORMS,
                "review_confirmation": live_confirmation(db, settings, p),
            }
            for p in PLATFORMS
        ],
        "modes": ["dry-run", "review"],
        "default_mode": "dry-run",
        "live_possible": block is None and not settings.browser_headless,
        "live_note": block[1]
        if block
        else ("Set BROWSER_HEADLESS=false for review mode." if settings.browser_headless else None),
        "max_analysis": MAX_ANALYSIS,
    }


@router.post("", status_code=202)
def start(
    body: StartRunIn,
    background: BackgroundTasks,
    db: DB,
    settings: SettingsDep,
    profile: Annotated[UserProfile, Depends(profile_dep)],
    rules: Annotated[ShortlistRules, Depends(rules_dep)],
    config: Annotated[JobSourcesConfig, Depends(sources_config_dep)],
    build: Annotated[Callable[[str], Any], Depends(source_builder_dep)],
    registry: Annotated[ToolRegistry, Depends(registry_dep)],
    browser: Annotated[Any, Depends(browser_dep)],
    factory: Annotated[sessionmaker[Session], Depends(session_factory_dep)],
    claude_factory: Annotated[Callable[[], Any], Depends(claude_factory_dep)],
) -> dict[str, Any]:
    req = PipelineRequest(
        steps=tuple(body.steps),
        platforms=tuple(dict.fromkeys(body.platforms)),
        mode=body.mode,
        analysis_limit=body.analysis_limit,
        tailor_limit=body.tailor_limit,
        apply_limit=body.apply_limit,
    )
    try:
        run = start_pipeline(db, settings, req, body.confirmation)
    except PipelineError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    deps = PipelineDeps(
        plan=lambda: build_daily_plan(
            settings, config=config, profile=profile, rules=rules, build_source=build
        ),
        settings=settings,
        profile=profile,
        registry=registry,
        browser=browser,
        claude_factory=claude_factory,
    )
    background.add_task(PipelineService(factory).execute, run.id, req, deps)
    return {"run_id": str(run.id), "message": f"Started: {', '.join(req.steps)} ({req.mode})"}


@router.get("")
def history(db: DB, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> dict[str, Any]:
    runs = db.scalars(
        select(AgentRun)
        .where(AgentRun.run_type.in_(SHOWN_TYPES))
        .order_by(AgentRun.started_at.desc())
        .limit(limit)
    ).all()
    return {"items": [_run_out(r) for r in runs]}


@router.get("/{run_id}")
def detail(run_id: uuid.UUID, db: DB) -> dict[str, Any]:
    run = db.get(AgentRun, run_id)
    if run is None or run.run_type not in SHOWN_TYPES:
        raise HTTPException(404, "Run not found")
    done_actions = (JobLogAction.PIPELINE_COMPLETED.value, JobLogAction.DAILY_RUN_COMPLETED.value)
    done = db.scalar(
        select(AgentAction).where(
            AgentAction.run_id == run.id, AgentAction.tool_name.in_(done_actions)
        )
    )
    progress = db.scalars(
        select(AgentAction)
        .where(
            AgentAction.run_id == run.id, AgentAction.tool_name == JobLogAction.PIPELINE_STEP.value
        )
        .order_by(AgentAction.sequence.desc())
    ).first()
    return {
        **_run_out(run),
        "report": done.output if done else None,
        "progress": progress.output if progress else None,
    }


@router.post("/{run_id}/stop")
def stop(run_id: uuid.UUID, db: DB) -> dict[str, Any]:
    """Stop safely after the current job."""
    run = request_cancel(db, run_id)
    if run is None or run.run_type not in SHOWN_TYPES:
        raise HTTPException(404, "Run not found")
    return _run_out(run)
