"""Daily discovery + analysis: one run a day, discovery and analysis only.

    discover (every enabled source) -> normalize -> dedupe -> freshness / role / location /
    company filtering -> Claude analysis of FRESH jobs -> shortlist rules -> persist -> report

Everything reuses the existing services (DiscoveryService, AnalysisService, the shortlist
rules); this module only sequences them under one parent run (run_type=daily) and records a
concise report. It never creates applications, never opens a browser and never submits
anything - the modules that do are not imported here.

Idempotent by construction: discovery dedupes against stored jobs, only never-analyzed (or
failed) FRESH jobs are sent to the analyzer, and the scheduler skips a day whose slot a daily
run already used. A crashed run stops heart-beating and is closed as FAILED by the existing
stale-run rule, so the next run is never blocked by it.
"""

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from agent.analysis.analyzers import AnalyzerUnavailableError, JobAnalyzer, resolve_analyzer
from backend.core.config import Settings
from backend.core.logging import redact
from backend.core.profile import UserProfile, load_user_profile
from backend.services.analysis import (
    AnalysisContext,
    AnalysisService,
    create_analysis_run,
    select_jobs_for_daily,
)
from backend.services.discovery import FAILURE_KEYS, DiscoveryService, create_discovery_run
from backend.services.leads import LeadsPlan, run_leads_step
from backend.services.runs import (
    ACTIVE_STATUSES,
    RunConflictError,
    RunLogger,
    cancel_requested,
    ensure_no_active_run,
    finish_run,
)
from backend.services.shortlist import ShortlistRules, load_shortlist_rules
from database.base import utcnow
from database.enums import (
    AgentActionStatus,
    AgentRunStatus,
    JobLogAction,
    JobPipelineState,
    RunType,
)
from database.models import (
    AgentAction,
    AgentRun,
    Application,
    ApplicationEvent,
    BrowserSession,
    Job,
    JobAnalysis,
    SubmissionAttempt,
)
from integrations.job_sources.base import JobSource, SearchQuery
from integrations.job_sources.config import JobSourcesConfig, load_job_sources_config
from integrations.job_sources.registry import build_source as build_source_from_config

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 60.0

# Report keys, in display order.
# "fresh" counts fresh POSTINGS seen (a posting seen again, or listed twice, counts again);
# "new_fresh_jobs" counts jobs first stored by this run that are fresh.
COUNT_KEYS = [
    "discovered", "new", "duplicates", "fresh", "stale", "unknown", "new_fresh_jobs",
    "analyzed", "shortlisted", "rejected", "ready_to_apply", "failures",
]  # fmt: skip


@dataclass
class DailyPlan:
    """What a daily run needs. Built once per run from config (see build_daily_plan)."""

    sources: list[str]
    build_source: Callable[[str], JobSource]
    query: SearchQuery
    analyzer: Callable[[], JobAnalyzer]  # resolved lazily: a missing analyzer is a step failure
    profile: UserProfile
    rules: ShortlistRules
    dev_mode: bool
    analysis_limit: int
    # Lead -> trusted ATS resolution; None unless lead_resolution.enabled in job_sources.yaml.
    leads: LeadsPlan | None = None


def build_daily_plan(
    settings: Settings,
    *,
    config: JobSourcesConfig | None = None,
    profile: UserProfile | None = None,
    rules: ShortlistRules | None = None,
    build_source: Callable[[str], JobSource] | None = None,
) -> DailyPlan:
    """The plan from the shipped config: every enabled source, the user's preferences and the
    shortlist rules exactly as the manual discovery/analysis endpoints use them."""
    config = config or load_job_sources_config(settings.job_sources_path)
    profile = profile or load_user_profile()
    rules = rules or load_shortlist_rules(settings.shortlist_rules_path)
    prefs = profile.preferences
    query = config.build_query(
        prefs.preferred_roles,
        prefs.preferred_locations,
        excluded_companies=prefs.excluded_companies,
        max_age_hours=prefs.maximum_application_age_hours,
    )
    cfg = config
    return DailyPlan(
        sources=[s.name for s in config.enabled_sources()],
        build_source=build_source or (lambda name: build_source_from_config(name, cfg, settings)),
        query=query,
        analyzer=lambda: resolve_analyzer(
            settings, cfg.search.location_patterns, cfg.search.term_aliases
        ),
        profile=profile,
        rules=rules,
        dev_mode=settings.development_mode,
        analysis_limit=settings.daily_run_analysis_limit,
        leads=LeadsPlan(
            rules=config.lead_resolution,
            sources_config=config,
            settings=settings,
            candidate_years=profile.verified_profile.total_experience_years,
        )
        if config.lead_resolution.enabled
        else None,
    )


