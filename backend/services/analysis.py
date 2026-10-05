"""ANALYZE -> SHORTLIST. Runs an analyzer on structured job facts, validates the output,
applies the configured shortlist rules and records everything for audit."""

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session, sessionmaker

from agent.analysis.analyzers import AnalyzerError, AnalyzerUnavailableError, JobAnalyzer
from agent.analysis.prompt import PROMPT_VERSION
from agent.analysis.schema import AnalysisJob, AnalysisValidationError, validate_output
from backend.core.profile import UserProfile
from backend.services.freshness import compute_freshness, freshness_filter
from backend.services.runs import RunLogger, cancel_requested, ensure_no_active_run, finish_run
from backend.services.shortlist import (
    DIMENSION_WEIGHTS,
    ShortlistRules,
    combined_score,
    evaluate,
    exclusion_check,
)
from database.base import utcnow
from database.enums import (
    AgentActionStatus,
    AgentRunStatus,
    EventSource,
    Freshness,
    JobLogAction,
    JobPipelineState,
    MatchLevel,
    RunType,
)
from database.models import AgentRun, ApplicationEvent, Job, JobAnalysis
from database.state_machine import assert_transition

logger = logging.getLogger(__name__)

# FAILED is not here on purpose: a job is FAILED only when its application failed, and only
# the user may bring it back (backend.services.failed_jobs.retry_failed_job). A failed
# *analysis* leaves the job DISCOVERED (with last_error), so it is simply analyzed again.
ANALYZABLE_STATES = {
    JobPipelineState.DISCOVERED,
    JobPipelineState.ANALYZED,
    JobPipelineState.SHORTLISTED,
    JobPipelineState.REJECTED_BY_AGENT,
}
MAX_ATTEMPTS = 2


class AnalysisFailedError(RuntimeError):
    pass


class NotAnalyzableError(ValueError):
    pass


@dataclass
class AnalysisContext:
    analyzer: JobAnalyzer
    profile: UserProfile
    rules: ShortlistRules
    now: Callable[[], datetime] = field(default=utcnow)
    # After a successful analysis, delete this job's analyses made with a different profile.
    replace_outdated: bool = False

    @property
    def max_age_hours(self) -> int:
        return self.profile.preferences.maximum_application_age_hours


