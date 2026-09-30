"""Browser execution of applications (Phase 5).

- BrowserSession: one browser run against one application's page (dry run or live). Stores
  enough to resume safely: target URL, page state, detected fields, what was filled, missing
  answers, challenges, screenshots and evidence. Never passwords, cookies or session tokens.
- ExternalActionLog: every external action attempted (or refused), for audit.
- SubmissionAttempt: written and committed BEFORE the final submit click. Its unique
  idempotency key makes a second submission of the same application impossible.
- ApplicationAnswer: answers the user supplied for questions the profile did not cover.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base, JSONType, TimestampMixin, UUIDPrimaryKeyMixin, enum_column, utcnow
from database.enums import (
    AnswerStatus,
    BrowserExecutionMode,
    BrowserSessionState,
    ExternalActionResult,
    SubmissionOutcome,
)


class BrowserSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "browser_sessions"

    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    mode: Mapped[BrowserExecutionMode] = mapped_column(enum_column(BrowserExecutionMode, 16))
    state: Mapped[BrowserSessionState] = mapped_column(
        enum_column(BrowserSessionState, 24), default=BrowserSessionState.CREATED, index=True
    )
    current_step: Mapped[str | None] = mapped_column(String(200))
    target_url: Mapped[str] = mapped_column(String(2048))
    target_domain: Mapped[str] = mapped_column(String(255))
    current_url: Mapped[str | None] = mapped_column(String(2048))  # sensitive params redacted
    page_title: Mapped[str | None] = mapped_column(String(512))
    # Field descriptors and the deterministic mapping plan (labels/keys only).
    detected_fields: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    filled_fields: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    skipped_fields: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    missing_answers: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    challenges: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    uploaded_resume: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    screenshots: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    blocked_requests: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    user_action_required: Mapped[str | None] = mapped_column(Text)
    prepared_at: Mapped[datetime | None]
    # LIVE only: the user's explicit confirmation for THIS prepared submission.
    user_confirmed_at: Mapped[datetime | None]
    user_confirmation: Mapped[str | None] = mapped_column(Text)
    submission_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    closed_at: Mapped[datetime | None]


class ExternalActionLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "external_action_logs"

    occurred_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("browser_sessions.id", ondelete="CASCADE"), index=True
    )
    tool: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(16), default="agent")
    target_domain: Mapped[str | None] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text)
    result: Mapped[ExternalActionResult] = mapped_column(enum_column(ExternalActionResult, 16))
    error: Mapped[str | None] = mapped_column(Text)


class SubmissionAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "submission_attempts"

    # sha256(application + canonical job URL + "submit"): at most one attempt per application.
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("browser_sessions.id", ondelete="SET NULL")
    )
    attempted_at: Mapped[datetime] = mapped_column(default=utcnow)
    outcome: Mapped[SubmissionOutcome] = mapped_column(
        enum_column(SubmissionOutcome, 24), default=SubmissionOutcome.PENDING
    )
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONType)


class ApplicationAnswer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An answer the user gave. application_id NULL = reusable for every application."""

    __tablename__ = "application_answers"

    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    question_key: Mapped[str] = mapped_column(String(200), index=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    status: Mapped[AnswerStatus] = mapped_column(
        enum_column(AnswerStatus, 16), default=AnswerStatus.VERIFIED
    )
    source: Mapped[str] = mapped_column(String(200))
