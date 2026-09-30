import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from agent.analysis.analyzers import AnalyzerError, AnalyzerUnavailableError, JobAnalyzer
from agent.claude_client import StructuredClaude
from agent.tailoring.experience import experience_gap
from agent.tools import ToolContext, ToolErrorCode, ToolRegistry
from backend.api.deps import (
    SourceBuilder,
    agent_claude_dep,
    analyzer_dep,
    profile_dep,
    registry_dep,
    rules_dep,
    session_factory_dep,
    source_builder_dep,
    sources_config_dep,
)
from backend.api.routes.resumes import version_detail
from backend.api.schemas import (
    AnalysisOut,
    AnalyzeBatchRequest,
    ApplicationBrief,
    DiscoverRequest,
    DraftRequest,
    JobDetail,
    JobFilterOptions,
    JobList,
    JobSummary,
    ResumeInfo,
    ResumeVersionBrief,
    RunAccepted,
    TailorRequest,
    TimelineEvent,
    ToolResultOut,
)
from backend.core.config import Settings, get_settings
from backend.core.profile import UserProfile
from backend.services.analysis import (
    AnalysisContext,
    AnalysisFailedError,
    AnalysisService,
    NotAnalyzableError,
    analyze_single,
    create_analysis_run,
    select_jobs_for_batch,
)
from backend.services.discovery import DiscoveryService, create_discovery_run
from backend.services.freshness import freshness_filter
from backend.services.readiness import evaluate_readiness
from backend.services.resumes import locate_master_resume
from backend.services.runs import RunConflictError, ensure_no_active_run
from backend.services.shortlist import ShortlistRules
from database.base import utcnow
from database.enums import Freshness, JobPipelineState, ResumeValidationStatus, ToolPermission
from database.models import (
    Application,
    ApplicationEvent,
    Company,
    Job,
    JobAnalysis,
    ResumeVersion,
)
from database.session import get_db
from integrations.job_sources.config import JobSourcesConfig
from integrations.job_sources.registry import UnknownSourceError

router = APIRouter(prefix="/jobs", tags=["jobs"])

DB = Annotated[Session, Depends(get_db)]
SortKey = Literal["newest", "posted", "salary", "match", "company"]
SHORTLISTED_STATES = [JobPipelineState.SHORTLISTED]


def _latest_analyses(db: Session, job_ids: list[uuid.UUID]) -> dict[uuid.UUID, JobAnalysis]:
    if not job_ids:
        return {}
    rows = db.scalars(
        select(JobAnalysis).where(JobAnalysis.job_id.in_(job_ids)).order_by(JobAnalysis.created_at)
    ).all()
    return {a.job_id: a for a in rows}  # later rows overwrite earlier: latest wins