def profile_fingerprint(profile: UserProfile) -> str:
    """Hash of the full profile; stored with each analysis to detect outdated ones."""
    return hashlib.sha256(
        json.dumps(profile.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()


def _analysis_profile(a: JobAnalysis) -> str | None:
    return (a.input_snapshot or {}).get("profile_sha256")


def drop_outdated_analyses(db: Session, job: Job, current: str, keep: JobAnalysis | None) -> int:
    """Delete this job's analyses produced with a different profile. Returns how many."""
    removed = 0
    for old in db.scalars(select(JobAnalysis).where(JobAnalysis.job_id == job.id)).all():
        if old is not keep and _analysis_profile(old) != current:
            db.delete(old)
            removed += 1
    return removed


def has_outdated_analysis(db: Session, job_id: uuid.UUID, current: str) -> bool:
    """True if the job was never analyzed or its latest analysis used another profile."""
    latest = db.scalar(
        select(JobAnalysis)
        .where(JobAnalysis.job_id == job_id)
        .order_by(JobAnalysis.created_at.desc())
        .limit(1)
    )
    return latest is None or _analysis_profile(latest) != current


def build_analysis_job(job: Job, now: datetime, max_age_hours: int) -> AnalysisJob:
    return AnalysisJob(
        title=job.title,
        company=job.company.name if job.company else None,
        location=job.location,
        remote_type=job.remote_type.value,
        salary_min=float(job.salary_min) if job.salary_min is not None else None,
        salary_max=float(job.salary_max) if job.salary_max is not None else None,
        salary_currency=job.salary_currency,
        experience_required=job.experience_required,
        description=(job.description or None) and job.description[:12000],
        requirements=list(job.requirements or []),
        skills=list(job.skills or []),
        posted_at=job.posted_at.isoformat() if job.posted_at else None,
        freshness=compute_freshness(job.posted_at, now, max_age_hours, job.source).value,
    )


def _event(
    job: Job,
    event_type: str,
    prev: JobPipelineState,
    new: JobPipelineState,
    reason: str,
    evidence: dict | None = None,
) -> ApplicationEvent:
    return ApplicationEvent(
        job_id=job.id,
        event_type=event_type,
        source=EventSource.AGENT,
        previous_status=prev.value,
        new_status=new.value,
        reason=reason,
        evidence=evidence,
    )


def analyze_job(db: Session, job: Job, ctx: AnalysisContext, log: RunLogger) -> JobAnalysis | None:
    """Analyze one job and apply shortlist rules. Returns None if excluded before analysis.

    Commits its own work. Raises AnalysisFailedError / AnalyzerUnavailableError on failure
    (the job keeps its state and gets last_error set).
    """
    if job.pipeline_state not in ANALYZABLE_STATES:
        raise NotAnalyzableError(f"Job is {job.pipeline_state.value}; it cannot be re-analyzed now")

    company = job.company.name if job.company else None
    excluded = exclusion_check(company, job.title, ctx.profile.preferences)
    if excluded:
        prev = job.pipeline_state
        if prev is not JobPipelineState.REJECTED_BY_AGENT:
            assert_transition(prev, JobPipelineState.REJECTED_BY_AGENT)
            job.pipeline_state = JobPipelineState.REJECTED_BY_AGENT
            db.add(_event(job, "job_rejected", prev, job.pipeline_state, excluded))
        replaced = (
            drop_outdated_analyses(db, job, profile_fingerprint(ctx.profile), None)
            if ctx.replace_outdated
            else 0
        )
        log.log(
            JobLogAction.JOB_REJECTED,
            job_id=job.id,
            reasoning=excluded,
            output={"stage": "preference_exclusion", "outdated_analyses_removed": replaced},
        )
        db.commit()
        return None

    now = ctx.now()
    analysis_job = build_analysis_job(job, now, ctx.max_age_hours)
    verified_skills = ctx.profile.verified_profile.all_skills()
    log.log(
        JobLogAction.JOB_ANALYSIS_STARTED,
        job_id=job.id,
        input={
            "analyzer": ctx.analyzer.name,
            "prompt_version": PROMPT_VERSION,
            "freshness": analysis_job.freshness,
        },
    )
    db.commit()

    feedback: str | None = None
    output = None
    model = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = ctx.analyzer.analyze(analysis_job, ctx.profile, feedback)
            model = raw.model
            output = validate_output(raw.payload, analysis_job, verified_skills)
            break
        except AnalysisValidationError as exc:
            feedback = str(exc)
            log.log(
                JobLogAction.JOB_ANALYSIS_FAILED,
                job_id=job.id,
                status=AgentActionStatus.FAILED,
                error=f"Attempt {attempt}: rejected model output: {exc}",
            )
            db.commit()
        except AnalyzerError as exc:
            job.last_error = f"Analysis failed: {exc}"
            log.log(
                JobLogAction.JOB_ANALYSIS_FAILED,
                job_id=job.id,
                status=AgentActionStatus.FAILED,
                error=str(exc),
            )
            db.commit()
            raise
    if output is None:
        job.last_error = f"Analysis output rejected after {MAX_ATTEMPTS} attempts: {feedback}"
        db.commit()
        raise AnalysisFailedError(job.last_error)

    shortlisted, rule_results = evaluate(output, ctx.rules)
    profile_hash = profile_fingerprint(ctx.profile)
    analysis = JobAnalysis(
        job_id=job.id,
        agent_run_id=log.run.id,
        match_score=combined_score(
            output.match_score, {d: str(getattr(output, d)) for d in DIMENSION_WEIGHTS}
        )[0],
        role_match=MatchLevel(output.role_match),
        skills_match=MatchLevel(output.skills_match),
        experience_match=MatchLevel(output.experience_match),
        location_match=MatchLevel(output.location_match),
        salary_match=MatchLevel(output.salary_match),
        freshness=Freshness(output.freshness),
        matching_skills=output.matching_skills,
        missing_skills=output.missing_skills,
        important_requirements=output.important_requirements,
        concerns=output.concerns,
        reason=output.reason,
        should_shortlist=output.should_shortlist,
        analyzer=ctx.analyzer.name,
        model=model,
        prompt_version=PROMPT_VERSION,
        input_snapshot={
            "job": analysis_job.model_dump(mode="json"),
            "profile_sha256": profile_hash,
        },
        shortlisted=shortlisted,
        rule_results=rule_results,
    )
    db.add(analysis)
    replaced = (
        drop_outdated_analyses(db, job, profile_hash, analysis) if ctx.replace_outdated else 0
    )

    prev = job.pipeline_state
    if prev is not JobPipelineState.ANALYZED:
        assert_transition(prev, JobPipelineState.ANALYZED)
    job.pipeline_state = JobPipelineState.ANALYZED
    db.add(
        _event(
            job,
            "job_analyzed",
            prev,
            JobPipelineState.ANALYZED,
            f"Analyzed by {ctx.analyzer.name}; match score {output.match_score}",
            {
                "dimensions": {
                    d: getattr(output, d)
                    for d in (
                        "role_match",
                        "skills_match",
                        "experience_match",
                        "location_match",
                        "salary_match",
                        "freshness",
                    )
                }
            },
        )
    )
    target = JobPipelineState.SHORTLISTED if shortlisted else JobPipelineState.REJECTED_BY_AGENT
    assert_transition(JobPipelineState.ANALYZED, target)
    job.pipeline_state = target
    failed_rules = [r for r in rule_results if not r["passed"]]
    decision_reason = (
        "All shortlist rules passed"
        if shortlisted
        else "Failed rules: " + ", ".join(f"{r['rule']} ({r['detail']})" for r in failed_rules)
    )
    db.add(
        _event(
            job,
            "job_shortlisted" if shortlisted else "job_rejected",
            JobPipelineState.ANALYZED,
            target,
            decision_reason,
            {"rules": rule_results},
        )
    )
    job.last_error = None

    log.log(
        JobLogAction.JOB_ANALYSIS_COMPLETED,
        job_id=job.id,
        output={**output.model_dump(), "outdated_analyses_removed": replaced},
        reasoning=output.reason,
    )
    log.log(
        JobLogAction.JOB_SHORTLISTED if shortlisted else JobLogAction.JOB_REJECTED,
        job_id=job.id,
        reasoning=decision_reason,
        output={"rules": rule_results},
    )
    db.commit()
    if shortlisted:
        route_manual_only(db, job)
    return analysis


def route_manual_only(db: Session, job: Job) -> None:
    """A shortlisted job from a search-only source goes to the Manual apply list."""
    from backend.services.manual_apply import send_to_manual
    from backend.services.platforms import MANUAL_ONLY_SOURCES

    if (job.source or "").lower() in MANUAL_ONLY_SOURCES:
        send_to_manual(
            db, job.id, f"Found on {job.source}, which only lists it: apply on the employer's page "
            "(the link is here)", EventSource.SYSTEM,
        )  # fmt: skip


# --- Runs ---------------------------------------------------------------------------------


def select_jobs_for_batch(
    now: datetime,
    max_age_hours: int,
    include_stale: bool,
    reanalyze: bool,
    limit: int,
    source: str | None = None,
) -> Select[Job]:
    states = [JobPipelineState.DISCOVERED]  # never FAILED: see ANALYZABLE_STATES
    if reanalyze:
        states += [
            JobPipelineState.ANALYZED,
            JobPipelineState.SHORTLISTED,
            JobPipelineState.REJECTED_BY_AGENT,
        ]
    # A row linked to its trusted ATS job (duplicate_of_id) is never analyzed: the trusted
    # job is, once.
    stmt = select(Job).where(Job.pipeline_state.in_(states), Job.duplicate_of_id.is_(None))
    if source:
        stmt = stmt.where(Job.source == source)
    if not include_stale:
        # Explicit IS NULL: NOT (posted_at < cutoff) is NULL for undated jobs and would drop
        # them, but a missing post date means "unknown", which must not be skipped.
        stmt = stmt.where(
            or_(
                freshness_filter(Freshness.FRESH, now, max_age_hours),
                freshness_filter(Freshness.UNKNOWN, now, max_age_hours),
            )
        )
    return stmt.order_by(Job.posted_at.desc().nulls_last(), Job.discovered_at.desc()).limit(limit)


def select_jobs_for_daily(
    db: Session, now: datetime, max_age_hours: int, limit: int
) -> list[uuid.UUID]:
    """The jobs a daily run sends to the analyzer: FRESH ones only (STALE and UNKNOWN are
    ineligible for the shortlist anyway, so analyzing them would only spend Claude calls).

    - DISCOVERED jobs that are fresh now (including ones whose analysis failed before);
    - REJECTED_BY_AGENT jobs whose latest analysis saw them as UNKNOWN and whose own source
      has since published a post date that makes them fresh. Nothing else is re-analyzed.
    """
    fresh = freshness_filter(Freshness.FRESH, now, max_age_hours)
    pending = db.scalars(
        select(Job.id)
        .where(
            Job.pipeline_state == JobPipelineState.DISCOVERED,  # never FAILED (user retries)
            Job.duplicate_of_id.is_(None),
            fresh,
        )
        .order_by(Job.posted_at.desc(), Job.discovered_at.desc())
    ).all()
    rejected = db.scalars(
        select(Job).where(
            Job.pipeline_state == JobPipelineState.REJECTED_BY_AGENT,
            Job.duplicate_of_id.is_(None),
            fresh,
        )
    ).all()
    reconsider = []
    for job in rejected:
        latest = db.scalar(
            select(JobAnalysis)
            .where(JobAnalysis.job_id == job.id)
            .order_by(JobAnalysis.created_at.desc())
            .limit(1)
        )
        if latest is not None and latest.freshness is Freshness.UNKNOWN:
            reconsider.append(job.id)
    return [*pending, *reconsider][:limit]


def create_analysis_run(
    db: Session, task: str, dev_mode: bool, *, check_active: bool = True
) -> AgentRun:
    if check_active:
        ensure_no_active_run(db)
    run = AgentRun(
        task=task,
        run_type=RunType.ANALYSIS,
        status=AgentRunStatus.RUNNING,
        development_mode=dev_mode,
        current_task="Starting analysis",
        heartbeat_at=utcnow(),
    )
    db.add(run)
    db.commit()
    return run


def analyze_single(
    db: Session, job: Job, ctx: AnalysisContext, dev_mode: bool
) -> JobAnalysis | None:
    """Analyze one job inside its own small, fully-logged run."""
    run = create_analysis_run(db, f"Analyze job: {job.title}", dev_mode)
    run.model = getattr(ctx.analyzer, "model", None)
    log = RunLogger(db, run)
    log.heartbeat(f"Analyzing {job.title}")
    try:
        analysis = analyze_job(db, job, ctx, log)
    except (AnalysisFailedError, AnalyzerError, NotAnalyzableError) as exc:
        run.jobs_failed = 1
        log.error(str(exc), job_id=str(job.id))
        finish_run(run, AgentRunStatus.FAILED, summary=str(exc))
        db.commit()
        raise
    run.jobs_analyzed = 1
    run.jobs_processed = 1
    if analysis is not None and analysis.shortlisted:
        run.jobs_shortlisted = 1
    else:
        run.jobs_rejected = 1
    finish_run(run, AgentRunStatus.COMPLETED, summary="Analyzed 1 job")
    db.commit()
    return analysis


class AnalysisService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def execute(
        self, run_id: uuid.UUID, job_ids: list[uuid.UUID], ctx: AnalysisContext
    ) -> AgentRun:
        with self.session_factory() as db:
            run = db.get(AgentRun, run_id)
            if run is None:
                raise ValueError(f"Run {run_id} not found")
            run.model = getattr(ctx.analyzer, "model", None)
            run.searches_total = len(job_ids)  # progress denominator for analysis runs
            log = RunLogger(db, run)
            db.commit()
            try:
                for index, job_id in enumerate(job_ids, start=1):
                    if cancel_requested(db, run):
                        log.log(JobLogAction.RUN_CANCELLED, output={"analyzed": index - 1})
                        finish_run(
                            run, AgentRunStatus.STOPPED, self._summary(run, "Stopped by user")
                        )
                        db.commit()
                        return run
                    job = db.get(Job, job_id)
                    if job is None:
                        continue
                    log.heartbeat(
                        f"Analyzing {job.title}"
                        + (f" at {job.company.name}" if job.company else "")
                    )
                    db.commit()
                    try:
                        analysis = analyze_job(db, job, ctx, log)
                    except AnalyzerUnavailableError as exc:
                        log.error(str(exc))
                        finish_run(
                            run,
                            AgentRunStatus.FAILED,
                            self._summary(run, f"Analyzer unavailable: {exc}"),
                        )
                        db.commit()
                        return run
                    except (AnalysisFailedError, AnalyzerError, NotAnalyzableError) as exc:
                        run.jobs_failed += 1
                        log.error(str(exc), job_id=str(job_id))
                    else:
                        run.jobs_analyzed += 1
                        if analysis is not None and analysis.shortlisted:
                            run.jobs_shortlisted += 1
                        else:
                            run.jobs_rejected += 1
                    run.jobs_processed = index
                    run.searches_completed = index
                    db.commit()
                finish_run(run, AgentRunStatus.COMPLETED, self._summary(run, "Completed"))
                db.commit()
            except Exception as exc:
                logger.exception("Analysis run %s crashed", run_id)
                db.rollback()
                run = db.get(AgentRun, run_id) or run
                RunLogger(db, run).error(f"Analysis crashed: {type(exc).__name__}: {exc}")
                finish_run(run, AgentRunStatus.FAILED, "Analysis crashed; see errors.")
                db.commit()
            return run

    @staticmethod
    def _summary(run: AgentRun, headline: str) -> str:
        return (
            f"{headline}. Analyzed {run.jobs_analyzed}/{run.searches_total}; "
            f"shortlisted {run.jobs_shortlisted}, rejected {run.jobs_rejected}, "
            f"failed {run.jobs_failed}."
        )


def reapply_shortlist_rules(
    db: Session, rules: ShortlistRules, max_age_hours: int, now: datetime | None = None
) -> dict[str, int]:
    """Re-check already analyzed jobs against the CURRENT rules and score formula, from the
    saved analysis (no Claude call). Freshness is recomputed as of now. Jobs the user skipped
    and FAILED jobs are left alone (B4: failed jobs are never re-shortlisted automatically).
    Returns counts."""
    from types import SimpleNamespace

    from backend.services.freshness import compute_freshness

    now = now or utcnow()
    counts = {"checked": 0, "newly_shortlisted": 0}
    jobs = db.scalars(
        select(Job).where(
            Job.pipeline_state.in_([JobPipelineState.ANALYZED, JobPipelineState.REJECTED_BY_AGENT])
        )
    ).all()
    for job in jobs:
        skipped = db.scalar(
            select(ApplicationEvent.id).where(
                ApplicationEvent.job_id == job.id, ApplicationEvent.event_type == "skipped_by_user"
            )
        )
        analysis = db.scalars(
            select(JobAnalysis)
            .where(JobAnalysis.job_id == job.id)
            .order_by(JobAnalysis.created_at.desc())
        ).first()
        if skipped or analysis is None or analysis.match_score is None:
            continue
        earlier = next(
            (r for r in analysis.rule_results or [] if r.get("rule") == "min_match_score"), {}
        )
        claude = int(earlier.get("claude_score", analysis.match_score))
        output = SimpleNamespace(
            match_score=claude,
            role_match=analysis.role_match.value,
            skills_match=analysis.skills_match.value,
            experience_match=analysis.experience_match.value,
            location_match=analysis.location_match.value,
            salary_match=analysis.salary_match.value,
            freshness=compute_freshness(job.posted_at, now, max_age_hours, job.source).value,
            missing_skills=analysis.missing_skills or [],
            should_shortlist=analysis.should_shortlist,
        )
        shortlisted, results = evaluate(output, rules)  # type: ignore[arg-type]
        counts["checked"] += 1
        analysis.match_score = combined_score(
            claude, {d: getattr(output, d) for d in DIMENSION_WEIGHTS}
        )[0]
        analysis.rule_results, analysis.shortlisted = results, shortlisted
        if shortlisted:
            previous = job.pipeline_state
            assert_transition(previous, JobPipelineState.SHORTLISTED)
            job.pipeline_state = JobPipelineState.SHORTLISTED
            db.add(
                ApplicationEvent(
                    job_id=job.id,
                    event_type="shortlisted_after_rule_change",
                    source=EventSource.SYSTEM,
                    previous_status=previous.value,
                    new_status=JobPipelineState.SHORTLISTED.value,
                    reason=f"Passes the current rules (score {analysis.match_score})",
                )
            )
            counts["newly_shortlisted"] += 1
    db.commit()
    for job in db.scalars(
        select(Job).where(Job.pipeline_state == JobPipelineState.SHORTLISTED)
    ).all():
        route_manual_only(db, job)
    return counts