def create_daily_run(db: Session, plan: DailyPlan, trigger: str) -> AgentRun:
    """Register the parent run. Raises RunConflictError if any run is active."""
    ensure_no_active_run(db)
    run = AgentRun(
        task=f"Daily discovery & analysis ({trigger})",
        run_type=RunType.DAILY,
        status=AgentRunStatus.RUNNING,
        development_mode=plan.dev_mode,
        source=",".join(plan.sources)[:64] or None,
        search_terms=plan.query.terms,
        locations=plan.query.locations,
        current_task="Starting daily run",
        heartbeat_at=utcnow(),
        # progress: one step per source (+ leads) + analysis
        searches_total=len(plan.sources) + (2 if plan.leads else 1),
    )
    db.add(run)
    db.commit()
    return run


def _safety_counts(db: Session) -> dict[str, int]:
    return {
        "applications": db.scalar(select(func.count()).select_from(Application)) or 0,
        "browser_sessions": db.scalar(select(func.count()).select_from(BrowserSession)) or 0,
        "submission_attempts": db.scalar(select(func.count()).select_from(SubmissionAttempt)) or 0,
    }


class _Heartbeat:
    """Keeps the parent run alive while a long step (e.g. a Claude batch) runs."""

    def __init__(self, factory: sessionmaker[Session], run_id: uuid.UUID, seconds: float) -> None:
        self._factory, self._run_id, self._seconds = factory, run_id, seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="daily-heartbeat", daemon=True)

    def __enter__(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self._seconds):
            try:
                with self._factory() as db:
                    db.execute(
                        update(AgentRun)
                        .where(AgentRun.id == self._run_id, AgentRun.status.in_(ACTIVE_STATUSES))
                        .values(heartbeat_at=utcnow())
                    )
                    db.commit()
            except Exception:  # a missed beat is harmless; the next one retries
                logger.warning("Daily run heartbeat failed", exc_info=True)