def _latest_tailored_numbers(db: Session, job_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not job_ids:
        return {}
    rows = db.execute(
        select(ResumeVersion.job_id, func.max(ResumeVersion.version_number))
        .where(ResumeVersion.job_id.in_(job_ids),
               ResumeVersion.validation_status == ResumeValidationStatus.VALIDATED)
        .group_by(ResumeVersion.job_id)
    ).all()  # fmt: skip
    return {job_id: number for job_id, number in rows if job_id is not None}


def _latest_score_subquery():  # type: ignore[no-untyped-def]
    ranked = select(
        JobAnalysis.job_id,
        JobAnalysis.match_score,
        func.row_number()
        .over(partition_by=JobAnalysis.job_id, order_by=JobAnalysis.created_at.desc())
        .label("rn"),
    ).subquery()
    return select(ranked.c.job_id, ranked.c.match_score).where(ranked.c.rn == 1).subquery()


def _list_jobs(
    db: Session,
    profile: UserProfile,
    *,
    q: str | None,
    location: str | None,
    source: str | None,
    freshness: Freshness | None,
    status: list[JobPipelineState] | None,
    shortlisted: bool | None,
    min_salary: float | None,
    salary_known_only: bool,
    sort: SortKey,
    limit: int,
    offset: int,
) -> JobList:
    now = utcnow()
    max_age = profile.preferences.maximum_application_age_hours
    latest = _latest_score_subquery()
    stmt = (
        select(Job)
        .outerjoin(Company, Job.company_id == Company.id)
        .outerjoin(latest, latest.c.job_id == Job.id)
        .options(selectinload(Job.company))
    )
    where = []
    if q:
        like = f"%{q.lower()}%"
        where.append(or_(func.lower(Job.title).like(like), func.lower(Company.name).like(like)))
    if location:
        where.append(func.lower(Job.location).like(f"%{location.lower()}%"))
    if source:
        where.append(Job.source == source)
    if freshness:
        where.append(freshness_filter(freshness, now, max_age))
    if status:
        where.append(Job.pipeline_state.in_(status))
    if shortlisted is True:
        where.append(Job.pipeline_state.in_(SHORTLISTED_STATES))
    elif shortlisted is False:
        where.append(Job.pipeline_state.notin_(SHORTLISTED_STATES))
    if min_salary is not None:
        meets = or_(
            Job.salary_max >= min_salary,
            and_(Job.salary_max.is_(None), Job.salary_min >= min_salary),
        )
        unknown = and_(Job.salary_min.is_(None), Job.salary_max.is_(None))
        # Missing salary is not a rejection unless the caller asks for disclosed salaries only.
        where.append(meets if salary_known_only else or_(meets, unknown))
    elif salary_known_only:
        where.append(or_(Job.salary_min.is_not(None), Job.salary_max.is_not(None)))
    if where:
        stmt = stmt.where(*where)

    total = (
        db.scalar(select(func.count()).select_from(stmt.with_only_columns(Job.id).subquery())) or 0
    )
    orders: dict[str, list[Any]] = {
        "newest": [Job.discovered_at.desc()],
        "posted": [Job.posted_at.desc().nulls_last()],
        "salary": [func.coalesce(Job.salary_max, Job.salary_min).desc().nulls_last()],
        "match": [latest.c.match_score.desc().nulls_last()],
        "company": [Company.name.asc().nulls_last(), Job.title.asc()],
    }
    order = orders[sort]
    jobs = db.scalars(stmt.order_by(*order, Job.id).limit(limit).offset(offset)).unique().all()
    analyses = _latest_analyses(db, [j.id for j in jobs])
    tailored = _latest_tailored_numbers(db, [j.id for j in jobs])
    return JobList(
        items=[
            JobSummary.of(j, analyses.get(j.id), now, max_age).model_copy(
                update={"tailored_version": tailored.get(j.id)}
            )
            for j in jobs
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("", response_model=JobList)
def list_jobs(
    db: DB,
    profile: Annotated[UserProfile, Depends(profile_dep)],
    q: str | None = Query(None, description="Search title or company"),
    location: str | None = None,
    source: str | None = None,
    freshness: Freshness | None = None,
    status: Annotated[list[JobPipelineState] | None, Query()] = None,
    shortlisted: bool | None = None,
    min_salary: float | None = Query(None, ge=0),
    salary_known_only: bool = False,
    sort: SortKey = "newest",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> JobList:
    return _list_jobs(
        db,
        profile,
        q=q,
        location=location,
        source=source,
        freshness=freshness,
        status=status,
        shortlisted=shortlisted,
        min_salary=min_salary,
        salary_known_only=salary_known_only,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@router.get("/shortlisted", response_model=JobList)
def list_shortlisted(
    db: DB,
    profile: Annotated[UserProfile, Depends(profile_dep)],
    sort: SortKey = "match",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> JobList:
    return _list_jobs(
        db,
        profile,
        q=None,
        location=None,
        source=None,
        freshness=None,
        status=None,
        shortlisted=True,
        min_salary=None,
        salary_known_only=False,
        sort=sort,
        limit=limit,
        offset=offset,
    )


@router.get("/filters", response_model=JobFilterOptions)
def filter_options(
    db: DB, profile: Annotated[UserProfile, Depends(profile_dep)]
) -> JobFilterOptions:
    sources = db.scalars(select(Job.source).distinct().order_by(Job.source)).all()
    locations = db.execute(
        select(Job.location, func.count())
        .where(Job.location.is_not(None))
        .group_by(Job.location)
        .order_by(func.count().desc())
        .limit(30)
    ).all()
    return JobFilterOptions(
        sources=list(sources),
        locations=[loc for loc, _ in locations if loc],
        statuses=[s.value for s in JobPipelineState],
        freshness_window_hours=profile.preferences.maximum_application_age_hours,
    )


@router.post("/discover", response_model=RunAccepted, status_code=202)
def discover(
    background: BackgroundTasks,
    db: DB,
    settings: Annotated[Settings, Depends(get_settings)],
    config: Annotated[JobSourcesConfig, Depends(sources_config_dep)],
    profile: Annotated[UserProfile, Depends(profile_dep)],
    build: Annotated[SourceBuilder, Depends(source_builder_dep)],
    factory: Annotated[sessionmaker[Session], Depends(session_factory_dep)],
    body: DiscoverRequest | None = None,
) -> RunAccepted:
    names = [body.source] if body and body.source else [s.name for s in config.enabled_sources()]
    if not names:
        raise HTTPException(400, "No job sources are enabled in config/job_sources.yaml")
    query = config.build_query(
        profile.preferences.preferred_roles,
        profile.preferences.preferred_locations,
        excluded_companies=profile.preferences.excluded_companies,
        max_age_hours=profile.preferences.maximum_application_age_hours,
    )
    if not query.terms or not query.locations:
        raise HTTPException(400, "No search terms or locations configured")
    try:
        sources = [build(name) for name in names]
    except (UnknownSourceError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        first = create_discovery_run(db, sources[0], query, settings.development_mode)
    except RunConflictError as exc:
        for s in sources:
            s.close()
        raise HTTPException(409, str(exc)) from exc

    def work() -> None:
        service = DiscoveryService(factory)
        service.execute(first.id, sources[0], query)
        for source in sources[1:]:
            with factory() as s:
                run = create_discovery_run(s, source, query, settings.development_mode)
            service.execute(run.id, source, query)

    background.add_task(work)
    return RunAccepted(run_ids=[first.id], message=f"Discovery started for: {', '.join(names)}")


@router.post("/analyze-shortlist", response_model=RunAccepted, status_code=202)
def analyze_and_shortlist(
    background: BackgroundTasks,
    db: DB,
    settings: Annotated[Settings, Depends(get_settings)],
    profile: Annotated[UserProfile, Depends(profile_dep)],
    rules: Annotated[ShortlistRules, Depends(rules_dep)],
    analyzer: Annotated[JobAnalyzer, Depends(analyzer_dep)],
    factory: Annotated[sessionmaker[Session], Depends(session_factory_dep)],
    body: AnalyzeBatchRequest | None = None,
) -> RunAccepted:
    body = body or AnalyzeBatchRequest()
    include_stale = (
        rules.analyze_stale_jobs_in_batch if body.include_stale is None else body.include_stale
    )
    try:
        ensure_no_active_run(db)
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    job_ids = list(
        db.scalars(
            select_jobs_for_batch(
                utcnow(),
                profile.preferences.maximum_application_age_hours,
                include_stale,
                body.reanalyze,
                body.limit,
            ).with_only_columns(Job.id)
        ).all()
    )
    if not job_ids:
        return RunAccepted(run_ids=[], message="No jobs waiting for analysis")
    run = create_analysis_run(
        db, f"Analyze & shortlist {len(job_ids)} jobs", settings.development_mode
    )
    ctx = AnalysisContext(analyzer=analyzer, profile=profile, rules=rules)
    background.add_task(AnalysisService(factory).execute, run.id, job_ids, ctx)
    return RunAccepted(
        run_ids=[run.id], message=f"Analyzing {len(job_ids)} jobs with {analyzer.name}"
    )


def _detail(db: Session, job: Job, profile: UserProfile, settings: Settings) -> JobDetail:
    now = utcnow()
    max_age = profile.preferences.maximum_application_age_hours
    analyses = list(
        db.scalars(
            select(JobAnalysis)
            .where(JobAnalysis.job_id == job.id)
            .order_by(JobAnalysis.created_at.desc())
        ).all()
    )
    events = db.scalars(
        select(ApplicationEvent)
        .where(ApplicationEvent.job_id == job.id)
        .order_by(ApplicationEvent.occurred_at)
    ).all()
    summary = JobSummary.of(job, analyses[0] if analyses else None, now, max_age)
    app = db.scalar(select(Application).where(Application.job_id == job.id))
    versions = db.scalars(
        select(ResumeVersion)
        .where(ResumeVersion.job_id == job.id,
               ResumeVersion.validation_status == ResumeValidationStatus.VALIDATED)
        .order_by(ResumeVersion.version_number.desc())
    ).all()  # fmt: skip
    summary = summary.model_copy(
        update={"tailored_version": versions[0].version_number if versions else None}
    )
    readiness = evaluate_readiness(db, settings, profile, job, app)
    resume = ResumeInfo(
        master_available=locate_master_resume(settings) is not None,
        tailored_versions=[ResumeVersionBrief.of(v) for v in versions],
        latest=version_detail(db, versions[0]) if versions else None,
    )
    return JobDetail(
        **summary.model_dump(),
        readiness=readiness.as_dict(),
        experience_gap=experience_gap(
            profile,
            {"experience_required": job.experience_required, "requirements": job.requirements},
        ),
        resume=resume,
        application=ApplicationBrief(
            id=app.id,
            status=app.status.value,
            requires_user_input=app.requires_user_input,
            user_action_required=app.user_action_required,
            applied_at=app.applied_at,
        )
        if app
        else None,
        description=job.description,
        requirements=job.requirements,
        skills=job.skills,
        application_url=job.application_url,
        company_url=job.company_url,
        last_seen_at=job.last_seen_at,
        analyses=[AnalysisOut.of(a) for a in analyses],
        timeline=[TimelineEvent.of(e) for e in events],
    )


def _get_job(db: Session, job_id: uuid.UUID) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@router.get("/{job_id}", response_model=JobDetail)
def get_job(
    job_id: uuid.UUID,
    db: DB,
    profile: Annotated[UserProfile, Depends(profile_dep)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JobDetail:
    return _detail(db, _get_job(db, job_id), profile, settings)


@router.post("/{job_id}/analyze", response_model=JobDetail)
def analyze_one(
    job_id: uuid.UUID,
    db: DB,
    settings: Annotated[Settings, Depends(get_settings)],
    profile: Annotated[UserProfile, Depends(profile_dep)],
    rules: Annotated[ShortlistRules, Depends(rules_dep)],
    analyzer: Annotated[JobAnalyzer, Depends(analyzer_dep)],
) -> JobDetail:
    job = _get_job(db, job_id)
    ctx = AnalysisContext(analyzer=analyzer, profile=profile, rules=rules)
    try:
        analyze_single(db, job, ctx, settings.development_mode)
    except RunConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except NotAnalyzableError as exc:
        raise HTTPException(409, str(exc)) from exc
    except AnalyzerUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    except (AnalysisFailedError, AnalyzerError) as exc:
        raise HTTPException(502, str(exc)) from exc
    db.refresh(job)
    return _detail(db, job, profile, settings)


@router.post("/{job_id}/application-draft", response_model=ToolResultOut)
def create_application_draft(
    job_id: uuid.UUID,
    db: DB,
    settings: Annotated[Settings, Depends(get_settings)],
    profile: Annotated[UserProfile, Depends(profile_dep)],
    registry: Annotated[ToolRegistry, Depends(registry_dep)],
    body: DraftRequest | None = None,
) -> ToolResultOut:
    """Create an INTERNAL application draft (the same registered tool the agent uses).

    Nothing is sent to the employer. Validation, permission checks and state-machine rules
    are identical to the agent's.
    """
    ctx = ToolContext(
        db=db, settings=settings, profile=profile, actor="user",
        allowed=frozenset({ToolPermission.READ_ONLY, ToolPermission.WRITE}),
    )  # fmt: skip
    args = {"job_id": str(job_id), **({"notes": body.notes} if body and body.notes else {})}
    result = registry.execute("create_application_draft", args, ctx)
    if not result.ok:
        code = {
            ToolErrorCode.NOT_FOUND: 404,
            ToolErrorCode.PRECONDITION_FAILED: 409,
            ToolErrorCode.INVALID_TRANSITION: 409,
        }.get(result.error_code, 400)  # type: ignore[arg-type]
        raise HTTPException(code, result.message)
    return ToolResultOut(**result.model_dump(include=set(ToolResultOut.model_fields)))


@router.post("/{job_id}/tailored-resume", response_model=ToolResultOut)
def create_tailored_resume(
    job_id: uuid.UUID,
    db: DB,
    settings: Annotated[Settings, Depends(get_settings)],
    profile: Annotated[UserProfile, Depends(profile_dep)],
    registry: Annotated[ToolRegistry, Depends(registry_dep)],
    claude: Annotated[StructuredClaude, Depends(agent_claude_dep)],
    body: TailorRequest | None = None,
) -> Any:
    """Create a new tailored resume version (the same registered tool the agent uses).

    Internal artifact only: nothing is submitted or sent. The master resume is only read.
    """
    body = body or TailorRequest()
    ctx = ToolContext(
        db=db, settings=settings, profile=profile, actor="user",
        allowed=frozenset({ToolPermission.READ_ONLY, ToolPermission.WRITE}),
        claude_factory=lambda: claude,
    )  # fmt: skip
    args = {"job_id": str(job_id), **body.model_dump()}
    result = registry.execute("create_tailored_resume", args, ctx)
    if not result.ok:
        status = {
            ToolErrorCode.NOT_FOUND: 404,
            ToolErrorCode.PRECONDITION_FAILED: 409,
            ToolErrorCode.VALIDATION_FAILED: 422,
            ToolErrorCode.EXECUTION_FAILED: 502,
        }.get(result.error_code, 400)  # type: ignore[arg-type]
        violations = (result.data or {}).get("details", {}).get("violations", [])
        return JSONResponse(
            status_code=status, content={"detail": result.message, "violations": violations}
        )
    return ToolResultOut(**result.model_dump(include=set(ToolResultOut.model_fields)))
