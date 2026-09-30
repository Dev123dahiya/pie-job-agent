"""Agent/state tools: let the agent inspect its own run and leave notes in the audit log."""

import uuid
from typing import Any

from pydantic import Field
from sqlalchemy import select

from agent.tools.base import (
    NoInput,
    Tool,
    ToolContext,
    ToolError,
    ToolErrorCode,
    ToolInput,
    ToolOutput,
)
from backend.services.runs import active_run
from database.enums import AgentActionStatus, ToolPermission
from database.models import AgentAction, AgentRun


def _run_summary(run: AgentRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "type": run.run_type.value,
        "status": run.status.value,
        "task": run.task,
        "started_at": run.started_at.isoformat(),
        "summary": run.summary,
    }


class AgentStatusOutput(ToolOutput):
    active_run: dict[str, Any] | None
    recent_runs: list[dict[str, Any]]


def get_agent_status(ctx: ToolContext, _: NoInput) -> AgentStatusOutput:
    live = active_run(ctx.db)
    recent = ctx.db.scalars(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(5)).all()
    return AgentStatusOutput(
        active_run=_run_summary(live) if live else None,
        recent_runs=[_run_summary(r) for r in recent],
    )


class CurrentRunOutput(ToolOutput):
    run_id: str
    task: str
    mode: str | None
    iteration: int
    max_iterations: int | None
    recent_events: list[dict[str, Any]]


def get_current_run(ctx: ToolContext, _: NoInput) -> CurrentRunOutput:
    run = ctx.run
    if run is None:
        raise ToolError(ToolErrorCode.NOT_FOUND, "Not running inside an agent run")
    events = ctx.db.scalars(
        select(AgentAction)
        .where(AgentAction.run_id == run.id)
        .order_by(AgentAction.sequence.desc())
        .limit(10)
    ).all()
    return CurrentRunOutput(
        run_id=str(run.id),
        task=run.task,
        mode=run.mode,
        iteration=run.iterations,
        max_iterations=run.max_iterations,
        recent_events=[
            {
                "event": a.tool_name,
                "tool": a.tool,
                "status": a.status.value,
                "summary": a.reasoning or a.error,
            }
            for a in reversed(events)
        ],
    )


class NoteInput(ToolInput):
    summary: str = Field(min_length=3, max_length=500)
    job_id: uuid.UUID | None = None


class NoteOutput(ToolOutput):
    recorded: bool
    sequence: int


def record_agent_action(ctx: ToolContext, p: NoteInput) -> NoteOutput:
    if ctx.log is None or ctx.run is None:
        raise ToolError(ToolErrorCode.PRECONDITION_FAILED, "Not running inside an agent run")
    entry = ctx.log.log(
        "AGENT_NOTE", job_id=p.job_id, reasoning=p.summary, status=AgentActionStatus.SUCCESS
    )
    return NoteOutput(recorded=True, sequence=entry.sequence)


TOOLS = [
    Tool(
        name="get_agent_status",
        description="Status of the PIE agent: the active run (if any) and recent runs.",
        permission=ToolPermission.READ_ONLY,
        input_model=NoInput,
        output_model=AgentStatusOutput,
        handler=get_agent_status,
    ),
    Tool(
        name="get_current_run",
        description="This run's task, mode, iteration count and last 10 events.",
        permission=ToolPermission.READ_ONLY,
        input_model=NoInput,
        output_model=CurrentRunOutput,
        handler=get_current_run,
    ),
    Tool(
        name="record_agent_action",
        description="Write a short factual note to this run's audit log (e.g. a finding).",
        permission=ToolPermission.WRITE,
        input_model=NoteInput,
        output_model=NoteOutput,
        handler=record_agent_action,
    ),
]
