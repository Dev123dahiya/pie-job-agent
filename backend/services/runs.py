"""Agent-run bookkeeping: action log, heartbeats, cancellation, one-run-at-a-time rule."""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.base import utcnow
from database.enums import AgentActionStatus, AgentRunStatus, JobLogAction
from database.models import AgentAction, AgentRun

logger = logging.getLogger("pie.agent")

# A RUNNING run whose worker has not reported progress for this long is considered dead.
STALE_AFTER = timedelta(minutes=10)
ACTIVE_STATUSES = (AgentRunStatus.RUNNING, AgentRunStatus.STOPPING, AgentRunStatus.PENDING)


class RunConflictError(RuntimeError):
    """Another run is already active."""


class RunLogger:
    """Writes AgentAction rows (the audit log) and mirrors them to the application log."""

    def __init__(self, db: Session, run: AgentRun) -> None:
        self.db = db
        self.run = run
        self._seq = (
            db.scalar(
                select(func.coalesce(func.max(AgentAction.sequence), 0)).where(
                    AgentAction.run_id == run.id
                )
            )
            or 0
        )

    def log(
        self,
        action: JobLogAction | str,
        *,
        job_id: uuid.UUID | None = None,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        status: AgentActionStatus = AgentActionStatus.SUCCESS,
        error: str | None = None,
        reasoning: str | None = None,
    ) -> AgentAction:
        self._seq += 1
        now = utcnow()
        entry = AgentAction(
            run_id=self.run.id,
            sequence=self._seq,
            tool_name=str(action),
            input=input,
            output=output,
            status=status,
            error=error,
            reasoning=reasoning,
            job_id=job_id,
            started_at=now,
            ended_at=now,
        )
        self.db.add(entry)
        logger.log(
            logging.WARNING if error else logging.INFO,
            "run=%s action=%s job=%s result=%s%s",
            self.run.id,
            action,
            job_id or "-",
            status.value,
            f" error={error}" if error else "",
        )
        return entry

    def error(self, message: str, **context: Any) -> None:
        self.run.errors = [
            *self.run.errors,
            {"at": utcnow().isoformat(), "error": message, **context},
        ]

    def heartbeat(self, task: str | None = None) -> None:
        self.run.heartbeat_at = utcnow()
        if task is not None:
            self.run.current_task = task


def _is_stale(run: AgentRun, now: datetime) -> bool:
    last = run.heartbeat_at or run.started_at
    return now - last > STALE_AFTER


def active_run(db: Session, now: datetime | None = None) -> AgentRun | None:
    """The live run, if any. Runs whose worker died are closed out as FAILED."""
    now = now or utcnow()
    runs = db.scalars(select(AgentRun).where(AgentRun.status.in_(ACTIVE_STATUSES))).all()
    live = None
    for run in runs:
        if _is_stale(run, now):
            run.status = AgentRunStatus.FAILED
            run.ended_at = now
            run.current_task = None
            run.errors = [
                *run.errors,
                {"at": now.isoformat(), "error": "Worker stopped responding"},
            ]
        else:
            live = run
    db.commit()
    return live


def ensure_no_active_run(db: Session) -> None:
    run = active_run(db)
    if run is not None:
        raise RunConflictError(f"Run {run.id} ({run.task}) is still in progress")


def request_cancel(db: Session, run_id: uuid.UUID) -> AgentRun | None:
    run = db.get(AgentRun, run_id)
    if run is None:
        return None
    db.refresh(run)  # decide on the current status, not a stale in-session copy
    if run.status in (AgentRunStatus.RUNNING, AgentRunStatus.PENDING):
        run.status = AgentRunStatus.STOPPING  # the worker stops at its next safe point
        db.commit()
    elif run.status is AgentRunStatus.NEEDS_USER_INPUT:
        # Paused: no worker is running, so close it directly.
        finish_run(run, AgentRunStatus.STOPPED, summary="Cancelled while waiting for input")
        run.pending_user_input = None
        db.commit()
    return run


def cancel_requested(db: Session, run: AgentRun) -> bool:
    db.refresh(run, attribute_names=["status"])
    return run.status is AgentRunStatus.STOPPING


def finish_run(run: AgentRun, status: AgentRunStatus, summary: str | None = None) -> None:
    run.status = status
    run.ended_at = utcnow()
    run.current_task = None
    run.heartbeat_at = run.ended_at
    if summary:
        run.summary = summary
