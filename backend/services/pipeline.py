"""Runs started from the dashboard's Runs page (REQUIREMENTS 19.2.9).

    discover -> analyze -> tailor -> apply      (the user picks the steps)

- discover / analyze reuse the daily run's steps (every enabled source; Claude analysis of
  fresh jobs, capped at ``analysis_limit`` <= 50).
- tailor: shortlisted jobs get a validated tailored resume and an internal draft (the same
  registered tools the agent uses). Nothing is sent.
- apply, per platform, in the chosen mode:
    dry-run: fill everything, stop before submit, close the session (default).
    review: prepare the form LIVE and stop before submit; the user approves or rejects each
            one in the Review queue. Needs the live switches (DEVELOPMENT_MODE=false,
            EXTERNAL_LIVE_ENABLED=true, visible browser) and a second confirmation that names
            the platform and today's remaining limit. Never more forms than that limit.
  "auto" (submit without asking) does not exist yet (Phase E).
- Only company sites can be applied to so far; Naukri and LinkedIn come with their modules.

One job failing never stops the run; Stop takes effect after the current job. Every step is
logged on the run (counts and job ids; never answers).
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agent.tools import ToolContext, ToolRegistry
from backend.core.config import Settings
from backend.core.profile import UserProfile
from backend.services import execution as svc
from backend.services.daily import HEARTBEAT_SECONDS, DailyPlan, DailyRunService, _Heartbeat
from backend.services.limits import limit_block, usage
from backend.services.platforms import PLATFORMS, Platform, platform_of
from backend.services.runs import RunLogger, cancel_requested, ensure_no_active_run, finish_run
from database.base import utcnow
from database.enums import (
    AgentActionStatus,
    AgentRunStatus,
    BrowserExecutionMode,
    JobLogAction,
    JobPipelineState,
    RunType,
    ToolPermission,
)
from database.models import AgentRun, Job

logger = logging.getLogger(__name__)

Step = Literal["discover", "analyze", "tailor", "apply"]
STEPS: tuple[Step, ...] = ("discover", "analyze", "tailor", "apply")
Mode = Literal["dry-run", "review"]
BUILT_PLATFORMS: frozenset[Platform] = frozenset({"company_site"})
MAX_ANALYSIS = 50  # agreed cap for one run


# Execution refusals that apply to every remaining job: stop the apply step.
STOP_APPLYING = frozenset(
    {"DAILY_LIMIT_REACHED", "LIVE_NOT_ENABLED", "DEVELOPMENT_MODE_BLOCKED",
     "VISIBLE_BROWSER_REQUIRED"}
)  # fmt: skip
DRAFT_OUTCOME = {
    JobPipelineState.READY_TO_APPLY: "ready_to_apply",
    JobPipelineState.BLOCKED_NEEDS_USER: "needs_input",
}


def _failure(step: str, job: Job, message: str | None) -> dict[str, str]:
    return {"step": step, "job_id": str(job.id), "error": (message or "failed")[:300]}


class PipelineError(ValueError):
    """The run can't start as asked; the message says why (nothing was started)."""


@dataclass(frozen=True)
class PipelineRequest:
    steps: tuple[Step, ...]
    platforms: tuple[Platform, ...] = ("company_site",)
    mode: Mode = "dry-run"
    analysis_limit: int = 25
    tailor_limit: int = 5
    apply_limit: int = 5


@dataclass
class PipelineDeps:
    """Everything a run needs, resolved by the caller (so tests inject fakes)."""

    plan: Callable[[], DailyPlan]
    settings: Settings
    profile: UserProfile
    registry: ToolRegistry
    browser: Any
    claude_factory: Callable[[], Any] | None = None


@dataclass
class Report:
    steps: list[str]
    platforms: list[str]
    mode: str
    counts: dict[str, int] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    failures: list[dict[str, str]] = field(default_factory=list)

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def as_dict(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "platforms": self.platforms,
            "mode": self.mode,
            "counts": self.counts,
            "details": self.details,
            "failures": self.failures,
        }


def live_confirmation(db: Session, settings: Settings, platform: Platform) -> dict[str, Any]:
    """What the user must confirm to start a review (live) run on one platform."""
    statuses = {s.platform: s for s in usage(db, settings.automation_path, settings.timezone)}
    remaining = min(statuses["total"].remaining, statuses[platform].remaining)
    return {
        "platform": platform,
        "remaining": remaining,
        "phrase": f"LIVE on {platform}: up to {remaining} today",
    }