class DailyRunService:
    def __init__(
        self, session_factory: sessionmaker[Session], heartbeat_seconds: float = HEARTBEAT_SECONDS
    ) -> None:
        self.session_factory = session_factory
        self.heartbeat_seconds = heartbeat_seconds

    def execute(self, run_id: uuid.UUID, plan: DailyPlan) -> dict[str, Any]:
        """Run every step; one failing step never stops the others. Returns the report."""
        with self.session_factory() as db:
            run = db.get(AgentRun, run_id)
            if run is None:
                raise ValueError(f"Run {run_id} not found")
            log = RunLogger(db, run)
            started = run.started_at
            report: dict[str, Any] = {
                "sources": list(plan.sources),
                "counts": dict.fromkeys(COUNT_KEYS, 0),
                "per_source": {},
                "analysis": None,
                "failures": [],
                "newly_shortlisted": [],
                "newly_ready_to_apply": [],
            }
            safety_before = _safety_counts(db)
            log.log(
                JobLogAction.DAILY_RUN_STARTED,
                input={"sources": plan.sources, "analysis_limit": plan.analysis_limit},
                output={"safety": safety_before},
            )
            db.commit()
            try:
                with _Heartbeat(self.session_factory, run_id, self.heartbeat_seconds):
                    stopped = self._run_steps(db, run, log, plan, report)
                self._collect_outcomes(db, started, report)
                report["safety"] = {
                    "before": safety_before,
                    "after": _safety_counts(db),
                    "development_mode": plan.dev_mode,
                }
                self._finish(db, run, log, report, stopped)
            except Exception as exc:
                logger.exception("Daily run %s crashed", run_id)
                db.rollback()
                run = db.get(AgentRun, run_id) or run
                report["failures"].append({"step": "daily", "error": f"{type(exc).__name__}"})
                RunLogger(db, run).error(f"Daily run crashed: {type(exc).__name__}: {exc}")
                finish_run(run, AgentRunStatus.FAILED, "Daily run crashed; see errors.")
                db.commit()
            return report

    # --- steps --------------------------------------------------------------------------

    def _run_steps(
        self, db: Session, run: AgentRun, log: RunLogger, plan: DailyPlan, report: dict[str, Any]
    ) -> bool:
        counts = report["counts"]
        child_runs: list[uuid.UUID] = []
        for name in plan.sources:
            if cancel_requested(db, run):
                return True
            log.heartbeat(f"Discovering from {name}")
            db.commit()
            try:
                step = self._discover(name, plan)
            except Exception as exc:  # e.g. the source could not be built (missing key)
                step = {"status": "FAILED", "error": _safe_error(exc)}
            report["per_source"][name] = step
            for key in ("discovered", "new", "duplicates", "fresh", "stale", "unknown"):
                counts[key] += int(step.get(key, 0))
            self._record_step(db, run, log, report, f"discover:{name}", step)
            if step.get("run_id"):
                child_runs.append(uuid.UUID(step["run_id"]))

        lead_jobs: set[uuid.UUID] = set()
        if plan.leads is not None:
            if cancel_requested(db, run):
                return True
            log.heartbeat("Resolving aggregator leads")
            db.commit()
            try:
                outcome = run_leads_step(
                    self.session_factory, db, log, plan.leads, plan.query, run.started_at,
                    plan.dev_mode,
                )  # fmt: skip
                child_runs.extend(outcome.crawl_run_ids)
                lead_jobs = set(outcome.linked_job_ids)
                if outcome.crawl_run_ids:
                    lead_jobs |= set(
                        db.scalars(
                            select(Job.id).where(Job.discovery_run_id.in_(outcome.crawl_run_ids))
                        ).all()
                    )
                report["leads"] = dict(outcome.metrics)
                step = {"status": "COMPLETED", **outcome.metrics,
                        "errors": [_safe_text(e) for e in outcome.errors[:10]]}  # fmt: skip
            except Exception as exc:
                db.rollback()
                step = {"status": "FAILED", "error": _safe_error(exc)}
            self._record_step(db, run, log, report, "leads", step)

        max_age = plan.profile.preferences.maximum_application_age_hours
        counts["new_fresh_jobs"] = (
            (
                db.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(
                        Job.discovery_run_id.in_(child_runs),
                        Job.posted_at >= utcnow() - timedelta(hours=max_age),
                    )
                )
                or 0
            )
            if child_runs
            else 0
        )

        if cancel_requested(db, run):
            return True
        log.heartbeat("Analyzing fresh jobs")
        db.commit()
        try:
            step = self._analyze(db, plan)
        except Exception as exc:
            step = {"status": "FAILED", "error": _safe_error(exc)}
        report["analysis"] = step
        for key in ("analyzed", "shortlisted", "rejected"):
            counts[key] += int(step.get(key, 0))
        if "leads" in report:
            report["leads"]["claude_analyses_triggered"] = (
                (
                    db.scalar(
                        select(func.count())
                        .select_from(JobAnalysis)
                        .where(
                            JobAnalysis.agent_run_id == uuid.UUID(step["run_id"]),
                            JobAnalysis.job_id.in_(lead_jobs),
                        )
                    )
                    or 0
                )
                if step.get("run_id") and lead_jobs
                else 0
            )
        self._record_step(db, run, log, report, "analysis", step)
        return False

    def _discover(self, name: str, plan: DailyPlan) -> dict[str, Any]:
        source = plan.build_source(name)
        with self.session_factory() as db:
            child = create_discovery_run(db, source, plan.query, plan.dev_mode, check_active=False)
        lead_rules = plan.leads.rules if plan.leads else None
        summary = DiscoveryService(self.session_factory, lead_rules).execute(
            child.id, source, plan.query
        )
        with self.session_factory() as db:
            done = db.scalar(
                select(AgentAction).where(
                    AgentAction.run_id == summary.run_id,
                    AgentAction.tool_name == JobLogAction.DISCOVERY_COMPLETED.value,
                )
            )
            totals = ((done.output or {}).get("totals") or {}) if done else {}
            learned = db.scalar(
                select(func.count())
                .select_from(AgentAction)
                .where(
                    AgentAction.run_id == summary.run_id,
                    AgentAction.tool_name == JobLogAction.JOB_POST_DATE_LEARNED.value,
                )
            )
        failed_searches = sum(int(totals.get(k, 0)) for k in FAILURE_KEYS)
        return {
            "run_id": str(summary.run_id),
            "status": summary.status.value,
            "searches": f"{summary.searches_completed}/{summary.searches_total}",
            "discovered": summary.jobs_found,
            "new": summary.jobs_new,
            "duplicates": summary.jobs_duplicate,
            "fresh": int(totals.get("freshness_fresh", 0)),
            "stale": int(totals.get("freshness_stale", 0)),
            "unknown": int(totals.get("freshness_unknown", 0)),
            "rejected_role": int(totals.get("rejected_role", 0)),
            "rejected_location": int(totals.get("rejected_location", 0)),
            "excluded_company": int(totals.get("excluded_company", 0)),
            "failed_searches": failed_searches,
            "failed_jobs": summary.jobs_failed,
            "post_dates_learned": learned or 0,
        }

    def _analyze(self, db: Session, plan: DailyPlan) -> dict[str, Any]:
        max_age = plan.profile.preferences.maximum_application_age_hours
        job_ids = select_jobs_for_daily(db, utcnow(), max_age, plan.analysis_limit)
        if not job_ids:
            return {"status": "SKIPPED", "selected": 0, "note": "No fresh jobs waiting"}
        try:
            analyzer = plan.analyzer()
        except AnalyzerUnavailableError as exc:
            return {"status": "FAILED", "selected": len(job_ids), "error": _safe_error(exc)}
        with self.session_factory() as s:
            child = create_analysis_run(
                s, f"Daily analysis of {len(job_ids)} fresh jobs", plan.dev_mode, check_active=False
            )
        ctx = AnalysisContext(analyzer=analyzer, profile=plan.profile, rules=plan.rules)
        done = AnalysisService(self.session_factory).execute(child.id, job_ids, ctx)
        return {
            "run_id": str(done.id),
            "status": done.status.value,
            "analyzer": analyzer.name,
            "selected": len(job_ids),
            "analyzed": done.jobs_analyzed,
            "shortlisted": done.jobs_shortlisted,
            "rejected": done.jobs_rejected,
            "failed_jobs": done.jobs_failed,
        }

    def _record_step(
        self,
        db: Session,
        run: AgentRun,
        log: RunLogger,
        report: dict[str, Any],
        step: str,
        result: dict[str, Any],
    ) -> None:
        failed = result.get("status") == "FAILED"
        if failed:
            report["failures"].append({"step": step, "error": result.get("error") or "failed"})
            report["counts"]["failures"] += 1
        log.log(
            JobLogAction.DAILY_STEP_FAILED if failed else JobLogAction.DAILY_STEP_COMPLETED,
            input={"step": step},
            output=result,
            status=AgentActionStatus.FAILED if failed else AgentActionStatus.SUCCESS,
            error=result.get("error") if failed else None,
        )
        run.searches_completed += 1
        db.commit()

    def _collect_outcomes(self, db: Session, started: datetime, report: dict[str, Any]) -> None:
        """Jobs that became SHORTLISTED / READY_TO_APPLY during this run (from the timeline)."""
        for state, key in (
            (JobPipelineState.SHORTLISTED, "newly_shortlisted"),
            (JobPipelineState.READY_TO_APPLY, "newly_ready_to_apply"),
        ):
            job_ids = db.scalars(
                select(ApplicationEvent.job_id)
                .where(
                    ApplicationEvent.new_status == state.value,
                    ApplicationEvent.occurred_at >= started,
                )
                .distinct()
            ).all()
            jobs = db.scalars(select(Job).where(Job.id.in_(job_ids))).all() if job_ids else []
            report[key] = [
                {
                    "job_id": str(j.id),
                    "title": j.title,
                    "company": j.company.name if j.company else None,
                    "location": j.location,
                    "posted_at": j.posted_at.isoformat() if j.posted_at else None,
                    "url": j.url,
                    "state": j.pipeline_state.value,
                }
                for j in jobs
            ]
        report["counts"]["ready_to_apply"] = len(report["newly_ready_to_apply"])
        report["counts"]["shortlisted"] = len(report["newly_shortlisted"])

    def _finish(
        self,
        db: Session,
        run: AgentRun,
        log: RunLogger,
        report: dict[str, Any],
        stopped: bool,
    ) -> None:
        c = report["counts"]
        run.jobs_found = c["discovered"]
        run.jobs_new = c["new"]
        run.jobs_duplicate = c["duplicates"]
        run.jobs_analyzed = c["analyzed"]
        run.jobs_shortlisted = c["shortlisted"]
        run.jobs_rejected = c["rejected"]
        run.jobs_failed = c["failures"]
        run.jobs_processed = c["analyzed"]
        sources_failed = sum(1 for s in report["per_source"].values() if s["status"] == "FAILED")
        all_failed = bool(report["sources"]) and sources_failed == len(report["sources"])
        if stopped:
            status, headline = AgentRunStatus.STOPPED, "Stopped by user"
        elif all_failed and (report["analysis"] or {}).get("status") in ("FAILED", "SKIPPED"):
            status, headline = AgentRunStatus.FAILED, "Every step failed"
        else:
            status, headline = AgentRunStatus.COMPLETED, "Completed"
        summary = (
            f"{headline}. Sources {len(report['sources']) - sources_failed}/"
            f"{len(report['sources'])} ok; discovered {c['discovered']} (new {c['new']}, "
            f"duplicates {c['duplicates']}; fresh {c['fresh']}, stale {c['stale']}, "
            f"unknown {c['unknown']}); analyzed {c['analyzed']}; newly shortlisted "
            f"{c['shortlisted']}; READY_TO_APPLY {c['ready_to_apply']}; "
            f"failures {c['failures']}."
        )
        log.log(
            JobLogAction.DAILY_RUN_COMPLETED,
            output=report,
            status=AgentActionStatus.SUCCESS
            if status is AgentRunStatus.COMPLETED
            else AgentActionStatus.FAILED,
        )
        finish_run(run, status, summary)
        db.commit()


