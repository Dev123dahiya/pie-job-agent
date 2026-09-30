"""Job tools. search_jobs queries PIE's own job database; it does not browse or discover."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field
from sqlalchemy import func, or_, select

from agent.tailoring.experience import experience_gap
from agent.tools.base import (
    StateChange,
    Tool,
    ToolContext,
    ToolError,
    ToolErrorCode,
    ToolInput,
    ToolOutput,
)
from backend.services.freshness import compute_freshness, freshness_filter
from database.base import utcnow
from database.enums import Freshness, JobPipelineState, ToolPermission
from database.models import Application, Company, Job, JobAnalysis

DESCRIPTION_CHARS = 4000


def latest_analysis(ctx: ToolContext, job_id: uuid.UUID) -> JobAnalysis | None:
    return ctx.db.scalar(
        select(JobAnalysis)
        .where(JobAnalysis.job_id == job_id)
        .order_by(JobAnalysis.created_at.desc())
        .limit(1)
    )


def get_job_or_error(ctx: ToolContext, job_id: uuid.UUID) -> Job:
    job = ctx.db.get(Job, job_id)
    if job is None:
        raise ToolError(ToolErrorCode.NOT_FOUND, f"No job with id {job_id}")
    return job


def _salary(job: Job) -> str | None:
    if job.salary_min is None and job.salary_max is None:
        return None
    lo, hi = job.salary_min or job.salary_max, job.salary_max or job.salary_min
    return f"{float(lo):,.0f}-{float(hi):,.0f} {job.salary_currency or ''}".strip()  # type: ignore[arg-type]


class JobBrief(ToolOutput):
    id: str
    company: str | None
    title: str
    location: str | None
    posted_at: datetime | None
    freshness: str
    status: str
    salary: str | None
    match_score: int | None
    shortlisted: bool | None


def brief(ctx: ToolContext, job: Job, now: datetime) -> JobBrief:
    a = latest_analysis(ctx, job.id)
    return JobBrief(
        id=str(job.id),
        company=job.company.name if job.company else None,
        title=job.title,
        location=job.location,
        posted_at=job.posted_at,
        freshness=compute_freshness(
            job.posted_at, now, ctx.profile.preferences.maximum_application_age_hours
        ).value,
        status=job.pipeline_state.value,
        salary=_salary(job),
        match_score=a.match_score if a else None,
        shortlisted=a.shortlisted if a else None,
    )


class SearchJobsInput(ToolInput):
    query: str | None = Field(None, max_length=200, description="Text in title or company")
    status: JobPipelineState | None = None
    freshness: Freshness | None = None
    limit: int = Field(20, ge=1, le=50)


class JobListOutput(ToolOutput):
    total: int
    jobs: list[JobBrief]


def search_jobs(ctx: ToolContext, p: SearchJobsInput) -> JobListOutput:
    now = utcnow()
    stmt = select(Job).outerjoin(Company, Job.company_id == Company.id)
    if p.query:
        like = f"%{p.query.lower()}%"
        stmt = stmt.where(
            or_(func.lower(Job.title).like(like), func.lower(Company.name).like(like))
        )
    if p.status:
        stmt = stmt.where(Job.pipeline_state == p.status)
    if p.freshness:
        stmt = stmt.where(
            freshness_filter(
                p.freshness, now, ctx.profile.preferences.maximum_application_age_hours
            )
        )
    total = ctx.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    jobs = ctx.db.scalars(
        stmt.order_by(Job.posted_at.desc().nulls_last(), Job.id).limit(p.limit)
    ).all()
    return JobListOutput(total=total, jobs=[brief(ctx, j, now) for j in jobs])


class JobIdInput(ToolInput):
    job_id: uuid.UUID


class JobDetailOutput(ToolOutput):
    job: JobBrief
    url: str
    remote_type: str
    experience_required: str | None
    requirements: list[str]
    skills: list[str]
    description: str | None
    analysis: dict[str, Any] | None
    application: dict[str, Any] | None
    experience_gap: dict[str, Any]


def get_job(ctx: ToolContext, p: JobIdInput) -> JobDetailOutput:
    job = get_job_or_error(ctx, p.job_id)
    a = latest_analysis(ctx, job.id)
    app = ctx.db.scalar(select(Application).where(Application.job_id == job.id))
    desc = job.description
    return JobDetailOutput(
        job=brief(ctx, job, utcnow()),
        url=job.url,
        remote_type=job.remote_type.value,
        experience_required=job.experience_required,
        experience_gap=experience_gap(
            ctx.profile,
            {"experience_required": job.experience_required, "requirements": job.requirements},
        ),
        requirements=job.requirements,
        skills=job.skills,
        description=desc[:DESCRIPTION_CHARS]
        + ("...(truncated)" if len(desc) > DESCRIPTION_CHARS else "")
        if desc
        else None,
        analysis=None
        if a is None
        else {
            "match_score": a.match_score,
            "dimensions": {
                d: getattr(a, d).value
                for d in (
                    "role_match",
                    "skills_match",
                    "experience_match",
                    "location_match",
                    "salary_match",
                    "freshness",
                )
            },
            "matching_skills": a.matching_skills,
            "missing_skills": a.missing_skills,
            "concerns": a.concerns,
            "reason": a.reason,
            "shortlisted": a.shortlisted,
            "failed_rules": [r for r in a.rule_results if not r["passed"]],
        },
        application=None
        if app is None
        else {
            "id": str(app.id),
            "status": app.status.value,
            "user_action_required": app.user_action_required,
        },
    )


class AnalyzeOutput(ToolOutput):
    job_id: str
    status: str
    shortlisted: bool | None
    match_score: int | None
    failed_rules: list[dict[str, Any]]
    reason: str | None


def analyze_job(ctx: ToolContext, p: JobIdInput) -> AnalyzeOutput:
    from agent.analysis.analyzers import AnalyzerError
    from backend.services.analysis import AnalysisFailedError, NotAnalyzableError
    from backend.services.analysis import analyze_job as run_analysis

    if ctx.analysis_context_factory is None or ctx.log is None:
        raise ToolError(ToolErrorCode.EXECUTION_FAILED, "Analysis is not available in this context")
    job = get_job_or_error(ctx, p.job_id)
    previous = job.pipeline_state.value
    try:
        analysis = run_analysis(ctx.db, job, ctx.analysis_context_factory(), ctx.log)
    except NotAnalyzableError as exc:
        raise ToolError(ToolErrorCode.PRECONDITION_FAILED, str(exc)) from exc
    except (AnalysisFailedError, AnalyzerError) as exc:
        raise ToolError(ToolErrorCode.EXECUTION_FAILED, str(exc), retryable=False) from exc
    if job.pipeline_state.value != previous:
        ctx.state_changes.append(
            StateChange("job", str(job.id), previous, job.pipeline_state.value, "analyze_job")
        )
    return AnalyzeOutput(
        job_id=str(job.id),
        status=job.pipeline_state.value,
        shortlisted=analysis.shortlisted if analysis else False,
        match_score=analysis.match_score if analysis else None,
        failed_rules=[r for r in analysis.rule_results if not r["passed"]] if analysis else [],
        reason=analysis.reason if analysis else "Excluded by the user's preferences",
    )


class LimitInput(ToolInput):
    limit: int = Field(20, ge=1, le=50)


def get_shortlisted_jobs(ctx: ToolContext, p: LimitInput) -> JobListOutput:
    return search_jobs(
        ctx, SearchJobsInput(query=None, status=JobPipelineState.SHORTLISTED, limit=p.limit)
    )


TOOLS = [
    Tool(
        name="search_jobs",
        description="Search jobs already stored in PIE (title/company text, pipeline status, "
        "freshness). Does not browse the web or discover new jobs.",
        permission=ToolPermission.READ_ONLY,
        input_model=SearchJobsInput,
        output_model=JobListOutput,
        handler=search_jobs,
    ),
    Tool(
        name="get_job",
        description="Full details of one stored job: posting facts, latest analysis with every "
        "dimension and failed rule, and any application draft.",
        permission=ToolPermission.READ_ONLY,
        input_model=JobIdInput,
        output_model=JobDetailOutput,
        handler=get_job,
    ),
    Tool(
        name="analyze_job",
        description="(Re-)analyze one job with Claude against the verified profile and apply the "
        "shortlist rules. Updates the job's status. Slow (about 20s) - use only when needed.",
        permission=ToolPermission.WRITE,
        input_model=JobIdInput,
        output_model=AnalyzeOutput,
        handler=analyze_job,
        manages_transaction=True,
    ),
    Tool(
        name="get_shortlisted_jobs",
        description="List jobs currently in SHORTLISTED status.",
        permission=ToolPermission.READ_ONLY,
        input_model=LimitInput,
        output_model=JobListOutput,
        handler=get_shortlisted_jobs,
    ),
]