def start_pipeline(
    db: Session,
    settings: Settings,
    req: PipelineRequest,
    confirmation: str | None = None,
) -> AgentRun:
    """Validate and register a run. Raises PipelineError / RunConflictError; nothing starts."""
    steps = tuple(s for s in STEPS if s in req.steps)
    if not steps:
        raise PipelineError("Choose at least one step")
    unknown = [p for p in req.platforms if p not in PLATFORMS]
    if unknown:
        raise PipelineError(f"Unknown platform(s): {', '.join(unknown)}")
    if not 1 <= req.analysis_limit <= MAX_ANALYSIS:
        raise PipelineError(f"analysis_limit must be 1-{MAX_ANALYSIS}")
    if "apply" in steps:
        missing = [p for p in req.platforms if p not in BUILT_PLATFORMS]
        if missing:
            raise PipelineError(
                f"Applying on {', '.join(missing)} is not built yet (Naukri: Phase C, "
                "LinkedIn: Phase D). Choose company sites."
            )
        if req.mode == "review":
            from backend.core.config import live_execution_block

            block = live_execution_block(settings)
            if block is not None:
                raise PipelineError(block[1])
            if settings.browser_headless:
                raise PipelineError("Review mode needs a visible browser: BROWSER_HEADLESS=false")
            for platform in req.platforms:
                expected = live_confirmation(db, settings, platform)
                if expected["remaining"] == 0:
                    raise PipelineError(f"No live applications left today on {platform}")
                if confirmation != expected["phrase"]:
                    raise PipelineError(
                        f'Review mode is live. Confirm by typing exactly: "{expected["phrase"]}"'
                    )
    ensure_no_active_run(db)
    run = AgentRun(
        task=f"Run: {', '.join(steps)} ({req.mode})",
        run_type=RunType.PIPELINE,
        status=AgentRunStatus.RUNNING,
        development_mode=settings.development_mode,
        mode=req.mode,
        source=",".join(req.platforms)[:64],
        current_task="Starting",
        heartbeat_at=utcnow(),
        searches_total=len(steps),
    )
    db.add(run)
    db.commit()
    return run