def run_daily(
    factory: sessionmaker[Session], plan: DailyPlan, trigger: str
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Start and finish one daily run synchronously (CLI and scheduler).
    Raises RunConflictError if another run is active."""
    with factory() as db:
        run_id = create_daily_run(db, plan, trigger).id
    return run_id, DailyRunService(factory).execute(run_id, plan)


def _safe_text(text: str) -> str:
    return redact(text)[:300]


def _safe_error(exc: BaseException) -> str:
    """Error text for the report. Source errors are already scrubbed of credentials by the
    HTTP layer; this also caps the length."""
    return redact(f"{type(exc).__name__}: {exc}")[:300]


def latest_daily_report(db: Session) -> tuple[AgentRun, dict[str, Any] | None] | None:
    run = db.scalar(
        select(AgentRun)
        .where(AgentRun.run_type == RunType.DAILY)
        .order_by(AgentRun.started_at.desc())
        .limit(1)
    )
    if run is None:
        return None
    done = db.scalar(
        select(AgentAction).where(
            AgentAction.run_id == run.id,
            AgentAction.tool_name == JobLogAction.DAILY_RUN_COMPLETED.value,
        )
    )
    return run, (done.output if done else None)


# --- schedule -----------------------------------------------------------------------------


def parse_run_time(value: str) -> time:
    """'HH:MM' (24h) -> time. Raises ValueError on anything else."""
    hours, _, minutes = value.strip().partition(":")
    return time(int(hours), int(minutes or 0))


def slot_for(now_local: datetime, at: time) -> datetime:
    """Today's scheduled instant (timezone-aware, in now_local's zone)."""
    return now_local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)


def next_slot(now_local: datetime, at: time) -> datetime:
    today = slot_for(now_local, at)
    return today if now_local < today else slot_for(now_local + timedelta(days=1), at)


def slot_used(db: Session, slot: datetime) -> bool:
    """Did any daily run (scheduled or manual, any outcome) start at/after this slot?"""
    return (
        db.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.run_type == RunType.DAILY, AgentRun.started_at >= slot.astimezone(UTC))
        )
        or 0
    ) > 0


class DailyScheduler:
    """Smallest local scheduler: a daemon thread inside the API process.

    Once the day's slot time has passed and no daily run started since it, it triggers one.
    Also catches up after the machine was off at slot time (on the next start that day).
    If another run is active, it retries a few minutes later.
    """

    RETRY = timedelta(minutes=10)
    MAX_SLEEP = timedelta(hours=1)  # re-check the clock regularly (sleep/hibernate, DST)

    def __init__(
        self,
        *,
        at: time,
        tz: ZoneInfo,
        factory: sessionmaker[Session],
        trigger: Callable[[], object],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.at, self.tz, self.factory, self.trigger, self.clock = at, tz, factory, trigger, clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> tuple[str, timedelta]:
        """One scheduling decision: (what happened, how long to wait before the next tick)."""
        now_local = self.clock().astimezone(self.tz)
        slot = slot_for(now_local, self.at)
        wait = min(next_slot(now_local, self.at) - now_local, self.MAX_SLEEP)
        if now_local < slot:
            return "waiting", wait
        with self.factory() as db:
            if slot_used(db, slot):
                return "done_today", wait
        try:
            self.trigger()
        except RunConflictError:
            return "busy", self.RETRY
        except Exception:
            logger.exception("Scheduled daily run failed to start")
            return "error", self.MAX_SLEEP
        after = self.clock().astimezone(self.tz)
        return "ran", min(next_slot(after, self.at) - after, self.MAX_SLEEP)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="daily-scheduler", daemon=True)
        self._thread.start()
        logger.info("Daily run scheduled at %s %s", self.at.strftime("%H:%M"), self.tz.key)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                _, wait = self.tick()
            except Exception:
                logger.exception("Daily scheduler tick failed")
                wait = self.RETRY
            self._stop.wait(max(wait.total_seconds(), 1.0))
