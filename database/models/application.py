"""Applications and their full event timeline."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, JSONType, TimestampMixin, UUIDPrimaryKeyMixin, enum_column, utcnow
from database.enums import ApplicationStatus, EventSource

if TYPE_CHECKING:
    from database.models.email import Email
    from database.models.job import Job
    from database.models.resume import ResumeVersion


class Application(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "applications"

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), unique=True
    )
    resume_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resume_versions.id", ondelete="SET NULL")
    )
    status: Mapped[ApplicationStatus] = mapped_column(
        enum_column(ApplicationStatus), default=ApplicationStatus.SHORTLISTED, index=True
    )
    # Set only when the browser workflow has confirmed submission.
    applied_at: Mapped[datetime | None]
    confirmed_at: Mapped[datetime | None]
    requires_user_input: Mapped[bool] = mapped_column(Boolean, default=False)
    user_action_required: Mapped[str | None] = mapped_column(Text)
    next_action: Mapped[str | None] = mapped_column(Text)
    recruiter: Mapped[str | None]
    notes: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)

    job: Mapped[Job] = relationship(back_populates="application")
    resume_version: Mapped[ResumeVersion | None] = relationship()
    events: Mapped[list[ApplicationEvent]] = relationship(
        back_populates="application", order_by="ApplicationEvent.occurred_at"
    )
    emails: Mapped[list[Email]] = relationship(back_populates="application")


class ApplicationEvent(UUIDPrimaryKeyMixin, Base):
    """One entry in a job/application timeline, including every status change.

    Keyed on job_id so the timeline can start at discovery, before an Application exists.
    """

    __tablename__ = "application_events"

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    source: Mapped[EventSource] = mapped_column(enum_column(EventSource))
    previous_status: Mapped[str | None] = mapped_column(String(40))
    new_status: Mapped[str | None] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    email_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("emails.id", ondelete="SET NULL"))
    agent_action_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_actions.id", ondelete="SET NULL")
    )

    application: Mapped[Application | None] = relationship(back_populates="events")
