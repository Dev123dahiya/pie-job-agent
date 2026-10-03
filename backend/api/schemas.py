"""API response/request schemas. Database objects are never returned directly."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.services.freshness import compute_freshness
from backend.services.platforms import apply_type_of, platform_of
from backend.services.statuses import user_status
from database.enums import Freshness
from database.models import (
    AgentAction,
    AgentRun,
    ApplicationEvent,
    Job,
    JobAnalysis,
    ResumeVersion,
)


class AnalysisOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    match_score: int | None
    role_match: str
    skills_match: str
    experience_match: str
    location_match: str
    salary_match: str
    freshness: str
    matching_skills: list[str]
    missing_skills: list[str]
    important_requirements: list[str]
    concerns: list[str]
    reason: str | None
    should_shortlist: bool
    shortlisted: bool | None
    rule_results: list[dict[str, Any]]
    analyzer: str | None
    model: str | None
    prompt_version: str | None

    @classmethod
    def of(cls, a: JobAnalysis) -> "AnalysisOut":
        return cls(
            id=a.id,
            created_at=a.created_at,
            match_score=a.match_score,
            role_match=a.role_match.value,
            skills_match=a.skills_match.value,
            experience_match=a.experience_match.value,
            location_match=a.location_match.value,
            salary_match=a.salary_match.value,
            freshness=a.freshness.value,
            matching_skills=a.matching_skills,
            missing_skills=a.missing_skills,
            important_requirements=a.important_requirements,
            concerns=a.concerns,
            reason=a.reason,
            should_shortlist=a.should_shortlist,
            shortlisted=a.shortlisted,
            rule_results=a.rule_results,
            analyzer=a.analyzer,
            model=a.model,
            prompt_version=a.prompt_version,
        )


class JobSummary(BaseModel):
    id: uuid.UUID
    source: str
    company: str | None
    title: str
    location: str | None
    remote_type: str
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    experience_required: str | None
    posted_at: datetime | None
    discovered_at: datetime
    freshness: Freshness
    status: str  # PIE's detailed pipeline state
    user_status: str  # one of the six: found / skipped / applied / needs_input / manual / failed
    platform: str  # naukri | linkedin | company_site
    apply_type: str  # company_form | naukri_direct | linkedin_easy_apply | external_link
    url: str
    times_seen: int
    last_error: str | None
    analysis: AnalysisOut | None
    tailored_version: int | None = None  # latest VALIDATED tailored resume version, if any
    # Provenance: what the post date is, other sources the same posting was seen on, and the
    # salary as the source stated it when it could not be normalized.
    posted_at_kind: str | None = None
    also_on: list[str] = []
    salary_period: str | None = None
    salary_note: str | None = None

    @classmethod
    def of(
        cls, job: Job, analysis: JobAnalysis | None, now: datetime, max_age_hours: int
    ) -> "JobSummary":
        return cls(
            id=job.id,
            source=job.source,
            company=job.company.name if job.company else None,
            title=job.title,
            location=job.location,
            remote_type=job.remote_type.value,
            salary_min=float(job.salary_min) if job.salary_min is not None else None,
            salary_max=float(job.salary_max) if job.salary_max is not None else None,
            salary_currency=job.salary_currency,
            experience_required=job.experience_required,
            posted_at=job.posted_at,
            discovered_at=job.discovered_at,
            freshness=compute_freshness(job.posted_at, now, max_age_hours),
            posted_at_kind=job.posted_at_kind,
            also_on=sorted(
                {a.get("source", "") for a in job.source_attributions or []} - {job.source, ""}
            ),
            salary_period=job.salary_period,
            salary_note=_salary_note(job),
            status=job.pipeline_state.value,
            user_status=user_status(job.pipeline_state),
            platform=platform_of(job),
            apply_type=apply_type_of(job),
            url=job.url,
            times_seen=job.times_seen,
            last_error=job.last_error,
            analysis=AnalysisOut.of(analysis) if analysis else None,
        )


class TimelineEvent(BaseModel):
    occurred_at: datetime
    event_type: str
    source: str
    previous_status: str | None
    new_status: str | None
    reason: str | None

    @classmethod
    def of(cls, e: ApplicationEvent) -> "TimelineEvent":
        return cls(
            occurred_at=e.occurred_at,
            event_type=e.event_type,
            source=e.source.value,
            previous_status=e.previous_status,
            new_status=e.new_status,
            reason=e.reason,
        )


class ApplicationBrief(BaseModel):
    id: uuid.UUID
    status: str
    requires_user_input: bool
    user_action_required: str | None
    applied_at: datetime | None


class ResumeUse(BaseModel):
    application_id: uuid.UUID
    job_id: uuid.UUID
    title: str
    company: str | None
    user_status: str
    locked: bool


class ResumeVersionBrief(BaseModel):
    id: uuid.UUID
    version_number: int
    is_master: bool
    validation_status: str | None
    tailoring_strategy: str | None
    artifact_format: str | None
    job_id: uuid.UUID | None
    company: str | None
    role: str | None
    created_at: datetime
    changes_summary: str | None
    # Filled in by the resumes routes: the applications using this version, and whether any
    # of them locks it (in progress or submitted, REQUIREMENTS 7).
    used_by: list["ResumeUse"] = []
    locked: bool = False

    @classmethod
    def of(cls, v: ResumeVersion) -> "ResumeVersionBrief":
        return cls(
            id=v.id, version_number=v.version_number, is_master=v.is_master,
            validation_status=v.validation_status.value if v.validation_status else None,
            tailoring_strategy=v.tailoring_strategy, artifact_format=v.artifact_format,
            job_id=v.job_id, company=v.company, role=v.role, created_at=v.created_at,
            changes_summary=v.changes_summary,
        )  # fmt: skip


class ResumeVersionDetail(ResumeVersionBrief):
    job_title: str | None
    content: dict[str, Any] | None
    change_details: dict[str, Any] | None
    tailoring_report: dict[str, Any] | None
    validation_report: dict[str, Any] | None
    source_version_id: uuid.UUID | None
    source_sha256: str | None
    model: str | None
    prompt_version: str | None
    artifacts: list[str]  # formats available for download
    markdown: str | None


class MasterResumeStatus(BaseModel):
    available: bool
    file_name: str | None
    source: str | None
    sha256: str | None
    detail: str | None


class ResumeInfo(BaseModel):
    master_available: bool
    tailored_versions: list[ResumeVersionBrief]
    latest: ResumeVersionDetail | None


class JobDetail(JobSummary):
    application: ApplicationBrief | None = None
    readiness: dict[str, Any] | None = None
    # Factual constraint (required vs verified full-time experience); never concealed.
    experience_gap: dict[str, Any] | None = None
    resume: ResumeInfo | None = None
    description: str | None
    requirements: list[str]
    skills: list[str]
    application_url: str | None
    company_url: str | None
    last_seen_at: datetime | None
    analyses: list[AnalysisOut]
    timeline: list[TimelineEvent]


class JobList(BaseModel):
    items: list[JobSummary]
    total: int
    limit: int
    offset: int


class JobFilterOptions(BaseModel):
    sources: list[str]
    locations: list[str]
    statuses: list[str]
    freshness_window_hours: int


class DiscoverRequest(BaseModel):
    source: str | None = Field(default=None, description="Source name; default: all enabled")


class AnalyzeBatchRequest(BaseModel):
    limit: int = Field(default=25, ge=1, le=200)
    include_stale: bool | None = None  # None -> config default
    reanalyze: bool = False


class RunAccepted(BaseModel):
    run_ids: list[uuid.UUID]
    message: str


class ActionOut(BaseModel):
    sequence: int
    action: str
    status: str
    tool: str | None
    iteration: int | None
    job_id: uuid.UUID | None
    input: dict[str, Any] | None
    output: dict[str, Any] | None
    reasoning: str | None
    error: str | None
    at: datetime

    @classmethod
    def of(cls, a: AgentAction) -> "ActionOut":
        return cls(
            sequence=a.sequence,
            action=a.tool_name,
            status=a.status.value,
            tool=a.tool,
            iteration=a.iteration,
            job_id=a.job_id,
            input=a.input,
            output=a.output,
            reasoning=a.reasoning,
            error=a.error,
            at=a.started_at,
        )


class RunOut(BaseModel):
    id: uuid.UUID
    task: str
    run_type: str
    status: str
    source: str | None
    search_terms: list[str] | None
    locations: list[str] | None
    current_task: str | None
    started_at: datetime
    ended_at: datetime | None
    searches_total: int
    searches_completed: int
    jobs_found: int
    jobs_new: int
    jobs_duplicate: int
    jobs_failed: int
    jobs_analyzed: int
    jobs_shortlisted: int
    jobs_rejected: int
    errors: list[dict[str, Any]]
    summary: str | None
    model: str | None
    mode: str | None
    iterations: int
    max_iterations: int | None
    pending_user_input: str | None

    @classmethod
    def of(cls, r: AgentRun) -> "RunOut":
        return cls(
            id=r.id,
            task=r.task,
            run_type=r.run_type.value,
            status=r.status.value,
            source=r.source,
            search_terms=r.search_terms,
            locations=r.locations,
            current_task=r.current_task,
            started_at=r.started_at,
            ended_at=r.ended_at,
            searches_total=r.searches_total,
            searches_completed=r.searches_completed,
            jobs_found=r.jobs_found,
            jobs_new=r.jobs_new,
            jobs_duplicate=r.jobs_duplicate,
            jobs_failed=r.jobs_failed,
            jobs_analyzed=r.jobs_analyzed,
            jobs_shortlisted=r.jobs_shortlisted,
            jobs_rejected=r.jobs_rejected,
            errors=r.errors,
            summary=r.summary,
            model=r.model,
            mode=r.mode,
            iterations=r.iterations,
            max_iterations=r.max_iterations,
            pending_user_input=r.pending_user_input,
        )


class RunDetail(RunOut):
    actions: list[ActionOut]


class AgentStatus(BaseModel):
    state: Literal["idle", "running", "stopping"]
    active_run: RunOut | None
    # Latest agent run paused until the user answers (NEEDS_USER_INPUT), if any.
    awaiting_user: RunOut | None = None
    recent_runs: list[RunOut]
    analyzer: str
    analyzer_detail: str
    # External work defaults to a dry run; LIVE is available only when this is true.
    # Effective: true only with DEVELOPMENT_MODE=false AND EXTERNAL_LIVE_ENABLED=true.
    external_live_enabled: bool = False
    external_live_configured: bool = False  # the raw EXTERNAL_LIVE_ENABLED value
    development_mode: bool = True
    default_external_mode: str = "external_dry_run"


class StartAgentRequest(BaseModel):
    task: str = Field(min_length=3, max_length=2000)
    mode: Literal["read_only", "internal_writes", "external_dry_run", "external_live"] = (
        "internal_writes"
    )
    max_iterations: int = Field(default=12, ge=1, le=40)


class ResumeAgentRequest(BaseModel):
    message: str | None = Field(default=None, max_length=4000)
    extra_iterations: int = Field(default=0, ge=0, le=20)


class ToolInfo(BaseModel):
    name: str
    description: str
    permission: str
    enabled: bool
    input_schema: dict[str, Any]


class DraftRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=1000)


class TailorRequest(BaseModel):
    tailoring_strategy: Literal[
        "balanced", "skills_first", "projects_first", "experience_first"
    ] = "balanced"
    requested_format: Literal["pdf", "markdown"] = "pdf"
    force_new: bool = False


class ToolResultOut(BaseModel):
    ok: bool
    tool: str
    data: dict[str, Any] | None
    error_code: str | None
    message: str | None
    state_changes: list[dict[str, Any]]


def _salary_note(job: Job) -> str | None:
    """The source's own salary text when PIE could not (or must not) turn it into numbers."""
    if job.salary_min is not None or job.salary_max is not None or not job.salary_raw:
        return None
    raw = job.salary_raw
    if raw.get("salary_is_predicted") not in (None, "0", 0):
        return "Estimated by the job board, not disclosed by the employer (ignored)"
    text = raw.get("text") or raw.get("summary")
    return f"As stated: {str(text)[:80]}" if text else "Disclosed in a form PIE does not convert"
