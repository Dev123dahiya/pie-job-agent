"""Resumes. The master version is the source of truth and is never overwritten."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, JSONType, TimestampMixin, UUIDPrimaryKeyMixin, enum_column
from database.enums import ResumeValidationStatus

if TYPE_CHECKING:
    from database.models.core import User


class Resume(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A logical resume (e.g. "Master resume - SDE"). Content lives in ResumeVersion rows."""

    __tablename__ = "resumes"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str]
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped[User] = relationship(back_populates="resumes")
    versions: Mapped[list[ResumeVersion]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
        order_by="ResumeVersion.version_number",
        foreign_keys="ResumeVersion.resume_id",
    )


class ResumeVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An immutable file version: either a master or a job-tailored derivative."""

    __tablename__ = "resume_versions"
    __table_args__ = (UniqueConstraint("resume_id", "version_number"),)

    resume_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE"))
    version_number: Mapped[int] = mapped_column(Integer)
    is_master: Mapped[bool] = mapped_column(Boolean, default=False)
    file_path: Mapped[str] = mapped_column(String(1024))
    content_sha256: Mapped[str | None] = mapped_column(String(64))

    # Tailoring provenance (null for master versions).
    source_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resume_versions.id", ondelete="SET NULL")
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    company: Mapped[str | None]
    role: Mapped[str | None]
    changes_summary: Mapped[str | None] = mapped_column(Text)  # one-paragraph plain summary

    # --- Phase 4: tailoring provenance, validation and reports (immutable once written) ---
    validation_status: Mapped[ResumeValidationStatus | None] = mapped_column(
        enum_column(ResumeValidationStatus, length=16)
    )
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    tailoring_strategy: Mapped[str | None] = mapped_column(String(32))
    artifact_format: Mapped[str | None] = mapped_column(String(16))  # pdf | markdown
    content: Mapped[dict[str, Any] | None] = mapped_column(JSONType)  # structured resume
    change_details: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    tailoring_report: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    source_sha256: Mapped[str | None] = mapped_column(String(64))  # master file it came from
    profile_sha256: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    # The application that uses a version links to it via Application.resume_version_id.

    resume: Mapped[Resume] = relationship(back_populates="versions", foreign_keys=[resume_id])
    source_version: Mapped[ResumeVersion | None] = relationship(remote_side="ResumeVersion.id")
