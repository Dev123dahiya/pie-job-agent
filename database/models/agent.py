"""Agent runs and the tool calls made within them (the audit log)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, JSONType, TimestampMixin, UUIDPrimaryKeyMixin, enum_column, utcnow
from database.enums import AgentActionStatus, AgentRunStatus, RunType


class AgentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_runs"

    task: Mapped[str] = mapped_column(String(512))
    run_type: Mapped[RunType] = mapped_column(
        enum_column(RunType), default=RunType.DISCOVERY, server_default=RunType.MOCK.value
    )
    status: Mapped[AgentRunStatus] = mapped_column(
        enum_column(AgentRunStatus), default=AgentRunStatus.PENDING, index=True
    )
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    ended_at: Mapped[datetime | None]
    development_mode: Mapped[bool] = mapped_column(default=True)
    model: Mapped[str | None] = mapped_column(String(128))
    jobs_processed: Mapped[int] = mapped_column(Integer, default=0)
    applications_submitted: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    summary: Mapped[str | None] = mapped_column(Text)

    # --- Discovery / analysis parameters and live progress ---
    source: Mapped[str | None] = mapped_column(String(64))
    search_terms: Mapped[list[str] | None] = mapped_column(JSONType)
    locations: Mapped[list[str] | None] = mapped_column(JSONType)
    current_task: Mapped[str | None] = mapped_column(Text)
    searches_total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    searches_completed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    jobs_found: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    jobs_new: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    jobs_duplicate: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    jobs_failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    jobs_analyzed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    jobs_shortlisted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    jobs_rejected: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Updated as the worker makes progress; a RUNNING run with an old heartbeat is dead.
    heartbeat_at: Mapped[datetime | None]

    # --- Autonomous agent runs (run_type=agent) ---
    mode: Mapped[str | None] = mapped_column(String(32))  # read_only | internal_writes
    iterations: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_iterations: Mapped[int | None] = mapped_column(Integer)
    # What the user must provide before the run can continue (status NEEDS_USER_INPUT).
    pending_user_input: Mapped[str | None] = mapped_column(Text)

    actions: Mapped[list[AgentAction]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AgentAction.sequence"
    )


class AgentAction(UUIDPrimaryKeyMixin, Base):
    """A single logged action or tool call: what happened, with what input, and why.

    ``tool_name`` holds the tool or action code (e.g. JOB_DISCOVERED, see JobLogAction).
    """

    __tablename__ = "agent_actions"
    __table_args__ = (UniqueConstraint("run_id", "sequence"),)

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(String(128), index=True)
    # Agent runs: the registered tool involved (tool_name then holds the AgentEvent code).
    tool: Mapped[str | None] = mapped_column(String(128), index=True)
    iteration: Mapped[int | None] = mapped_column(Integer)
    # sha256 of tool + canonical arguments; guards against repeating a write in one run.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), index=True)
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    status: Mapped[AgentActionStatus] = mapped_column(
        enum_column(AgentActionStatus), default=AgentActionStatus.STARTED
    )
    reasoning: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    ended_at: Mapped[datetime | None]
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL")
    )

    run: Mapped[AgentRun] = relationship(back_populates="actions")