class PipelineService:
    def __init__(
        self, factory: sessionmaker[Session], heartbeat_seconds: float = HEARTBEAT_SECONDS
    ) -> None:
        self.factory = factory
        self.heartbeat_seconds = heartbeat_seconds

    def execute(
        self, run_id: uuid.UUID, req: PipelineRequest, deps: PipelineDeps
    ) -> dict[str, Any]:
        with self.factory() as db:
            run = db.get(AgentRun, run_id)
            if run is None:
                raise ValueError(f"Run {run_id} not found")
            log = RunLogger(db, run)
            report = Report(steps=list(req.steps), platforms=list(req.platforms), mode=req.mode)
            log.log(JobLogAction.PIPELINE_STARTED, input=report.as_dict())
            db.commit()
            stopped = False
            try:
                with _Heartbeat(self.factory, run_id, self.heartbeat_seconds):
                    for step in STEPS:
                        if step not in req.steps:
                            continue
                        if cancel_requested(db, run):
                            stopped = True
                            break
                        log.heartbeat(f"Step: {step}")
                        db.commit()
                        stopped = getattr(self, f"_{step}")(db, run, log, req, deps, report)
                        run.searches_completed = (run.searches_completed or 0) + 1
                        log.log(JobLogAction.PIPELINE_STEP, output={"step": step, **report.counts})
                        db.commit()
                        if stopped:
                            break
                self._finish(db, run, log, report, stopped)
            except Exception as exc:
                logger.exception("Pipeline run %s crashed", run_id)
                db.rollback()
                run = db.get(AgentRun, run_id) or run
                report.failures.append({"step": "run", "error": type(exc).__name__})
                RunLogger(db, run).error(f"Run crashed: {type(exc).__name__}: {exc}")
                finish_run(run, AgentRunStatus.FAILED, "The run crashed; see errors.")
                db.commit()
            return report.as_dict()

    # --- steps (each returns True when the run was stopped) ---------------------------------

    def _discover(self, db: Session, run: AgentRun, log: RunLogger, req: PipelineRequest,
                  deps: PipelineDeps, report: Report) -> bool:  # fmt: skip
        plan = deps.plan()
        daily = DailyRunService(self.factory, self.heartbeat_seconds)
        for name in plan.sources:
            if cancel_requested(db, run):
                return True
            log.heartbeat(f"Discovering from {name}")
            db.commit()
            try:
                step = daily._discover(name, plan)
            except Exception as exc:
                step = {"status": "FAILED", "error": f"{type(exc).__name__}"[:200]}
                report.failures.append({"step": f"discover:{name}", "error": step["error"]})
            report.add("discovered", int(step.get("discovered", 0)))
            report.add("new", int(step.get("new", 0)))
        return False

    def _analyze(self, db: Session, run: AgentRun, log: RunLogger, req: PipelineRequest,
                 deps: PipelineDeps, report: Report) -> bool:  # fmt: skip
        plan = deps.plan()
        plan.analysis_limit = min(req.analysis_limit, MAX_ANALYSIS)
        try:
            step = DailyRunService(self.factory, self.heartbeat_seconds)._analyze(db, plan)
        except Exception as exc:
            step = {"status": "FAILED", "error": f"{type(exc).__name__}"[:200]}
        if step.get("status") == "FAILED":
            report.failures.append({"step": "analyze", "error": str(step.get("error", ""))})
        report.details["analysis"] = step
        for key in ("analyzed", "shortlisted", "rejected"):
            report.add(key, int(step.get(key, 0)))
        return False

    def _tool_ctx(self, db: Session, deps: PipelineDeps, run: AgentRun) -> ToolContext:
        return ToolContext(
            db=db,
            settings=deps.settings,
            profile=deps.profile,
            actor="agent",
            allowed=frozenset({ToolPermission.READ_ONLY, ToolPermission.WRITE}),
            claude_factory=deps.claude_factory,
            run=run,
        )

    def _tailor(self, db: Session, run: AgentRun, log: RunLogger, req: PipelineRequest,
                deps: PipelineDeps, report: Report) -> bool:  # fmt: skip
        jobs = db.scalars(
            select(Job)
            .where(Job.pipeline_state == JobPipelineState.SHORTLISTED)
            .order_by(Job.updated_at)
            .limit(req.tailor_limit)
        ).all()
        for job in jobs:
            if cancel_requested(db, run):
                return True
            log.heartbeat(f"Tailoring a resume for {job.title}"[:200])
            db.commit()
            ctx = self._tool_ctx(db, deps, run)
            made = deps.registry.execute("create_tailored_resume", {"job_id": str(job.id)}, ctx)
            if not made.ok:
                report.failures.append(_failure("tailor", job, made.message))
                continue
            report.add("tailored")
            draft = deps.registry.execute("create_application_draft", {"job_id": str(job.id)}, ctx)
            if not draft.ok:
                report.failures.append(_failure("draft", job, draft.message))
                continue
            db.refresh(job)
            report.add(DRAFT_OUTCOME.get(job.pipeline_state, "drafted"))
            run.jobs_processed = (run.jobs_processed or 0) + 1
        return False

    def _apply(self, db: Session, run: AgentRun, log: RunLogger, req: PipelineRequest,
               deps: PipelineDeps, report: Report) -> bool:  # fmt: skip
        live = req.mode == "review"
        candidates = db.scalars(
            select(Job)
            .where(Job.pipeline_state == JobPipelineState.READY_TO_APPLY)
            .order_by(Job.updated_at)
        ).all()
        done = 0
        for job in candidates:
            if done >= req.apply_limit:
                break
            if platform_of(job) not in req.platforms:
                continue
            if cancel_requested(db, run):
                return True
            if live:
                reason = limit_block(db, deps.settings.automation_path, deps.settings.timezone, job)
                if reason:
                    report.details["stopped_by_limit"] = reason
                    break
            log.heartbeat(f"Applying ({req.mode}): {job.title}"[:200])
            db.commit()
            ctx = svc.ExecContext(
                db=db,
                settings=deps.settings,
                profile=deps.profile,
                browser=deps.browser,
                mode=BrowserExecutionMode.LIVE if live else BrowserExecutionMode.DRY_RUN,
                actor="agent",
                run=run,
            )
            try:
                out = svc.run_execution(ctx, job.id)
            except svc.ExecutionError as exc:
                report.failures.append(_failure("apply", job, f"{exc.code}: {exc.message}"))
                if exc.code in STOP_APPLYING:
                    break
                continue
            done += 1
            run.jobs_processed = (run.jobs_processed or 0) + 1
            if out.get("failed"):
                report.add("failed")
            elif out.get("blocked_needs_user"):
                report.add("needs_input")
            elif live and out.get("state") == "READY_FOR_SUBMISSION":
                report.add("waiting_for_review")
            elif out.get("dry_run_complete"):
                report.add("dry_run_complete")
            db.commit()
        return False

    def _finish(
        self, db: Session, run: AgentRun, log: RunLogger, report: Report, stopped: bool
    ) -> None:
        c = report.counts
        parts = [f"{k.replace('_', ' ')}: {v}" for k, v in c.items() if v]
        summary = ("Stopped. " if stopped else "") + ("; ".join(parts) or "Nothing to do.")
        if report.failures:
            summary += f" ({len(report.failures)} problem(s); see the run report)"
        status = AgentRunStatus.STOPPED if stopped else AgentRunStatus.COMPLETED
        log.log(
            JobLogAction.PIPELINE_COMPLETED,
            output=report.as_dict(),
            status=AgentActionStatus.SUCCESS,
        )
        finish_run(run, status, summary[:2000])
        db.commit()
