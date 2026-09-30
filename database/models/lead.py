"""Leads from untrusted aggregators and the employer ATS boards they resolve to (Phase 5.7).

A lead is NOT a job. Its aggregator timestamp is stored for reference only, is always marked
untrusted and is never read by freshness code. Only the trusted ATS posting a lead resolves to
enters the normal pipeline, with that ATS's own publication timestamp.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, JSONType, TimestampMixin, UUIDPrimaryKeyMixin, enum_column, utcnow
from database.enums import BoardStatus, LeadStatus


class EmployerBoard(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """employer -> supported ATS board. NOT_FOUND rows (platform/board_id NULL) are the
    negative cache. Only VERIFIED + active boards are ever crawled; configured boards are
    already crawled by normal discovery and are only used for matching."""

    __tablename__ = "employer_boards"
    __table_args__ = (UniqueConstraint("platform", "board_id"),)

    normalized_company: Mapped[str] = mapped_column(String(255), index=True)
    company_name: Mapped[str] = mapped_column(String(255))
    platform: Mapped[str | None] = mapped_column(
        String(32)
    )  # greenhouse|lever|ashby|smartrecruiters
    board_id: Mapped[str | None] = mapped_column(String(255))  # exact identifier the adapter uses
    status: Mapped[BoardStatus] = mapped_column(enum_column(BoardStatus), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    configured: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    approved_at: Mapped[datetime | None]
    last_checked_at: Mapped[datetime | None]
    next_check_at: Mapped[datetime | None]

    leads: Mapped[list[JobLead]] = relationship(back_populates="board")

    @property
    def label(self) -> str:
        return f"{self.platform}:{self.board_id}" if self.platform else "(none)"


class JobLead(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "job_leads"
    __table_args__ = (UniqueConstraint("source", "source_lead_id"),)

    source: Mapped[str] = mapped_column(String(64))  # adzuna | himalayas
    source_lead_id: Mapped[str] = mapped_column(String(512))
    company_name: Mapped[str | None] = mapped_column(String(255))
    normalized_company: Mapped[str | None] = mapped_column(String(255), index=True)
    company_hint: Mapped[str | None] = mapped_column(String(255))  # e.g. the aggregator's slug
    title: Mapped[str] = mapped_column(String(512))
    location: Mapped[str | None] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(Text)  # capped; evidence for matching
    # Query string always stripped (Adzuna redirect URLs would carry the app id).
    source_url: Mapped[str | None] = mapped_column(String(2048))
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    times_seen: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # Untrusted by definition: display only, NEVER a posted_at, never used for freshness.
    aggregator_timestamp: Mapped[datetime | None]
    aggregator_timestamp_trusted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    status: Mapped[LeadStatus] = mapped_column(
        enum_column(LeadStatus), default=LeadStatus.NEW, index=True
    )
    # The aggregator's own job row, if it keeps one (Adzuna does).
    aggregator_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL")
    )
    employer_board_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("employer_boards.id", ondelete="SET NULL")
    )
    matched_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    resolved_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    board: Mapped[EmployerBoard | None] = relationship(back_populates="leads")
