import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.analysis.analyzers import describe_analyzer
from agent.tools import ToolRegistry
from backend.api.deps import agent_runner_dep, registry_dep
from backend.api.schemas import (
    ActionOut,
    AgentStatus,
    ResumeAgentRequest,
    RunDetail,
    RunOut,
    StartAgentRequest,
    ToolInfo,
)
from backend.core.config import Settings, get_settings, live_execution_block
from backend.services.agent_runner import (
    AgentRunner,
    LiveModeDisabledError,
    resume_agent_run,
    start_agent_run,
)
from backend.services.runs import RunConflictError, active_run, request_cancel
from database.enums import AgentRunStatus, RunType
from database.models import AgentAction, AgentRun
from database.session import get_db

router = APIRouter(prefix="/agent", tags=["agent"])
DB = Annotated[Session, Depends(get_db)]


@router.get("/status", response_model=AgentStatus)
def status(db: DB, settings: Annotated[Settings, Depends(get_settings)]) -> AgentStatus:
    run = active_run(db)
    recent = db.scalars(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(5)).all()
    waiting = db.scalar(
        select(AgentRun)
        .where(
            AgentRun.run_type == RunType.AGENT, AgentRun.status == AgentRunStatus.NEEDS_USER_INPUT
        )
        .order_by(AgentRun.started_at.desc())
        .limit(1)
    )
    analyzer_state, analyzer_detail = describe_analyzer(settings)
    state: Literal["idle", "running", "stopping"] = (
        "idle"
        if run is None
        else ("stopping" if run.status is AgentRunStatus.STOPPING else "running")
    )
    return AgentStatus(
        state=state,
        active_run=RunOut.of(run) if run else None,
        awaiting_user=RunOut.of(waiting) if waiting else None,
        recent_runs=[RunOut.of(r) for r in recent],
        analyzer=analyzer_state,
        analyzer_detail=analyzer_detail,
        external_live_enabled=live_execution_block(settings) is None,
        external_live_configured=settings.external_live_enabled,
        development_mode=settings.development_mode,
    )


@router.get("/tools", response_model=list[ToolInfo])
def list_tools(registry: Annotated[ToolRegistry, Depends(registry_dep)]) -> list[ToolInfo]:
    """Read-only catalog of the agent's tools and their permissions (no execution here)."""
    return [ToolInfo(**entry) for entry in registry.catalog()]


@router.post("/runs", response_model=RunOut, status_code=202)
def start_run(
    body: StartAgentRequest,
    background: BackgroundTasks,
    db: DB,
    settings: Annotated[Settings, Depends(get_settings)],
    runner: Annotated[AgentRunner, Depends(agent_runner_dep)],
) -> RunOut:
    try:
        run = start_agent_run(
            db, body.task, mode=body.mode, max_iterations=body.max_iterations,
            dev_mode=settings.development_mode, backend=runner.claude.name,
            live_enabled=settings.external_live_enabled,
            development_mode=settings.development_mode,
        )  # fmt: skip
    except LiveModeDisabledError as exc:
        raise HTTPException(403, str(exc)) from exc
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    background.add_task(runner.execute, run.id)
    return RunOut.of(run)


@router.post("/runs/{run_id}/resume", response_model=RunOut, status_code=202)
def resume_run(
    run_id: uuid.UUID,
    background: BackgroundTasks,
    db: DB,
    settings: Annotated[Settings, Depends(get_settings)],
    runner: Annotated[AgentRunner, Depends(agent_runner_dep)],
    body: ResumeAgentRequest | None = None,
) -> RunOut:
    body = body or ResumeAgentRequest()
    try:
        run = resume_agent_run(
            db, run_id, message=body.message, extra_iterations=body.extra_iterations,
            live_enabled=settings.external_live_enabled,
            development_mode=settings.development_mode,
        )  # fmt: skip
    except LiveModeDisabledError as exc:
        raise HTTPException(403, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    background.add_task(runner.execute, run.id)
    return RunOut.of(run)


@router.get("/runs", response_model=list[RunOut])
def list_runs(
    db: DB,
    limit: int = Query(20, ge=1, le=100),
    run_type: RunType | None = None,
) -> list[RunOut]:
    stmt = select(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit)
    if run_type is not None:
        stmt = stmt.where(AgentRun.run_type == run_type)
    return [RunOut.of(r) for r in db.scalars(stmt).all()]


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: uuid.UUID, db: DB, actions_limit: int = Query(500, ge=1, le=5000)) -> RunDetail:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    actions = db.scalars(
        select(AgentAction)
        .where(AgentAction.run_id == run_id)
        .order_by(AgentAction.sequence.desc())
        .limit(actions_limit)
    ).all()
    return RunDetail(
        **RunOut.of(run).model_dump(), actions=[ActionOut.of(a) for a in reversed(actions)]
    )


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
def cancel_run(run_id: uuid.UUID, db: DB) -> RunOut:
    run = request_cancel(db, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return RunOut.of(run)
