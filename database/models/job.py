"""Job postings and Claude's analysis of them."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import (
    Base,
    JSONType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    enum_column,
    utcnow,
)
from database.enums import Freshness, JobPipelineState, MatchLevel, RemoteType

if TYPE_CHECKING:
    from database.models.application import Application
    from database.models.core import Company


class Job(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A normalized job posting (spec section 8)."""

    __tablename__ = "jobs"
    __table_args__ = (
        # Primary dedupe key.
        UniqueConstraint("source", "source_job_id"),
        # Supports the secondary company + title similarity check.
        Index("ix_jobs_company_title", "company_id", "title"),
    )

    source: Mapped[str] = mapped_column(String(64))
    source_job_id: Mapped[str]
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(512))
    location: Mapped[str | None]
    remote_type: Mapped[RemoteType] = mapped_column(
        enum_column(RemoteType), default=RemoteType.UNKNOWN
    )
    # Salary is nullable throughout: missing salary is "unknown", never a rejection.
    salary_min: Mapped[float | None] = mapped_column(Numeric(14, 2))
    salary_max: Mapped[float | None] = mapped_column(Numeric(14, 2))
    salary_currency: Mapped[str | None] = mapped_column(String(8))
    experience_required: Mapped[str | None]
    description: Mapped[str | None] = mapped_column(Text)
    requirements: Mapped[list[str]] = mapped_column(JSONType, default=list)
    skills: Mapped[list[str]] = mapped_column(JSONType, default=list)
    url: Mapped[str] = mapped_column(String(2048), index=True)
    application_url: Mapped[str | None] = mapped_column(String(2048))
    company_url: Mapped[str | None] = mapped_column(String(2048))
    posted_at: Mapped[datetime | None]
    discovered_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    pipeline_state: Mapped[JobPipelineState] = mapped_column(
        enum_column(JobPipelineState), default=JobPipelineState.DISCOVERED, index=True
    )
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    # --- Deduplication (secondary keys; the primary key is source + source_job_id) ---
    # URL with tracking parameters/fragments removed; same posting across sources.
    canonical_url: Mapped[str | None] = mapped_column(String(2048), index=True)
    # sha256 of normalized company | title | location; catches re-posts under a new id.
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    # What posted_at is (published / released / created / published_tz_unknown / updated_only /
    # unknown - see integrations/job_sources/base.py). An update time is never a post date.
    posted_at_kind: Mapped[str | None] = mapped_column(String(32))
    source_updated_at: Mapped[datetime | None]
    # Salary provenance: salary_min/max are annual figures only when the source stated a yearly
    # or monthly amount; the original source value is kept as-is in salary_raw.
    salary_period: Mapped[str | None] = mapped_column(String(16))
    salary_raw: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    # Every source this posting was seen on ({source, source_job_id, url, first_seen}).
    source_attributions: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    last_seen_at: Mapped[datetime | None]
    times_seen: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    discovery_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    # Last analysis/processing error, shown in the dashboard; cleared on success.
    last_error: Mapped[str | None] = mapped_column(Text)
    # Set on an aggregator row (e.g. Adzuna, untrusted date) once the same posting is found on
    # the employer's trusted ATS: that job is the one analyzed; this row never is again.
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL")
    )

    company: Mapped[Company | None] = relationship(back_populates="jobs")
    analyses: Mapped[list[JobAnalysis]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobAnalysis.created_at"
    )
    application: Mapped[Application | None] = relationship(back_populates="job")


class JobAnalysis(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Structured, validated output of job analysis, plus the shortlist decision.

    Every dimension is stored individually so the decision stays auditable; match_score is
    decision support only. ``should_shortlist`` is the analyzer's recommendation, while
    ``shortlisted`` is the outcome of the configurable rules in config/shortlist_rules.yaml.
    """

    __tablename__ = "job_analyses"

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL")
    )
    match_score: Mapped[int | None] = mapped_column(Integer)
    role_match: Mapped[MatchLevel] = mapped_column(enum_column(MatchLevel))
    skills_match: Mapped[MatchLevel] = mapped_column(enum_column(MatchLevel))
    experience_match: Mapped[MatchLevel] = mapped_column(enum_column(MatchLevel))
    location_match: Mapped[MatchLevel] = mapped_column(enum_column(MatchLevel))
    salary_match: Mapped[MatchLevel] = mapped_column(enum_column(MatchLevel))
    freshness: Mapped[Freshness] = mapped_column(enum_column(Freshness))
    missing_skills: Mapped[list[str]] = mapped_column(JSONType, default=list)
    matching_skills: Mapped[list[str]] = mapped_column(JSONType, default=list)
    important_requirements: Mapped[list[str]] = mapped_column(JSONType, default=list)
    concerns: Mapped[list[str]] = mapped_column(JSONType, default=list)
    application_complexity: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    should_shortlist: Mapped[bool]

    # Provenance: which analyzer produced this, and exactly what it was given.
    analyzer: Mapped[str | None] = mapped_column(String(32))  # claude_api | claude_cli | heuristic
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    input_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    # Outcome of the configured shortlist rules (null until rules are evaluated).
    shortlisted: Mapped[bool | None] = mapped_column(Boolean)
    rule_results: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)

    job: Mapped[Job] = relationship(back_populates="analyses")
